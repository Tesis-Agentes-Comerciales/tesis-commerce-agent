# P0: cambio de producto, desistimiento y cantidad (UX08, UX09 y UX10)

En la corrida registrada, sobre `f59df0f`, pasan los 45 casos en vivo: 15 celdas por 3
repeticiones. Las 15 quedan en 3/3 y cumplen todos sus oráculos en todas las repeticiones.

Las 17 celdas de CI pasan sus 46 pruebas, en el mismo commit.

Ninguna corrida usó un modelo real, llamó a un proveedor ni tuvo costo.

## Qué se midió

Se probaron las celdas de `docs/design/p0-ux08-ux10.md` §4, de dos maneras.

- **En vivo**, con `RUN_LIVE_STACK=1`, contra el Merchant real, el proveedor de pagos sandbox real
  y el PostgreSQL efímero de `tests/integration`. Módulo: `tests/integration/test_p0_ux08_ux10.py`.
- **En CI**, con pytest y sin red. Son las pruebas que fija el prerregistro en `ci_cells`.

| Matriz | Qué pasa | HP | HNP |
| --- | --- | --- | --- |
| UX08 | El comprador cambia de A a B después de la propuesta y antes de autorizar | vivo y CI | CI; el control, en vivo |
| UX09 | «Ya no quiero comprar» mientras el Agent todavía trabaja | vivo y CI | vivo y CI |
| UX10 | Pide dos unidades cuando el grant autoriza una | no aplica | vivo (b) y CI (a, b, c) |

Las condiciones en vivo son las B2 gobernadas:

- **B2-HP**: carril b0, `enforce`, `governed`, `b2-hp-v2` y AP2.
- **B2-HNP**: carril hnp, selector `llm`; revisor `llm` solo en UX09-HNP-rev y su control.

Las celdas de CI cubren además B0 y B0-HNP. Sus condiciones constan en la tabla de CI.

A es la variante `Q100286751-256GB` y B es `Q100348826-256GB`.

### El doble del modelo

- **Qué es real.** El SDK de OpenAI, su cliente HTTP y todo lo que el Agent hace con la
  respuesta.
- **Qué es falso.** Solo el transporte HTTP. `ModelServer` (`httpx.MockTransport`) sirve el
  guion del caso dentro del proceso de pruebas, con un modelo (`p0-scripted-model`) y una
  credencial sintéticos. `ScriptedModel` está en `tests/integration/live_model.py`.
- **La respuesta retenida.** `held(...)` marca `entered` cuando llega la solicitud y espera a
  `release`. La prueba detiene la corrida mientras espera y después la suelta.

### Cómo se detiene

- **HP: desconexión real.** El cliente abre `POST /assistant` en su propia conexión y la cierra
  mientras el modelo está retenido. Un gancho de prueba en `RunController._mark_cancelled` avisa
  cuando el servidor marcó la corrida cancelada. La respuesta se suelta 100 ms después (a y b) o
  enseguida (race).
- **HNP: revocación o kill switch.** El dueño revoca (`POST /delegations/{grant_id}/revoke`) o el
  operador enciende el kill switch (`POST /internal/hnp/kill-switch`) mientras el selector o el
  revisor están retenidos. En UX09-HNP-post, el dueño revoca dentro del proxy del Merchant,
  justo después de que el Merchant respondió `POST /checkout_sessions`.

### Qué se observa

- **Intentos HTTP al modelo** y filas `model_requests` proyectadas.
- **Lo que ve el comprador**: el texto del chat, el estado de la compra, los códigos de REST, el
  estado de la tarea, su `outcome_code` y los eventos de la tarea desde `task_claimed`.
- **Cero efectos fuera del Agent.**
  - Merchant: ningún POST de checkout por el proxy, ningún checkout del episodio y una huella
    completa igual antes y después.
  - PSP: ningún token nuevo, una huella igual y, en HNP, ningún POST.
- **Cero efectos en el registro del Agent**: ninguna fila nueva en `approvals`,
  `completion_results`, `grant_reservations`, `dispatch_outbox`, `orders`, `hp_evidence`,
  `governance_evidence` ni `ap2_evidence`, y ningún mandato cerrado.
- **Después del stop (HNP)**: los POST al Merchant y las solicitudes al PSP que llegan después de
  la marca del stop.
- **Controles positivos** por modo: el mismo guion sin detener (UX09-HP-control,
  UX09-HNP-control, UX09-HNP-rev-control), B aprobado con su propia huella (UX08-HP-control), el
  grant de B (UX08-HNP-control) y una unidad (UX10-HNP-control).

## Celdas en vivo (corrida registrada)

En cada repetición, la columna da los oráculos cumplidos sobre los oráculos de la celda.

| Celda | Modo | Condición | r1 | r2 | r3 | Repeticiones |
| --- | --- | --- | --- | --- | --- | --- |
| UX09-HP-a | HP | B2-HP | 14/14 | 14/14 | 14/14 | 3/3 |
| UX09-HP-b | HP | B2-HP | 14/14 | 14/14 | 14/14 | 3/3 |
| UX09-HP-race | HP | B2-HP | 11/11 | 11/11 | 11/11 | 3/3 |
| UX09-HNP-sel | HNP | B2-HNP | 16/16 | 16/16 | 16/16 | 3/3 |
| UX09-HNP-sel-kill | HNP | B2-HNP | 16/16 | 16/16 | 16/16 | 3/3 |
| UX09-HNP-rev | HNP | B2-HNP | 16/16 | 16/16 | 16/16 | 3/3 |
| UX08-HP | HP | B2-HP | 27/27 | 27/27 | 27/27 | 3/3 |
| UX09-HNP-post | HNP | B2-HNP | 16/16 | 16/16 | 16/16 | 3/3 |
| UX09-HP-control | HP | B2-HP | 5/5 | 5/5 | 5/5 | 3/3 |
| UX08-HP-control | HP | B2-HP | 38/38 | 38/38 | 38/38 | 3/3 |
| UX08-HNP-control | HNP | B2-HNP | 9/9 | 9/9 | 9/9 | 3/3 |
| UX09-HNP-control | HNP | B2-HNP | 8/8 | 8/8 | 8/8 | 3/3 |
| UX09-HNP-rev-control | HNP | B2-HNP | 8/8 | 8/8 | 8/8 | 3/3 |
| UX10-HNP-control | HNP | B2-HNP | 6/6 | 6/6 | 6/6 | 3/3 |
| UX10-HNP-b | HNP | B2-HNP | 10/10 | 10/10 | 10/10 | 3/3 |

UX09-HP-REST no aplica: REST es síncrono y no se puede interrumpir. Un segundo pedido espera el
lock y recibe la guarda de compra pendiente (spec §2.2 y §4.4). Tampoco aplican UX10-HP (la
matriz pone UX10 solo en HNP) ni, en vivo, UX10-HNP-c (el Merchant vivo no tiene una falla que
devuelva cantidad 2), UX10-HNP-a ni UX08-HNP (sin modelo ni nada que añada el stack). Se declaran
en el prerregistro y no se ejecutan ni se cuentan.

Las tres repeticiones de cada celda observan lo mismo. La única excepción es UX08-HP-control:
el Merchant devuelve los dos checkouts del episodio ordenados por su id, que es aleatorio. En r2
y r3 salieron en orden inverso; el conjunto es el mismo (A cancelado y B completado, una unidad
cada uno). Por eso esa celda marca `observed_identical_across_repetitions: false`. Lo verifiqué
comparando los `exp01-observed.json` de las tres repeticiones con los checkouts ordenados.

### Estímulo, oráculo, observación y veredicto

Estos son los valores de la repetición 1, iguales en r2 y r3.

| Celda | Estímulo | Oráculo | Observado | Veredicto |
| --- | --- | --- | --- | --- |
| UX09-HP-a | Chat «Quiero A» con `route_a` retenido; el cliente cierra; se suelta 100 ms después de la marca; luego «hola» | 1 intento; el turno no se guarda; 0 POST de checkout; 0 compras; el turno siguiente responde con 1 intento y sin compra pendiente; cero efectos | 1 intento (`model.route` ok); turno sin guardar; «Entendido. Dime qué quieres buscar.»; 0 llamadas al Merchant; cero efectos | pasa 3/3 |
| UX09-HP-b | Igual, con `route_a` respondido y `choose_a` retenido (el catálogo ya leído) | 2 intentos; lo demás como en a | 2 intentos (`model.route`, `model.choose_candidate`); turno sin guardar; 0 llamadas al Merchant; cero efectos | pasa 3/3 |
| UX09-HP-race | Igual que b, pero `choose_a` se suelta apenas el servidor marca la corrida (0 ms) | 2 intentos; turno sin guardar; 0 POST de checkout; 0 compras; cero efectos | 2 intentos; turno sin guardar; 0 llamadas al Merchant; cero efectos | pasa 3/3 |
| UX09-HNP-sel | `choice_ok` retenido; el dueño revoca; se suelta | 1 intento; tarea `canceled` / `revoked_by_owner`; eventos `task_claimed`, `revoked`, `task_finished`; 0 compras; sin `checkout_cancel`; 0 POST ni PSP después del stop; cero efectos | exactamente eso; grant `revoked` (`owner`); el revoke responde 200 con `effect: no_effect`; 0 llamadas al Merchant y al PSP | pasa 3/3 |
| UX09-HNP-sel-kill | `choice_ok` retenido; el operador enciende el kill switch; se suelta | 1 intento; tarea `canceled` / `kill_switch`; eventos `task_claimed`, `kill_switch_blocked`, `revoked`, `task_finished`; lo demás como en sel | exactamente eso; grant `revoked` (`operator`); 0 llamadas al Merchant y al PSP | pasa 3/3 |
| UX09-HNP-rev | `choice_ok` y luego `review_confirmed` retenido; el dueño revoca; se suelta | 2 intentos; `canceled` / `revoked_by_owner`; eventos como en sel; una compra `canceled` sin `checkout_id`; 0 POST ni PSP después del stop; cero efectos | exactamente eso; `choice_reviewed` = `confirmed` queda registrado y nada viene después; 0 llamadas al Merchant y al PSP | pasa 3/3 |
| UX08-HP | Turno 1 «Quiero A»; turno 2 «Mejor quiero B» en chat y REST; cancelar A; turno 3 «Mejor quiero B»; aprobar B con fA; aprobar A con fA; completar B sin aprobar | turno 1: 2 intentos, 1 POST, A esperando; turno 2: 0 intentos, la guarda de compra pendiente en chat y REST; cancelar A: 200 y 1 cancel en el Merchant; turno 3: B es una corrida y un checkout nuevos, con otra huella y su `checkout_jwt`; los tres intentos dan 409; B sigue esperando; exactamente dos checkouts; sin pago ni orden | intentos [2, 0, 2]; turno 2: «Ya hay una compra pendiente. Revísala o cancélala antes de iniciar otra.» y REST 200 `clarify`; cancelar A: 200 `canceled`; rechazos 409 `checkout_terms_changed`, 409 `invalid_stage`, 409 `invalid_stage`; Merchant: A `canceled` y B `ready_for_payment`, 1 unidad cada uno; 0 `approvals`, 0 órdenes, 0 filas D2 | pasa 3/3 |
| UX09-HNP-post | `choice_ok`; el dueño revoca justo después de que el Merchant respondió `POST /checkout_sessions` | 1 intento; `canceled` / `revoked_by_owner`; compra `canceled`; 1 POST de checkout (201); 1 cancel; un solo checkout, cancelado, de una unidad; `checkout_cancel` = `canceled`; 0 reservas, mandatos, despacho, orden, token y POST al PSP | exactamente eso. Después de la revocación el Agent aún envió la actualización de entrega (`POST /checkout_sessions/*`) y leyó la oferta (`POST /lab/offers/get`); luego D2 negó y canceló. El registro tiene D1 `allow`, D2 `deny` y una `freshness_check` | pasa 3/3 |
| UX09-HP-control | El guion de UX09-HP-b leído hasta el final, sin detener | 2 intentos; turno guardado; compra esperando; 1 POST (201); sin pago | exactamente eso; un checkout `ready_for_payment` de A, 1 unidad | pasa 3/3 |
| UX08-HP-control | Todo UX08-HP; luego releer la aprobación, aprobar B con fB y completar B | todo UX08-HP; aprobar 200; completar 200; la orden es de B; A `canceled` sin orden; 1 complete; 1 orden; 1 token; solo B aprobado | exactamente eso; Merchant: A `canceled` y B `completed`, 1 unidad cada uno | pasa 3/3 |
| UX08-HNP-control | Proponer A, proponer B, consentir B, disparar con `choice_ok` | huellas de propuesta distintas; consentir B 201; grant con el alcance de B; 1 intento; tarea `purchased`; un solo checkout, de B, una unidad; 1 orden; 1 token | exactamente eso; ningún checkout de A | pasa 3/3 |
| UX09-HNP-control | `choice_ok`, nadie revoca | 1 intento; tarea `purchased`; 1 POST (201); 1 orden; 1 token | exactamente eso; grant `consumed` | pasa 3/3 |
| UX09-HNP-rev-control | `choice_ok` y `review_confirmed`, nadie revoca | 2 intentos; tarea `purchased`; 1 POST (201); 1 orden; 1 token | exactamente eso; `choice_reviewed` = `confirmed` | pasa 3/3 |
| UX10-HNP-control | Delegación sin texto de preferencia; `choice_ok` | 1 intento; `purchased`; un solo checkout, completado, de una unidad; 1 orden; 1 token | exactamente eso | pasa 3/3 |
| UX10-HNP-b | Proponer con «Quiero dos unidades, una para mi hermana.»; consentir dos veces; `choice_ok` | consentimientos 201 y 200; el mismo grant; 1 uso; 1 intento; `purchased`; un solo checkout, completado, de una unidad; 1 orden; 1 token; grant `consumed` | exactamente eso | pasa 3/3 |

`summary.json` guarda, por celda, cada oráculo en k/3, cada repetición con sus oráculos fallidos
y lo observado en la primera repetición.

## Celdas de CI

Se corrieron una vez, en `f59df0f` con el árbol limpio, después de la corrida en vivo:
`uv run pytest <los 46 node ids de ci_cells> --junitxml=ci-junit.xml`. Resultado: 46 pasan, 0
fallan, 0 omitidas. Las pruebas exactas de cada celda están en el prerregistro.

| Celda | Modo | Condiciones | Pruebas | Pasan |
| --- | --- | --- | --- | --- |
| UX08-HP | HP | B0; B2-HP | 2 | 2/2 |
| UX08-HP-control | HP | B0; B2-HP | 2 | 2/2 |
| UX08-HNP | HNP | delegation API (B0-HNP and B2-HNP share it) | 1 | 1/1 |
| UX09-HP-a | HP | B0; B2-HP | 2 | 2/2 |
| UX09-HP-b | HP | B0; B2-HP | 2 | 2/2 |
| UX09-HP-race | HP | B0 | 4 | 4/4 |
| UX09-HP-disconnect | HP | B0 | 1 | 1/1 |
| UX09-HP-control | HP | B0; B2-HP | 6 | 6/6 |
| UX09-HNP-sel | HNP | B0-HNP (Bench, off); B2-HNP (Bench, enforce) | 2 | 2/2 |
| UX09-HNP-sel-kill | HNP | B0-HNP (Bench, off); B2-HNP (Bench, enforce) | 2 | 2/2 |
| UX09-HNP-rev | HNP | B2-HNP (Bench, enforce, reviewer) | 2 | 2/2 |
| UX09-HNP-post | HNP | B0-HNP (Bench, off); B2-HNP (Bench, enforce); B2-HNP (real D1-HNP harness) | 6 | 6/6 |
| UX09-HNP-control | HNP | B0-HNP (Bench, off); B2-HNP (Bench, enforce) | 2 | 2/2 |
| UX10-HNP-a | HNP | delegation API | 4 | 4/4 |
| UX10-HNP-b | HNP | delegation API; assistant (D3) | 2 | 2/2 |
| UX10-HNP-c | HNP | B0-HNP (Bench); B2-HNP (real D1-HNP harness) | 4 | 4/4 |
| UX10-HNP-control | HNP | B0-HNP (Bench); B2-HNP (real D1-HNP harness) | 2 | 2/2 |

### Estímulo, oráculo, observación y veredicto

| Celda | Estímulo | Oráculo | Observado | Veredicto |
| --- | --- | --- | --- | --- |
| UX08-HP | Modelo guionado: A propuesta, «Mejor quiero B», cancelar A, B propuesta; aprobar B con fA, aprobar A, completar B sin aprobar | spec §4.2 UX08-HP: nada aprobado para A sirve para B | las 2 pasan (B0 y B2-HP) | pasa |
| UX08-HP-control | Lo mismo, y aprobar B con fB | B se compra con su propia huella | las 2 pasan | pasa |
| UX08-HNP | API de delegación: proponer A, proponer B, consentir | solo B se puede consentir (D2) y el grant tiene el alcance de B | pasa | pasa |
| UX09-HP-a | Detener mientras el modelo interpreta | no se prepara nada | las 2 pasan (B0 y B2-HP) | pasa |
| UX09-HP-b | Detener mientras el modelo elige | no se prepara nada | las 2 pasan | pasa |
| UX09-HP-race | Unitaria del worker con la corrida marcada (`[stopped]`) y desconexión real con 0, 5 y 20 ms | spec §3.2: la unitaria `[stopped]` es el oráculo determinista | las 4 pasan | pasa |
| UX09-HP-disconnect | Desconexión del cliente mientras responde el router | el turno se cancela antes del checkout (spec §6) | pasa | pasa |
| UX09-HP-control | Los mismos guiones sin detener | se prepara el checkout | las 6 pasan | pasa |
| UX09-HNP-sel | `Bench`: revocar mientras el selector responde | la tarea termina antes de cualquier POST | las 2 pasan (off y enforce) | pasa |
| UX09-HNP-sel-kill | `Bench`: kill switch mientras el selector responde | igual | las 2 pasan | pasa |
| UX09-HNP-rev | `Bench`: revocar o kill switch mientras el revisor responde | igual | las 2 pasan | pasa |
| UX09-HNP-post | `Bench` y arnés D1-HNP real: revocar o kill switch justo después del POST; el Merchant que rechaza la cancelación | un cancel en el Merchant (D4), a mejor esfuerzo; el rechazo queda registrado y el terminal no cambia | las 6 pasan | pasa |
| UX09-HNP-control | `Bench` sin stop | la corrida llega al compromiso del despacho | las 2 pasan | pasa |
| UX10-HNP-a | Proponer con `quantity`, `quantity_max`, `uses` o `uses_max` en 2 | cuatro 422 `delegation_request_invalid`; 0 propuestas | las 4 pasan | pasa |
| UX10-HNP-b | Proponer con «Quiero dos unidades…»; consentir dos veces; el mensaje que ofrece la delegación | 1 grant (201 y 200), `quantity_max=1`, uso único; el mensaje dice «1 unidad» (D3) | las 2 pasan | pasa |
| UX10-HNP-c | El Merchant devuelve 2 unidades al crear y, en otra corrida, desde la actualización | B0-HNP: `checkout_failed`, compra `failed` con `checkout_mismatch`, 1 cancel (D4). B2-HNP con D1 real: compra `uncertain`, 0 cancels, `left_to_d1_reconciliation` | las 4 pasan | pasa |
| UX10-HNP-control | El Merchant devuelve 1 unidad, en B0-HNP y con D1 real | llega al despacho con una sola línea de 1 unidad | las 2 pasan | pasa |

La tarjeta de D3 («1 unidad») se prueba en Storybook, fuera de este JUnit.

## Discriminación del arreglo de §3.2 (UX09-HP-race)

El arreglo de §3.2 hace que el worker del chat lea `controller.is_cancelled` antes de su
siguiente paso (`assistant_transport.py`, PR-A, #187). Hay dos mediciones sin el arreglo.

**La del revisor de PR-A**, que no repetí:

- la unitaria `[stopped]` cae 5 de 5;
- `[5-stopped]` cae 5 de 5;
- `[0-stopped]` cae 3 de 5, no siempre;
- `[20-stopped]` pasa 5 de 5, así que no distingue nada.

**La mía**, en `df3df33` con solo esa línea revertida (`desarrollo/sin-arreglo/arreglo-revertido.diff`).
No es evidencia registrada:

| Prueba | Sin el arreglo (5 corridas) | Con el arreglo (5 corridas) |
| --- | --- | --- |
| unitaria `[stopped]` | cae 5/5 | pasa 5/5 |
| unitaria `[running]` (control) | pasa 5/5 | pasa 5/5 |
| desconexión `[0-stopped]` | cae 4/5 | pasa 5/5 |
| desconexión `[5-stopped]` | cae 5/5 | pasa 5/5 |
| desconexión `[20-stopped]` | pasa 5/5 | pasa 5/5 |
| desconexión `[0, 5, 20-control]` | pasan 5/5 | pasan 5/5 |
| en vivo UX09-HP-race (3 repeticiones) | cae 3/3: se crea el checkout | pasa 3/3 (corrida registrada) |
| en vivo UX09-HP-a y UX09-HP-b (3 repeticiones) | pasan 3/3 | pasan 3/3 |

Conclusiones:

- **El oráculo determinista es la unitaria `[stopped]`.** Cae 5 de 5 sin el arreglo.
- **La celda de 20 ms no discrimina.** Pasa con y sin el arreglo.
- `[0-stopped]` discrimina, pero no siempre (3/5 y 4/5).
- En vivo, solo UX09-HP-race discrimina. UX09-HP-a y UX09-HP-b pasan sin el arreglo, en vivo y
  en CI (`test_ux08_ux09_hp.py`, tres corridas de tres): cubren UX09-HP, no §3.2.
- La ventana entre el cierre TCP y `is_cancelled` está declarada en §3.2 y no se mide: la
  respuesta se suelta después de la marca.

En vivo sin el arreglo, UX09-HP-race falló en los oráculos `checkout_posts`, `purchases`,
`merchant_no_checkout_post`, `merchant_no_checkout`, `merchant_snapshot_equal` y
`ledger_no_new_rows`: el Agent creó el checkout de A después de la desconexión.

## Corridas de desarrollo

Antes de congelar el prerregistro hubo tres corridas de desarrollo en vivo. No son evidencia ni
se registran. Se conservan en `desarrollo/`.

1. **`corrida-1/`** (solo la repetición 1, con `-x`): paró en UX09-HP-a con `error`. El
   estímulo no ocurrió (`stimulus_verified: false`): el arnés descartaba el iterador de líneas
   de la respuesta, el recolector lo cerraba y la conexión se cerraba antes de llamar al modelo.
   Ahora el arnés conserva el iterador.
2. **`corrida-2/`** (solo la repetición 1): pasó 15 de 15.
3. **`sin-arreglo/vivo/`**: los tres casos de stop HP, 3 repeticiones, con el arreglo de §3.2
   revertido. Ver la sección anterior.

Las tres corrieron sobre `df3df33` con el árbol sucio (el arnés aún sin commit), y su
`summary.json` lo dice (`dirty: true`). Su prerregistro tiene otro hash (`76ff10c…`): cambió
después solo fuera de `cases`. Lo verifiqué comparando la definición de cada caso que congeló
cada repetición (`exp01-frozen.json`) con el prerregistro final: son idénticas en las tres
corridas de desarrollo y en la registrada. La versión intermedia del prerregistro no se
conserva.

`desarrollo/sin-arreglo/ci/` y `desarrollo/con-arreglo/ci/` guardan los JUnit de las cinco
corridas de las 8 pruebas de `tests/test_assistant_transport.py` de la tabla anterior.

## Límites declarados

- **UX10-HNP-c en B2-HNP.** El checkout de 2 unidades queda abierto en el Merchant hasta que la
  reconciliación de D1 resuelva la compra `uncertain`. D4 no cancela un estado incierto.
- **`left_to_d1_reconciliation`.** `task_finished` registra `checkout_cancel {checkout_id, result:
  left_to_d1_reconciliation}` en cualquier `checkout_failed` con la compra `uncertain`, no solo
  después de un mismatch de cantidad. Por ejemplo, un timeout de la actualización en B2-HNP. No
  cancela nada, pero cambia el cuerpo de `task_finished` en esas corridas B2-HNP nuevas.
- **UX09-HNP-rev.** La compra que abrió el revisor termina `canceled` sin checkout.
- **Alcance de D4.** D4 no cubre `grant_expired`, `policy_denied`, `governance_unavailable`,
  `mandate_proof_invalid`, `checkout_failed` al cerrar, la reclamación después de una caída ni un
  estado `uncertain` de D1.
- **UX09-HNP-post.** Después de la revocación, y antes de que D2 la detectara, el Agent aún envió
  la actualización de entrega y leyó la oferta en el Merchant. No hubo reserva, mandato,
  despacho, orden ni token. El checkout quedó cancelado.
- **UX09-HP-race.** Ver la discriminación: la celda de 20 ms no discrimina, y UX09-HP-a y -b no
  discriminan §3.2.
- **UX08: desviación D1.** «Actualizar propuesta» son dos pasos: cancelar A y pedir B, como fija
  la decisión D1 de la spec.
- **UX09-HP-REST no aplica.** Ver arriba.
- **El proxy b0 no permite cancelar.** La lista de rutas del carril b0 no incluye
  `POST /checkout_sessions/*/cancel`. UX08-HP y UX08-HP-control la añaden para el caso y la
  restauran después.
- **El modelo es falso.** No se prueba comprensión semántica ni el comportamiento de un proveedor
  real. Las respuestas válidas son fijas.
- **El Agent HNP corre en el proceso de pruebas** (`ModelHnpAgent`). Estas celdas no cubren caídas
  ni reinicios del Agent.
- **Las repeticiones no miden varianza.** Muestran que el arnés y el producto son estables.

## Versiones y hashes

### Commits y checkouts

| Pieza | Versión |
| --- | --- |
| Agent, base | `df3df33a864022612c8d1d25c30d4a34b074afc7` (main, con PR-A #187 y PR-B #185) |
| Agent, prerregistro | `4719c3369d69f15d30bc8a3eb3aef67e77739fc0` |
| Agent, corrida registrada y CI | `f59df0fdb176cf7ad6360b0b0916d061105cbe38`, árbol limpio |
| Merchant | `abf30df3b6a611284dedcc26c00accac6a853329`, limpio |
| Proveedor de pagos | `e6ab67b5963434a9a3caae2012b70232a3cca65d`, limpio |

### Documentos

| Documento | SHA-256 |
| --- | --- |
| Prerregistro `evaluation/scenarios/p0-ux08-ux10.json` | `1d8d78729bccdec921ad38a84e7088044bfa9823ead77f963e04f01aa7c371ed` |
| Especificación `docs/design/p0-ux08-ux10.md` | `1ebc84bdbe0611e4080cc1998f6132730bf24747e3dd80628d124f66239e04a0` |

- El prerregistro se confirmó en `4719c33`, antes de la corrida registrada, y no cambió después.
- La especificación coincide con la que fija el prerregistro (`spec.matches_protocol: true`).
- Los hashes se calculan con LF, como `preregistration_digest` de `p0_observed.py`.

## Cómo reproducir

Hace falta Docker. El módulo borra cualquier variable `OPENAI_*` del entorno; no hace falta ni se
debe definir ninguna.

```sh
uv sync --frozen --all-groups
R=$(mktemp -d)
RUN_LIVE_STACK=1 \
MERCHANT_REPO=/ruta/a/tesis-commerce-merchant \
PAYMENT_REPO=/ruta/a/tesis-payment-provider-sandbox \
uv run pytest tests/integration/test_p0_ux08_ux10.py -v -rs -p no:cacheprovider \
  --basetemp="$R/basetemp" --junitxml="$R/junit.xml"
```

- `MERCHANT_REPO` y `PAYMENT_REPO` apuntan a clones en los SHAs de la tabla de versiones.
- El resumen queda en `$R/basetemp/live-stack0/p0-ux08-ux10/summary.json`.
- Sin `RUN_LIVE_STACK=1`, los 45 casos se omiten.
- Las celdas de CI: los node ids de `ci_cells` del prerregistro, con `uv run pytest ...
  --junitxml=ci-junit.xml`.

## Celda × repetición × oráculo

<details>
<summary>Tabla completa (corrida registrada)</summary>

#### UX09-HP-a

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `stop.entered_marked_and_finished` | sí | sí | sí |
| `model_http_attempts_stopped_turn` | sí | sí | sí |
| `stopped_turn_saved` | sí | sí | sí |
| `checkout_posts` | sí | sí | sí |
| `purchases` | sí | sí | sí |
| `next_turn_text` | sí | sí | sí |
| `next_turn_model_http_attempts` | sí | sí | sí |
| `pending_purchase_after` | sí | sí | sí |
| `zero_effects.merchant_no_checkout_post` | sí | sí | sí |
| `zero_effects.merchant_no_checkout` | sí | sí | sí |
| `zero_effects.merchant_snapshot_equal` | sí | sí | sí |
| `zero_effects.psp_no_token` | sí | sí | sí |
| `zero_effects.psp_snapshot_equal` | sí | sí | sí |
| `zero_effects.ledger_no_new_rows` | sí | sí | sí |

#### UX09-HP-b

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `stop.entered_marked_and_finished` | sí | sí | sí |
| `model_http_attempts_stopped_turn` | sí | sí | sí |
| `stopped_turn_saved` | sí | sí | sí |
| `checkout_posts` | sí | sí | sí |
| `purchases` | sí | sí | sí |
| `next_turn_text` | sí | sí | sí |
| `next_turn_model_http_attempts` | sí | sí | sí |
| `pending_purchase_after` | sí | sí | sí |
| `zero_effects.merchant_no_checkout_post` | sí | sí | sí |
| `zero_effects.merchant_no_checkout` | sí | sí | sí |
| `zero_effects.merchant_snapshot_equal` | sí | sí | sí |
| `zero_effects.psp_no_token` | sí | sí | sí |
| `zero_effects.psp_snapshot_equal` | sí | sí | sí |
| `zero_effects.ledger_no_new_rows` | sí | sí | sí |

#### UX09-HP-race

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `stop.entered_marked_and_finished` | sí | sí | sí |
| `model_http_attempts_stopped_turn` | sí | sí | sí |
| `stopped_turn_saved` | sí | sí | sí |
| `checkout_posts` | sí | sí | sí |
| `purchases` | sí | sí | sí |
| `zero_effects.merchant_no_checkout_post` | sí | sí | sí |
| `zero_effects.merchant_no_checkout` | sí | sí | sí |
| `zero_effects.merchant_snapshot_equal` | sí | sí | sí |
| `zero_effects.psp_no_token` | sí | sí | sí |
| `zero_effects.psp_snapshot_equal` | sí | sí | sí |
| `zero_effects.ledger_no_new_rows` | sí | sí | sí |

#### UX09-HNP-sel

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `trigger_accepted` | sí | sí | sí |
| `stop_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `task` | sí | sí | sí |
| `events_after_claim` | sí | sí | sí |
| `purchases` | sí | sí | sí |
| `task_finished_checkout_cancel` | sí | sí | sí |
| `merchant_posts_after_stop` | sí | sí | sí |
| `psp_requests_after_stop` | sí | sí | sí |
| `zero_effects.merchant_no_checkout_post` | sí | sí | sí |
| `zero_effects.merchant_no_checkout` | sí | sí | sí |
| `zero_effects.merchant_snapshot_equal` | sí | sí | sí |
| `zero_effects.psp_no_token` | sí | sí | sí |
| `zero_effects.psp_snapshot_equal` | sí | sí | sí |
| `zero_effects.ledger_no_new_rows` | sí | sí | sí |
| `zero_effects.psp_no_post` | sí | sí | sí |

#### UX09-HNP-sel-kill

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `trigger_accepted` | sí | sí | sí |
| `stop_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `task` | sí | sí | sí |
| `events_after_claim` | sí | sí | sí |
| `purchases` | sí | sí | sí |
| `task_finished_checkout_cancel` | sí | sí | sí |
| `merchant_posts_after_stop` | sí | sí | sí |
| `psp_requests_after_stop` | sí | sí | sí |
| `zero_effects.merchant_no_checkout_post` | sí | sí | sí |
| `zero_effects.merchant_no_checkout` | sí | sí | sí |
| `zero_effects.merchant_snapshot_equal` | sí | sí | sí |
| `zero_effects.psp_no_token` | sí | sí | sí |
| `zero_effects.psp_snapshot_equal` | sí | sí | sí |
| `zero_effects.ledger_no_new_rows` | sí | sí | sí |
| `zero_effects.psp_no_post` | sí | sí | sí |

#### UX09-HNP-rev

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `trigger_accepted` | sí | sí | sí |
| `stop_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `task` | sí | sí | sí |
| `events_after_claim` | sí | sí | sí |
| `purchases` | sí | sí | sí |
| `task_finished_checkout_cancel` | sí | sí | sí |
| `merchant_posts_after_stop` | sí | sí | sí |
| `psp_requests_after_stop` | sí | sí | sí |
| `zero_effects.merchant_no_checkout_post` | sí | sí | sí |
| `zero_effects.merchant_no_checkout` | sí | sí | sí |
| `zero_effects.merchant_snapshot_equal` | sí | sí | sí |
| `zero_effects.psp_no_token` | sí | sí | sí |
| `zero_effects.psp_snapshot_equal` | sí | sí | sí |
| `zero_effects.ledger_no_new_rows` | sí | sí | sí |
| `zero_effects.psp_no_post` | sí | sí | sí |

#### UX08-HP

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `turn_1.model_http_attempts` | sí | sí | sí |
| `turn_1.checkout_posts` | sí | sí | sí |
| `turn_1.stage` | sí | sí | sí |
| `turn_1.line_variant` | sí | sí | sí |
| `turn_2.chat_text` | sí | sí | sí |
| `turn_2.rest` | sí | sí | sí |
| `turn_2.model_http_attempts` | sí | sí | sí |
| `turn_2.checkout_posts` | sí | sí | sí |
| `turn_2.pending_purchase` | sí | sí | sí |
| `cancel_a.status_and_stage` | sí | sí | sí |
| `cancel_a.merchant_cancels` | sí | sí | sí |
| `cancel_a.merchant_checkout_status` | sí | sí | sí |
| `turn_3.model_http_attempts` | sí | sí | sí |
| `turn_3.checkout_posts` | sí | sí | sí |
| `turn_3.stage` | sí | sí | sí |
| `turn_3.line_variant` | sí | sí | sí |
| `turn_3.new_run_and_checkout` | sí | sí | sí |
| `turn_3.fingerprint_differs` | sí | sí | sí |
| `turn_3.checkout_jwt` | sí | sí | sí |
| `refused.approve_b_with_fa` | sí | sí | sí |
| `refused.approve_a_with_fa` | sí | sí | sí |
| `refused.complete_b_unapproved` | sí | sí | sí |
| `b_after` | sí | sí | sí |
| `merchant_checkouts` | sí | sí | sí |
| `no_complete` | sí | sí | sí |
| `no_payment_effects` | sí | sí | sí |
| `agent_ledger` | sí | sí | sí |

#### UX09-HNP-post

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `trigger_accepted` | sí | sí | sí |
| `stop_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `task` | sí | sí | sí |
| `purchase_stage` | sí | sí | sí |
| `checkout_posts` | sí | sí | sí |
| `merchant_cancels` | sí | sí | sí |
| `merchant_checkouts` | sí | sí | sí |
| `task_finished_checkout_cancel` | sí | sí | sí |
| `grant_reservations` | sí | sí | sí |
| `closed_mandates` | sí | sí | sí |
| `dispatch_outbox` | sí | sí | sí |
| `no_complete` | sí | sí | sí |
| `orders_created` | sí | sí | sí |
| `psp_tokens_created` | sí | sí | sí |
| `psp_posts` | sí | sí | sí |

#### UX09-HP-control

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `model_http_attempts` | sí | sí | sí |
| `turn_saved` | sí | sí | sí |
| `stage` | sí | sí | sí |
| `checkout_posts` | sí | sí | sí |
| `no_payment` | sí | sí | sí |

#### UX08-HP-control

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `ux08_hp.turn_1.model_http_attempts` | sí | sí | sí |
| `ux08_hp.turn_1.checkout_posts` | sí | sí | sí |
| `ux08_hp.turn_1.stage` | sí | sí | sí |
| `ux08_hp.turn_1.line_variant` | sí | sí | sí |
| `ux08_hp.turn_2.chat_text` | sí | sí | sí |
| `ux08_hp.turn_2.rest` | sí | sí | sí |
| `ux08_hp.turn_2.model_http_attempts` | sí | sí | sí |
| `ux08_hp.turn_2.checkout_posts` | sí | sí | sí |
| `ux08_hp.turn_2.pending_purchase` | sí | sí | sí |
| `ux08_hp.cancel_a.status_and_stage` | sí | sí | sí |
| `ux08_hp.cancel_a.merchant_cancels` | sí | sí | sí |
| `ux08_hp.cancel_a.merchant_checkout_status` | sí | sí | sí |
| `ux08_hp.turn_3.model_http_attempts` | sí | sí | sí |
| `ux08_hp.turn_3.checkout_posts` | sí | sí | sí |
| `ux08_hp.turn_3.stage` | sí | sí | sí |
| `ux08_hp.turn_3.line_variant` | sí | sí | sí |
| `ux08_hp.turn_3.new_run_and_checkout` | sí | sí | sí |
| `ux08_hp.turn_3.fingerprint_differs` | sí | sí | sí |
| `ux08_hp.turn_3.checkout_jwt` | sí | sí | sí |
| `ux08_hp.refused.approve_b_with_fa` | sí | sí | sí |
| `ux08_hp.refused.approve_a_with_fa` | sí | sí | sí |
| `ux08_hp.refused.complete_b_unapproved` | sí | sí | sí |
| `ux08_hp.b_after` | sí | sí | sí |
| `ux08_hp.merchant_checkouts` | sí | sí | sí |
| `ux08_hp.no_complete` | sí | sí | sí |
| `ux08_hp.no_payment_effects` | sí | sí | sí |
| `ux08_hp.agent_ledger` | sí | sí | sí |
| `fingerprint_reread` | sí | sí | sí |
| `approve_b` | sí | sí | sí |
| `complete_b` | sí | sí | sí |
| `order_checkout` | sí | sí | sí |
| `order_lines` | sí | sí | sí |
| `a_after` | sí | sí | sí |
| `merchant_checkouts` | sí | sí | sí |
| `complete_calls` | sí | sí | sí |
| `orders_created` | sí | sí | sí |
| `psp_tokens_created` | sí | sí | sí |
| `approvals` | sí | sí | sí |

#### UX08-HNP-control

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `proposal_digests_differ` | sí | sí | sí |
| `consent_b` | sí | sí | sí |
| `grant_scope` | sí | sí | sí |
| `trigger_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `task` | sí | sí | sí |
| `merchant_checkouts` | sí | sí | sí |
| `orders_created` | sí | sí | sí |
| `psp_tokens_created` | sí | sí | sí |

#### UX09-HNP-control

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `trigger_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `task` | sí | sí | sí |
| `merchant_checkouts` | sí | sí | sí |
| `orders_created` | sí | sí | sí |
| `psp_tokens_created` | sí | sí | sí |
| `model_requests` | sí | sí | sí |
| `checkout_posts` | sí | sí | sí |

#### UX09-HNP-rev-control

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `trigger_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `task` | sí | sí | sí |
| `merchant_checkouts` | sí | sí | sí |
| `orders_created` | sí | sí | sí |
| `psp_tokens_created` | sí | sí | sí |
| `model_requests` | sí | sí | sí |
| `checkout_posts` | sí | sí | sí |

#### UX10-HNP-control

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `trigger_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `task` | sí | sí | sí |
| `merchant_checkouts` | sí | sí | sí |
| `orders_created` | sí | sí | sí |
| `psp_tokens_created` | sí | sí | sí |

#### UX10-HNP-b

| Oráculo | r1 | r2 | r3 |
| --- | --- | --- | --- |
| `consents` | sí | sí | sí |
| `same_grant` | sí | sí | sí |
| `scope_uses` | sí | sí | sí |
| `trigger_accepted` | sí | sí | sí |
| `model_http_attempts` | sí | sí | sí |
| `task` | sí | sí | sí |
| `merchant_checkouts` | sí | sí | sí |
| `orders_created` | sí | sí | sí |
| `psp_tokens_created` | sí | sí | sí |
| `grant_state` | sí | sí | sí |

</details>

## Archivos

| Archivo | Contenido |
| --- | --- |
| `summary.json` | Corrida registrada, copiada byte a byte |
| `junit.xml` | Corrida registrada: 45 tests, 0 failures, 0 errors, 0 skipped, 105.0 s |
| `ci-junit.xml` | Celdas de CI: 46 tests, 0 failures, 0 errors, 0 skipped, 8.5 s |
| `desarrollo/corrida-1/` | Desarrollo: 1 caso, `error` (iterador descartado) |
| `desarrollo/corrida-2/` | Desarrollo: 15 casos de la repetición 1, pasan |
| `desarrollo/sin-arreglo/vivo/` | Desarrollo: stop HP sin el arreglo de §3.2, 9 casos, 3 fallan (race) |
| `desarrollo/sin-arreglo/ci/` | Cinco JUnit de las 8 pruebas de `test_assistant_transport.py` sin el arreglo |
| `desarrollo/con-arreglo/ci/` | Las mismas cinco, con el arreglo |
| `desarrollo/sin-arreglo/arreglo-revertido.diff` | La única línea revertida |
| `SHA256SUMS` | Hashes de estos archivos |

- Los JUnit con fallos conservan, en las trazas, rutas absolutas del directorio temporal. No se
  reescribieron.
- No se guardan en Git las bases SQLite del Agent, la evidencia expandida por caso
  (`exp01-*.json`), `evidence.jsonl` ni los logs. `summary.json` solo lleva conteos, códigos y
  filas proyectadas.
