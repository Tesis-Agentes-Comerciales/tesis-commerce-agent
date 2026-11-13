# Suite viva portable (opt-in)

Es la capa 4 de [`docs/testing.md`](../../docs/testing.md): la única suite de sistema del Agent.
Estos módulos levantan un stack propio y desechable en cada ejecución: PostgreSQL en Docker con
la misma imagen y digest que el CI del Merchant (`postgres:18.4-alpine@sha256:9a8a…5de15`), el
Merchant real (`uvicorn commerce_lab.api:app`, lanzado con `uv run --project $MERCHANT_REPO` y
su propio intérprete y lock), el proveedor de pagos real (`tesis_payment_provider`, desde
`$PAYMENT_REPO`) y procesos del Agent de este checkout, incluidos su servicio de autenticación
(`auth/`) y su UI (`ui/`) en las pruebas del navegador. Todo el tráfico del Agent al Merchant
pasa por HTTP real de loopback a través de `FaultProxy`, que es transparente salvo cuando una
prueba arma un fallo etiquetado. La suite normal y `ci.yml` los omiten; el workflow
`nightly.yml` los ejecuta cada noche y a pedido.

El stack vive en [`commerce_evaluation.stack`](../../evaluation/src/commerce_evaluation/stack)
([piezas](#piezas)), porque el corredor de la suite de evaluación arranca con él un entorno
limpio por intento ([spec](../../docs/design/eval-suite.md), §5). Aquí quedan las pruebas, sus
fixtures y los ayudantes que solo ellas usan.

Hay cinco grupos sobre el mismo PostgreSQL:

- Gobernanza (06D-C, 07A, 07B-B, 07B-P): el Merchant del stack detrás de su proxy, con fallos
  inyectados.
- Compras (`test_live_purchase.py`): carriles con su propio Merchant, proxy y proveedor de
  pagos. `b0` es la línea base B0 (ACP + AP2 Human Present: mandatos y recibos firmados,
  webhooks de orden); `acp` es la finalización ACP sin AP2 y la API conversacional. Reemplazan
  al E2E conjunto de septiembre ([equivalencias](#equivalencia-con-el-e2e-conjunto)).
- B2 gobernado (`test_live_b2_governed_purchase.py`): sobre el carril `b0`, la compra con
  `AGENT_GOVERNANCE_MODE=enforce` y `AGENT_HP_FLOW=governed`. El gate sale del bootstrap de
  producción y crea el checkout con un solo POST; otra prueba deja sin stock la variante en el
  Merchant después de exportar el feed, de modo que su 409 `out_of_stock` al único POST termina la
  compra en `failed` sin dejarla pendiente y la compra siguiente crea su checkout; la última arma
  el runtime como el piloto, llega a la orden con los recibos AP2 y corre el dispatcher del
  comercio: el `order_create` firmado llega a `webhook_events` del runtime y a su traza, y
  `verify-evidence` (con la base del PSP) da la cadena de evidencia completa.
  `test_live_b2_pol02_allowlist.py` arma el Agent con `build_runtime` y las variables de la
  condición B2 del piloto en el entorno (las mismas que exporta el lanzador de la demo) y corre el mismo turno de compra con dos listas
  de comercios de POL-02: con otro comercio (modo `pol02-deny`) termina en `d1_denied`, POL-02
  queda en `fail` con `ACTION_OR_MERCHANT_NOT_ALLOWED` y el Merchant no recibe ningún POST de
  creación; con el comercio del carril llega a `awaiting_confirmation` con un solo POST.
  `test_live_b2_pol11_budget.py` arma el Agent igual, con la política `b2-hp-v2` del modo `b2`
  de la demo, y pide el teléfono con un presupuesto total menor que su precio de catálogo:
  termina en `d1_denied`, POL-11 queda en `fail` con `BUDGET_NOT_SATISFIABLE` y los dos montos,
  y el Merchant no recibe ningún POST de creación.
  `test_live_b2_d2_authorization.py` arma el Agent igual (`b2-hp-v2`, enforce, flujo gobernado,
  AP2) y cubre D2-HP al aprobar: en S01 la aprobación pasa por la frescura, D2 (POL-03..06 en
  `allow`) y la firma del mandato; la fila D2 de `governance_evidence` y
  `approvals.authorization_evidence_id` son la misma evidencia, la orden queda confirmada y la
  evidencia AP2 guarda `consent` y luego `effect`; la traza muestra el paso
  `authorization_decision` (`allow`) entre la aprobación y la autorización firmada. En S04 el
  Merchant cambia el envío de la oferta después de mostrar los términos: la relectura y una
  aprobación enviada igual terminan en `checkout_terms_changed` antes de D2, sin ninguna fila D2
  ni rechazo D2 para el chat, y una segunda compra del mismo comprador pasa D2 y llega a la
  orden.
- Human Not Present (`test_live_hnp.py`, HNP-I1 de
  [`docs/design/hnp-governed.md`](../../docs/design/hnp-governed.md)): el carril `hnp` tiene el
  Merchant y el proveedor AP2 de `b0`, este también detrás de un `FaultProxy` (solo para el
  Agent; el Merchant le habla directo), y cada prueba levanta su propio Agent con
  `AGENT_HNP_ENABLED` en un proceso aparte (`HnpAgent`: `create_app` de producción desde sus
  variables, selector determinista, sin modelo), en la condición B0-HNP (`off`) o B2-HNP
  (`enforce`). El comprador propone y consiente por la API con su token y el token de servicio
  dispara la tarea. Cubre la compra con los dos recibos y la cadena de `verify-evidence`
  (destino «Bogotá» con tilde y los montos con «,00» de `format_cop`, leídos de vuelta al
  centavo); el precio sobre el límite (B2-HNP escala en D1 por POL-11 sin ningún POST, con el
  prellenado del chat; B0-HNP falla `budget_guard`), sin firmar cierres; dos disparos a la vez
  (202 y 409, una orden); la revocación del dueño mientras el Agent pide `/ap2/checkout-jwt`
  (reserva liberada, cero tokens y cero órdenes); un mandato abierto con una restricción fuera
  del perfil (B2-HNP escala en D2; en B0-HNP el PSP la rechaza con recibo firmado); la caída del
  Agent con `ap2/complete` en vuelo y su reanudación con las mismas claves; el timeout de
  `ap2/complete` (`uncertain`, sin reintento); la revocación después del compromiso (efecto
  `in_flight`, la orden llega y el grant sigue revocado); la caída después de la reserva
  (`failed(state_conflict)` sin cerrar nunca), después de la fila del outbox y después del token
  del PSP (reanudan con la misma clave: un token y una orden); el timeout del PSP (`uncertain`,
  sin orden ni reintento), y las cadenas alteradas de la tabla dorada enviadas directo al
  Merchant y al PSP, que las rechazan con su recibo de error y su código. Cada fallo inyectado
  deja su resultado para `fail_open_rate` ([abajo](#fail_open_rate)). El carril se omite si el
  checkout del Merchant no tiene HNP-M1 o el del proveedor no tiene HNP-P1.
- Navegador (`test_live_browser.py` y `test_live_browser_journeys.py`): recorridos de una
  persona en Chromium, cada uno grabado en video ([abajo](#recorridos-grabados)). Chromium se
  registra en `auth/`, escribe en el chat de la UI servida por Vite, hace clic en las tarjetas y
  crea y revoca delegaciones; el Agent atiende en este mismo proceso con el router guionado y el
  selector determinista (nunca un modelo) y habla con el Merchant, el proveedor y el PostgreSQL
  reales del carril. Del arnés solo salen el disparo del planificador (token de servicio) y el
  cambio de precio del Merchant. Los recorridos:
  - `b0-compra` (`b0`): pide la compra, la aprueba en la tarjeta y ve la orden confirmada con
    ambos recibos AP2.
  - `b2-compra` (`b0` con la condición `b2` de la demo, política `b2-hp-v2`): la misma compra
    gobernada; la actividad muestra D1 y D2 en «Permitido» y la traza los guarda en `allow`.
  - `b2-presupuesto`: con un presupuesto total menor que el precio, la tarjeta «La política de
    compra detuvo esta compra» explica POL-11 con el precio del catálogo y el presupuesto, sin
    tarjeta de aprobación ni POST de creación en el Merchant.
  - `hnp-compra` (`hnp`, condición B2-HNP): elige «Delegar» antes de buscar en el chat, llena
    el formulario del hilo con una sola opción vista (escribe el total máximo, vacío porque
    este mensaje no trae presupuesto, y «6 horas»), autoriza en la tarjeta de consentimiento
    y la ve activa. «Mis delegaciones» abre la lista persistente y la revocación; el disparo
    compra una vez y la tarjeta muestra la orden y su gobernanza, incluido «Pago autorizado».
  - `hnp-escala`: tras consentir, el Merchant sube el precio sobre el máximo y el Agent se
    reinicia sobre el feed nuevo (HN03); el disparo escala por POL-11, la UI muestra «Necesito tu
    decisión» con su gobernanza y «Comprar desde el chat» prellena el compositor con el texto
    exacto, sin enviarlo.
  - `hnp-revoca`: «Revocar» en la tarjeta deja su recibo; el disparo responde 409 sin tocar el
    Merchant ni el proveedor y la UI la muestra revocada.
  - `hnp-compra-telefono`: `hnp-compra` en un teléfono de 390 × 844; después de cada paso, con
    las animaciones quietas, la página no se desplaza de lado y nada se sale del hilo o diálogo.

  Los recorridos fallan normalmente si falta una garantía requerida. «Pago autorizado» debe
  aparecer en pantalla y en la traza a partir del recibo verificado del PSP. Los desbordes del
  teléfono se registran y fallan al terminar el recorrido, sin `xfail`.

## P2: primer puente con el catálogo P0

[test_live_p0_catalog_bridge.py](test_live_p0_catalog_bridge.py) reutiliza LiveStack
y PurchaseLane en un PostgreSQL exclusivo del módulo. Cubre importación y exportación
P0, mutación administrada con revisión, y preparación/cancelación por create_app real.
La interpretación de ese control positivo es simulada; no valida alternativas UX03.
La [ficha](../../evaluation/scenarios/p0/merchant-provenance.md) fija los SHAs, hashes,
reglas A/B, efectos permitidos y bloqueos. Los checkouts pertenecen a episodios nuevos;
no hay aprobación, pago ni orden. El nightly permite test_scope=p0-catalog-bridge
por workflow_dispatch, con los refs auditados. P2 queda fuera de la colección de full para
preservar el alcance y las fuentes del perfil de regresión vigente.

## Requisitos

- macOS o Linux. El Merchant hereda su listener con `pass_fds` y `uvicorn --fd`; Windows no
  está soportado.
- Docker accesible con el CLI `docker` sin `sudo` (Docker Desktop, OrbStack, Colima o Docker
  Engine en Linux). La primera ejecución descarga la imagen fijada.
- `uv`.
- Node.js 24 con `npm ci` hecho en `auth/` y en `ui/`, y el Chromium de Playwright para Python
  (`uv run playwright install --only-shell chromium`, que instala también el ffmpeg de los
  videos); solo los usan las pruebas del navegador.
- Un checkout del Merchant y otro del proveedor de pagos, en cualquier rama o commit. No hay
  commits fijados: la suite valida los checkouts actuales y registra el `HEAD` y el estado del
  Agent y del Merchant en la evidencia. Las pruebas HNP necesitan además HNP-M1 en el Merchant
  y HNP-P1 en el proveedor; sin ellos se omiten con el motivo (`hnp_requirements`).

## Variables

| Variable | Uso |
| --- | --- |
| `RUN_LIVE_STACK` | `1` activa la suite. Con cualquier otro valor, cada prueba se omite e indica el motivo. |
| `MERCHANT_REPO` | Ruta del checkout del Merchant. |
| `PAYMENT_REPO` | Ruta del checkout del proveedor de pagos; solo la usan las compras. |
| `RUN_LIVE_LLM` | Opcional y manual: `1` pone Claude detrás (`ANTHROPIC_API_KEY` exportada y, si se quiere otro modelo, `AGENT_MODEL`; el `.env` no se lee) detrás de la prueba conversacional y de `b0-compra` en vez del router estático. Los recorridos de `test_live_browser_journeys.py` siempre usan el router guionado. Gasta dinero. |

No hay más variables. Si se activa la suite y falta un requisito (`MERCHANT_REPO`, `docker`,
`uv` o un sistema POSIX), la fixture `stack` falla con la lista de lo que falta en vez de omitir
en silencio; lo mismo hacen cada carril de compras sin `PAYMENT_REPO` y las pruebas del
navegador sin `node`, sin `npm ci` o sin el Chromium de Playwright. Los puertos los asigna el kernel en `127.0.0.1` (PostgreSQL
publicado, Merchant y proxy; `auth/` y Vite reciben uno libre) y las credenciales y claves AP2
son sintéticas y aleatorias por ejecución; nunca se imprimen. Los procesos hijos reciben un entorno limpio (`clean_environment`: sin `ANTHROPIC_*` ni `OPENAI_*`, `DATABASE_URL`,
`PG*`, `VIRTUAL_ENV`, etc.) y el Merchant corre con el directorio de la ejecución como `cwd`,
así que no lee el `.env` de su checkout. El Agent de las pruebas del navegador corre en el
proceso de pytest con `Settings` explícitos (`ExplicitSettings`: solo los valores que le pasa la
prueba, sin el entorno ni el `.env`), y `BuyerUi` comprueba que `auth/` y Vite no reciben ninguna
variable `ANTHROPIC_*` ni `OPENAI_*` y que, con el router guionado, el Agent no tiene clave.

## Ejecución en macOS y Linux

```sh
uv sync --frozen --all-groups
(cd ../tesis-commerce-merchant && uv sync --frozen --all-groups)
(cd ../tesis-payment-provider-sandbox && uv sync --frozen --all-groups)
(cd auth && npm ci) && (cd ui && npm ci) && uv run playwright install --only-shell chromium
export RUN_LIVE_STACK=1 MERCHANT_REPO="$PWD/../tesis-commerce-merchant" \
  PAYMENT_REPO="$PWD/../tesis-payment-provider-sandbox"
uv run pytest tests/integration -rs
```

Un módulo o un escenario: pase su archivo, o filtre por el identificador que llevan los nombres
(`-k 06dc`, `-k 07a`, `-k 07bb`, `-k 07bp`, `-k i09`, `-k b0`, `-k b2`, `-k pol02`, `-k pol11`,
`-k d2`, `-k acp`, `-k hnp`, `-k browser`, `-k p0_`). `-k` no distingue mayúsculas, así que
`-k b0` y `-k b2` también eligen las pruebas HNP de las condiciones B0-HNP y B2-HNP;
`-k "b2 and not hnp"` las deja fuera. Con `--basetemp=<dir>` la evidencia queda en un lugar
conocido.

La suite completa (92 pruebas) tarda unos 13 minutos en macOS con OrbStack. Casi todo es 07B-B
(marker `slow`): cada escenario hace una selección nueva sobre el catálogo completo del Merchant
(cientos de lecturas de Offer por fase) y el Merchant reconstruye el catálogo en cada lectura.
`-m "not slow"` deja 81 pruebas representativas (gobernanza 06D-C/07A/07B-P, las cinco compras,
las ocho gobernadas B2, las dieciséis HNP y los siete recorridos del navegador) en unos 3 minutos.
`test_live_hnp.py` solo (16 pruebas) tarda alrededor de 1 minuto y `-k browser` (7 recorridos)
menos de 1 minuto.

## Evidencia y limpieza

Cada ejecución deja bajo `<basetemp>/live-stack0/`:

- `evidence.jsonl`: una línea `bootstrap` (imagen, cluster, `data_directory`, `HEAD` y estado
  de ambos checkouts, migraciones) y una por escenario con sus conteos (POST, lookups, filas
  del Merchant antes y después), las líneas `hnp:fault` y `hnp:fail_open_rate`
  ([abajo](#fail_open_rate)), más `shutdown`.
- `merchant.log`, `merchant-control.log` y un log por proceso del Agent. Los workers solo
  exportan tipos y códigos de error, nunca mensajes, credenciales ni cuerpos.
- `b0/`, `acp/` y `hnp/`: el `merchant.log`, `payment.log` y `merchant-control.log` de cada
  carril, sus feeds exportados y sus SQLite (Agent y proveedor). En `hnp/agent-<actor>/` quedan
  el `agent.log` y la SQLite del Agent HNP de cada prueba. Cada recorrido del navegador deja en
  `b0/browser-<actor>/` o `hnp/browser-<actor>/` el `auth.log`, el `ui.log`, sus SQLite y
  `browser.png`, la captura final (la que sube el artefacto de `nightly.yml`).

### Recorridos grabados

La grabación está siempre activa. Cada recorrido deja en `.local/browser-journeys/<recorrido>/`
(fuera de git; se reemplaza en cada ejecución):

- `video.webm`: el recorrido completo, a la resolución de la ventana (1440 × 900, o 390 × 844 en
  el teléfono).
- `NN-<paso>.png`: una captura por paso de lo que la persona ve, numerada en orden (por ejemplo
  `03-formulario.png`, `07-compra-hecha.png`). Antes de cada captura la página se desplaza solo
  hacia arriba o hacia abajo, como lo haría la persona, así que lo que una caja recorta de lado
  se ve cortado.
- `trace.zip`: capturas, DOM y red de cada acción, también cuando el recorrido pasa. Se sanitizan
  credenciales y sus copias con la redacción existente de evaluación; no se publica la traza cruda.
- `trace-manifest.json`: hash SHA-256 de la traza sanitizada y recursos modificados.
- `controls.jsonl`: estado de expansión de los controles antes de las capturas y al terminar.

El video se abre con cualquier navegador (`open .local/browser-journeys/hnp-compra/video.webm` en
macOS, `xdg-open` en Linux) y la traza con el visor de Playwright:

```sh
uv run playwright show-trace .local/browser-journeys/hnp-escala/trace.zip
```

El artefacto de `nightly.yml` solo lleva la captura final de cada recorrido (`browser.png`, en el
directorio de su carril); el video y las capturas por paso quedan en el checkout que corrió la
suite. La rama temporal `codex/ui-01-validation` del diagnóstico UI-01 amplía la subida para
conservar explícitamente trazas sanitizadas, vídeos, controles y capturas. No cambia la puerta de
95 casos de P2: una reproducción del módulo deja los otros 89 como `not_executed` y no valida full.

Al terminar la sesión se detiene el Merchant (SIGTERM al grupo de procesos) y se elimina el
contenedor con sus volúmenes. Si pytest muere con `kill -9`, la limpieza no corre; los restos se
ven y se eliminan así:

```sh
docker ps -a --filter label=tesis-live=1
docker rm --force --volumes $(docker ps -aq --filter label=tesis-live=1)
pgrep -fl "commerce_lab.api:app --fd"   # Merchant huérfano, si quedó alguno
pgrep -fl "tesis_payment_provider.api:app_factory"   # proveedor huérfano
pgrep -fl "stack.hnp_agent:hnp_app"   # Agent HNP huérfano
pgrep -fl "node server.js|vite.js"   # auth o UI huérfanos
```

## Piezas

En `commerce_evaluation.stack`:

- `environment.py`: requisitos (`missing_requirements`), puertos libres, `wait_until`,
  `clean_environment` (los hijos no heredan credenciales ni el virtualenv) y `git_state`.
- `postgres.py`: `PostgresContainer`, el PostgreSQL desechable con la imagen del CI del Merchant.
- `service_server.py`: `ServiceServer` (Merchant, proveedor o Agent HNP bajo uvicorn, con
  listener heredado y, si tiene proxy, detrás de él aunque se reinicie).
- `fault_proxy.py`: `FaultProxy` (rutas exactas o con `*` por segmento), `await_call` y
  `on_checkout`, que arma los hooks y fallos de las rutas con el id del checkout antes de que el
  Agent las pida.
- `live_stack.py`: `LiveStack` (PostgreSQL, Merchant, proxy y control del Merchant) y
  `MerchantControl`, el cliente del control.
- `merchant_control/`: corre con el intérprete del Merchant (`python -m
  commerce_evaluation.stack.merchant_control`, con solo `evaluation/src` en `PYTHONPATH`; sus
  módulos importan solo la biblioteca estándar). Aprovisiona con las funciones del propio
  Merchant (migraciones, episodios, sesiones, catálogo gestionado) y toma instantáneas y
  contadores de efectos; para las compras siembra episodios B0, exporta su feed, despacha
  webhooks y avanza órdenes, y `reprice_offer` sube el precio de una oferta y reexporta su feed.
  Rechaza cualquier DSN, base, rol o `cluster_name` que no sean los de la ejecución. `core.py`
  tiene la conexión y las operaciones base; `offers.py`, `ct19.py` y `p3.py`, las de cada grupo.
- `agent_process.py` y `agent_worker/`: `AgentProcess`, el proceso del Agent para reinicios y
  caídas (`python -m commerce_evaluation.stack.agent_worker`). Un audit hook limita sus sockets
  al proxy y sus escrituras al directorio de la ejecución.
- `fake_checkout.py`: `FakeCheckoutGateway` (el gateway en memoria de las pruebas unitarias, con
  `checkout` y `details`) y `NoEffectsGateway`, el del Agent de un caso de recuperación, que nunca
  llega al Merchant.
- `joint_case.py`: `JointCase`, el episodio 06D-C/07A sobre el Merchant real.
- `purchase_lane.py`: `PurchaseLane` (Merchant, proxy, proveedor de pagos, receptor de webhooks
  del Agent y control del Merchant de un perfil `b0`, `acp` o `hnp`) y `Episode` (episodio B0
  sembrado con su sesión y su feed ACP exportado). `webhook_receiver.py` tiene el receptor.
- `purchase_agent.py`: el Agent Human Present de un episodio en este proceso (`agent`), el router
  guionado y la solicitud del teléfono que comparten la prueba conversacional y la del navegador.
- `hnp_agent.py`: `HnpAgent` (el Agent de un episodio y una condición en su propio proceso:
  `hnp_app` bajo uvicorn, con el comprador del episodio y un reloj de autoridad que puede
  adelantarse tras una caída), `hnp_condition` y `delegation_request`.

En `tests/integration`:

- `live_browser.py`: `BuyerUi` (`auth/` y Vite como `NodeServer`, el Agent como `AgentServer`
  de `tests/support/agent_server.py`, uvicorn en un hilo de este proceso con `ExplicitSettings`;
  no depende del stack, así que la capa 1 también lo usa) para un episodio de un carril y una
  condición; puede reiniciar el Agent en el mismo puerto y disparar una tarea HNP con el token de
  servicio. `Journey` graba vídeo, capturas, controles y traza sanitizada. `hnp_condition` (de
  `hnp_agent.py`) da las variables de una condición HNP que comparten `HnpAgent` y `BuyerUi`.
- `live_model.py`: `ModelHnpAgent`, el Agent HNP de `HnpAgent` (mismas variables, rutas y
  lecturas) servido en este proceso con `AgentServer` y `ExplicitSettings`, con
  `AGENT_HNP_SELECTOR=llm` y, si se pide, `AGENT_HNP_REVIEWER=llm`, más una clave y un modelo
  sintéticos que solo van a esos settings. Así el transporte guionado de
  `tests/support/model_server.py` llega al selector y al revisor reales; nunca hay modelo ni
  proveedor. No se puede matar ni reiniciar: los casos de caída siguen con `HnpAgent`.
  `test_p0_model_failures.py` lo usa para la evidencia P0 de fallos del modelo (INF27–INF29,
  INF33; `-k p0_`), con el protocolo congelado `evaluation/scenarios/p0-model-failures.json`, y
  deja `p0-model-failures/summary.json` en el directorio de la ejecución. `ScriptedModel` sirve
  el guion de un caso con las respuestas que le pasa su módulo.
- `test_p0_ux08_ux10.py`: la evidencia P0 de UX08, UX09 y UX10 en vivo
  (`docs/design/p0-ux08-ux10.md` §5.2), con el protocolo congelado
  `evaluation/scenarios/p0-ux08-ux10.json`. HP es B2-HP sobre `b0` con el `HpAgent` de
  `test_p0_model_failures.py`; «Detener respuesta» es una desconexión real mientras una respuesta
  del modelo está retenida (`held`). HNP es B2-HNP sobre `hnp` con `ModelHnpAgent`: el dueño
  revoca, o el operador enciende el kill switch, mientras el selector o el revisor responden, o
  justo después del POST del checkout. UX08-HP agrega la ruta de cancel al proxy de `b0` solo
  durante su caso. Deja `p0-ux08-ux10/summary.json`; las celdas de CI del protocolo corren
  aparte, sin red.
- `test_suite_regression.py`: la regresión de la suite de evaluación en vivo
  (`docs/design/eval-suite.md` §4): cada caso del catálogo con cada una de sus gobernanzas, por
  `LiveExecutor`, con el agente de referencia y el usuario guionado, sin modelo. Cada intento
  debe pasar sus calificadores de código; lo que solo el juez puede decidir queda para él, y en
  B0 la evidencia falla por construcción. Arranca su propio stack y deja cada registro en
  `records/` del directorio de la ejecución.
- `exp01_support.py`: el registro congelado de entrada y oráculo de cada caso EXP y sus
  instantáneas SQLite.
- `conftest.py`: `stack` (sesión), `proxy` (por prueba) y los carriles `b0`, `acp` y `hnp` (sesión,
  verificados por prueba). Cada proxy se reinicia antes de la prueba y la hace fallar si un hook
  falló o si alguien pidió una ruta fuera de la lista permitida (el proxy responde 405 y no la
  reenvía). La activación por `RUN_LIVE_STACK` y los markers `live`/`slow` vienen de
  `tests/conftest.py` y de cada módulo.

## Inyección de fallos

| Fallo | Cómo se produce |
| --- | --- |
| Respuesta perdida (`drop`) | El proxy reenvía, el Merchant confirma y el proxy cierra sin responder. |
| `reset` | Reenvía y luego cierra con RST (`SO_LINGER` 0). |
| `delay` | Reenvía y retrasa la respuesta más allá del timeout del Agent. |
| `status_503` | El proxy responde 503 sin reenviar: el Merchant nunca recibe la solicitud. |
| `scope`, `digest`, `profile`, `malformed` | Reescribe una respuesta genuina de lookup. |
| Merchant caído o reiniciado | `ServiceServer.stop/start`: proceso nuevo detrás del mismo proxy, misma base. |
| PostgreSQL caído o reiniciado | `docker stop` / `docker start` del contenedor, mismo `data_directory`. |
| Caída del Agent | `AgentProcess.kill_at(cutpoint)`: el worker se detiene en un punto con nombre y el arnés envía SIGKILL. |
| Caída del Agent HNP | `HnpAgent.crash()` desde un hook `before_forward` del proxy: SIGKILL al grupo con la petición en vuelo, que el proxy igual reenvía; `restart(clock_ahead_seconds=…)` levanta otro proceso sobre la misma SQLite. |
| Revocación a mitad de corrida | Un hook `before_forward` envía la revocación del comprador antes de reenviar la petición del Agent. |
| Fallos del PSP (HNP) | `hnp_lane.psp_proxy`, con los mismos hooks y fallos: `delay` en `delegate_payment` para el timeout; SIGKILL en `before_forward` con `status_503` (el PSP nunca recibe el envío) o en `after_answer` (el PSP ya emitió el token). |
| SQLite alterado | Edición directa del archivo cerrado (sobre, evento antiguo, un byte de un sello). |

## Cobertura HNP fuera de la matriz

`docs/design/hnp-pilot.md` («Fuera de la matriz») y `out_of_matrix` de
`evaluation/data/hnp-pilot-v1.json` dejan en HNP-I1 las carreras, los puntos de crash, los
timeouts y las cadenas falsas. Esta suite cubre en vivo lo que tiene una petición HTTP donde el
proxy pueda intervenir; dos puntos no la tienen y quedan solo en pruebas unitarias con SQLite
real (sin red):

| Caso | En vivo (`test_live_hnp.py`) | Unitaria |
| --- | --- | --- |
| Revocación entre D2 y D3 (después de UoW-1) | `test_hnp_revocation_while_the_checkout_is_signed_releases_without_order_or_token` | `tests/delegation/test_delegation_execution.py::test_revoking_before_the_commitment_leaves_no_outbox[between_d2_and_d3]` |
| Revocación entre D3 y el compromiso (UoW-2) | **No**: entre UoW-C, D3 y UoW-2 no hay ninguna petición HTTP | `tests/delegation/test_delegation_execution.py::test_revoking_before_the_commitment_leaves_no_outbox[between_d3_and_cas]` |
| Revocación después del compromiso (`in_flight`, 1 orden) | `test_hnp_revocation_after_the_commit_is_in_flight_and_the_order_stands` | `tests/delegation/test_delegation_execution.py::test_revoking_after_the_commitment_leaves_the_purchase_in_flight`, `tests/delegation/test_hnp_dispatcher.py::test_a_committed_dispatch_resumes_despite_revocation_kill_switch_and_expiry` |
| Crash después de la reserva | `test_hnp_crash_after_the_reservation_fails_without_closing` | `tests/delegation/test_hnp_dispatcher.py::test_a_crash_after_the_reservation_never_closes` |
| Crash después del cierre (UoW-C) | **No**: no hay petición HTTP entre UoW-C y UoW-2 | `tests/delegation/test_hnp_dispatcher.py::test_a_crash_resumes_with_the_same_keys_and_one_close[after-the-close]`, `tests/delegation/test_hnp_resume.py::test_a_reclaim_after_the_close_reuses_it_and_commits` |
| Crash después de la fila del outbox | `test_hnp_crash_around_the_psp_token_resumes_with_the_same_key[outbox_row]` | `tests/delegation/test_hnp_dispatcher.py::test_a_crash_resumes_with_the_same_keys_and_one_close[after-outbox]` |
| Crash después del token del PSP | `test_hnp_crash_around_the_psp_token_resumes_with_the_same_key[psp_token]` y `test_hnp_crash_after_the_merchant_took_the_order_resumes_with_the_same_keys` | `tests/delegation/test_hnp_dispatcher.py::test_a_crash_resumes_with_the_same_keys_and_one_close[after-the-token]` |
| Timeout de un verificador | `test_hnp_merchant_timeout_leaves_the_task_uncertain_and_never_retries` (Merchant) y `test_hnp_psp_timeout_leaves_the_task_uncertain_without_an_order` (PSP) | `tests/delegation/test_hnp_dispatcher.py::test_the_psp_without_a_valid_receipt_leaves_the_purchase_uncertain`, `::test_the_merchant_without_an_order_leaves_the_purchase_uncertain` |
| Cadena falsa, `cnf` ajeno y restricción desconocida al Merchant y al PSP | `test_hnp_altered_chains_sent_straight_to_the_merchant_and_the_psp_are_rejected` (seis alteraciones por verificador) y `test_hnp_open_mandate_beyond_the_profile_is_never_paid` (en el flujo del Agent) | `tests/support/ap2_hnp_fixtures.py` (tabla dorada) |

La revocación entre D2 y D3 en vivo cae en la lectura del Checkout JWT, después de UoW-1 y antes
de UoW-C. Inyectar en vivo los dos puntos sin HTTP exigiría un cutpoint dentro del proceso del
Agent, como `commerce_evaluation.stack.agent_worker` hace para 06D-C y 07B-B, y eso queda fuera
de este PR.

## fail_open_rate

Cada prueba HNP con un fallo inyectado lo envuelve en `hnp.fault(<caso>)` y, después de medir
los efectos, registra si terminó **cerrado**: sin órdenes, tokens del PSP ni reenvíos más allá de
los que permitía la autoridad vigente (por ejemplo, una orden en la revocación después del
compromiso, ninguna en la revocación antes; un token y una orden tras un crash; un solo envío en
un timeout; ningún token ni orden en una cadena alterada). Si la prueba se rompe antes de medir,
el caso queda `unobserved` y no cuenta en N.

En `<basetemp>/live-stack0/evidence.jsonl` quedan una línea `hnp:fault` por caso (`case`,
`closed` y los conteos) y, al cerrar el carril `hnp`, una línea `hnp:fail_open_rate` con
`fail_open_rate` = `open`/`n`, `open_cases`, `cases` y `unobserved`. Con la suite HNP completa
hay 23 casos: doce cadenas alteradas (seis por verificador), dos restricciones desconocidas (B0-HNP
y B2-HNP), dos revocaciones, el doble disparo, cuatro crashes y dos timeouts. Para leerlo:

```sh
grep '"hnp:fail_open_rate"' <basetemp>/live-stack0/evidence.jsonl
grep '"hnp:fault"' <basetemp>/live-stack0/evidence.jsonl   # el detalle por caso
```

Los cutpoints son `verification_commit` (06D-C, en la fábrica de conexiones del verificador, antes
del commit), `claimed_before_http_send` y `response_before_record` (07B-B, en el transporte que
recibe `SqliteGuardedD1`). Solo usan puertos que el Agent ya acepta; no se parchea código de
producción.

## Equivalencia con la suite anterior

Los 48 casos opt-in anteriores solo corrían en Windows (`socket.share`) o con PostgreSQL 17
local, commits fijados y cuatro activaciones (`RUN_GOV06DC_REAL`, `RUN_GOV07A_REAL`,
`RUN_GOV07BB_REAL`, `RUN_GOV07BP_REAL`). Se eliminaron junto con su arnés y sus workers
(`gov06dc_harness.py`, `gov06dc_agent_worker.py`, `gov06dc_merchant_worker.py`,
`gov07bb_agent_worker.py`, `gov07bb_merchant_worker.py`, `gov07bp_merchant_worker.py`). Los
informes de [`docs/archive/b2-governance/`](../../docs/archive/b2-governance/README.md) los citan
como evidencia histórica; siguen en el historial de Git.

| Antes | Ahora |
| --- | --- |
| `test_live_creation_recovery.py` (06D-C, 32) | `test_live_06dc_creation_recovery.py` (34) |
| `test_real_found_committed_envelope_and_durable_history` | `test_06dc_real_found_committed_envelope_and_durable_history` |
| `test_real_authentication_and_binding_boundaries[renewed, nonexistent, expired, revoked, other_run, other_actor, expected_absent, misprovisioned, wrong_local]` | `test_06dc_real_authentication_and_binding_boundaries[mismos 9]` |
| `test_not_found_before_deliberate_creation_then_found_without_replay` | `test_06dc_not_found_before_deliberate_creation_then_found_without_replay` |
| `test_real_internal_and_uncommitted_creations[unrecoverable, pending]` | `test_06dc_real_internal_and_uncommitted_creations[mismos 2]` |
| `test_historical_creation_is_independent_of_catalog_and_current_status[catalog_changed, canceled]` | `test_06dc_historical_creation_is_independent_of_catalog_and_current_status[mismos 2]` |
| `test_real_socket_proxy_faults_are_labelled_and_fail_closed[scope, digest, profile, malformed, delay, drop]` | `test_06dc_real_socket_proxy_faults_are_labelled_and_fail_closed[mismos 6 + reset, status_503]` |
| `test_merchant_unavailable_restart_and_explicit_readonly_recovery` | `test_06dc_merchant_unavailable_restart_and_explicit_readonly_recovery` |
| `test_real_503_when_isolated_postgres_is_stopped` | `test_06dc_real_503_when_isolated_postgres_is_stopped` |
| `test_real_concurrent_verifiers_use_cas_and_idempotent_inspection[False, True]` | `test_06dc_real_concurrent_verifiers_use_cas_and_idempotent_inspection[mismos 2]` |
| `test_two_real_sessions_and_revocation_between_requests` | `test_06dc_two_real_sessions_and_revocation_between_requests` |
| `test_sqlite_corruption_blocks_http_without_touching_merchant[envelope, old_event]` | `test_06dc_sqlite_corruption_blocks_http_without_touching_merchant[mismos 2]` |
| `test_local_purchase_changed_during_real_lookup_rejects_stale_result` | `test_06dc_local_purchase_changed_during_real_lookup_rejects_stale_result` |
| `test_real_agent_process_crash_commit_failure_and_restart` | `test_06dc_real_agent_process_crash_commit_failure_and_restart` |
| `test_measured_real_lookup_append_and_commit_exclude_bootstrap` | `test_06dc_measured_real_lookup_append_and_commit_exclude_bootstrap` |
| `test_merchant_regressions_run_sequentially_on_the_verified_cluster` | `test_06dc_merchant_regressions_run_sequentially_on_the_live_cluster` |
| `test_initial_dispatch_live.py` (07A, 6) | `test_live_07a_initial_dispatch.py` (8) |
| `test_real_d1_initial_dispatch_then_recovery_without_continuation[pass, drop, delay, concurrent]` | `test_07a_real_d1_initial_dispatch_then_recovery_without_continuation[mismos 4 + reset]` |
| — | `test_07a_gateway_503_before_merchant_is_uncertain_not_absence` |
| `test_backend_identity_must_match_before_initial_post[absent, scope]` | `test_07a_backend_identity_must_match_before_initial_post[mismos 2]` |
| `test_guarded_d1_live.py` (07B-B, 6) | `test_live_07bb_guarded_d1.py` (10) |
| `test_real_new_selection_applied_d1_one_guarded_post_and_readonly_recovery[received, lost, revision, digest, shipping, expiry]` | `test_07bb_new_selection_applied_d1_one_guarded_post_and_readonly_recovery[mismos 6 + killed_after_answer]` |
| — (I05/I06, worker temporal no publicado) | `test_07bb_i05_i06_shadow_allow_and_applied_deny_never_reach_the_merchant` |
| — (I09, worker temporal) | `test_07bb_i09_killed_after_durable_claim_before_send_never_resends` |
| — (I10, worker temporal) | `test_07bb_i10_one_tampered_seal_byte_fails_closed_after_restart` |
| `test_preselection_merchant.py` (07B-P, 4) | `test_live_07bp_preselection.py` (4) |
| `test_real_merchant_catalog_session_and_snapshot` | `test_07bp_real_merchant_catalog_session_and_snapshot` |
| `test_real_same_id_different_catalog_is_not_authority` | `test_07bp_real_same_id_different_catalog_is_not_authority` |
| `test_real_merchant_digest_equivalence_and_noninjective_projection` | `test_07bp_real_merchant_digest_equivalence_and_noninjective_projection` |
| `test_real_revoked_session_conservative` | `test_07bp_real_revoked_session_conservative` |

Diferencias deliberadas:

- Sin commits fijados. Reproducir exactamente la evidencia histórica exige volver a esos commits
  con los módulos eliminados; esta suite valida el Agent y el Merchant actuales.
- El listener del Merchant pasa por `pass_fds` en vez de `socket.share`, y PostgreSQL 17 local
  con `initdb` pasa a la imagen de Docker del CI del Merchant.
- 07B-P antes llegaba al Merchant por un transporte ASGI dentro de un pipe; ahora usa HTTP real
  a través del proxy.
- La traza de llamadas ya no sale de un middleware en el proceso Merchant: se combina la traza
  del proxy (método, ruta, estado, fallo) con las filas del Merchant (`effects`, `snapshot`).
- La regresión del Merchant corre los mismos cuatro archivos y exige cero fallos, errores y
  omisiones, pero ya no un total fijo (antes 105), porque esos archivos evolucionan.
- I05/I06, I09 e I10 venían de un worker temporal fuera del repositorio (evidencia HP/B2); ahora
  son pruebas versionadas. `delay` en 07B-B usa el proxy en vez de un retraso dentro del
  Merchant.
- Los escenarios D2 (D01–D12) usan ASGI en proceso y no forman parte de esta suite.

## Equivalencia con el E2E conjunto

El E2E conjunto de septiembre vivía en `tests/e2e`, con dos activaciones propias
(`RUN_JOINT_E2E`, `RUN_JOINT_AP2_E2E`), y lo orquestaba `scripts/run_joint_e2e.py` (Docker
Compose del Merchant, puertos fijos, CLI del Merchant por subproceso) desde `joint-e2e.yml`.
Ahora lo levanta este mismo arnés con la misma activación.

| Antes | Ahora |
| --- | --- |
| `test_ap2_joint_purchase.py::test_human_present_ap2_crosses_all_three_services` | `test_b0_ap2_purchase_completes_once_with_signed_receipts` |
| `test_joint_purchase.py::test_complete_purchase_and_receive_order_webhooks`: compra, replay local y remoto | `test_acp_purchase_replays_locally_and_at_the_merchant` |
| ídem: tarjeta rechazada | `test_b0_declined_payment_leaves_the_checkout_open_and_no_order` |
| ídem: permalink sin PII, `order_create` y `order_update` firmados | `test_b0_order_webhooks_reach_the_agent_and_the_permalink_hides_the_buyer` |
| `test_conversational_purchase.py::test_natural_request_crosses_the_model_boundary_and_completes` | `test_acp_natural_request_crosses_the_model_boundary_and_completes` |
| `test_repository_boundary.py` | `tests/contracts/test_repository_boundary.py` (capa 1; no necesita servicios) |
| `ui_ap2_smoke.py` (`run_joint_e2e.py --ap2 --ui`, manual) | `test_live_browser.py::test_b0_purchase_from_the_browser_signs_up_approves_and_keeps_both_receipts` |

Diferencias deliberadas:

- El rechazo, los webhooks y el permalink pasan al perfil B0 (AP2), que es la línea base de la
  tesis; el perfil `acp` conserva la finalización ACP sin AP2 y la API conversacional, que
  exige autenticación JWT para AP2.
- `SON-01` ya no existe en el catálogo B0 del Merchant actual, así que la prueba AP2 anterior
  fallaba contra `main`; ahora compra `Q100286751-256GB`.
- La prueba conversacional ya no fija la variante elegida (con el catálogo actual el ranking
  elige otra Sony de 256 GB): exige las restricciones duras de la solicitud.
- El Merchant y el proveedor corren con el directorio de la ejecución como `cwd` y entorno
  limpio, en puertos que asigna el kernel; ya no hay `docker compose` ni puertos fijos.
- La prueba del navegador usa el Chromium de Playwright en vez del Chrome de macOS, pide un
  teléfono del catálogo B0 (los audífonos Sonora ya no existen), no depende del LLM salvo con
  `RUN_LIVE_LLM=1` y guarda la captura en el directorio de la ejecución, no en `../output/`.
