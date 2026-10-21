# D1 enforce opt-in en el runtime

Estado: **opt-in experimental**. El default sigue siendo `AGENT_GOVERNANCE_MODE=off`.
Qué pasa después de un allow lo decide `AGENT_HP_FLOW`:

- `blocked` (default): el allow termina en `d1_dispatch_blocked` con la reserva
  durable y sin POST al Merchant. Es el comportamiento anterior a B2.
- `governed`: el allow crea el checkout y la compra sigue a la aprobación humana, la
  cancelación y la finalización (ver [Flujo gobernado](#flujo-gobernado)). Es la
  condición B2 del piloto (`docs/design/b2-governed-pilot.md`).

## Configuración

| Variable | Uso |
|---|---|
| AGENT_GOVERNANCE_MODE | `off` (default), `shadow` o `enforce` |
| AGENT_HP_FLOW | `blocked` (default) o `governed`; solo cambia algo con `enforce` |
| AGENT_D1_POLICY_SET_ID | Namespace de la política |
| AGENT_D1_POLICY_REVISION | Entero positivo; el registro es append-only por (set, revisión) |
| AGENT_D1_KEY_ID | Identificador de las claves |
| AGENT_D1_HMAC_KEY_HEX | Clave HMAC hexadecimal, >=32 bytes |
| AGENT_D1_AES_KEY_HEX | Clave AES-256-GCM hexadecimal de 32 bytes, distinta de la HMAC |
| AGENT_D1_ALLOWED_MERCHANT_ORIGINS | Orígenes permitidos separados por comas; excluyente con la política por archivo |
| AGENT_D1_POLICY_PATH / AGENT_D1_POLICY_DIGEST | Política revisada por archivo y su pin SHA-256 |

Con enforce, una configuración inválida impide arrancar la app. Shadow sigue
degradándose a B0 y registra `D1 shadow observation lost: <razón>` en cada compra.

## Escenario «HP inválido» (deny en vivo)

Con el Merchant local corriendo (la conversación hace discovery):

```
AGENT_GOVERNANCE_MODE=enforce
AGENT_D1_ALLOWED_MERCHANT_ORIGINS=https://otro-comercio.example
AGENT_D1_POLICY_SET_ID=demo-hp-d1
AGENT_D1_POLICY_REVISION=1
AGENT_D1_KEY_ID=demo-local
AGENT_D1_HMAC_KEY_HEX=<openssl rand -hex 32>
AGENT_D1_AES_KEY_HEX=<openssl rand -hex 32, distinta>
```

Resultado esperado: `/conversation/turn` responde 409 `d1_denied`, la evidencia
registra `ACTION_OR_MERCHANT_NOT_ALLOWED`, la compra queda en `received` y no hay
peticiones de creación al Merchant.

Para volver al allow sobre la misma base, quitar la allowlist (o incluir
`MERCHANT_BASE_URL`) y subir `AGENT_D1_POLICY_REVISION`. Si no se sube, cada
compra falla cerrada con `d1_unavailable`; el conflicto no se detecta al arrancar.

## Flujo gobernado

Con `AGENT_GOVERNANCE_MODE=enforce` y `AGENT_HP_FLOW=governed`, el bootstrap le da al gate
los puertos de `governance/runtime_dispatch.py`:

- **Creación.** Después de comprometer el allow y reclamar la operación, el gate envía un
  solo `POST /checkout_sessions` con el cliente ACP y la clave de idempotencia del sobre.
  No hay reintentos. Un timeout, un fallo de transporte (también una conexión rechazada),
  cualquier otro error HTTP (4xx o 5xx) o una respuesta inválida dejan la operación
  `uncertain` con `DISPATCH_UNCERTAIN` y la compra en `uncertain`: ninguno prueba que el
  checkout no exista. La siguiente compra del mismo `run_id` responde `d1_recovery_required`
  sin otro POST.
- **Rechazo definitivo.** El comercio responde 409 `out_of_stock`, 409 `OFFER_EXPIRED` y 404
  `invalid_item` después de comprobar que la clave no creó ningún checkout y antes de crear uno
  (`runtime_dispatch.DEFINITIVE_CREATION_REFUSALS`). Con D1 hay un solo POST por clave, así que
  esas respuestas prueban que no existe nada: la operación queda `definitive_failure` con
  `VERIFIED_NOT_CREATED` y la compra en `failed`, con el código del agente (`out_of_stock`,
  `offer_expired`, `invalid_item`) en `failure_code` y en la respuesta, como en B0. La compra no
  queda pendiente: el comprador la pide de nuevo y obtiene otro `run_id`, otra decisión D1 y otra
  clave. El mismo `run_id` sigue sin reenviarse (`d1_recovery_required`). Prueba live:
  `test_b2_merchant_refusal_fails_the_purchase_and_frees_the_buyer`. Detalle en
  `docs/design/hp-p5a-definitive-failures.md`.
- **Fallos antes del POST.** Si la revisión no se completa (contexto, evidencia o un deny que
  no se pudo registrar), la compra responde `d1_unavailable`. Si la política ya permitió y lo
  que falló fue comprometer el allow, verificarlo, validar la sesión o reclamar la operación,
  responde `d1_allow_unapplied`, cuyo texto dice que la política permitió la compra. En los
  dos casos no hubo POST. La traza guarda ese allow como bloqueado; la tarjeta del bloqueo, el paso
  de la política y la fila de actividad dicen lo mismo que el chat: la política permitió y la compra
  se detuvo por precaución.
- **Verificación.** El gate lee el checkout con un GET y exige que sea idéntico a la
  respuesta del POST. Con el Merchant real lo es (prueba live
  `test_b2_governed_purchase_creates_once_and_awaits_approval`), así que se mantiene la
  comparación exacta en vez de comparar solo campos materiales.
- **Post-creación.** Datos del comprador, relectura y, si el total supera el presupuesto,
  cancelación: cada efecto queda en el diario `d1_post_create_events` y se ejecuta una vez.
  La compra termina en `awaiting_confirmation` con la operación `confirmed`.
- **Continuación.** Desde ahí la compra sigue a la aprobación humana, la cancelación y la
  finalización (ver abajo). Prueba live con comercio y PSP reales:
  `test_b2_governed_purchase_completes_once_with_signed_receipts`.

Un deny sigue sin tocar el Merchant: `d1_denied`, compra en `received` y cero POST.
Esa compra no cuenta como pendiente, así que no impide la siguiente.

### Continuación gobernada

Con enforce, presentar los términos, aprobar, cancelar y completar responden
`d1_continuation_blocked` para cualquier compra. En el flujo gobernado,
`governance/hp_continuation.py` levanta ese bloqueo solo si se cumplen a la vez:

1. hay un allow de D1 comprometido para la compra;
2. la operación de despacho que ese allow autorizó quedó `confirmed` con el mismo
   `checkout_id` que la compra;
3. el diario de post-creación terminó en `awaiting_confirmation` con ese checkout.

Si un hecho falta, no se puede leer o no coincide, la regla rechaza. Si no se puede leer
(diario ilegible o manipulado), deja además el aviso `governed_continuation_unreadable` con
solo el tipo de error, sin `run_id` ni datos de la compra. Con
`AGENT_HP_FLOW=blocked` la regla no existe y todo sigue bloqueado, también para una
compra que se preparó antes en el flujo gobernado.

Cada paso (presentar, aprobar, cancelar, completar) evalúa la regla una sola vez. Una
evaluación lee la compra y verifica el ledger de D1, el resultado del despacho y el diario
de post-creación: unos 4 ms en local, la mitad en el diario
(`test_the_continuation_rule_costs_milliseconds`). Antes, presentar, aprobar y cancelar la
evaluaban dos veces.

El rechazo responde 409 `d1_continuation_blocked` también por REST, con el texto en español
del chat. Ese texto no promete que no hubo cobro, porque `/complete` también lo usa y puede
llegar después de un intento `uncertain`. Todos los códigos `d1_*` llegan por REST con ese
mismo texto, nunca con el mensaje interno. La aprobación, la cancelación del
comprador y la finalización usan los mismos servicios que B0 (`approval.py`,
`completion.py`) y dejan su evidencia en las tablas de B0, no en el diario de D1.

## Límites

- La guarda B0 `invalid_candidate` corre antes del gate: POL-01 nunca deniega en
  el flujo integrado. El deny demostrable es POL-02 (merchant no autorizado).
- La cancelación del comprador y la finalización no se registran en
  `d1_post_create_events`: ese diario termina en `awaiting_confirmation`.
- Una compra `uncertain` cuenta como pendiente y bloquea compras nuevas del mismo
  comprador. No hay salida automática de ese estado: la demo usa una base limpia. Un rechazo
  definitivo del comercio al crear no llega a ese estado (ver «Rechazo definitivo»).
