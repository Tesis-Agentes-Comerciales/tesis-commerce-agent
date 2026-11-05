# P0: fallos del modelo (INF27, INF28, INF29 e INF33)

En la corrida registrada (corrida 2, sobre `d3740a8`) pasan los 36 casos. Las 12 celdas quedan en 3/3
y cumplen todos sus oráculos en todas las repeticiones.

La corrida 1, sobre `54ebd5b`, se conserva en `corrida-1/` y dio 33 de 36. INF29-HP falló en el chat
en las tres repeticiones por un defecto del arnés, no del producto; ver
[Fallos de arnés conservados](#fallos-de-arnés-conservados).

Ninguna corrida usó un modelo real, llamó a un proveedor ni tuvo costo.

## Qué se midió

Se probaron las celdas P0 de `docs/design/p0-model-failures.md` §4 contra el stack live: Merchant
real, proveedor de pagos sandbox real y PostgreSQL efímero de `tests/integration`.

| Matriz | Fallo | HP | HNP |
| --- | --- | --- | --- |
| INF27 | 429 por *rate limit* | sí | sí |
| INF28 | 429 `insufficient_quota` | sí | sí |
| INF29 | Modelo inalcanzable | sí | sí |
| INF33 | Revisor que no responde a tiempo (a) o responde JSON fuera de su contrato (b) | no aplica | sí |

Las condiciones son las B2 gobernadas:

- **B2-HP**: `enforce`, `governed`, `b2-hp-v2` y AP2.
- **B2-HNP**: selector `llm`; revisor `llm` solo en INF33 y CTRL-HNP-REVIEW.

### El doble del modelo

- **Qué es real.** El SDK de OpenAI (2.54.0, `max_retries=1`), su política de reintentos, el hook
  httpx del Agent (`refuse_quota_retry`) y la clasificación del fallo (`classify_model_failure`)
  son los del producto.
- **Qué es falso.** Solo el transporte HTTP. `ModelServer` (`httpx.MockTransport`) sirve el
  guion de cada caso dentro del proceso de pruebas, con un modelo (`p0-scripted-model`) y una
  credencial sintéticos.
- **Cómo avanza el guion.** Cada intento HTTP toma la siguiente respuesta del guion y la última
  respuesta persiste. El guion empieza de nuevo en cada superficie (HP) o corrida de tarea (HNP).

### Qué se observa

- **Intentos HTTP al modelo**, contados por superficie (HP: REST `/conversation/turn` y chat
  `/assistant`) o por corrida de tarea (HNP).
- **Filas `model_requests` de la ventana**, proyectadas a `stage`, `outcome`, `error_kind`,
  `http_status`, `provider_error_code` y `retries`. No se guardan cabeceras, claves ni payloads.
- **Lo que ve el comprador**:
  - HP: el 502 de REST con `model_failure` y el estado, los pasos y el paso fallido del chat.
  - HNP: el estado de la tarea, `outcome_code`, el `failure_kind` de `task_finished`, el evento
    `choice_reviewed`, las compras y el grant.
- **El 429 sin `x-should-retry`.** Cada 429 guionado sale sin `x-should-retry` y se comprueba dos
  veces: tal como se envía y después del hook del Agent. En INF27 la cabecera sigue ausente y el
  SDK reintenta una vez. En INF28 el hook la fija en `"false"` y el SDK no reintenta.
- **Cero efectos fuera del Agent.**
  - Merchant: ningún POST por el proxy, ningún checkout del episodio y una huella completa igual
    antes y después (conteo y SHA-256 de cada tabla).
  - PSP: ningún POST por el proxy (HNP), el mismo conteo de tokens `vt_` y la misma huella
    completa del SQLite.
- **Cero efectos en el registro del Agent.**
  - Ninguna fila nueva en `approvals`, `completion_results`, `grant_reservations`,
    `dispatch_outbox`, `orders`, `hp_evidence`, `governance_evidence` ni `ap2_evidence`.
  - Ningún mandato cerrado.
  - Compras: ninguna en HP ni en las celdas del selector; en INF33, una cancelada sin
    `checkout_id`.
  - En HP, además, 0 filas `governed_recovery`.
- **Controles.** Hay controles positivos por modo: CTRL-HP, CTRL-HNP y CTRL-HNP-REVIEW. Además,
  INF27-HP-control prueba la recuperación: un 429 y luego respuestas válidas deben llevar a un
  checkout por superficie.

## Resultado por celda (corrida 2)

En cada repetición, la columna da los oráculos cumplidos sobre los oráculos de la celda.

| Celda | Modo | Condición | r1 | r2 | r3 | Repeticiones |
| --- | --- | --- | --- | --- | --- | --- |
| INF27-HP | HP | B2-HP | 19/19 | 19/19 | 19/19 | 3/3 |
| INF28-HP | HP | B2-HP | 19/19 | 19/19 | 19/19 | 3/3 |
| INF29-HP | HP | B2-HP | 17/17 | 17/17 | 17/17 | 3/3 |
| INF27-HNP | HNP | B2-HNP | 15/15 | 15/15 | 15/15 | 3/3 |
| INF28-HNP | HNP | B2-HNP | 15/15 | 15/15 | 15/15 | 3/3 |
| INF29-HNP | HNP | B2-HNP | 15/15 | 15/15 | 15/15 | 3/3 |
| INF33-HNP-a | HNP | B2-HNP | 15/15 | 15/15 | 15/15 | 3/3 |
| INF33-HNP-b | HNP | B2-HNP | 15/15 | 15/15 | 15/15 | 3/3 |
| INF27-HP-control | HP | B2-HP | 10/10 | 10/10 | 10/10 | 3/3 |
| CTRL-HP | HP | B2-HP | 10/10 | 10/10 | 10/10 | 3/3 |
| CTRL-HNP | HNP | B2-HNP | 8/8 | 8/8 | 8/8 | 3/3 |
| CTRL-HNP-REVIEW | HNP | B2-HNP | 8/8 | 8/8 | 8/8 | 3/3 |

INF33-HP no aplica: en Human Present no hay revisor del modelo, porque aprueba el comprador
(§4). Se declara en el prerregistro y no se ejecuta ni se cuenta.

Las tres repeticiones de cada celda observan exactamente lo mismo. La única excepción es el
`checkout_id` de la compra de CTRL-HNP y CTRL-HNP-REVIEW, que es nuevo en cada repetición; por
eso esas dos celdas marcan `observed_identical_across_repetitions: false`.

### Lo observado

Estos son los valores de la repetición 1, iguales en r2 y r3. En HP, los intentos se cuentan por
superficie y REST y chat coinciden.

| Celda | Intentos | `model_requests` | Lo que ve el comprador | Efectos |
| --- | --- | --- | --- | --- |
| INF27-HP | 2 | `model.route` error `RateLimitError` 429 `rate_limit_exceeded`, retries 1 | REST 502 `model_failure=rate_limited`; chat `incomplete` en `interpret_request` con «El modelo está recibiendo demasiadas solicitudes…»; `x-should-retry` ausente antes y después del hook | 0 |
| INF28-HP | 1 | `model.route` error `RateLimitError` 429 `insufficient_quota`, retries 0 | REST 502 `model_failure=quota_exhausted`; chat `incomplete` en `interpret_request` con «El asistente no está disponible por ahora…»; `x-should-retry` ausente al enviarse y `"false"` tras el hook | 0 |
| INF29-HP | 3 (1 interpretación + 2 de selección) | `model.route` ok, retries 0; `model.choose_candidate` error `APIConnectionError`, retries 1 | REST 502 `model_failure=unreachable` («The candidate model request failed»); chat `incomplete` con pasos `interpret_request`, `search_catalog`, `select_product`, falla en `select_product` con «No pude comunicarme con el modelo…» | 0; 0 filas `governed_recovery`; 0 compras; 0 POST de checkout |
| INF27-HNP | 2 | `model.hnp_select` error `RateLimitError` 429 `rate_limit_exceeded`, retries 1 | tarea `escalated` / `selection_unavailable`, `failure_kind=rate_limited`, grant `revoked` (`escalated`) | 0 |
| INF28-HNP | 1 | `model.hnp_select` error `RateLimitError` 429 `insufficient_quota`, retries 0 | igual, con `failure_kind=quota_exhausted`; hook `"false"` | 0 |
| INF29-HNP | 2 | `model.hnp_select` error `APIConnectionError`, retries 1 | igual, con `failure_kind=unreachable`; nunca `selection_not_eligible` | 0 |
| INF33-HNP-a | 3 | `model.hnp_select` ok; `model.hnp_review` error `APITimeoutError`, retries 1 | `escalated` / `choice_not_confirmed`; `choice_reviewed` = `not_confirmed`, `review_unavailable`, `failure_kind=unreachable`; compra `canceled` sin `checkout_id` | 0 |
| INF33-HNP-b | 2 | `model.hnp_select` ok; `model.hnp_review` error `ValidationError`, retries 0 | `escalated` / `choice_not_confirmed`; `choice_reviewed` con `failure_kind=invalid_output`; compra `canceled` sin `checkout_id` | 0 |
| INF27-HP-control | 3 | `model.route` ok, retries 1; `model.choose_candidate` ok | REST 200 y chat `complete`, ambos con compra `awaiting_confirmation`; 1 POST de checkout 201 por superficie; sin pago | checkout cancelado en la limpieza |
| CTRL-HP | 2 | `model.route` ok; `model.choose_candidate` ok | igual que la fila anterior | checkout cancelado en la limpieza |
| CTRL-HNP | 1 | `model.hnp_select` ok | tarea `purchased`; 1 POST de checkout 201 | +1 orden, +1 sesión, +1 token |
| CTRL-HNP-REVIEW | 2 | `model.hnp_select` ok; `model.hnp_review` ok | `choice_reviewed` = `confirmed` (`matches_request`); tarea `purchased` | +1 orden, +1 sesión, +1 token |

`summary.json` guarda, por celda, cada oráculo en k/3, cada repetición con sus oráculos fallidos
y lo observado en la primera repetición.

### Celda × repetición × oráculo

<details>
<summary>Tabla completa (corrida 2)</summary>

#### INF27-HP

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `rest.model_http_attempts` | sí | sí | sí |
| `rest.model_requests` | sí | sí | sí |
| `rest.scripted_429` | sí | sí | sí |
| `chat.model_http_attempts` | sí | sí | sí |
| `chat.model_requests` | sí | sí | sí |
| `chat.scripted_429` | sí | sí | sí |
| `rest.status_and_body` | sí | sí | sí |
| `chat.status` | sí | sí | sí |
| `chat.steps` | sí | sí | sí |
| `chat.failed_step` | sí | sí | sí |
| `chat.purchase` | sí | sí | sí |
| `governed_recovery_rows` | sí | sí | sí |
| `no_purchase` | sí | sí | sí |
| `zero_effects.merchant_no_post` | sí | sí | sí |
| `zero_effects.merchant_no_checkout` | sí | sí | sí |
| `zero_effects.merchant_snapshot_equal` | sí | sí | sí |
| `zero_effects.psp_no_token` | sí | sí | sí |
| `zero_effects.psp_snapshot_equal` | sí | sí | sí |
| `zero_effects.ledger_no_new_rows` | sí | sí | sí |

#### INF28-HP

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `rest.model_http_attempts` | sí | sí | sí |
| `rest.model_requests` | sí | sí | sí |
| `rest.scripted_429` | sí | sí | sí |
| `chat.model_http_attempts` | sí | sí | sí |
| `chat.model_requests` | sí | sí | sí |
| `chat.scripted_429` | sí | sí | sí |
| `rest.status_and_body` | sí | sí | sí |
| `chat.status` | sí | sí | sí |
| `chat.steps` | sí | sí | sí |
| `chat.failed_step` | sí | sí | sí |
| `chat.purchase` | sí | sí | sí |
| `governed_recovery_rows` | sí | sí | sí |
| `no_purchase` | sí | sí | sí |
| `zero_effects.merchant_no_post` | sí | sí | sí |
| `zero_effects.merchant_no_checkout` | sí | sí | sí |
| `zero_effects.merchant_snapshot_equal` | sí | sí | sí |
| `zero_effects.psp_no_token` | sí | sí | sí |
| `zero_effects.psp_snapshot_equal` | sí | sí | sí |
| `zero_effects.ledger_no_new_rows` | sí | sí | sí |

#### INF29-HP

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `rest.model_http_attempts` | sí | sí | sí |
| `rest.model_requests` | sí | sí | sí |
| `chat.model_http_attempts` | sí | sí | sí |
| `chat.model_requests` | sí | sí | sí |
| `rest.status_and_body` | sí | sí | sí |
| `chat.status` | sí | sí | sí |
| `chat.steps` | sí | sí | sí |
| `chat.failed_step` | sí | sí | sí |
| `chat.purchase` | sí | sí | sí |
| `governed_recovery_rows` | sí | sí | sí |
| `no_purchase` | sí | sí | sí |
| `zero_effects.merchant_no_post` | sí | sí | sí |
| `zero_effects.merchant_no_checkout` | sí | sí | sí |
| `zero_effects.merchant_snapshot_equal` | sí | sí | sí |
| `zero_effects.psp_no_token` | sí | sí | sí |
| `zero_effects.psp_snapshot_equal` | sí | sí | sí |
| `zero_effects.ledger_no_new_rows` | sí | sí | sí |

#### INF27-HNP

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `trigger_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `model_requests` | sí | sí | sí |
| `task` | sí | sí | sí |
| `choice_reviewed` | sí | sí | sí |
| `task_finished_failure_kind` | sí | sí | sí |
| `purchases` | sí | sí | sí |
| `scripted_429` | sí | sí | sí |
| `zero_effects.merchant_no_post` | sí | sí | sí |
| `zero_effects.merchant_no_checkout` | sí | sí | sí |
| `zero_effects.merchant_snapshot_equal` | sí | sí | sí |
| `zero_effects.psp_no_token` | sí | sí | sí |
| `zero_effects.psp_snapshot_equal` | sí | sí | sí |
| `zero_effects.ledger_no_new_rows` | sí | sí | sí |
| `zero_effects.psp_no_post` | sí | sí | sí |

#### INF28-HNP

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `trigger_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `model_requests` | sí | sí | sí |
| `task` | sí | sí | sí |
| `choice_reviewed` | sí | sí | sí |
| `task_finished_failure_kind` | sí | sí | sí |
| `purchases` | sí | sí | sí |
| `scripted_429` | sí | sí | sí |
| `zero_effects.merchant_no_post` | sí | sí | sí |
| `zero_effects.merchant_no_checkout` | sí | sí | sí |
| `zero_effects.merchant_snapshot_equal` | sí | sí | sí |
| `zero_effects.psp_no_token` | sí | sí | sí |
| `zero_effects.psp_snapshot_equal` | sí | sí | sí |
| `zero_effects.ledger_no_new_rows` | sí | sí | sí |
| `zero_effects.psp_no_post` | sí | sí | sí |

#### INF29-HNP

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `trigger_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `model_requests` | sí | sí | sí |
| `task` | sí | sí | sí |
| `choice_reviewed` | sí | sí | sí |
| `task_finished_failure_kind` | sí | sí | sí |
| `task_outcome_never` | sí | sí | sí |
| `purchases` | sí | sí | sí |
| `zero_effects.merchant_no_post` | sí | sí | sí |
| `zero_effects.merchant_no_checkout` | sí | sí | sí |
| `zero_effects.merchant_snapshot_equal` | sí | sí | sí |
| `zero_effects.psp_no_token` | sí | sí | sí |
| `zero_effects.psp_snapshot_equal` | sí | sí | sí |
| `zero_effects.ledger_no_new_rows` | sí | sí | sí |
| `zero_effects.psp_no_post` | sí | sí | sí |

#### INF33-HNP-a

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `trigger_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `model_requests` | sí | sí | sí |
| `task` | sí | sí | sí |
| `choice_reviewed` | sí | sí | sí |
| `task_finished_failure_kind` | sí | sí | sí |
| `no_confirmed_review` | sí | sí | sí |
| `purchases` | sí | sí | sí |
| `zero_effects.merchant_no_post` | sí | sí | sí |
| `zero_effects.merchant_no_checkout` | sí | sí | sí |
| `zero_effects.merchant_snapshot_equal` | sí | sí | sí |
| `zero_effects.psp_no_token` | sí | sí | sí |
| `zero_effects.psp_snapshot_equal` | sí | sí | sí |
| `zero_effects.ledger_no_new_rows` | sí | sí | sí |
| `zero_effects.psp_no_post` | sí | sí | sí |

#### INF33-HNP-b

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `trigger_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `model_requests` | sí | sí | sí |
| `task` | sí | sí | sí |
| `choice_reviewed` | sí | sí | sí |
| `task_finished_failure_kind` | sí | sí | sí |
| `no_confirmed_review` | sí | sí | sí |
| `purchases` | sí | sí | sí |
| `zero_effects.merchant_no_post` | sí | sí | sí |
| `zero_effects.merchant_no_checkout` | sí | sí | sí |
| `zero_effects.merchant_snapshot_equal` | sí | sí | sí |
| `zero_effects.psp_no_token` | sí | sí | sí |
| `zero_effects.psp_snapshot_equal` | sí | sí | sí |
| `zero_effects.ledger_no_new_rows` | sí | sí | sí |
| `zero_effects.psp_no_post` | sí | sí | sí |

#### INF27-HP-control

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `rest.model_http_attempts` | sí | sí | sí |
| `rest.model_requests` | sí | sí | sí |
| `rest.purchase` | sí | sí | sí |
| `rest.checkout_posts` | sí | sí | sí |
| `rest.no_payment` | sí | sí | sí |
| `chat.model_http_attempts` | sí | sí | sí |
| `chat.model_requests` | sí | sí | sí |
| `chat.purchase` | sí | sí | sí |
| `chat.checkout_posts` | sí | sí | sí |
| `chat.no_payment` | sí | sí | sí |

#### CTRL-HP

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `rest.model_http_attempts` | sí | sí | sí |
| `rest.model_requests` | sí | sí | sí |
| `rest.purchase` | sí | sí | sí |
| `rest.checkout_posts` | sí | sí | sí |
| `rest.no_payment` | sí | sí | sí |
| `chat.model_http_attempts` | sí | sí | sí |
| `chat.model_requests` | sí | sí | sí |
| `chat.purchase` | sí | sí | sí |
| `chat.checkout_posts` | sí | sí | sí |
| `chat.no_payment` | sí | sí | sí |

#### CTRL-HNP

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `trigger_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `model_requests` | sí | sí | sí |
| `task` | sí | sí | sí |
| `choice_reviewed` | sí | sí | sí |
| `checkout_posts` | sí | sí | sí |
| `orders_created` | sí | sí | sí |
| `psp_tokens_created` | sí | sí | sí |

#### CTRL-HNP-REVIEW

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `trigger_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `model_requests` | sí | sí | sí |
| `task` | sí | sí | sí |
| `choice_reviewed` | sí | sí | sí |
| `checkout_posts` | sí | sí | sí |
| `orders_created` | sí | sí | sí |
| `psp_tokens_created` | sí | sí | sí |

</details>

## Fallos de arnés conservados

La corrida 1 (`corrida-1/`, sobre `54ebd5b`, árbol limpio) se conserva tal como salió y no
cuenta como resultado. Tuvo dos defectos del arnés, los dos corregidos en `d3740a8`. Los
oráculos y el prerregistro no cambiaron.

### 1. El guion del modelo no se reiniciaba entre REST y chat

En la corrida 1, INF29-HP quedó en 0/3 y falló en el chat con 13 de 17 oráculos. Los cuatro
oráculos fallidos son los mismos en r1, r2 y r3:

| Oráculo | Esperado | Observado en el chat |
| --- | --- | --- |
| `chat.model_http_attempts` | 3 | 2 |
| `chat.model_requests` | `model.route` ok, luego `model.choose_candidate` con `APIConnectionError` y retries 1 | solo `model.route` con `APIConnectionError` y retries 1 |
| `chat.steps` | `interpret_request`, `search_catalog`, `select_product` | `interpret_request` |
| `chat.failed_step` | `select_product` | `interpret_request` |

REST cumplió sus oráculos 3/3 y los oráculos de cero efectos se cumplieron 3/3.

**Causa.** La prueba usaba un solo `ScriptedModel` para las dos superficies, con un índice
global sobre el guion `route_ok, disconnect`.

- REST consumió `route_ok`, `disconnect` y `disconnect`.
- El chat recibió desde su primera solicitud el `disconnect` persistente, en lugar de empezar el
  guion otra vez. Las respuestas servidas al chat fueron `disconnect, disconnect`, y así quedó
  registrado en `corrida-1/summary.json`.

El prerregistro dice lo contrario: el guion corre «Per surface (HP) or per task run (HNP)». En
INF27-HP e INF28-HP el defecto no se veía, porque su guion es una sola respuesta persistente.

**Comportamiento del producto.** Fue coherente con el estímulo que de verdad recibió: modelo
inalcanzable en la interpretación, con la copia de `unreachable`. No hay defecto de producto.

**Corrección.** `ScriptedModel.restart()`, que `HpAgent.turn` llama al empezar cada superficie.

### 2. El orden de ejecución no era el del prerregistro

Pytest ejecuta las pruebas en el orden en que están definidas. En la corrida 1 los controles HP
(INF27-HP-control y CTRL-HP, que crean checkouts) corrieron antes de las celdas HNP sin efectos.
El `execution_order` del protocolo los pone después. El orden real de la corrida 1 consta en
`corrida-1/junit.xml`.

**Efecto observado: ninguno.** Las dos vías comparten el PostgreSQL efímero, pero:

- cada control termina y cancela su checkout antes de que el caso siguiente tome su huella
  «antes»;
- las celdas HNP pasaron 3/3 en las dos corridas.

Aun así es una desviación.

**Corrección.** Las funciones de prueba quedan en el orden del protocolo. `summary.json` ahora
registra `execution_order.observed` y `matches_protocol`, que en la corrida 2 es `true`. El
resumen de la corrida 1 no tiene ese campo.

## Versiones y hashes

### Commits y checkouts

| Pieza | Versión |
| --- | --- |
| Agent, base | `cd6d39fcc198f1a8e3bf9a7c4ffaebbd23c1af83` (main) |
| Agent, arnés y prerregistro | `54ebd5b32fecdc5b0965c9109ba70f332edb6a5e` (corrida 1) |
| Agent, corrección del arnés | `d3740a8816b5fea21124bbfedc40b91b78175032` (corrida 2, árbol limpio) |
| Merchant | `abf30df3b6a611284dedcc26c00accac6a853329`, limpio en las dos corridas |
| Proveedor de pagos | `e6ab67b5963434a9a3caae2012b70232a3cca65d`, limpio en las dos corridas |

### Documentos

| Documento | SHA-256 |
| --- | --- |
| Prerregistro `evaluation/scenarios/p0-model-failures.json` | `e2406af9b667d9b9f9a4b68e1f682fef304eaa513c34a959112a182bf3d397ae` |
| Especificación `docs/design/p0-model-failures.md` | `0b70704c51f6b27ceb73f1e5f7a8be9d16cac16d08cf41e7969d6c1957492e1c` |

- El prerregistro se confirmó en `54ebd5b`, antes de la primera corrida, y no cambió después.
  `git diff 54ebd5b -- evaluation/scenarios/p0-model-failures.json` está vacío.
- La especificación coincide con la que fija el prerregistro (`spec.matches_protocol: true`).

## Cómo reproducir

Hace falta Docker. El módulo borra cualquier variable `OPENAI_*` del entorno; no hace falta ni se
debe definir ninguna.

```sh
uv sync
R=$(mktemp -d)
RUN_LIVE_STACK=1 \
MERCHANT_REPO=/ruta/a/tesis-commerce-merchant \
PAYMENT_REPO=/ruta/a/tesis-payment-provider-sandbox \
uv run pytest tests/integration/test_p0_model_failures.py -v -rs -p no:cacheprovider \
  --basetemp="$R/basetemp" --junitxml="$R/junit.xml"
```

- El resumen queda en `$R/basetemp/live-stack0/p0-model-failures/summary.json`.
- La evidencia por caso (`exp01-frozen.json` y `exp01-observed.json`) queda en
  `$R/basetemp/test_p0_*`.
- Sin `RUN_LIVE_STACK=1`, los 36 casos se omiten.
- El arnés está descrito en `tests/integration/README.md`: `live_model.py` y
  `test_p0_model_failures.py`.

## Límites

- **El modelo es falso.** Se prueba cómo el Agent maneja el fallo HTTP: clasificación,
  reintentos del SDK, cabecera `x-should-retry`, copias, escalamiento y ausencia de efectos. No
  se prueba el comportamiento de un proveedor real:
  - cabeceras y cuerpos reales del 429, más allá de la forma de error de OpenAI que replica el
    guion;
  - latencias y esperas reales: el 429 lleva `retry-after-ms: 1` y el *timeout* de INF33-a es un
    `httpx.ReadTimeout` inmediato;
  - la caída de red de INF29 es un `httpx.ConnectError` guionado.
- **No se prueba comprensión semántica.** Las respuestas válidas son fijas: la ruta de compra del
  teléfono, el primer candidato admitido y la confirmación del revisor. Nada aquí mide si un
  modelo entiende la petición o elige bien.
- **INF33-HP no aplica.** En HP aprueba el comprador; no hay revisor del modelo.
- **El Agent HNP corre en el proceso de pruebas.** `ModelHnpAgent` no corre en un proceso propio
  como `HnpAgent`, porque el doble del transporte solo llega a los clientes HTTP del proceso de
  pruebas. Por eso estas celdas no cubren caídas ni reinicios del Agent.
- **Las repeticiones no miden varianza.** El doble es determinista: las tres repeticiones
  muestran que el arnés y el producto son estables, no una varianza estadística.
- **Organización de los Agents en HP.** En las celdas de fallo, un mismo Agent atiende REST y
  luego chat. En los controles hay un Agent y un episodio por superficie, porque la guarda de
  compra pendiente bloquea una segunda compra del mismo dueño.
- **La matriz P0 no se actualiza aquí.** No se editaron `evaluation/scenarios/p0/matrix.json`
  ni `matrix-observed.json`. El registro observado de estas celdas depende del PR #174, de otro
  autor, y se hará aparte.

## Archivos

| Archivo | Contenido |
| --- | --- |
| `summary.json` | Corrida 2, la registrada, copiada byte a byte |
| `junit.xml` | Corrida 2: 36 tests, 0 failures, 0 errors, 0 skipped, 74.7 s |
| `corrida-1/summary.json` | Corrida 1, conservada |
| `corrida-1/junit.xml` | Corrida 1: 36 tests, 3 failures (INF29-HP r1–r3), 0 errors |
| `SHA256SUMS` | Hashes de estos archivos |

- `corrida-1/junit.xml` conserva, en las trazas de fallo, rutas absolutas del directorio
  temporal de la corrida. No se reescribieron.
- No se guardan en Git:
  - las bases SQLite del Agent;
  - la evidencia expandida por caso (`exp01-*.json`, con las filas completas de
    `model_requests`);
  - `evidence.jsonl` y los logs.

  `summary.json` solo lleva conteos, códigos y las filas proyectadas.
