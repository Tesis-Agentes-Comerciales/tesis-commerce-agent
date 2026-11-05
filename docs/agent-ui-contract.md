# Contrato UI del agente (Assistant Transport)

La UI conversa con el agente mediante el runtime `useAssistantTransportRuntime` de
`@assistant-ui/react` y el paquete Python `assistant-stream`. El backend es dueño del estado: cada
respuesta es un stream SSE de operaciones `update-state` que reconstruyen el objeto descrito aquí.
El runtime del frontend solo lee ese estado; ignora partes de mensaje sueltas (`append_text`,
`add_tool_call`), así que el backend escribe los mensajes dentro de `controller.state`.

`/conversation/turn` conserva su respuesta y acepta un `conversation_id` opcional. Los endpoints
REST de compra conservan su contrato; completar o cancelar reanuda el grafo suspendido.

## Endpoint

`POST /assistant` → `AssistantTransportResponse(create_run(callback, state=...))`. En el frontend,
`protocol: "assistant-transport"`. Mismo CORS que el resto de la API.

Cuerpo que envía el runtime (claves desconocidas en el nivel superior se ignoran, porque el runtime
esparce `callSettings` y `config` ahí):

| Campo | Uso en el backend |
| --- | --- |
| `commands` | Validado estrictamente; ver comandos. |
| `state` | No confiable. Aporta mensajes para mostrar y el `run_id` a verificar; con `conversation_id`, el historial del modelo se lee del checkpoint. |
| `details` | Datos de entrega, mismo esquema `CheckoutDetails` de `/conversation/turn`. |
| `turn_id` | UUID por envío generado en el frontend; alimenta la idempotencia existente. |
| `conversation_id` | UUID estable durante esta conversación; selecciona el thread de LangGraph. Una nueva compra crea otro ID. |
| `mode` | `"present"` (por defecto, si falta) o `"delegate"`. Con `"delegate"` el turno busca y prepara una delegación en vez de un checkout; ver «Modo `delegate`». La UI lo envía solo con «Delegar». |
| `system`, `tools`, `callSettings`, `config`, `threadId`, `parentId` | Se ignoran. Nunca llegan al modelo. |

Reglas de confianza:

- Todo dato de compra (candidato, checkout, términos, total, etapa, orden) se relee de SQLite o del
  feed validado por `run_id`/`variant_id`. Nada de `state` se usa como dato autoritativo.
- En particular, la etapa (`purchase`) y el `result` de cada `request_approval` que el cliente
  devuelve se reescriben con lo que hay en SQLite antes de reenviarlos: el total que muestra la
  tarjeta es el que el servidor presentó, no el que llegó en el cuerpo.
- Con `conversation_id`, LangGraph guarda el historial para el modelo (máximo 20 mensajes) en
  checkpoints SQLite. En turnos posteriores solo se toma el último mensaje del usuario; el
  historial que el cliente devuelve no reemplaza el checkpoint. Sin `conversation_id`, se mantiene
  el historial textual anterior por compatibilidad. Tool calls, razonamiento y resultados del
  cliente no se envían al modelo.
- Límite de tamaño del cuerpo y de mensajes; si se excede, error saneado.
- Ninguna excepción cruda llega a `create_run` (que emitiría `str(error)` al cliente): se capturan y
  se traducen a mensajes en español sin secretos.

## Comandos

- `add-message` (del runtime): `{"type": "add-message", "message": {"role": "user", "parts":
  [{"type": "text", "text": "..."}]}, "parentId": null, "sourceId": null}`. No trae id: el backend
  asigna `id` a cada mensaje que agrega.
- `purchase-updated` (propio): `{"type": "purchase-updated", "runId": "run_..."}`. La UI lo envía
  después de aprobar y completar, o cancelar, por REST. El backend verifica que el `run_id` exista en
  SQLite, lee la etapa y agrega un mensaje del asistente con el tool `purchase_update` y un texto
  determinista (sin modelo).
- `add-tool-result`: no se usa en esta versión; se ignora.

La aprobación sigue exclusivamente en `POST /purchases/{run_id}/approval` con el fingerprint y
`POST /purchases/{run_id}/complete`. El grafo se pausa después de presentar los términos; completar
reanuda el nodo de aprobación, verifica la etapa autoritativa en SQLite y entonces llama al gateway
determinista. Cancelar también cierra esa pausa. Ningún comando transporta evidencia de aprobación.

Si hay una compra pendiente, un `add-message` responde con un texto que pide revisarla o cancelarla,
sin llamar al modelo. La compra pendiente se busca en SQLite (`SELECT … WHERE stage IN …`), no por el
`run_id` que declare `state`: recargar la página o mandar varios `add-message` en una sola petición
no abre un segundo checkout. Cuentan como pendientes `selected`, `checkout_prepared`,
`awaiting_confirmation`, `approved`, `completing` y `uncertain`; un turno que falla a mitad de camino
deja la compra en `failed`, así que no bloquea la siguiente.
`/conversation/turn` aplica la misma guarda antes de ejecutar el grafo, incluso si el cliente usa
otro `conversation_id`. Una respuesta bloqueada no se guarda como resultado del turno: después de
cancelar o completar la compra, ese envío puede reintentarse.

## Estado

```json
{
  "messages": [
    {"id": "msg_…", "role": "user", "content": [{"type": "text", "text": "…"}]},
    {
      "id": "msg_…",
      "role": "assistant",
      "status": {"type": "running"},
      "content": [
        {"type": "reasoning", "text": "…"},
        {
          "type": "tool-call",
          "toolCallId": "call_…",
          "toolName": "search_catalog",
          "args": {},
          "argsText": "{}",
          "result": {},
          "isError": false
        },
        {"type": "text", "text": "…"}
      ]
    }
  ],
  "purchase": {"run_id": "run_…", "stage": "awaiting_confirmation", "order_id": null}
}
```

- Los mensajes son compatibles con `ThreadMessageLike`.
- `status` del asistente: `{"type": "running"}` mientras se genera; `{"type": "complete", "reason":
  "stop"}` al terminar; `{"type": "incomplete", "reason": "error", "error": "<mensaje saneado>"}`;
  `{"type": "incomplete", "reason": "cancelled"}` si se cancela.
- Texto y razonamiento se transmiten con operaciones `append-text` sobre la parte correspondiente.
  `result` se asigna al terminar cada paso; mientras falte, la UI muestra el paso en curso.
- `purchase` es `null` hasta que exista un checkout; refleja la etapa leída de SQLite. Si `state` no
  reclama ningún `run_id` válido pero hay una compra pendiente, el backend informa esa.
- `delegation_draft` solo aparece después de un turno `delegate` (ver abajo). El primer frame de cada
  respuesta reconstruye la raíz con `messages` y `purchase`, así que un borrador que el cliente
  devuelva en `state` nunca se reenvía.

## Modo `delegate`

La compra delegada dentro del chat (`docs/design/hnp-chat-delegate.md`). Con `mode: "delegate"`:

- Si el backend no expone HNP (`/health` sin `hnp_enabled`), responde 422 `{"code":
  "delegation_disabled", "message": "La compra delegada no está habilitada en este entorno."}` sin
  abrir el stream ni llamar al modelo.
- El grafo corre `interpret_request` y, si la acción es `purchase`, `search_catalog`, y termina ahí:
  no hay `select_product`, `prepare_checkout` ni `request_approval`, no se crea checkout ni compra
  y no se llama al comercio. `purchase` queda en `null`.
- El texto final es fijo: cuántas opciones encontró y que abajo se delega, o, sin opciones, que
  ajuste la búsqueda (sin la recuperación del modelo).
- El estado agrega:

```json
"delegation_draft": {
  "offers": [ProductCard],
  "budget_minor": 200000000,
  "currency": "COP",
  "preference_text": "Quiero audífonos negros por menos de 2 millones"
}
```

- `offers`: la lista corta de `search_catalog` (máx. 6), solo disponibles y en COP. `budget_minor`:
  `max_total_amount_minor` de la intención o, si falta, `max_unit_amount_minor`; `null` sin
  presupuesto. `preference_text`: el último mensaje del comprador. Si la acción no es `purchase`,
  `delegation_draft` es `null`.
- `priority` (opcional, `docs/design/p0-conditional-intent.md` §3.9): el orden que el turno leyó
  entre `offers`, de 1 a 3 variantes distintas. Se omite si no hay orden; nunca es `null`. El
  formulario lo propone y el comprador lo consiente en la tarjeta.
- Nada de esto es autoritativo: la UI lo usa para prellenar el formulario y `POST
  /delegations/proposals` revalida variantes, total y destino. La idempotencia es la de siempre por
  comprador (`turn_id`); la huella del turno incluye el modo.

## Pasos como tool calls

Son nodos que orquesta LangGraph y que el backend representa como tool calls en la UI. Son distintos
de `lookup_catalog`, la única herramienta que puede invocar el modelo: consulta la instantánea
validada del feed de solo lectura y no puede preparar checkout ni ejecutar pagos.

| `toolName` | Cuándo | `args` | `result` |
| --- | --- | --- | --- |
| `interpret_request` | Cada `add-message` | `{}` | `{"action": "respond" \| "clarify" \| "purchase", "constraints": HardConstraints \| null, "preferences": SoftPreferences \| null}` |
| `search_catalog` | `action == "purchase"` | `{"constraints", "preferences"}` | `{"matches": int, "rejections": {motivo: int}, "products": [ProductCard] (máx. 6)}` |
| `select_product` | Hay candidatos | `{"candidate_variant_ids": [str]}` | `{"decision": "select" \| "clarify", "product": ProductCard \| null, "rationale": str, "conditional"?: ConditionalView}` |
| `prepare_checkout` | Producto elegido | `{"variant_id": str, "run_id": str}` | `{"run_id", "checkout_id", "checkout_status", "price_changed": bool, "total_minor": int, "currency": str}` |
| `request_approval` | Checkout listo | `{"run_id": str}` | `{"run_id", "stage", "terms": ApprovalTerms}` (igual a `GET /purchases/{run_id}/approval`) |
| `purchase_update` | Comando `purchase-updated` | `{"run_id": str}` | `{"run_id", "stage", "order_id": str \| null}` |

`HardConstraints` y `SoftPreferences` son el JSON de `PurchaseIntent`. `rejections` son los conteos
de `rank_candidates`. Si no hay candidatos, `search_catalog` devuelve `products: []` y el texto final
es la recuperación conversacional actual.

El razonamiento aparece como partes `reasoning` solo si `OPENAI_REASONING_SUMMARY=true` y el modelo
lo soporta; son resúmenes de la Responses API, nunca razonamiento crudo.

### ConditionalView

Solo en un turno con intención condicional cuyas ramas se evaluaron con la oferta del comercio
(`docs/design/p0-conditional-intent.md` §3.3 y §3.10); en los demás, `select_product` no trae la
clave. El modelo nunca elige la rama: la decide el backend. Montos en centavos COP.

```json
{
  "outcome": "branch",
  "chosen_index": 1,
  "currency": "COP",
  "branches": [
    {"index": 0, "variant_id": "SON-X5II-256-BLK", "title": "Sony Xperia 5 II 256 GB negro",
     "status": "over_limit", "total_minor": 292400000, "limit_minor": 292399999,
     "items_total_minor": 289900000, "unit_limit_minor": null},
    {"index": 1, "variant_id": "ONP-8T-256-GRN", "title": "OnePlus 8T 256 GB verde",
     "status": "eligible", "total_minor": 202400000, "limit_minor": 202400000,
     "items_total_minor": 199900000, "unit_limit_minor": null}
  ],
  "checkout_total_minor": null
}
```

- `outcome`: `branch`, `none`, `undetermined` u `offer_changed` (el checkout preparado no tiene el
  total de la oferta: se canceló y no se probó otra rama). `clarify` y `exclusions_only` no traen
  vista: la aclaración es una pregunta y la exclusión se ve en `rejections.excluded` de
  `search_catalog`.
- `chosen_index`: solo con `branch` u `offer_changed`, y siempre una rama `eligible`.
- `status` de cada rama: `eligible`, `over_limit`, `out_of_stock`, `inactive`, `intent_mismatch`
  (contradice otra condición del pedido; su oferta no se lee), `unknown` (la lectura falló o el feed
  y la oferta no coinciden), `not_triggered` («solo si se agota» no se cumplió) o `not_evaluated`.
- `total_minor` e `items_total_minor` (con y sin envío) salen de la misma oferta, o son `null` si no
  se leyó. `limit_minor` es el límite de total efectivo de la rama; `unit_limit_minor`, el límite por
  unidad del pedido. Un `over_limit` con `total_minor` ≤ `limit_minor` es por precio unitario.
- `checkout_total_minor`: solo con `offer_changed`.
- La UI valida con `parseConditionalView` (`ui/src/lib/agent-contract.ts`) y el backend con
  `ConditionalView` (`application/conditional.py`), sobre las mismas fixtures
  (`ui/src/storybook/fixtures/conditional-views.json`).

### ProductCard

```json
{
  "product_id": "prod-son-01",
  "variant_id": "SON-01",
  "title": "Audífonos Sonora SON-01",
  "variant_title": "Sonora negro",
  "description": "Synthetic Sonora headphones, new.",
  "image": {"url": "https://merchant.example.test/assets/products/prod-son-01.jpg", "alt": "…"},
  "observed_unit_amount_minor": 72000,
  "currency": "USD",
  "available": true,
  "options": [{"name": "Color", "value": "black"}],
  "product_url": "https://merchant.example.test/products/prod-son-01"
}
```

- El feed no es confiable. `image` solo se incluye si la URL es `https`, sin credenciales, de hasta
  2048 caracteres y con el mismo host que la URL del producto; si no, `null`. `description` es texto
  plano, sin caracteres de control, colapsado y truncado a 500 caracteres; `null` si falta.
- `observed_unit_amount_minor` es el precio observado del catálogo, no un término autoritativo. La UI
  lo rotula como precio de catálogo; el total autoritativo es `terms.total_minor` de
  `request_approval`.
- Los datos de presentación (imagen, descripción) no se agregan a los prompts del modelo.

## Frontend

- Runtime: `useAssistantTransportRuntime({ api: AGENT_API_URL + "/assistant", protocol:
  "assistant-transport", initialState: {messages: [], purchase: null}, converter, body })`, con
  `body` que agrega `details`, un `turn_id` nuevo por envío y `mode: "delegate"` cuando el selector
  `purchaseMode` de la cabecera está en «Delegar» (se lee por referencia al enviar).
- Con `delegation_draft`, el hilo muestra al pie de los mensajes la compra delegada
  (`features/delegation-in-chat.tsx`): `DelegationForm` prellenado (opciones marcadas, total con el
  presupuesto, el mensaje como indicación), `DelegationConsentCard` y, tras autorizar,
  `DelegationStatusCard` con la gobernanza, releída cada 2 s mientras la tarea sigue en curso. La
  lista y la revocación están en el diálogo «Mis delegaciones».
- `converter`: convierte `state.messages` a mensajes del thread, agrega los `add-message` pendientes
  como mensajes optimistas y usa `isSending` como `isRunning`.
- Cada `toolName` se registra con un renderer `type: "backend"`. `select_product` y
  `request_approval` se muestran de forma destacada; los demás pasos y el razonamiento se agrupan en un
  bloque plegable "Pensando…".
- La tarjeta de aprobación llama a los endpoints REST existentes y después envía
  `purchase-updated`. Si ese comando se pierde porque el turno se canceló o falló, se reintenta una
  vez.
- La explicación de la elección vive solo en el texto final del turno; la tarjeta del producto no la
  repite.
- El compositor limita el mensaje a los mismos 2000 caracteres que acepta el backend.

## Fixtures

`scripts/export_assistant_fixtures.py` genera streams SSE reales del backend (router y merchant
simulados, ids deterministas) en `ui/src/storybook/fixtures/assistant-transport/`. Un test verifica
que los archivos versionados coincidan con la salida actual. Storybook los reproduce para simular el
backend.

## Precisiones de la implementación

Detalles que el contrato no fijaba y que el backend implementa así (`src/commerce_agent/assistant_transport.py`):

- El primer frame de cada respuesta es un `set` en la raíz con el estado saneado: mensajes
  mostrables del cliente (solo partes `text`, `reasoning` y `tool-call`; un asistente que quedó
  `running` pasa a `incomplete`/`cancelled`) y `purchase` releído de SQLite. Así las rutas de las
  operaciones siguientes siempre coinciden con el estado del cliente.
- En ese saneo, cada `request_approval` con `result` vuelve a leerse: la etapa sale de SQLite y los
  términos, de la instantánea que guardó el paso (tabla `approval_terms`). Un `run_id` que no existe
  deja el paso con `isError` y "No encontré esa compra.".
- Un `request_approval` con `isError` y el código `checkout_terms_changed`, `offer_expired`,
  `checkout_expired` u `offer_freshness_unavailable` (y `args.run_id`) es una compra pendiente cuyos
  términos ya no valen (S04): la UI muestra la tarjeta que solo permite cancelar. Llega así cuando
  presentar los términos falla en el stream, y la UI arma la misma parte al restaurar una compra de
  `GET /me/pending-purchase` con `terms_status`. Si aprobar responde 409 o 503 con esos códigos, la
  tarjeta pasa al mismo estado. `offer_expired` y `checkout_expired` (venció la oferta o el checkout;
  el comercio cancela también un checkout vencido) muestran «Las condiciones que revisaste vencieron»,
  también al recargar: `terms_status` vale `changed`, `expired` o `unavailable`. Al presentar los
  términos de la compra pendiente, `checkout_expired` (por `expires_at` o porque el comercio ya
  marcó el checkout `expired`) se informa como `expired` y `checkout_not_ready` como `changed`, en
  vez de un 409 (`docs/design/hp-p5a-definitive-failures.md`). Con D2 (`b2-hp-v2`), aprobar puede
  responder 409 `d2_denied` (la política no autorizó) o `d2_unavailable`/`d2_allow_unapplied` (no se
  pudo verificar o aplicar): la tarjeta pasa al mismo estado con «La política de compra no autorizó
  esta compra» o «No pude verificar la autorización de la compra», dice que no se firmó nada y, con
  la traza, muestra la regla que falló. Al recargar, `terms_status` vale `authorization_denied` o
  `authorization_unverified` (`docs/design/hp-p3-d2-authorization.md`).
- `checkout_not_ready`, y `checkout_expired` después de registrar la aprobación, llegan a la tarjeta
  con su texto en español, sin el `message` interno.
- El texto final de un turno de compra es la explicación del modelo (más la nota de precio si
  cambió). La invitación a aprobar la pone la tarjeta, que sabe si la compra sigue abierta.
- Un paso que falla o se cancela recibe `result: {"error": {"code", "message"}}` con
  `isError: true`, para que la UI no lo muestre en curso indefinidamente. Por eso `prepare_checkout`
  lleva el `run_id` en sus `args`: si la política de compra lo detiene (`d1_denied`), la UI lee de ahí
  la compra para pedir su traza. Un `purchase-updated` con
  un `run_id` desconocido produce el tool `purchase_update` con error y estado `incomplete`/`error`
  ("No encontré esa compra.").
- Las partes `reasoning` se agregan donde llega el primer delta, es decir, después del tool call del
  paso que lo produjo (por ejemplo `interpret_request` → razonamiento → `search_catalog`).
- Un body inválido (JSON, comandos, `details` o `turn_id`) responde HTTP 422 y un body de más de
  1 MiB responde 413, ambos con `{"code", "message"}` en español sin eco de la entrada; no se abre
  el stream. Un `add-message` exige `details`; un texto de más de 2000 caracteres es inválido.
- `ProductCard.product_url` es `null` si la URL del feed no es HTTP(S) sin credenciales.
- Si un request trae varios `add-message`, el primero usa `turn_id` como clave idempotente y los
  siguientes `turn_id:1`, `turn_id:2`, …
- `OPENAI_REASONING_EFFORT` también se aplica a los requests sin resumen; sin
  `OPENAI_REASONING_SUMMARY` ni `OPENAI_REASONING_EFFORT` los requests al modelo no cambian.
