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

## Monitor de gobernanza (opt-in)

`/governance` muestra en solo lectura evidencia histórica sanitizada de gobernanza HP/HNP (MON-02A/B).
Solo existe en builds con `VITE_GOVERNANCE_MONITOR=true`: con cualquier otro valor o sin la variable,
su código no entra en el bundle y `/governance` muestra la app normal. Activo, pasa por la misma puerta
de sesión de Better Auth que la app (`src/components/session-gate.tsx`): sin sesión muestra el
formulario de acceso y nunca la evidencia. No importa el runtime comercial ni el SSE.

```sh
VITE_GOVERNANCE_MONITOR=true npm run dev
```

`VITE_GOVERNANCE_SNAPSHOT_PORT` (5183 por defecto) fija el puerto del snapshot local que lee el monitor.
`npm run governance:demo` levanta la demo completa (snapshot, Better Auth y preview en
`http://127.0.0.1:5182/governance`) y la detiene con Ctrl+C; necesita `npm ci` también en `../auth`.
La reproducción, el servidor local de snapshots y sus variables están en
[`docs/governance-monitor/README.md`](../docs/governance-monitor/README.md).

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
- `Auth/SessionGate` cubre la puerta de sesión: pendiente, sin sesión (el doble rechaza las
  credenciales), registro, servicio caído y con sesión.
- `Governance/*` cubre el monitor con la proyección versionada (`evidence.json`, vía
  `src/storybook/governance.ts`): escenarios HP/HNP-01, el experimento HNP-02 en dry-run con sus dos
  avisos, cada estado de la fuente (offline, leyendo, leído, obsoleto, desconectado, inconsistente),
  la ruta con y sin sesión y el error de contrato. Los estados inconsistentes son mutaciones en
  memoria, las mismas de `tests/`. `SourceMonitor` lee un servidor de snapshots simulado: el
  parámetro `governanceSnapshot` da la respuesta de cada lectura (`snapshot`, `altered`, `rejected`,
  `unreachable`) y por defecto no hay servidor.

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
npm run test
npm run build-storybook
npm run test-storybook
npm run e2e
```

`npm run test` ejecuta `tests/*.test.*` con `node --test` (Node 24 quita los tipos de los `.ts`; un hook
transpila los `.tsx` y resuelve `@/`). No necesita build previo, red ni archivos bajo `.local`; el CI lo
corre después del build y comprueba que el build por defecto no incluya el monitor.

`npm run e2e` corre los tests de Playwright de `e2e/` contra el build de producción
(`playwright.config.ts` lo genera en `dist-e2e/` con el monitor activo y lo sirve con
`vite preview`). La red de Better Auth y de la API del agente se simula en cada spec con la sesión
y los datos de `src/storybook/fixtures.ts`; `POST /assistant` reproduce los mismos streams SSE de
Storybook. `e2e/chat.spec.ts` recorre compra, aprobación y completion, revisa que el cuerpo lleve
`details` y `turn_id`, que la aprobación solo viaje por REST con el fingerprint de los términos y
que en móvil no haya scroll horizontal. `e2e/governance.spec.ts` abre `/governance` con y sin
sesión y lee el snapshot del servidor real (`scripts/governance_snapshot_server.mjs`, levantado
con su fixture en `.local/governance-e2e/`), incluida la lectura obsoleta y la fuente desconectada.
Usa puertos propios (5190 y 5191) y el mismo Chromium que `test-storybook`.

La UI no contiene llaves ni tokens. Los datos de entrega se envían a FastAPI, y el único control que
autoriza la compra usa el fingerprint de términos que devuelve el backend.
