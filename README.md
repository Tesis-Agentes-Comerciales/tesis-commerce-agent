# Tesis Commerce Agent

Buyer Agent experimental que compra contra un ecommerce externo mediante ACP `2026-04-17`.
El merchant conserva la verdad de catálogo, stock, precios, checkout y orden; este repositorio
implementa intención, selección, aprobación humana, ejecución idempotente y recepción de eventos.

## Alcance implementado

- lectura y validación del Product Feed publicado por el merchant antes de cada turno;
- decisión conversacional tipada, `PurchaseIntent`, filtros duros, ranking determinista y elección
  explicada entre candidatos elegibles;
- cliente ACP para discovery, create, get, update, cancel y complete;
- validación de requests y responses contra el bundle ACP congelado;
- persistencia SQLite de episodios, claves idempotentes, aprobaciones, órdenes y webhooks;
- orquestación LangGraph con nodos explícitos, rutas condicionales, checkpoints SQLite por
  conversación y pausa para la aprobación humana;
- Human Present ligado mediante HMAC a comercio, checkout, líneas, identidad, dirección,
  fulfillment, total, revisión y expiración; el perfil AP2 añade mandatos SD-JWT y recibos firmados;
- gateway de completion fuera del modelo, delegación a un proveedor sandbox independiente y CAS local;
- estado `uncertain` y reconciliación desde el checkout autoritativo;
- receptor de webhooks con HMAC, ventana temporal, límite de body, deduplicación y proyección
  monotónica;
- API FastAPI para preparar, revisar, aprobar y completar compras;
- interfaz React basada en `assistant-ui`, con perfil sandbox y aprobación Human Present;
- harness E2E contra merchant y proveedor de pagos como procesos y repositorios separados.

El modelo recibe los mensajes del chat y vocabulario validado del feed. Primero produce una decisión
tipada con una intención estructurada. Puede invocar `lookup_catalog`, una herramienta de solo lectura
sobre la instantánea validada del feed, para responder preguntas sobre productos y precios
observados; después puede elegir o aclarar entre candidatos que ya
pasaron los filtros deterministas. No recibe datos de entrega, Bearer, secretos, token de pago,
actor, Request ID, clave idempotente, fingerprint de aprobación ni acceso al método de completion.
El navegador tampoco construye ni envía `PurchaseIntent`.

## Arquitectura

```text
UI → FastAPI → LangGraph (thread_id y checkpoints SQLite)
                interpretar → responder | aclarar | buscar catálogo
                                               buscar → recuperar | elegir candidato
                                                                   ↓
                              ACP: preparar checkout → Human Present → pausa
                                                                        ↓
                              aprobación humana REST → reanudar → CompletionService
                                                         ↓
                              token delegado → ACP complete → orden del merchant
```

Los nodos llaman a servicios tipados existentes. La única herramienta invocable por el modelo es
`lookup_catalog`: no crea checkouts ni accede a pagos, aprobación o datos de entrega.
LangGraph guarda el estado conversacional en un segundo archivo (por ejemplo,
`.local/agent.db` → `.local/agent.graph.db`). La base principal sigue siendo la autoridad de compras,
aprobaciones e idempotencia. El package `commerce_agent` nunca importa
`commerce_lab` ni accede a la base de datos del merchant.

La UI conserva `conversation_id` durante el montaje actual; una recarga inicia un hilo nuevo,
pero recupera la compra pendiente y su tarjeta de aprobación desde el backend. No hay todavía una
vista de historial completo de conversaciones.

Para la demo pública sintética, Better Auth (`auth/`) ofrece acceso por correo y contraseña.
La UI envía JWT cortos y la API valida identidad antes de cada conversación y operación de compra.
Los IDs de hilo y turno se separan por comprador, y las compras se autorizan por propietario.
El registro no verifica correos: no usar cuentas ni pagos reales. Véanse el
[runbook de despliegue](docs/deployment-railway.md) y el [perfil AP2 Human Present](docs/ap2-profile.md).

## Desarrollo

Requiere Python 3.12 y `uv`:

```sh
uv sync --frozen --all-groups
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
uv build
```

Las pruebas E2E quedan omitidas en la suite normal. Las unitarias y de contrato no requieren red,
modelo ni merchant. Las de integración real con PostgreSQL y Merchant son opt-in; sus variables y
el procedimiento en macOS, Linux y Windows están en [tests/integration](tests/integration/README.md).

## Inspección con LangGraph Studio

Studio permite ver los nodos, las rutas y el estado de ejecución del mismo grafo. Usa un Agent Server
local separado de FastAPI; sus hilos no son los de la UI y no comparte sus checkpoints. Esta
configuración es deliberadamente de solo lectura: puede explorar el catálogo y ver la selección
propuesta, pero nunca prepara un checkout ni completa una compra. Para probar la aprobación y el
checkout se usa la UI/API del agente. Studio carga el feed al iniciar; reinícielo para ver cambios
posteriores del merchant. La UI/API sí consulta una instantánea nueva antes de cada turno.

Con `OPENAI_API_KEY`, `OPENAI_MODEL` y el merchant disponibles en `.env`:

```sh
LANGSMITH_TRACING=false uv run langgraph dev --no-browser --host 127.0.0.1 --port 2024
```

Abra [LangGraph Studio](https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024) y seleccione
`commerce_agent`. Use la vista de grafo e ingrese, por ejemplo:

```json
{
  "incoming_messages": [{"role": "user", "content": "¿Qué audífonos Sonora tienes?"}],
  "turn_id": "demo-1"
}
```

Para otro turno en el mismo hilo, envíe solo el nuevo mensaje de usuario y un `turn_id` nuevo.
La CLI y su almacenamiento local se instalaron solo como dependencias de desarrollo. No exponga el
servidor local fuera de `127.0.0.1`; `LANGSMITH_TRACING=false` evita subir trazas de prueba.

## Configuración local

```sh
cp -n .env.example .env
```

Configure en `.env`:

- `MERCHANT_BASE_URL`: origen ACP; HTTP solo se admite en loopback.
- `MERCHANT_BEARER_TOKEN`: sesión emitida por el CLI del merchant.
- `MERCHANT_WEBHOOK_SECRET`: secreto compartido únicamente para verificar eventos.
- `AGENT_APPROVAL_SECRET`: secreto independiente para vincular la aprobación.
- `PAYMENT_PROVIDER_URL`: origen del proveedor sandbox independiente.
- `PAYMENT_AGENT_BEARER_TOKEN`: credencial del agente ante ese proveedor, no ante el merchant.
- `AGENT_DATABASE_PATH`: SQLite local; los checkpoints se guardan en un segundo archivo con el
  mismo nombre base y sufijo `.graph.db`.
- `AGENT_FEED_SOURCE=merchant` (predeterminado): lee `GET /feeds/current` del merchant al
  arrancar y antes de cada turno. El feed observado sirve para buscar y perfilar; el checkout
  del merchant sigue siendo la autoridad de precio y stock. Si la publicación falla, el turno
  falla cerrado. `AGENT_FEED_SOURCE=file` y `AGENT_FEED_PATH` quedan solo para pruebas con un
  snapshot ACP fijado.
- `AGENT_PRINCIPAL_ID`: identidad humana resuelta por el backend local.
- `AGENT_UI_ORIGIN`: origen exacto autorizado para la interfaz web local.
- `OPENAI_API_KEY`, `OPENAI_MODEL` y `OPENAI_TIMEOUT_SECONDS`: modelo conversacional que enruta la
  solicitud y elige o explica entre candidatos elegibles.
- `OPENAI_REASONING_SUMMARY` (por defecto `false`): si es `true`, `POST /assistant` pide a la
  Responses API resúmenes de razonamiento y los muestra como partes `reasoning`; nunca vuelven al
  prompt. `/conversation/turn` no los solicita.
- `OPENAI_REASONING_EFFORT` (opcional: `minimal`, `low`, `medium` o `high`): esfuerzo de
  razonamiento para modelos que lo soportan. Sin estas dos variables los requests no cambian.

Use valores aleatorios de al menos 32 caracteres para los secretos. No copie valores reales a
Git, logs, prompts, fixtures o artefactos.

## API del agente

Con el merchant y el feed preparados:

```sh
uv run uvicorn commerce_agent.api:app_factory --factory --host 127.0.0.1 --port 43120
```

Flujo:

1. `POST /conversation/turn` recibe un `turn_id` idempotente, un `conversation_id` opcional,
   mensajes y datos de entrega. Una respuesta o aclaración no
   contacta al merchant; una elección válida prepara el checkout y devuelve los términos Human
   Present en la misma operación. Si ningún producto pasa los filtros, el modelo explica alternativas
   reales del catálogo en vez de convertirlo en error HTTP.
2. `GET /purchases/{run_id}/approval` permite volver a consultar los términos exactos.
3. `POST /purchases/{run_id}/approval` recibe el fingerprint mostrado y, si AP2 está activo, el
   Checkout JWT asociado a esos términos; el actor viene del backend. El servidor vuelve a verificar el
   checkout y firma los mandatos fuera del LLM.
4. `POST /purchases/{run_id}/cancel` cancela un checkout que aún espera aprobación.
5. `POST /purchases/{run_id}/complete` ejecuta el gateway determinista.
6. `GET /purchases/{run_id}` consulta el estado local.
7. `POST /agentic_checkout/webhooks/order_events` recibe eventos firmados del merchant.
8. `POST /assistant` expone el mismo pipeline como stream Assistant Transport para
   `useAssistantTransportRuntime`: cada paso (`interpret_request`, `search_catalog`,
   `select_product`, `prepare_checkout`, `request_approval`) llega como tool call con argumentos y
   resultado, y `purchase` se relee de SQLite. El estado del cliente no es autoritativo; ver
   [`docs/agent-ui-contract.md`](docs/agent-ui-contract.md).

Los fixtures SSE de Storybook se regeneran con `uv run python scripts/export_assistant_fixtures.py`;
un test falla si los archivos versionados quedan desactualizados.

Con el agente y el merchant locales activos, la evaluación conversacional opcional usa el LLM
configurado y cancela todos los checkouts sandbox que prepara:

```sh
uv run python scripts/evaluate_conversation.py
```

`complete` devuelve conflicto si el episodio no está aprobado. Dos intentos concurrentes no pueden
adquirir la misma reserva local. Un timeout posterior al despacho queda `uncertain`; el siguiente
intento consulta primero el checkout y conserva la misma clave idempotente.

## Interfaz web

La UI vive en `ui/` y se conecta exclusivamente a la API del agente. Ningún secreto de OpenAI,
merchant o pago se entrega al navegador.

```sh
cd ui
cp -n .env.example .env.local
npm install
npm run dev
```

Abra `http://127.0.0.1:5173` después de iniciar FastAPI. El perfil incluido es sintético y editable;
el chat prepara el checkout, pero la compra solo se completa al pulsar el botón que muestra el total
y el fingerprint autoritativos.

## Integración entre los tres servicios

El smoke compartido levanta merchant y proveedor de pagos en procesos separados:

```sh
uv run python scripts/run_joint_e2e.py \
  --merchant-repo ../tesis-commerce-merchant \
  --payment-repo ../tesis-payment-provider-sandbox
```

El runner levanta PostgreSQL si hace falta, migra, crea un episodio B0, exporta el feed, emite un
Bearer efímero para cada frontera, inicia ambos servicios, completa una compra y verifica:

- replay local y replay idempotente remoto;
- permalink sin PII ni token sandbox;
- `order_create` firmado;
- `order_update` firmado y transición a `processing`.

Para verificar además el perfil AP2 Human Present con claves efímeras, mandatos y recibos firmados:

```sh
uv run python scripts/run_joint_e2e.py --ap2 \
  --merchant-repo ../tesis-commerce-merchant \
  --payment-repo ../tesis-payment-provider-sandbox
```

Si PostgreSQL ya está levantado, agregue `--skip-postgres-start`. Los puertos se cambian con
`--merchant-port`, `--payment-port` y `--webhook-port`. El checkout usa el reloj sintético del
episodio; el runner sincroniza ese reloj con el proveedor.

Para probar el recorrido AP2 desde un navegador real —registro, conversación con el LLM,
aprobación explícita y orden sandbox— instale Chromium para Playwright (`uv run playwright
install chromium`) y ejecute:

```sh
uv run python scripts/run_joint_e2e.py --ap2 --ui --skip-postgres-start \
  --merchant-port 4125 --payment-port 4135 --webhook-port 43126 \
  --agent-port 43125 --auth-port 4215 --ui-port 5175
```

El runner crea claves, cuentas y bases temporales. Los puertos alternos permiten mantener la demo
local en ejecución; cambie cualquiera que esté ocupado. La prueba deja una captura en `../output/`.

Para incluir la extracción de intención con el modelo OpenAI configurado en `.env`:

```sh
uv run python scripts/run_joint_e2e.py \
  --merchant-repo ../tesis-commerce-merchant \
  --payment-repo ../tesis-payment-provider-sandbox \
  --live-llm
```

## Límites deliberados

Este P0 usa un solo merchant registrado por configuración, una moneda, cantidad uno, SQLite y pago
sandbox independiente. No procesa dinero real ni datos de tarjetas reales. El modelo conversacional es una integración externa, pero su
salida se valida antes del ranking y nunca autoriza completion. B2, OPA, mandatos externos, Stripe
real y operación multi-instancia quedan fuera de este incremento.

La matriz de contrato, amenazas e hipótesis está en
[`docs/compatibility.md`](docs/compatibility.md).
La configuración de la demo remota está en
[`docs/deployment-railway.md`](docs/deployment-railway.md).
