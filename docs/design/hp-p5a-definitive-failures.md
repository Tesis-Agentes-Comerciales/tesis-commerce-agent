# HP: rechazos definitivos al crear y checkouts vencidos (P5a)

Estado: propuesta · 21-oct-2026 · ola «HP al 100 %», implementador P5a. Rama
`fix/hp-definitive-failures`, un PR.

## Problema

Tres seguimientos de corrección del flujo Human Present gobernado (`AGENT_HP_FLOW=governed`):

1. **Un rechazo definitivo del comercio al crear bloquea al comprador.** El gate D1 envía un solo
   `POST /checkout_sessions`. Si el comercio responde 409 `out_of_stock` o 409 `OFFER_EXPIRED`, el
   gate lo trata como cualquier error: operación `uncertain` (`DISPATCH_UNCERTAIN`) y compra
   `uncertain`. Esa compra cuenta como pendiente y cada pedido nuevo responde «Ya hay una compra
   pendiente», aunque el comercio no creó nada.
2. **Un checkout vencido rompe la compra pendiente.** Con la compra en `awaiting_confirmation` y el
   checkout vencido, `GET /me/pending-purchase` responde 409 (`checkout_expired` y
   `checkout_not_ready` no están en `_STALE_TERMS`) y la app dice «No pude recuperar tus compras»
   en vez de la tarjeta de condiciones con «Cancelar compra».
3. **El estado `expired` del comercio se informa como «no listo».** `require_approvable` revisa el
   estado antes que `expires_at`. Cuando el comercio ya marcó el checkout `status: "expired"` (lo
   hace al leerlo después de su vencimiento), el agente responde `checkout_not_ready`, no
   `checkout_expired`.

## Qué prueba que no se creó nada

En el comercio (`tesis-commerce-merchant`, `acp.py::create_in_transaction` y
`checkout.py::prepare_in_transaction`), la creación toma un candado por clave de idempotencia,
busca primero un checkout ya creado con esa clave (y lo devuelve como réplica) y solo después
valida la oferta. Estos rechazos salen de esa validación, antes de insertar nada:

| HTTP | `detail.code` | Causa en el comercio | Código del agente |
| --- | --- | --- | --- |
| 409 | `out_of_stock` | `OUT_OF_STOCK` (sin stock suficiente) | `out_of_stock` |
| 409 | `OFFER_EXPIRED` | `OFFER_EXPIRED` (la oferta venció) | `offer_expired` |
| 404 | `invalid_item` | `OFFER_NOT_FOUND` (la variante no existe en el episodio) | `invalid_item` |

Las pruebas del comercio fijan estos pares (`tests/test_api.py`, `tests/integration/test_acp_http.py`,
que además comprueba que la observación de esos POST no tiene `checkout_id`). Como D1 envía como
máximo un POST por clave, una respuesta con uno de esos pares prueba que esa clave no creó nada.
También se acepta 409 `offer_expired` en minúsculas, la forma que el agente ya usa para ese
código.

Siguen `uncertain`, porque no prueban ausencia: 409 `idempotency_in_flight` (otra petición con la
clave sigue en curso), 409 `creation_unrecoverable` (existe un checkout cuya respuesta no se puede
reconstruir), 422 `idempotency_conflict`, 400 `INVALID_INPUT`, 403, cualquier 5xx, un timeout, un
fallo de transporte, un 404 sin `invalid_item` (por ejemplo, de un proxy) y una respuesta inválida.
`OFFER_REVISION_MISMATCH` no puede salir del POST ACP (el comercio usa la revisión vigente), así
que no entra en la tabla.

## Alcance

**Dentro**

- D1: los pares de la tabla dejan la operación en `definitive_failure` con `VERIFIED_NOT_CREATED`
  (el estado y el código que la tabla `d1_checkout_results` y la reconciliación ya admiten; no
  cambia el esquema) y la compra en `failed` con `failure_code` igual al código del agente.
  `updated_at` toma la hora del rechazo, que es la del paso `failed` en la traza.
- La compra fallida no es pendiente: el comprador puede pedir de nuevo de inmediato; el pedido
  nuevo es otro `run_id`, con su propia decisión D1 y su propia clave.
- El error que recibe el chat y REST es el código del agente con el texto en español que ya existe
  (`El producto se agotó en el comercio. Elige otro.`, `La oferta del comercio venció…`,
  `El producto ya no está disponible en el comercio.`), igual que B0 para el mismo rechazo.
- `GET /me/pending-purchase`: `terms_status` admite `expired`. Mapeo de `_STALE_TERMS`:

  | Código al presentar los términos | `terms_status` |
  | --- | --- |
  | `checkout_terms_changed` | `changed` |
  | `offer_expired` | `expired` (antes `changed`) |
  | `checkout_expired` | `expired` (nuevo) |
  | `checkout_not_ready` | `changed` (nuevo) |
  | `offer_freshness_unavailable` | `unavailable` |

  `offer_expired` pasa a `expired` para que, al recargar, la tarjeta diga lo mismo que en la sesión
  que recibió el error («Las condiciones que revisaste vencieron»). `checkout_not_ready` queda en
  `changed`: el checkout dejó de estar listo para pagar (por ejemplo, el comercio lo canceló), así
  que las condiciones que revisó el comprador ya no son las del comercio.
- `require_approvable`: `status == "expired"` responde `checkout_expired`. El orden queda: vencido
  por estado, no listo, vencido por `expires_at`.
- UI: `PendingTermsStatus` suma `expired` y `StaleTermsReason` pasa a ser el mismo tipo. La parte
  restaurada de una compra vencida usa el código `checkout_expired`. `FAILURE_COPY` suma
  `offer_expired` e `invalid_item` para el paso `failed` de la traza.

**Fuera**

- Post-creación (detalles del comprador, relectura, cancelación por presupuesto): ahí el checkout
  ya existe, así que un rechazo del comercio no prueba que no haya nada que cancelar. Sigue
  `d1_post_create_uncertain`.
- El despacho inicial experimental (`acp/initial_dispatch.py`) sigue sin promover ningún rechazo
  HTTP.
- Salir de una compra `uncertain` ya existente: sigue requiriendo revisión.
- Cancelar un checkout que el comercio ya canceló (responde 405): la tarjeta muestra el error de
  cancelar, como hoy en cualquier otro fallo de cancelación.

## Contrato

- `governance/enforce.py`: `CheckoutCreationRefused(code)`, con `code` en
  `out_of_stock | offer_expired | invalid_item`. Un puerto de despacho la lanza solo para un rechazo
  que prueba que su único POST no creó nada. El gate la honra solo si salió del POST (antes de
  validar la respuesta); si llegara después, la operación queda `uncertain`. Si no puede registrar
  el rechazo, responde `d1_dispatch_uncertain`, que es lo que dice el disco.
- `governance/runtime_dispatch.py`: `DEFINITIVE_CREATION_REFUSALS`, la tabla de arriba, por
  `(status_code, code)` exactos del `ACPClientError`.
- `persistence/d1_dispatch_sqlite.py`: `observe` admite `definitive_failure` con
  `VERIFIED_NOT_CREATED`.
- `api.py`: `TermsStatus = changed | expired | unavailable`. `error_response` da el texto en
  español a `out_of_stock` e `invalid_item` cuando llegan como `PurchaseServiceError` (el rechazo
  gobernado), como ya hace con `offer_expired`; responde 409.
- `approval.py`: orden nuevo de `require_approvable`.
- UI: `ui/src/lib/commerce-api.ts` (`PendingTermsStatus`), `stale-terms.ts`, `stale-terms-card.tsx`,
  `trace-copy.ts`. La traza no cambia de contrato: `failed` ya lleva `code` libre.

## Componentes y estados

- `StaleTermsCard` ya tiene la variante `expired`; ahora también llega al recargar.
- Historias: `App.stories.tsx` suma `RestoredTermsExpired` (compra restaurada con
  `terms_status: "expired"`, se cancela). `control-step.stories.tsx` suma
  `FailedCreationRefused`, el paso `failed` con `offer_expired` traducido.
- E2E (`ui/e2e/chat.spec.ts`): los recorridos de «términos que ya no valen» suman `expired`:
  aprobar responde 409 `checkout_expired` y la compra restaurada con `terms_status: "expired"`;
  en los dos la compra se cancela.

## Impacto en B0

`require_approvable` es de `ApprovalService`, compartido por B0 y B2 al presentar, aprobar y
completar. Con un checkout que el comercio ya marcó `expired`, B0 responde ahora
`checkout_expired` en vez de `checkout_not_ready`, con el mismo HTTP 409. En el chat, antes de
aprobar, eso muestra la tarjeta «Las condiciones que revisaste vencieron» con «Cancelar compra» en
lugar del aviso «ya no está listo para pagar»; después de aprobar, solo cambia el texto del aviso.
Ningún escenario del piloto v1 vence un checkout (S01–S05 aprueban dentro de la vigencia) y ni el
runner ni el scorer leen esos códigos, así que los resultados de `b2-pilot-v1` no cambian. Un
piloto nuevo que compare B0 con B2 debe tratar `checkout_expired` y `checkout_not_ready` como el
mismo desenlace si mezcla corridas de antes y después de este cambio.

El rechazo definitivo en D1 no toca B0: B0 ya marca la compra `failed` con el código del comercio
(`_abandon_on_error`). Con este cambio, B2 queda igual que B0 para el mismo rechazo.

## Criterios de aceptación

1. Con el flujo gobernado y el cliente ACP real ante un comercio simulado, 409 `out_of_stock`,
   409 `OFFER_EXPIRED`, 409 `offer_expired` y 404 `invalid_item` al crear dejan la operación
   `definitive_failure`/`VERIFIED_NOT_CREATED`, la compra `failed` con su código, un solo POST,
   ninguna compra pendiente, y la compra siguiente crea su checkout.
2. 409 `idempotency_in_flight`, 409 `creation_unrecoverable`, 422 `idempotency_conflict` y un 404
   sin código siguen `uncertain`, con un solo POST.
3. La reconciliación local de una operación rechazada informa `definitive_failure`, sin
   `LOCAL_HISTORY_UNVERIFIABLE` y sin habilitar reenvío.
4. `GET /me/pending-purchase` con el checkout vencido (por estado o por `expires_at`) responde 200
   con `terms_status: "expired"`, y la compra se cancela; con el checkout no listo responde
   `changed`.
5. `require_approvable` responde `checkout_expired` para `status: "expired"`.
6. La UI restaura una compra con `terms_status: "expired"` con la tarjeta «Las condiciones que
   revisaste vencieron» y la cancela (historia y E2E).
7. Capa 4: ante el comercio real sin stock, la compra gobernada termina `failed` con
   `out_of_stock` y un solo POST, y otra compra del mismo comprador crea su checkout.

## Plan

Un PR, `fix/hp-definitive-failures`, con commits separados: spec; D1 (pruebas, código, prueba
viva y doc del flujo); compra pendiente vencida y `require_approvable` (backend, UI, historias,
E2E y docs).

## Riesgos

- Que el comercio cambie el orden de su validación (validar antes de buscar la réplica) haría
  falsa la prueba de ausencia. La prueba viva de la capa 4 lo detecta cada noche.
- Un código nuevo del comercio no entra solo: queda `uncertain` hasta agregarlo a la tabla, que es
  el lado seguro.
