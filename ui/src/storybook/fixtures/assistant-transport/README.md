# Fixtures de Assistant Transport

Generados por `uv run python scripts/export_assistant_fixtures.py`; no se editan a mano.
`tests/test_assistant_transport.py` verifica que coincidan con la salida actual.

Cada archivo `.sse` es el cuerpo real de `POST /assistant` con router y merchant
simulados, feed sintético (`tests/fixtures/assistant_feed`), ids deterministas y sin
líneas de heartbeat. Todos los datos son sintéticos. El primer frame siempre reemplaza
el estado completo (`set` en la raíz), así que un fixture que continúa otro ya incluye el
estado previo del que parte.

Cada request lleva además `details` (perfil sintético de Ana Pérez en Bogotá),
`turn_id = turn-<nombre>` y las claves que agrega el runtime (`system`, `tools`,
`threadId`, `callSettings`, `config`), que el backend ignora.

## `greeting.sse`

Saludo: `interpret_request` decide `respond` y el asistente contesta sin buscar.

- Estado enviado: `{messages: [], purchase: null}`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "hola"
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `clarify.sse`

Aclaración: `interpret_request` decide `clarify` y el asistente pide más detalles.

- Estado enviado: el estado final de `greeting.sse`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "Busco algo para escuchar música"
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `purchase.sse`

Compra preparada: razonamiento, los cinco pasos, tres productos con imagen y descripción, términos de aprobación y `purchase` en `awaiting_confirmation`.

- Estado enviado: `{messages: [], purchase: null}`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "Quiero audífonos negros por menos de 2 millones"
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `pending-purchase.sse`

Compra pendiente verificada en SQLite: el asistente pide revisarla o cancelarla sin llamar al modelo.

- Estado enviado: el estado final de `purchase.sse`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "También quiero unos audífonos blancos"
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `purchase-completed.sse`

Seguimiento tras aprobar y completar por REST: `purchase_update` con la orden y `purchase.stage = completed`.

- Estado enviado: el estado final de `purchase.sse`.
- Comandos:

```json
[
  {
    "type": "purchase-updated",
    "runId": "run_52df74bfc304253c74087d9927446bb3"
  }
]
```

## `purchase-canceled.sse`

Seguimiento tras cancelar por REST: `purchase_update` y `purchase.stage = canceled`.

- Estado enviado: el estado final de `purchase.sse`.
- Comandos:

```json
[
  {
    "type": "purchase-updated",
    "runId": "run_52df74bfc304253c74087d9927446bb3"
  }
]
```

## `purchase-repeated.sse`

La misma compra pedida de nuevo después de cancelar (S04, términos que cambiaron): un checkout nuevo con otro `run_id`, términos nuevos y `purchase` en `awaiting_confirmation`.

- Estado enviado: el estado final de `purchase-canceled.sse`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "Quiero audífonos negros por menos de 2 millones"
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `no-candidate.sse`

Sin candidatos: `search_catalog` devuelve `products: []` y el texto final es la recuperación conversacional.

- Estado enviado: `{messages: [], purchase: null}`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "Quiero audífonos por menos de 50 mil"
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `delegate.sse`

Compra delegada en el chat (`mode: "delegate"`, HNP encendido): `interpret_request` y `search_catalog`, sin elegir producto ni crear checkout. El estado termina con `purchase: null` y `delegation_draft` con las opciones, el presupuesto del mensaje y el mensaje como preferencia.

- Estado enviado: `{messages: [], purchase: null}`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "Quiero audífonos negros por menos de 2 millones"
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `purchase-blocked.sse`

Bloqueo de la política de compra en modo activo: la lista de comercios no incluye la tienda, `prepare_checkout` queda con `isError` y el código `d1_denied`, y el mensaje termina en `incomplete`/`error`. Los `args` del paso traen el `run_id` de la compra.

- Estado enviado: `{messages: [], purchase: null}`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "Quiero audífonos negros por menos de 2 millones"
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `error.sse`

Error saneado: el comercio no responde durante `prepare_checkout`; el paso queda con `isError` y el mensaje con estado `incomplete`/`error`.

- Estado enviado: `{messages: [], purchase: null}`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "Quiero audífonos negros por menos de 2 millones"
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `conditional-first.sse`

Intención condicional, elegida la primera opción: `select_product` lee solo la oferta de A, no consulta al modelo y trae `conditional` con A `eligible` y B `not_evaluated`; se prepara un solo checkout, de A, en `awaiting_confirmation`.

- Estado enviado: `{messages: [], purchase: null}`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "Compra los Sony WH-1000XM6 negros si el total con envío es de hasta $2.000.000; si no, los Bose QuietComfort Ultra negros si es de hasta $1.900.000."
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `conditional-second.sse`

Intención condicional, elegida la segunda opción: A queda `over_limit` por su total con envío, B `eligible`, y el único checkout es de B.

- Estado enviado: `{messages: [], purchase: null}`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "Compra los Sony WH-1000XM6 negros si el total con envío es de hasta $1.900.000; si no, los Bose QuietComfort Ultra negros si es de hasta $1.850.000."
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `conditional-none.sse`

Ninguna opción cumple: A y B quedan `over_limit`, la acción es `respond`, el texto nombra las dos causas y no menciona a C, y no se crea ningún checkout.

- Estado enviado: `{messages: [], purchase: null}`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "Compra los Sony WH-1000XM6 negros si el total con envío es de hasta $1.900.000; si no, los Bose QuietComfort Ultra negros si es de hasta $1.800.000."
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `conditional-undetermined.sse`

La lectura de la oferta de A falla: A queda `unknown` y B `not_evaluated`, no se pasa a B y no se crea ningún checkout.

- Estado enviado: `{messages: [], purchase: null}`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "Compra los Sony WH-1000XM6 negros si el total con envío es de hasta $1.950.000; si no, los Bose QuietComfort Ultra negros si es de hasta $1.900.000."
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `conditional-offer-changed.sse`

El checkout de A trae otro total que su oferta: `select_product` eligió A, `prepare_checkout` queda con `isError` y el código `offer_changed`, el checkout se cancela y no se prueba B.

- Estado enviado: `{messages: [], purchase: null}`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "Compra los Sony WH-1000XM6 negros si el total con envío es de hasta $1.980.000; si no, los Bose QuietComfort Ultra negros si es de hasta $1.900.000."
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `conditional-clarify.sse`

La primera opción coincide con dos variantes (negro y plata): `select_product` decide `clarify` sin `conditional`, la acción es `clarify` y no se lee ninguna oferta.

- Estado enviado: `{messages: [], purchase: null}`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "Compra unos Sony WH-1000XM6 si el total con envío es de hasta $2.000.000; si no, los Bose QuietComfort Ultra negros si es de hasta $1.900.000."
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```

## `conditional-exclusions.sse`

«Nunca compres los Bose»: sin ramas no se lee ninguna oferta para decidir; `search_catalog` informa `rejections.excluded = 1`, los Bose no están en la lista corta y la selección normal prepara el checkout de los Sony.

- Estado enviado: `{messages: [], purchase: null}`.
- Comandos:

```json
[
  {
    "type": "add-message",
    "message": {
      "role": "user",
      "parts": [
        {
          "type": "text",
          "text": "Quiero audífonos negros por menos de 2 millones. Prefiero los Sony, pero nunca compres los Bose QuietComfort Ultra."
        }
      ]
    },
    "parentId": null,
    "sourceId": null
  }
]
```
