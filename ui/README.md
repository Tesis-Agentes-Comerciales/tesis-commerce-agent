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
- Términos que ya no valen (S04, `src/features/stale-terms.ts`): si aprobar responde 409
  `checkout_terms_changed`, `offer_expired` o `checkout_expired`, o 503
  `offer_freshness_unavailable`, si `request_approval` falla con esos códigos o si
  `GET /me/pending-purchase` trae `terms_status` (`changed`, `expired` o `unavailable`), la tarjeta
  de aprobación cede su lugar a `StaleTermsCard`: dice que no se hizo ningún cobro y deja solo
  «Cancelar compra». Después de cancelar, el comprador pide la compra de nuevo en el chat y obtiene
  otro checkout.
- `src/features/purchase-trace.ts` relee `GET /purchases/{run_id}/trace` para la compra de cada
  mensaje: al aparecer el `run_id`, al terminar cada paso, al cerrar el mensaje y después de aprobar,
  completar o cancelar. Con la traza, el mensaje muestra la fila de actividad (`PurchaseActivity`),
  la tarjeta de bloqueo (`PolicyBlockedCard`) en lugar del aviso rojo y el recibo de la tarjeta de
  aprobación. Un 404 o un 503 no rompen el chat: la fila no aparece y queda el aviso del mensaje.
  La línea de tiempo también muestra dos controles de la aplicación del flujo gobernado, nunca como
  política: la frescura de la oferta (qué cambió entre el checkout y la oferta actual) y la
  recuperación gobernada (por qué el agente no siguió).
- Los `result` de cada paso se validan antes de dibujarlos (`src/features/agent-results.ts`): el feed
  y las respuestas ACP no son confiables.
- Delegaciones (Human Not Present, HNP-10): el selector «Modo de compra» de la cabecera
  (`src/components/purchase-mode-switch.tsx`, radios nativos: Tab y flechas) solo aparece si
  `GET /health` trae `hnp_enabled: true`; apagado, la cabecera es la de antes y la UI no llama a
  ninguna ruta de delegaciones. «Comprar conmigo» es el chat (HP); «Delegar» abre el diálogo
  «Delegaciones» de `src/features/delegations.tsx`, dentro del runtime, y al cerrarlo se vuelve a
  «Comprar conmigo». El diálogo trae el formulario (las opciones son las que el chat ya mostró en
  `search_catalog`; sin ninguna, una guía lleva de vuelta al chat a buscar; el total máximo lo
  escribe el comprador, §3.2), la propuesta con su consentimiento y «Mis delegaciones» con
  «Revocar» y «Revocar todo», contra las rutas del comprador (`src/lib/delegation-api.ts`,
  [`docs/design/hnp-governed.md`](../docs/design/hnp-governed.md) §5.1). Cada acción relee lo que
  cambió; el destino viaja tal como lo escribió el comprador, y los errores `{code, message}`
  muestran el `message` del backend salvo `delegation_destination_unsupported`, que la UI explica.
  «Comprar desde el chat» de un escalamiento escribe la compra HP en el compositor.
- Gobernanza de una compra delegada: la tarjeta de una delegación cuya tarea ya tiene `run_id`
  muestra «Gobernanza de la compra», la misma fila de actividad del chat con la línea de tiempo HNP
  (`GET /purchases/{run_id}/trace`, `src/features/delegation-traces.ts`). Con el diálogo abierto,
  la lista y las trazas se releen cada 2 s mientras una tarea está programada o en curso o una
  traza no terminó; al cerrarlo se aborta lo que iba en camino y no queda ningún temporizador. Una
  lectura que falla conserva la traza anterior o no muestra la gobernanza.

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
  `CompletionFails`, `TermsChangedOnApprove`, `TermsUnavailableOnApprove`, `RestoredTermsChanged`,
  `RestoredTermsUnavailable`) recorren el flujo con `play`.
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
- `Delegation/*` cubre las piezas de Human Not Present (`src/components/delegation/`): el
  formulario, el consentimiento de la propuesta, el estado de cada delegación y su tarea, el
  escalamiento «Necesito tu decisión», el recibo de revocación y «Mis delegaciones», con las
  fixtures de `src/storybook/fixtures/delegation-*.json` y las secuencias reales de la API de
  `src/storybook/fixtures/delegation-api/` (`uv run python scripts/export_delegation_replays.py`;
  `tests/test_delegation_replay.py` falla si se desactualizan). Los mensajes llegan escritos del
  backend y se muestran tal cual ([`docs/design/hnp-governed.md`](../docs/design/hnp-governed.md)
  §8.3).
  `Delegation/Contract` valida esas fixtures con los `parseDelegation*` de
  `src/lib/delegation-contract.ts`.
- `Auth/SessionGate` cubre la puerta de sesión: pendiente, sin sesión (el doble rechaza las
  credenciales), registro, servicio caído y con sesión.

`npm run build-storybook` genera la versión estática. Ningún módulo de `src/storybook/` ni de
`src/lib/__mocks__/` entra en el bundle de producción.

`npm run test-storybook` corre cada historia como un test de Vitest en Chromium headless
(`@storybook/addon-vitest`, `vitest.config.ts`): render, `play` y axe de `addon-a11y`. Cualquier
violación de accesibilidad hace fallar la historia (`a11y.test: "error"`); una historia solo puede
bajarla a `"todo"` con una razón escrita. No necesita Storybook levantado. La primera vez hace falta el
navegador de Playwright: `npx playwright install chromium` (en Linux/CI, `--with-deps`). También corre
el proyecto `unit` de Vitest (`src/**/*.test.ts` en Node; solo él con `npm test`), así el job `ui` de
CI lo ejecuta sin un paso nuevo.

La UI usa la fuente del sistema (`ui-sans-serif, system-ui, sans-serif`): SF Pro en macOS y DejaVu
Sans, bastante más ancha, en el Chromium del CI (Linux). Las historias «Phone 320» miden contra el
ancho real de un teléfono de 320 px (272 px para una tarjeta del hilo, 254 para una del diálogo
«Delegaciones»), así que un monto que cabe justo en macOS puede desbordar en CI: un texto sin
espacios necesita un tamaño que baje con la tarjeta o un punto de corte (`totalAmount` en
`agent-styles.ts`).

## Verificación

```sh
npx tsc -b
npm run lint
npm test
npm run build
npm run build-storybook
npm run test-storybook
npm run e2e
```

`npm run e2e` corre los tests de Playwright de `e2e/` contra el build de producción
(`playwright.config.ts` lo genera en `dist-e2e/` y lo sirve con `vite preview`). La red de Better
Auth y de la API del agente se simula en cada spec con la sesión y los datos de
`src/storybook/fixtures.ts`; `POST /assistant` reproduce los mismos streams SSE de Storybook y la
traza responde con las fixtures de `src/storybook/trace-fixtures/`. `e2e/chat.spec.ts` recorre compra,
aprobación y completion, revisa que el cuerpo lleve `details` y `turn_id`, que la aprobación solo
viaje por REST con el fingerprint de los términos y que en móvil no haya scroll horizontal. También
cubre la actividad de la compra: política permitida con sus dos reglas, bloqueo por el comercio con
su tarjeta y sin «error inesperado», política desactivada y una traza que responde 404 o 503. Y los
términos que ya no valen: aprobar responde 409 o 503, se cancela y se pide de nuevo, y una compra
restaurada con `terms_status` se puede cancelar. `e2e/delegations.spec.ts` responde cada ruta de
delegaciones con las secuencias de `src/storybook/fixtures/delegation-api/`, en orden: propuesta y
consentimiento (el destino viaja tal como se escribió), 409 de alcance distinto y de propuesta
vencida, 422 de destino no admitido, revocación con sus tres efectos, 409 de una delegación que ya
terminó, «Revocar todo», «Comprar desde el chat», HNP apagado y el diálogo a 320 px. Usa un puerto
propio (5190) y el mismo Chromium que `test-storybook`.

La UI no contiene llaves ni tokens. Los datos de entrega se envían a FastAPI, y el único control que
autoriza la compra usa el fingerprint de términos que devuelve el backend.
