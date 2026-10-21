# Suite viva portable (opt-in)

Es la capa 4 de [`docs/testing.md`](../../docs/testing.md): la única suite de sistema del Agent.
Estos módulos levantan un stack propio y desechable en cada ejecución: PostgreSQL en Docker con
la misma imagen y digest que el CI del Merchant (`postgres:18.4-alpine@sha256:9a8a…5de15`), el
Merchant real (`uvicorn commerce_lab.api:app`, lanzado con `uv run --project $MERCHANT_REPO` y
su propio intérprete y lock), el proveedor de pagos real (`tesis_payment_provider`, desde
`$PAYMENT_REPO`) y procesos del Agent de este checkout, incluidos su servicio de autenticación
(`auth/`) y su UI (`ui/`) en la prueba del navegador. Todo el tráfico del Agent al Merchant
pasa por HTTP real de loopback a través de `FaultProxy`, que es transparente salvo cuando una
prueba arma un fallo etiquetado. La suite normal y `ci.yml` los omiten; el workflow
`nightly.yml` los ejecuta cada noche y a pedido.

Hay cuatro grupos sobre el mismo PostgreSQL:

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
  comercio: el `order_create` firmado llega a `webhook_events` del runtime y a su traza.
  `test_live_b2_pol02_allowlist.py` arma el Agent con `build_runtime` y las variables de la condición B2 del piloto en el entorno (las
  mismas que exporta el lanzador de la demo) y corre el mismo turno de compra con dos listas
  de comercios de POL-02: con otro comercio (modo `pol02-deny`) termina en `d1_denied`, POL-02
  queda en `fail` con `ACTION_OR_MERCHANT_NOT_ALLOWED` y el Merchant no recibe ningún POST de
  creación; con el comercio del carril llega a `awaiting_confirmation` con un solo POST.
- Navegador (`test_live_browser.py`): sobre el carril `b0`, Chromium se registra en `auth/`,
  pide la compra en la UI servida por Vite, la aprueba en la tarjeta y ve la orden confirmada;
  el Agent atiende en este mismo proceso con el router guionado y guarda ambos recibos AP2.

## Requisitos

- macOS o Linux. El Merchant hereda su listener con `pass_fds` y `uvicorn --fd`; Windows no
  está soportado.
- Docker accesible con el CLI `docker` sin `sudo` (Docker Desktop, OrbStack, Colima o Docker
  Engine en Linux). La primera ejecución descarga la imagen fijada.
- `uv`.
- Node.js 24 con `npm ci` hecho en `auth/` y en `ui/`, y el Chromium de Playwright para Python
  (`uv run playwright install --only-shell chromium`); solo los usa la prueba del navegador.
- Un checkout del Merchant y otro del proveedor de pagos, en cualquier rama o commit. No hay
  commits fijados: la suite valida los checkouts actuales y registra el `HEAD` y el estado del
  Agent y del Merchant en la evidencia.

## Variables

| Variable | Uso |
| --- | --- |
| `RUN_LIVE_STACK` | `1` activa la suite. Con cualquier otro valor, cada prueba se omite e indica el motivo. |
| `MERCHANT_REPO` | Ruta del checkout del Merchant. |
| `PAYMENT_REPO` | Ruta del checkout del proveedor de pagos; solo la usan las compras. |
| `RUN_LIVE_LLM` | Opcional y manual: `1` pone el modelo de OpenAI del entorno (`OPENAI_API_KEY`, `OPENAI_MODEL` exportados; el `.env` no se lee) detrás de la prueba conversacional y la del navegador en vez del router estático. Gasta dinero. |

No hay más variables. Si se activa la suite y falta un requisito (`MERCHANT_REPO`, `docker`,
`uv` o un sistema POSIX), la fixture `stack` falla con la lista de lo que falta en vez de omitir
en silencio; lo mismo hacen cada carril de compras sin `PAYMENT_REPO` y la prueba del navegador
sin `node` o sin `npm ci`. Los puertos los asigna el kernel en `127.0.0.1` (PostgreSQL
publicado, Merchant y proxy; `auth/` y Vite reciben uno libre) y las credenciales y claves AP2
son sintéticas y aleatorias por ejecución; nunca se imprimen. Los procesos hijos reciben un entorno limpio (`clean_environment`: sin `OPENAI_*`, `DATABASE_URL`,
`PG*`, `VIRTUAL_ENV`, etc.) y el Merchant corre con el directorio de la ejecución como `cwd`,
así que no lee el `.env` de su checkout.

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
(`-k 06dc`, `-k 07a`, `-k 07bb`, `-k 07bp`, `-k i09`, `-k b0`, `-k b2`, `-k pol02`, `-k acp`,
`-k browser`). Con `--basetemp=<dir>` la evidencia queda en un lugar conocido.

La suite completa (66 pruebas) tarda unos 12 minutos en macOS con OrbStack. Casi todo es 07B-B
(marker `slow`): cada escenario hace una selección nueva sobre el catálogo completo del Merchant
(cientos de lecturas de Offer por fase) y el Merchant reconstruye el catálogo en cada lectura.
`-m "not slow"` deja 55 pruebas representativas (gobernanza 06D-C/07A/07B-P, las cinco compras,
las cuatro gobernadas B2 y la del navegador) en unos 2 minutos.

## Evidencia y limpieza

Cada ejecución deja bajo `<basetemp>/live-stack0/`:

- `evidence.jsonl`: una línea `bootstrap` (imagen, cluster, `data_directory`, `HEAD` y estado
  de ambos checkouts, migraciones) y una por escenario con sus conteos (POST, lookups, filas
  del Merchant antes y después), más `shutdown`.
- `merchant.log`, `merchant-control.log` y un log por proceso del Agent. Los workers solo
  exportan tipos y códigos de error, nunca mensajes, credenciales ni cuerpos.
- `b0/` y `acp/`: el `merchant.log`, `payment.log` y `merchant-control.log` de cada carril, sus
  feeds exportados y sus SQLite (Agent y proveedor). La prueba del navegador deja en
  `b0/browser-<actor>/` el `auth.log`, el `ui.log`, sus SQLite y `browser.png`, la captura final.

Al terminar la sesión se detiene el Merchant (SIGTERM al grupo de procesos) y se elimina el
contenedor con sus volúmenes. Si pytest muere con `kill -9`, la limpieza no corre; los restos se
ven y se eliminan así:

```sh
docker ps -a --filter label=tesis-live=1
docker rm --force --volumes $(docker ps -aq --filter label=tesis-live=1)
pgrep -fl "commerce_lab.api:app --fd"   # Merchant huérfano, si quedó alguno
pgrep -fl "tesis_payment_provider.api:app_factory"   # proveedor huérfano
pgrep -fl "node server.js|vite.js"   # auth o UI huérfanos
```

## Piezas

- `live_stack.py`: `PostgresContainer`, `ServiceServer` (Merchant o proveedor, con listener
  heredado), `MerchantControl`, `FaultProxy` (rutas exactas o con `*` por segmento),
  `LiveStack`, `AgentProcess` y `JointCase` (episodio 06D-C/07A sobre el Merchant real).
- `live_purchase.py`: `PurchaseLane` (Merchant, proxy, proveedor de pagos, receptor de webhooks
  del Agent y control del Merchant de un perfil `b0` o `acp`) y `Episode` (episodio B0 sembrado
  con su sesión y su feed ACP exportado), además del router guionado y la solicitud del teléfono
  que comparten la prueba conversacional y la del navegador.
- `live_browser.py`: `BuyerUi` (`auth/` y Vite como `NodeServer`, el Agent como `AgentServer`,
  uvicorn en un hilo de este proceso) para un episodio de un carril.
- `live_merchant_control.py`: corre con el intérprete del Merchant. Aprovisiona con las
  funciones del propio Merchant (migraciones, episodios, sesiones, catálogo gestionado) y toma
  instantáneas y contadores de efectos; para las compras siembra episodios B0, exporta su feed,
  despacha webhooks y avanza órdenes. Rechaza cualquier DSN, base, rol o `cluster_name` que no
  sean los de la ejecución.
- `live_agent_worker.py`: proceso del Agent para reinicios y caídas. Un audit hook limita sus
  sockets al proxy y sus escrituras al directorio de la ejecución.
- `conftest.py`: `stack` (sesión), `proxy` (por prueba) y los carriles `b0` y `acp` (sesión,
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
| SQLite alterado | Edición directa del archivo cerrado (sobre, evento antiguo, un byte de un sello). |

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
