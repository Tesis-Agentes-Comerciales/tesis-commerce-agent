# Tesis Commerce Agent

Buyer Agent experimental que compra contra un ecommerce externo mediante ACP `2026-04-17`.
El merchant conserva la verdad de catálogo, stock, precios, checkout y orden; este repositorio
implementa intención, selección, aprobación humana, ejecución idempotente y recepción de eventos.

## Alcance implementado

- carga y validación del Product Feed estático;
- decisión conversacional tipada, `PurchaseIntent`, filtros duros, ranking determinista y elección
  explicada entre candidatos elegibles;
- cliente ACP para discovery, create, get, update, cancel y complete;
- validación de requests y responses contra el bundle ACP congelado;
- persistencia SQLite de episodios, claves idempotentes, aprobaciones, órdenes y webhooks;
- Human Present ligado mediante HMAC a comercio, checkout, líneas, identidad, dirección,
  fulfillment, total, revisión y expiración;
- gateway de completion fuera del modelo, delegación a un proveedor sandbox independiente y CAS local;
- estado `uncertain` y reconciliación desde el checkout autoritativo;
- receptor de webhooks con HMAC, ventana temporal, límite de body, deduplicación y proyección
  monotónica;
- API FastAPI para preparar, revisar, aprobar y completar compras;
- interfaz React basada en `assistant-ui`, con perfil sandbox y aprobación Human Present;
- harness E2E contra merchant y proveedor de pagos como procesos y repositorios separados.

El modelo recibe los mensajes del chat y vocabulario validado del feed. Primero produce una decisión
tipada con una intención estructurada; después puede elegir o aclarar entre candidatos que ya
pasaron los filtros deterministas. No recibe datos de entrega, Bearer, secretos, token de pago,
actor, Request ID, clave idempotente, fingerprint de aprobación ni acceso al método de completion.
El navegador tampoco construye ni envía `PurchaseIntent`.

## Arquitectura

```text
mensajes + vocabulario --> router --> responder | aclarar | intención
                                                       |
                                                       v
feed validado --> ranking elegible --> selector LLM --> aclarar | candidato
          |
          v
PurchaseService --> ACP create/update/get --> ready_for_payment
          |
          v
resumen Human Present --> aprobación HMAC durable
          |
          v
CompletionService --> GET + CAS --> proveedor: delegate_payment
                              --> ACP complete --> merchant: proveedor /confirm --> orden
                                               |
merchant webhook --> HMAC + dedupe ------------+
```

SQLite demuestra autoridad y coordinación local. El package `commerce_agent` nunca importa
`commerce_lab` ni accede a la base de datos del merchant.

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
modelo ni merchant.

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
- `AGENT_DATABASE_PATH`: SQLite local.
- `AGENT_FEED_PATH`: directorio con `metadata.json` y `products.jsonl`.
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

1. `POST /conversation/turn` recibe un `turn_id` idempotente, mensajes y datos de entrega. Una respuesta o aclaración no
   contacta al merchant; una elección válida prepara el checkout y devuelve los términos Human
   Present en la misma operación. Si ningún producto pasa los filtros, el modelo explica alternativas
   reales del catálogo en vez de convertirlo en error HTTP.
2. `GET /purchases/{run_id}/approval` permite volver a consultar los términos exactos.
3. `POST /purchases/{run_id}/approval` recibe solo el fingerprint mostrado; el actor viene del
   backend.
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

Si PostgreSQL ya está levantado, agregue `--skip-postgres-start`. Los puertos se cambian con
`--merchant-port`, `--payment-port` y `--webhook-port`. El checkout usa el reloj sintético del
episodio; el runner sincroniza ese reloj con el proveedor.

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
