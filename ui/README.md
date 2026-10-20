# Commerce Agent UI

Interfaz React + Vite para el buyer agent, construida con [`assistant-ui`](https://www.assistant-ui.com/).
El backend es dueño del estado: la UI usa el runtime `useAssistantTransportRuntime` contra
`POST /assistant` y solo dibuja el estado que llega por SSE. El contrato está en
[`docs/agent-ui-contract.md`](../docs/agent-ui-contract.md).

## Desarrollo

Desde este directorio:

```sh
cp -n .env.example .env.local
npm install
npm run dev
```

La aplicación queda en `http://127.0.0.1:5173` y espera la API del agente en
`http://127.0.0.1:43120`. `VITE_AGENT_API_URL` permite cambiar ese origen.

## Runtime

- `src/features/commerce-runtime.tsx` monta el runtime: `api` = `AGENT_API_URL + "/assistant"`,
  `protocol: "assistant-transport"`, estado inicial `{messages: [], purchase: null}`, un `converter`
  que pasa `state.messages` al thread (más los `add-message` en cola como mensajes optimistas) y un
  `body` que agrega los datos de entrega y un `turn_id` nuevo por envío. Los errores de transporte se
  traducen a mensajes en español; el texto crudo del backend nunca se muestra.
  `AgentRuntime` está memoizado a propósito: un re-render del componente que llama al hook reinicia
  el thread, así que todas sus props deben ser estables (el perfil se lee por referencia).
- `src/features/agent-toolkit.ts` registra un renderer `type: "backend"` por cada paso del backend.
  `select_product` y `request_approval` se muestran destacados; el resto, junto con el razonamiento,
  se agrupa en el bloque plegable "Pensando…" del thread.
- `src/features/purchase-approval.tsx` conecta la tarjeta de aprobación con los endpoints REST
  (`/purchases/{run_id}/approval`, `/complete`, `/cancel`) y después envía el comando propio
  `purchase-updated`. Ningún comando del transporte lleva evidencia de aprobación: eso sigue solo por
  REST, con el fingerprint que devolvió el backend.
- Los `result` de cada paso se validan antes de dibujarlos (`src/features/agent-results.ts`): el feed
  y las respuestas ACP no son confiables.

## Storybook

```sh
npm run storybook
```

Queda en `http://127.0.0.1:6006` y no necesita la API del agente ni el servicio de acceso.
`.storybook/preview.tsx` intercepta `fetch` hacia `VITE_AGENT_API_URL` con las rutas de
`src/storybook/default-agent-api.ts`; cada historia puede sobrescribir rutas con el parámetro
`agentApi`, y una ruta sin mock responde 501 en vez de tocar un backend real. Better Auth tampoco se
carga: `sb.mock` cambia `src/lib/auth-client.ts` por `src/lib/__mocks__/auth-client.ts` y el parámetro
`session` (`authenticated` por defecto, `anonymous` o `pending`) elige qué devuelve
(`src/storybook/session.ts`).

- `Pages/App` monta la app completa. `Online` es interactiva: saluda, pide detalles, prepara una
  compra o recuerda la pendiente según el texto. Las demás historias (`PurchaseFlow`, `CancelFlow`,
  `NoCandidate`, `PendingPurchase`, `BackendError`, `RequestRejected`, `Offline`,
  `CompletionFails`) recorren el flujo con `play`.
- `Chat/Thread` congela momentos de un stream real: `src/storybook/assistant-transport.ts` aplica los
  frames de un fixture hasta un marcador y entrega ese estado como `initialState`, sin
  temporizadores.
- `src/storybook/fixtures/assistant-transport/*.sse` son los streams reales del backend
  (`uv run python scripts/export_assistant_fixtures.py`); son de solo lectura desde la UI. El mock los
  reproduce frame por frame (`sse` y `frameDelayMs` en `agent-api-mock.ts`) y solo reemplaza las
  imágenes del feed por SVG locales para que ninguna historia pida recursos externos.
- `Agent/*` y `Purchase/*` cubren los componentes sueltos y los datos de entrega.
- `Trace/*` cubre las primitivas de la traza de compra (`src/components/trace/`) con las fixtures de
  `src/storybook/trace-fixtures/`, una por escenario. `Trace/Contract` valida cada JSON de esa carpeta
  con `parsePurchaseTrace` (`src/lib/trace-contract.ts`); el modelo Pydantic del backend validará los
  mismos archivos ([`docs/design/trace-panel.md`](../docs/design/trace-panel.md)).
- `Auth/SessionGate` cubre la puerta de sesión: pendiente, sin sesión (el doble rechaza las
  credenciales), registro, servicio caído y con sesión.

`npm run build-storybook` genera la versión estática. Ningún módulo de `src/storybook/` ni de
`src/lib/__mocks__/` entra en el bundle de producción.

`npm run test-storybook` corre cada historia como un test de Vitest en Chromium headless
(`@storybook/addon-vitest`, `vitest.config.ts`): render, `play` y axe de `addon-a11y`. Cualquier
violación de accesibilidad hace fallar la historia (`a11y.test: "error"`); una historia solo puede
bajarla a `"todo"` con una razón escrita. No necesita Storybook levantado. La primera vez hace falta el
navegador de Playwright: `npx playwright install chromium` (en Linux/CI, `--with-deps`).

## Verificación

```sh
npx tsc -b
npm run lint
npm run build
npm run build-storybook
npm run test-storybook
npm run e2e
```

`npm run e2e` corre los tests de Playwright de `e2e/` contra el build de producción
(`playwright.config.ts` lo genera en `dist-e2e/` y lo sirve con `vite preview`). La red de Better
Auth y de la API del agente se simula en cada spec con la sesión y los datos de
`src/storybook/fixtures.ts`; `POST /assistant` reproduce los mismos streams SSE de Storybook. `e2e/chat.spec.ts` recorre compra, aprobación y completion, revisa que el cuerpo lleve
`details` y `turn_id`, que la aprobación solo viaje por REST con el fingerprint de los términos y
que en móvil no haya scroll horizontal. Usa un puerto propio (5190) y el mismo Chromium que
`test-storybook`.

La UI no contiene llaves ni tokens. Los datos de entrega se envían a FastAPI, y el único control que
autoriza la compra usa el fingerprint de términos que devuelve el backend.
