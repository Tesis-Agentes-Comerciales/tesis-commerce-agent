# D1 enforce opt-in en el runtime

Estado: **opt-in experimental**. El default sigue siendo `AGENT_GOVERNANCE_MODE=off`.
Enforce solo cubre el deny: no existe adaptador de despacho de producción, así que
un allow termina en `d1_dispatch_blocked` con la reserva durable y sin POST al
Merchant. Allow → checkout real exige el puerto D17 (P/Q, registro, Merchant) y
queda fuera de este alcance.

## Configuración

| Variable | Uso |
|---|---|
| AGENT_GOVERNANCE_MODE | `off` (default), `shadow` o `enforce` |
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

## Límites

- La guarda B0 `invalid_candidate` corre antes del gate: POL-01 nunca deniega en
  el flujo integrado. El deny demostrable es POL-02 (merchant no autorizado).
