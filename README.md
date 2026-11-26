# Tesis Commerce Agent

Agente comprador de una tesis de pregrado de la Universidad de los Andes sobre agentes de comercio
gobernables. Conversa con la persona usando Claude, de Anthropic, y compra en un comercio externo
por el protocolo ACP: una política de compra decide fuera del modelo y nada se paga sin aprobación.

El comercio ([`tesis-commerce-merchant`](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant))
y el proveedor de pagos sandbox
([`tesis-payment-provider-sandbox`](https://github.com/Tesis-Agentes-Comerciales/tesis-payment-provider-sandbox))
son repositorios y procesos aparte: este repositorio nunca importa su código ni toca su base de
datos. Todo es sandbox: no hay dinero ni tarjetas reales.

## Probar la demo

### Requisitos

- macOS o Linux.
- Git.
- Python 3.12 con [uv](https://docs.astral.sh/uv/).
- Node.js 24.15 o posterior.
- Docker abierto (Docker Desktop, OrbStack o el daemon en Linux): el comercio guarda su catálogo
  en PostgreSQL, dentro de Docker (puerto 55433).
- Los puertos 4120 (comercio), 4130 (proveedor de pagos), 4210 (cuentas), 43120 (agente) y 5173
  (interfaz) libres.
- Una clave de la API de Anthropic con saldo: la demo conversa con el modelo de verdad.

### Pasos

Los tres repositorios van lado a lado, con estos nombres exactos:

```sh
git clone https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent.git
git clone https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant.git
git clone https://github.com/Tesis-Agentes-Comerciales/tesis-payment-provider-sandbox.git
cd tesis-commerce-agent
```

Instala las dependencias de los tres, del servicio de cuentas y de la interfaz:

```sh
uv sync --frozen --all-groups
(cd ../tesis-commerce-merchant && uv sync --frozen --all-groups)
(cd ../tesis-payment-provider-sandbox && uv sync --frozen --all-groups)
(cd auth && npm ci) && (cd ui && npm ci)
```

Copia la configuración y pon tu clave en `ANTHROPIC_API_KEY`; es lo único que hay que llenar:

```sh
cp .env.example .env
```

Revisa los requisitos y arranca:

```sh
uv run demo-stack --dry-run
uv run demo-stack
```

`--dry-run` comprueba los repositorios hermanos, Docker, `npm ci`, la clave y los puertos sin
arrancar nada, y dice qué falta. `uv run demo-stack` levanta el PostgreSQL del comercio, migra,
carga el catálogo y arranca el proveedor de pagos, el comercio, el servicio de cuentas, el agente
y la interfaz. Las claves y los tokens se generan en cada arranque y no se imprimen ni se guardan,
y ningún servicio lee el `.env` de los repositorios hermanos.
Ctrl-C apaga todo; PostgreSQL sigue corriendo con sus datos.

Abre <http://127.0.0.1:5173>, crea una cuenta local con «Crear cuenta» (cualquier correo y una
contraseña de al menos 8 caracteres; el correo no se verifica) y haz un pedido, por ejemplo:

```text
Quiero comprar exactamente un Google Pixel 8 de 128 GB, negro y nuevo. Mi presupuesto máximo es 1.700.000 COP.
```

El agente busca en el catálogo, elige, prepara el checkout en el comercio y muestra «Aprueba la
compra» con el total. Solo se compra al pulsar «Aprobar y comprar», y el pago es sintético. La
fila de actividad de cada mensaje se abre para ver los controles por los que pasó la compra.

### Modos

| Comando | Qué muestra |
| --- | --- |
| `uv run demo-stack` | Con gobernanza (es `--mode b2`, el modo por defecto): la política revisa la elección antes de crear el checkout y otra vez al aprobar, antes de firmar el pago. |
| `uv run demo-stack --mode b0` | El mismo agente sin gobernanza, para comparar. |
| `uv run demo-stack --mode pol02-deny` | Con gobernanza, pero la lista de comercios permitidos deja fuera al de la demo: la política niega la compra antes de crear el checkout y la interfaz muestra por qué. |
| `uv run demo-stack --mode b2-hnp` | Con gobernanza y además compra delegada: con «Delegar» la persona deja autorizada una compra con sus límites y el agente la hace unos segundos después, sin que ella intervenga. |

Para ver qué pasa si el comercio cambia el precio: con «Aprueba la compra» en pantalla y sin
aprobar, corre `uv run demo-stack s04` en otra terminal. Al aprobar, el agente nota que el precio
ya no es el que viste, no cobra y pide cancelar y volver a pedir; la compra nueva sale con el
precio nuevo, que todavía cabe en el presupuesto del ejemplo. `uv run demo-stack s04 --restore`
devuelve el precio original. El nombre `s04` viene del escenario de cambio de precio de la
evaluación. Todas las opciones están en `uv run demo-stack --help`.

## Qué hay en el repo

| Carpeta | Contenido |
| --- | --- |
| `src/commerce_agent/` | El agente: conversación con Claude, cliente ACP, mandatos AP2, política de compra (`governance/`), persistencia SQLite, grafo LangGraph y API FastAPI. Es lo único que entra al paquete. |
| `evaluation/` | Los experimentos: la suite de evaluación (`eval-suite`), el lanzador de la demo (`demo-stack`) y los datos y resultados. Depende del agente; el agente nunca lo importa. Ver [evaluation/README.md](evaluation/README.md). |
| `tests/` | Las pruebas del agente y de la evaluación. `tests/integration/` es la suite de sistema. |
| `ui/` | Interfaz web (React, Vite y assistant-ui) con Storybook y pruebas E2E. Ver [ui/README.md](ui/README.md). |
| `auth/` | Servicio de cuentas (Better Auth, correo y contraseña) que emite los JWT del comprador. |
| `scripts/` | La compuerta local (`check.sh`), los exportadores de fixtures, una medición offline de la política y, en `tools/`, los generadores de diagramas. |
| `docs/` | Diseño, contrato entre la interfaz y el agente, perfil AP2 y estrategia de pruebas; el índice está en [docs/README.md](docs/README.md). |

Algunos módulos de `src/` son experimentales y solo los usan las pruebas o la evaluación, como
`d2_consent_api.py` (una API de consentimiento que la API principal no monta) y la creación
guardada del checkout (`governance/guarded_creation.py`).

## Pruebas

```sh
scripts/check.sh
```

Es la compuerta local de cada cambio; no hay CI alojado. Corre `uv lock --check`,
`ruff format --check`, `ruff check`, `pyright`, revisa que el wheel lleve solo el agente, corre
`pytest` (unitarias, contratos y la evaluación sin modelo) con un piso de cobertura del 85 % para
el producto, las pruebas de `auth/` y el lint y el build de `ui/`. No usa servicios externos ni
el modelo. En `ui/` hay además `npm run test-storybook` y `npm run e2e`, que necesitan el
Chromium de Playwright.

La suite de sistema es opcional: levanta PostgreSQL en Docker, el comercio y el proveedor de pagos
reales desde los repositorios hermanos, el agente, `auth/` y la interfaz, y tarda bastante más que
la compuerta (`-m "not slow"` deja fuera las pruebas más lentas):

```sh
uv run playwright install --only-shell chromium
RUN_LIVE_STACK=1 MERCHANT_REPO="$PWD/../tesis-commerce-merchant" \
  PAYMENT_REPO="$PWD/../tesis-payment-provider-sandbox" uv run pytest tests/integration -rs
```

Requisitos y evidencia en [tests/integration/README.md](tests/integration/README.md); capas,
marcadores y cobertura en [docs/testing.md](docs/testing.md).

## Evaluación

La suite de evaluación mide el agente sobre casos versionados: la regresión no usa el modelo, y
capacidad y adversarial conversan con un usuario simulado. Estos dos comandos muestran el plan
(casos, intentos y costo estimado) sin llamar al modelo:

```sh
uv run eval-suite run --suite regression --dry-run
uv run eval-suite run --suite capability --dry-run
```

Las corridas reales de capacidad y adversarial, y `uv run eval-suite judge`, llaman al modelo:
exigen un tope de gasto con `--max-usd` y una `ANTHROPIC_API_KEY`. Los resultados de cada corrida
quedan en `evaluation/results/suite/`, y el protocolo, los datos y cómo leerlos están en
[evaluation/README.md](evaluation/README.md). Los pilotos de gobernanza de octubre corrieron con
gpt-5-mini; sus datos y resultados también están en `evaluation/`.

## Para quien desarrolla

### Cómo funciona

```text
UI → FastAPI → LangGraph (hilo y checkpoints en SQLite)
                interpretar → responder | aclarar | buscar en el catálogo
                                               buscar → filtrar y ordenar → elegir
                                                                              ↓
                          política de compra → checkout ACP en el comercio → pausa
                                                                              ↓
                          aprobación de la persona (REST) → política → pago delegado → orden
```

- El modelo recibe los mensajes y el vocabulario validado del feed del comercio. Decide si
  responde, aclara o busca, y elige entre candidatos que ya pasaron filtros deterministas. Su
  única herramienta, `lookup_catalog`, solo lee el catálogo: no crea checkouts ni ve tokens,
  pagos, datos de entrega ni la aprobación.
- El checkout del comercio es la autoridad de precio y stock. La aprobación queda ligada a los
  términos exactos (comercio, líneas, total, entrega y vencimiento), y el pago lo hace el
  proveedor sandbox con un token delegado, fuera del modelo.
- En el arranque manual, la política de compra (`AGENT_GOVERNANCE_MODE`) y la compra delegada
  (`AGENT_HNP_ENABLED`) están apagadas por defecto; `demo-stack` enciende la política salvo con
  `--mode b0`.
- Límites deliberados: un solo comercio registrado por configuración, una moneda, cantidad uno,
  SQLite y pago sandbox. La matriz de contrato, amenazas e hipótesis está en
  [docs/compatibility.md](docs/compatibility.md).

### Configuración (`.env`)

`.env.example` tiene dos bloques: lo que necesita la demo (la clave y, si quieres, el modelo y su
esfuerzo) y el arranque manual completo, con un comentario por variable. Lo principal:

- `ANTHROPIC_API_KEY`: sin ella no hay modelo. `AGENT_MODEL` (por defecto `claude-haiku-5-5`; el
  mismo código corre `claude-sonnet-5-5`) y `AGENT_MODEL_EFFORT` (por defecto `low`).
- `MERCHANT_BASE_URL`, `MERCHANT_BEARER_TOKEN` y `MERCHANT_WEBHOOK_SECRET`: el comercio, la sesión
  que emite su CLI y el secreto de sus webhooks. HTTP solo se admite en loopback.
- `PAYMENT_PROVIDER_URL` y `PAYMENT_AGENT_BEARER_TOKEN`: el proveedor de pagos y la credencial
  del agente ante él.
- `AGENT_APPROVAL_SECRET`: el secreto que liga la aprobación a los términos.
- `AGENT_AUTH_MODE`: `local` (por defecto, el comprador es `AGENT_PRINCIPAL_ID`) o `jwt` (valida
  los JWT de `auth/`).
- `AGENT_GOVERNANCE_MODE`, `AGENT_HP_FLOW` y `AGENT_D1_*`: la política de compra; ver los
  comentarios de `.env.example`.

Los secretos llevan valores aleatorios de al menos 32 caracteres y nunca van a Git, logs, prompts
ni fixtures.

### Arranque manual

Para levantar cada servicio por separado, por ejemplo con otra configuración:

1. **Comercio**, en su repositorio (detalles en su README):

   ```sh
   docker compose up -d --wait postgres
   uv run python -m commerce_lab.db migrate
   uv run python -m commerce_lab.managed_catalog import-current   # imprime el run_id
   uv run python -m commerce_lab.db issue-session <run_id> merchant-agent
   uv run uvicorn commerce_lab.api:app --host 127.0.0.1 --port 4120
   ```

   `issue-session` imprime una sesión de una hora (`--ttl-seconds` la cambia) para
   `MERCHANT_BEARER_TOKEN`.
2. **Proveedor de pagos**, según su README, en `http://127.0.0.1:4130`.
3. **Agente**: `cp -n .env.example .env`, completar el segundo bloque y arrancar la
   [API](#api-del-agente).
4. **Cuentas**: `cd auth && npm ci && cp -n .env.example .env`, poner un `BETTER_AUTH_SECRET`
   aleatorio de al menos 32 caracteres y `node --env-file=.env server.js` (puerto 4210; Vite
   reenvía `/api/auth` allí).
5. **Interfaz**: ver [Interfaz web](#interfaz-web).

Variables que deben coincidir entre los servicios:

- `MERCHANT_BEARER_TOKEN` del agente es la sesión que imprime `issue-session`.
- `MERCHANT_WEBHOOK_SECRET` es igual en agente y comercio; `WEBHOOK_RECEIVER_URL` del comercio es
  solo el origen del agente (`http://127.0.0.1:43120`).
- `ACP_API_BASE_URL` del comercio es `MERCHANT_BASE_URL` del agente, y `PAYMENT_PROVIDER_URL` es
  el mismo en los dos.
- `PAYMENT_AGENT_BEARER_TOKEN` es igual en agente y proveedor; `PAYMENT_MERCHANT_BEARER_TOKEN` y
  `PAYMENT_MERCHANT_ID`, en comercio y proveedor. `PAYMENT_TOKEN_SECRET` solo existe en el
  proveedor.
- Con AP2, los tres servicios lo activan a la vez (`*_AP2_REQUIRED=true`), cada uno con su clave
  privada EC P-256; el comercio fija la clave pública del agente y el proveedor las del agente y
  del comercio, con `PAYMENT_AP2_MERCHANT_ISSUER` igual a `ACP_API_BASE_URL`.
- Con `AGENT_AUTH_MODE=jwt`, `AGENT_AUTH_ISSUER` es el `BETTER_AUTH_URL` de `auth/`: el origen de
  la interfaz.
- `ANTHROPIC_API_KEY`, `AGENT_MODEL` y `AGENT_APPROVAL_SECRET` viven solo en el agente.

### API del agente

Con el comercio y su feed listos:

```sh
uv run uvicorn commerce_agent.api:app_factory --factory --host 127.0.0.1 --port 43120
```

1. `POST /conversation/turn` recibe un `turn_id` idempotente, un `conversation_id` opcional,
   mensajes y datos de entrega. Una respuesta o aclaración no contacta al comercio; una elección
   válida prepara el checkout y devuelve los términos en la misma operación. Si ningún producto
   pasa los filtros, el modelo explica alternativas reales del catálogo.
2. `GET /purchases/{run_id}/approval` vuelve a consultar los términos exactos.
3. `POST /purchases/{run_id}/approval` recibe el fingerprint mostrado y, con AP2, el Checkout JWT
   de esos términos; el actor viene del backend. El servidor verifica otra vez el checkout y firma
   los mandatos fuera del modelo.
4. `POST /purchases/{run_id}/cancel` cancela un checkout que aún espera aprobación.
5. `POST /purchases/{run_id}/complete` ejecuta el pago de forma determinista.
6. `GET /purchases/{run_id}` consulta el estado local; `GET /purchases/{run_id}/trace`, la traza
   de controles, y `GET /me/purchases`, el historial del comprador, sin tokens ni mandatos.
7. `POST /agentic_checkout/webhooks/order_events` recibe los eventos firmados del comercio.
8. `POST /assistant` expone el mismo flujo como stream para la interfaz; el contrato está en
   [docs/agent-ui-contract.md](docs/agent-ui-contract.md).

`complete` responde conflicto si la compra no está aprobada, y dos intentos a la vez no pueden
tomar la misma reserva. Un timeout después de enviar el pago deja la compra `uncertain`: el
siguiente intento consulta primero el checkout y conserva la misma clave idempotente. Los fixtures
SSE de Storybook se regeneran con `uv run python scripts/export_assistant_fixtures.py`, y una
prueba falla si los versionados quedan desactualizados.

### Interfaz web

La interfaz vive en `ui/` y solo habla con la API del agente: ningún secreto del modelo, del
comercio o del pago llega al navegador.

```sh
cd ui
cp -n .env.example .env.local
npm ci
npm run dev
```

Abre <http://127.0.0.1:5173> con la API del agente y `auth/` activos. El perfil de entrega es
sintético y editable, y se guarda en el navegador por cuenta. Storybook y pruebas en
[ui/README.md](ui/README.md).

### LangGraph Studio

Studio muestra los nodos, las rutas y el estado del mismo grafo, en un servidor local aparte de
FastAPI y con sus propios hilos. Es de solo lectura: explora el catálogo y propone una selección,
pero nunca prepara un checkout ni completa una compra. Carga el feed al iniciar; reinícialo para
ver cambios del comercio. Con `ANTHROPIC_API_KEY` y el comercio en `.env`:

```sh
LANGSMITH_TRACING=false uv run langgraph dev --no-browser --host 127.0.0.1 --port 2024
```

Abre [LangGraph Studio](https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024), elige
`commerce_agent` y envía, por ejemplo:

```json
{
  "incoming_messages": [{"role": "user", "content": "¿Qué teléfonos Pixel tienes?"}],
  "turn_id": "demo-1"
}
```

Para otro turno en el mismo hilo, envía solo el mensaje nuevo con otro `turn_id`. No expongas el
servidor fuera de `127.0.0.1`; `LANGSMITH_TRACING=false` evita subir trazas.
