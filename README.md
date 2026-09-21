# Tesis Commerce Agent

Buyer Agent experimental que compra contra un ecommerce externo mediante ACP `2026-04-17`.
El merchant conserva la verdad de catálogo, stock, precios, checkout y orden; este repositorio
implementa intención, selección, aprobación humana, ejecución idempotente y recepción de eventos.

## Alcance implementado

- carga y validación del Product Feed estático;
- `PurchaseIntent`, filtros duros y ranking determinista sobre LangGraph;
- cliente ACP para discovery, create, get, update, cancel y complete;
- validación de requests y responses contra el bundle ACP congelado;
- persistencia SQLite de episodios, claves idempotentes, aprobaciones, órdenes y webhooks;
- Human Present ligado mediante HMAC a comercio, checkout, líneas, identidad, dirección,
  fulfillment, total, revisión y expiración;
- gateway de completion fuera del modelo, con pago sandbox reproducible y CAS local;
- estado `uncertain` y reconciliación desde el checkout autoritativo;
- receptor de webhooks con HMAC, ventana temporal, límite de body, deduplicación y proyección
  monotónica;
- API FastAPI para preparar, revisar, aprobar y completar compras;
- interfaz React basada en `assistant-ui`, con perfil sandbox y aprobación Human Present;
- harness E2E contra el merchant fijado, ejecutado como proceso y repositorio separados.

El modelo solo puede producir un `PurchaseIntent` estructurado. No recibe Bearer, secretos, token
de pago, actor, Request ID, clave idempotente, fingerprint de aprobación ni acceso al método de
completion.

## Arquitectura

```text
solicitud + PurchaseIntent
          |
          v
LangGraph read-only --> feed validado --> candidato
          |
          v
PurchaseService --> ACP create/update/get --> ready_for_payment
          |
          v
resumen Human Present --> aprobación HMAC durable
          |
          v
CompletionService --> GET + CAS + ACP complete --> orden durable
                                               |
merchant webhook --> HMAC + dedupe ------------+
```

SQLite demuestra autoridad y coordinación local; el checkpoint del grafo no demuestra efectos
comerciales. El package `commerce_agent` nunca importa `commerce_lab` ni accede a la base de datos
del merchant.

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
- `AGENT_PAYMENT_SECRET`: secreto independiente para el token sandbox reproducible.
- `AGENT_DATABASE_PATH`: SQLite local.
- `AGENT_FEED_PATH`: directorio con `metadata.json` y `products.jsonl`.
- `AGENT_PRINCIPAL_ID`: identidad humana resuelta por el backend local.
- `AGENT_UI_ORIGIN`: origen exacto autorizado para la interfaz web local.
- `OPENAI_API_KEY`, `OPENAI_MODEL` y `OPENAI_TIMEOUT_SECONDS`: intérprete opcional de intención.

Use valores aleatorios de al menos 32 caracteres para los tres secretos. No copie valores reales a
Git, logs, prompts, fixtures o artefactos.

## API del agente

Con el merchant y el feed preparados:

```sh
uv run uvicorn commerce_agent.api:app_factory --factory --host 127.0.0.1 --port 43120
```

Flujo:

1. `POST /purchases` recibe solicitud, `PurchaseIntent` tipado y, opcionalmente, datos de checkout.
2. `GET /purchases/{run_id}/approval` devuelve los términos exactos para Human Present.
3. `POST /purchases/{run_id}/approval` recibe solo el fingerprint mostrado; el actor viene del
   backend.
4. `POST /purchases/{run_id}/complete` ejecuta el gateway determinista.
5. `GET /purchases/{run_id}` consulta el estado local.
6. `POST /agentic_checkout/webhooks/order_events` recibe eventos firmados del merchant.

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

## Integración real con el merchant

El smoke compartido fija el merchant en
`361bfe7436dd0167cad3b4b3fedbf79c82a9c76b`:

```sh
uv run python scripts/run_joint_e2e.py \
  --merchant-repo ../tesis-commerce-merchant
```

El runner levanta PostgreSQL si hace falta, migra, crea un episodio B0, exporta el feed, emite un
Bearer efímero, inicia el merchant, completa una compra y verifica:

- replay local y replay idempotente remoto;
- permalink sin PII ni token sandbox;
- `order_create` firmado;
- `order_update` firmado y transición a `processing`.

Si PostgreSQL ya está levantado, agregue `--skip-postgres-start`. Los puertos se cambian con
`--merchant-port` y `--webhook-port`. GitHub Actions expone el mismo recorrido como workflow manual
`Joint ACP end-to-end`.

Para incluir la extracción de intención con el modelo OpenAI configurado en `.env`:

```sh
uv run python scripts/run_joint_e2e.py \
  --merchant-repo ../tesis-commerce-merchant \
  --live-llm
```

## Límites deliberados

Este P0 usa un solo merchant registrado por configuración, una moneda, cantidad uno, SQLite y pago
sandbox. No procesa dinero real. La extracción de intención por modelo es una integración externa:
la API recibe el `PurchaseIntent` ya validado para mantener idéntica la frontera determinista. B2,
OPA, mandatos externos, Stripe real y operación multi-instancia quedan fuera de este incremento.

La matriz de contrato, amenazas e hipótesis está en
[`docs/compatibility.md`](docs/compatibility.md).
