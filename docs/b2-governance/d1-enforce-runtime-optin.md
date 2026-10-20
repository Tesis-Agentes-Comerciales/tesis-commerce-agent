# D1 enforce opt-in en el runtime

Estado: **opt-in experimental**. El default sigue siendo `AGENT_GOVERNANCE_MODE=off`.
Qué pasa después de un allow lo decide `AGENT_HP_FLOW`:

- `blocked` (default): el allow termina en `d1_dispatch_blocked` con la reserva
  durable y sin POST al Merchant. Es el comportamiento anterior a B2.
- `governed`: el allow crea el checkout y la compra sigue hasta la aprobación humana
  (ver [Flujo gobernado](#flujo-gobernado)). Es la condición B2 del piloto
  (`docs/design/b2-governed-pilot.md`).

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
  No hay reintentos. Un timeout, un error HTTP o una respuesta inválida dejan la operación
  `uncertain` y la compra en `uncertain`; la siguiente compra del mismo `run_id` responde
  `d1_recovery_required` sin otro POST.
- **Verificación.** El gate lee el checkout con un GET y exige que sea idéntico a la
  respuesta del POST. Con el Merchant real lo es (prueba live
  `test_b2_governed_purchase_creates_once_and_awaits_approval`), así que se mantiene la
  comparación exacta en vez de comparar solo campos materiales.
- **Post-creación.** Datos del comprador, relectura y, si el total supera el presupuesto,
  cancelación: cada efecto queda en el diario `d1_post_create_events` y se ejecuta una vez.
  La compra termina en `awaiting_confirmation` con la operación `confirmed`.

Un deny sigue sin tocar el Merchant: `d1_denied`, compra en `received` y cero POST.

## Límites

- La guarda B0 `invalid_candidate` corre antes del gate: POL-01 nunca deniega en
  el flujo integrado. El deny demostrable es POL-02 (merchant no autorizado).
- Presentar, aprobar, cancelar y completar una compra con D1 siguen respondiendo
  `d1_continuation_blocked`, también en el flujo gobernado. La continuación gobernada
  es el PR siguiente del plan (`docs/design/b2-governed-pilot.md`).
- Una compra `uncertain` cuenta como pendiente y bloquea compras nuevas del mismo
  comprador. No hay salida automática de ese estado: la demo usa una base limpia.
