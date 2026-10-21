# Tesis Commerce Agent

Buyer Agent experimental de una tesis de maestría sobre agentes de comercio gobernables. Compra
contra un ecommerce externo mediante ACP `2026-04-17`. El merchant conserva la verdad de catálogo,
stock, precios, checkout y orden; este repositorio implementa intención, selección, aprobación
humana, ejecución idempotente, recepción de eventos y la gobernanza experimental de la decisión
(B2), además de los experimentos que la evalúan.

El Merchant ([`tesis-commerce-merchant`](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant))
y el proveedor de pagos sandbox (`tesis-payment-provider-sandbox`) son repositorios y procesos
separados. Este repositorio nunca importa `commerce_lab` ni accede a la base de datos del merchant.

## Arranque rápido

Requiere Python 3.12, `uv` y Node.js 24 o posterior. Desde la raíz del clon:

```sh
uv sync --frozen --all-groups   # agente y miembro evaluation/ del workspace
uv run pytest                   # unitarias, contratos y evaluación en dry-run; sin red ni modelo
cd ui && npm ci && npm run lint && npm run build
```

Sin más servicios se pueden lanzar las [corridas de evaluación en dry-run](#evaluación) y abrir
Storybook (`cd ui && npm run storybook`). Para conversar con el agente hacen falta el Merchant y
el proveedor de pagos: en un comando con la [Demo local](#demo-local), o a mano según
[Stack con el Merchant](#stack-con-el-merchant).

## Mapa del repositorio

La carpeta decide el papel del código: `src/` es el producto, `evaluation/` son los experimentos
y `tests/` es la verificación de ambos.

| Carpeta | Contenido |
| --- | --- |
| `src/commerce_agent/` | El producto: dominio, cliente ACP, AP2, LLM, gobernanza B2 (`governance/`), persistencia SQLite, grafo LangGraph y API FastAPI. Es lo único que entra al wheel y a la imagen del `Dockerfile`. |
| `evaluation/` | Miembro `tesis-commerce-evaluation` del workspace uv: código (`src/commerce_evaluation/`), datos (`data/`), resultados (`results/`) y scripts (`scripts/`) del piloto B0/B2, HNP-01 y HNP-02. Depende del producto; el producto nunca lo importa. Ver [evaluation/README.md](evaluation/README.md). |
| `tests/` | Pruebas de producto y evaluación. `tests/integration/` es la suite de sistema opt-in, `tests/evaluation/` la de los experimentos y `tests/support/` los dobles compartidos. |
| `ui/` | Interfaz React + Vite con `assistant-ui`, Storybook con pruebas de interacción y axe, y E2E de Playwright (`ui/e2e/`). Ver [ui/README.md](ui/README.md). |
| `auth/` | Servicio Better Auth (correo y contraseña) que emite los JWT del comprador; pruebas en `auth/test/`. |
| `scripts/` | Exportador de los fixtures SSE de Storybook, medición offline de D1 y herramientas puntuales en `scripts/tools/`. |
| `docs/` | Estrategia de pruebas, contrato UI–agente, perfil AP2, matriz de compatibilidad, despliegue, guion de la demo (`demo/`), diseño B2 (`b2-governance/`), evidencia histórica HP/HNP (`governance-monitor/`), HNP-01 (`hnp-experiment/`), diagramas y reportes históricos (`archive/`). |

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
- gobernanza B2 experimental: D1 detrás de `AGENT_GOVERNANCE_MODE` (`off` por defecto, `shadow`, o
  `enforce` opt-in que solo aplica el deny), creación ACP guardada D17 solo en backend y
  consentimiento D2-HP como aplicación separada que `api.py` no monta. Estado y límites en
  [docs/b2-governance](docs/b2-governance/README.md) y en la
  [guía de enforce](docs/b2-governance/d1-enforce-runtime-optin.md);
- Human Not Present en construcción detrás de `AGENT_HNP_ENABLED` (`false` por defecto): con el
  flag, rutas de propuesta, consentimiento y «Mis delegaciones» para el comprador
  ([spec](docs/design/hnp-governed.md));
- suite de sistema contra el Merchant y el proveedor de pagos reales como procesos y repositorios
  separados ([tests/integration](tests/integration/README.md)).

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
aprobaciones e idempotencia.

La UI conserva `conversation_id` durante el montaje actual; una recarga inicia un hilo nuevo,
pero recupera la compra pendiente y su tarjeta de aprobación desde el backend. No hay todavía una
vista de historial completo de conversaciones.

Better Auth (`auth/`) ofrece acceso por correo y contraseña; la UI exige sesión antes de mostrar
el chat y envía JWT cortos. Con `AGENT_AUTH_MODE=jwt` la API los valida antes de cada conversación
y operación de compra; con `local` (predeterminado) el backend usa `AGENT_PRINCIPAL_ID`. Los IDs
de hilo y turno se separan por comprador, y las compras se autorizan por propietario. El registro
no verifica correos: no usar cuentas ni pagos reales. Véanse el [runbook de despliegue](docs/deployment-railway.md) y el
[perfil AP2 Human Present](docs/ap2-profile.md).

## Pruebas

La estrategia completa (capas, markers, variables, workflows y cobertura) está en
[docs/testing.md](docs/testing.md). En resumen:

- `uv run pytest` corre las unitarias y de contrato y la evaluación en dry-run, sin red, modelo
  ni Merchant. `tests/integration` se recoge y se omite.
- `cd auth && npm ci && npm test` prueba el servicio de autenticación.
- En `ui/`: `npm run lint`, `npm run build`, `npm run test-storybook` (historias en Chromium con
  axe) y `npm run e2e` (Playwright sobre el build, con backends simulados).
- La suite de sistema (`tests/integration`) solo corre con `RUN_LIVE_STACK=1`; ver
  [Stack con el Merchant](#stack-con-el-merchant).

Antes de entregar cambios en Python: `uv run ruff format --check .`, `uv run ruff check .`,
`uv run pyright` y `uv run pytest`. En CI, `ci.yml` corre los jobs `python-lint`, `python-tests`,
`auth` y `ui` en cada PR; `nightly.yml` corre la suite de sistema cada noche si existe el secreto
`MERCHANT_REPO_TOKEN`. Ningún workflow llama a un LLM real.

## Evaluación

El código de los experimentos vive en `evaluation/` y se instala con `uv sync --frozen --all-groups`.
Estos entrypoints no abren conexiones a proveedores ni al Merchant; las salidas HNP quedan en
`.local/`:

```sh
uv run hnp-matrix --dry-run --env-file .env.example   # HNP-02 con FakeTransport, en .local/hnp02
uv run governance-pilot --dry-run                     # plan de B0'; repita --condition para intercalar B0' y B2
uv run hnp-review-demo                                # HNP-01 guionado, en .local/hnp01/demo
```

Las corridas reales se lanzan a mano, nunca en CI, y gastan dinero del modelo configurado:

- `uv run hnp-matrix --env-file .env` llama al proveedor solo si no hay `--dry-run`, `HNP_LIVE=1`,
  existe `OPENAI_API_KEY`, hay precios del validador y `HNP_MAX_COST_USD` cubre la cota superior que
  imprime antes de empezar (`--estimate-only` solo imprime el plan y el costo). Con
  `HNP_MAX_COST_USD=0` se niega a correr.
- `uv run governance-pilot` sin `--dry-run` levanta Merchant, proveedor y agente desde los checkouts
  hermanos (`--merchant-repo`, `--payment-repo`) y exige el `OPENAI_MODEL` fijado en la matriz.
- `uv run evaluate-conversation` evalúa un agente local ya activo (`--base-url`, por defecto
  `http://127.0.0.1:43120`) con su LLM y cancela los checkouts sandbox que prepara.

Solo HNP-02 tiene tope de costo propio. Datos, resultados y protocolo en
[evaluation/README.md](evaluation/README.md); el validador HNP-02 en
[evaluation/src/commerce_evaluation/hnp/llm/README.md](evaluation/src/commerce_evaluation/hnp/llm/README.md).
La evidencia histórica HP/HNP del 17 al 20 de octubre de 2026, con sus hashes, está en
[docs/governance-monitor/](docs/governance-monitor/README.md).

## Demo local

`uv run demo-stack` levanta en un comando lo necesario para comprar por chat en la UI: PostgreSQL
del Merchant (sin apagarlo ni borrar volúmenes), migraciones, catálogo administrado, proveedor de
pagos, Merchant, `auth/`, agente y UI. Los secretos y las claves AP2 y D1 son nuevos en cada
arranque y no se imprimen ni se escriben; del `.env` solo lee las variables `OPENAI_*`, y solo para
el agente. Arranca en B2; `--mode b0` y `--mode pol02-deny` cambian la condición, `--dry-run` revisa
requisitos y puertos, y `uv run demo-stack s04` cambia el precio del escenario S04. Requisitos,
pedidos y plan B en el [guion del 23 de octubre](docs/demo/guion-2026-10-23.md).

## Stack con el Merchant

### Automático: suite de sistema

`tests/integration` levanta en cada ejecución PostgreSQL en Docker, el Merchant y el proveedor de
pagos desde sus checkouts, el agente, `auth/` y la UI, con credenciales y claves AP2 sintéticas por
ejecución. Es la forma reproducible de recorrer una compra B0 completa. Requiere macOS o Linux y
Docker; con ambos repositorios clonados junto a este:

```sh
(cd ../tesis-commerce-merchant && uv sync --frozen --all-groups)
(cd ../tesis-payment-provider-sandbox && uv sync --frozen --all-groups)
(cd auth && npm ci) && (cd ui && npm ci) && uv run playwright install --only-shell chromium
RUN_LIVE_STACK=1 MERCHANT_REPO="$PWD/../tesis-commerce-merchant" \
  PAYMENT_REPO="$PWD/../tesis-payment-provider-sandbox" uv run pytest tests/integration -rs
```

La suite completa tarda unos 12 minutos; `-m "not slow"` deja un subconjunto de unos 2. Requisitos,
evidencia y limpieza en [tests/integration/README.md](tests/integration/README.md).

### Manual: servicios locales para la UI

1. **Merchant**, en su checkout (detalles en su README):

   ```sh
   uv sync --frozen
   docker compose up -d --wait postgres
   uv run python -m commerce_lab.db migrate
   uv run python -m commerce_lab.managed_catalog import-current   # imprime el run_id
   uv run python -m commerce_lab.db issue-session <run_id> merchant-agent
   uv run uvicorn commerce_lab.api:app --host 127.0.0.1 --port 4120
   ```

   `issue-session` imprime un Bearer sintético de una hora (`--ttl-seconds` lo cambia) para
   `MERCHANT_BEARER_TOKEN`; `merchant-agent` es el actor del catálogo administrado.
2. **Proveedor de pagos**, según el README de `tesis-payment-provider-sandbox`, en el origen de
   `PAYMENT_PROVIDER_URL` (`http://127.0.0.1:4130` en `.env.example`).
3. **Agente**: `cp -n .env.example .env`, completar la [configuración local](#configuración-local)
   y arrancar la [API](#api-del-agente).
4. **Autenticación**: `cd auth && npm ci && cp -n .env.example .env`, poner un
   `BETTER_AUTH_SECRET` aleatorio de al menos 32 caracteres y `node --env-file=.env server.js`
   (puerto 4210; Vite reenvía `/api/auth` allí).
5. **UI**: ver [Interfaz web](#interfaz-web).

Los secretos compartidos (webhook del merchant, credenciales del proveedor, `PAYMENT_MERCHANT_ID`
y claves AP2) deben coincidir entre los tres servicios; la lista está en
[Variables que deben coincidir](docs/deployment-railway.md#variables-que-deben-coincidir).

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
Git, logs, prompts, fixtures o artefactos. Las variables `HNP_*` de `.env.example` solo las lee
`hnp-matrix`.

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
7. `GET /purchases/{run_id}/trace` devuelve la traza de controles de la compra y
   `GET /me/purchases` el historial paginado del comprador, sin tokens, mandatos ni datos del dueño;
   ver [`docs/design/trace-panel.md`](docs/design/trace-panel.md).
8. `POST /agentic_checkout/webhooks/order_events` recibe eventos firmados del merchant.
9. `POST /assistant` expone el mismo pipeline como stream Assistant Transport para
   `useAssistantTransportRuntime`: cada paso (`interpret_request`, `search_catalog`,
   `select_product`, `prepare_checkout`, `request_approval`) llega como tool call con argumentos y
   resultado, y `purchase` se relee de SQLite. El estado del cliente no es autoritativo; ver
   [`docs/agent-ui-contract.md`](docs/agent-ui-contract.md).

Los fixtures SSE de Storybook se regeneran con `uv run python scripts/export_assistant_fixtures.py`;
un test falla si los archivos versionados quedan desactualizados.

`complete` devuelve conflicto si el episodio no está aprobado. Dos intentos concurrentes no pueden
adquirir la misma reserva local. Un timeout posterior al despacho queda `uncertain`; el siguiente
intento consulta primero el checkout y conserva la misma clave idempotente.

## Interfaz web

La UI vive en `ui/` y se conecta exclusivamente a la API del agente. Ningún secreto de OpenAI,
merchant o pago se entrega al navegador.

```sh
cd ui
cp -n .env.example .env.local
npm ci
npm run dev
```

Abra `http://127.0.0.1:5173` con la API del agente y `auth/` activos. El perfil incluido es
sintético y editable; el chat prepara el checkout, pero la compra solo se completa al pulsar el
botón que muestra el total y el fingerprint autoritativos. Storybook y pruebas en
[ui/README.md](ui/README.md).

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

## Límites deliberados

Este P0 usa un solo merchant registrado por configuración, una moneda, cantidad uno, SQLite y pago
sandbox independiente. No procesa dinero real ni datos de tarjetas reales. El modelo conversacional
es una integración externa, pero su salida se valida antes del ranking y nunca autoriza completion.
La gobernanza B2 es experimental y opt-in: su evaluador es la referencia en Python, sin OPA, y D17 y
D2-HP no están conectados al flujo público. Mandatos externos, Stripe real y operación
multi-instancia quedan fuera de este incremento.

La matriz de contrato, amenazas e hipótesis está en
[`docs/compatibility.md`](docs/compatibility.md).
La configuración de la demo remota está en
[`docs/deployment-railway.md`](docs/deployment-railway.md).
