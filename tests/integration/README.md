# Suite viva portable (opt-in)

Estos módulos levantan un stack propio y desechable en cada ejecución: PostgreSQL en Docker con
la misma imagen y digest que el CI del Merchant (`postgres:18.4-alpine@sha256:9a8a…5de15`), el
Merchant real (`uvicorn commerce_lab.api:app`, lanzado con `uv run --project $MERCHANT_REPO` y
su propio intérprete y lock) y procesos del Agent de este checkout. Todo el tráfico del Agent al
Merchant pasa por HTTP real de loopback a través de `FaultProxy`, que es transparente salvo
cuando una prueba arma un fallo etiquetado. La suite normal y `ci.yml` los omiten; el workflow
`live-stack.yml` los ejecuta a pedido y cada noche.

## Requisitos

- macOS o Linux. El Merchant hereda su listener con `pass_fds` y `uvicorn --fd`; Windows no
  está soportado.
- Docker accesible con el CLI `docker` sin `sudo` (Docker Desktop, OrbStack, Colima o Docker
  Engine en Linux). La primera ejecución descarga la imagen fijada.
- `uv`.
- Un checkout del Merchant en cualquier rama o commit. No hay commits fijados: la suite valida
  los checkouts actuales y registra el `HEAD` y el estado de ambos en la evidencia.

## Variables

| Variable | Uso |
| --- | --- |
| `RUN_LIVE_STACK` | `1` activa la suite. Con cualquier otro valor, cada prueba se omite e indica el motivo. |
| `MERCHANT_REPO` | Ruta del checkout del Merchant. Es el mismo nombre que usa el E2E conjunto. |

No hay más variables. Si se activa la suite y falta un requisito (`MERCHANT_REPO`, `docker`,
`uv` o un sistema POSIX), la fixture `stack` falla con la lista de lo que falta en vez de omitir
en silencio. Los puertos los asigna el kernel en `127.0.0.1` (PostgreSQL publicado, Merchant y
proxy) y las credenciales son sintéticas y aleatorias por ejecución; nunca se imprimen. Los
procesos hijos reciben un entorno limpio (`clean_environment`: sin `OPENAI_*`, `DATABASE_URL`,
`PG*`, `VIRTUAL_ENV`, etc.) y el Merchant corre con el directorio de la ejecución como `cwd`,
así que no lee el `.env` de su checkout.

## Ejecución en macOS y Linux

```sh
uv sync --frozen --all-groups
(cd ../tesis-commerce-merchant && uv sync --frozen --all-groups)
RUN_LIVE_STACK=1 MERCHANT_REPO="$PWD/../tesis-commerce-merchant" uv run pytest tests/integration -rs
```

Un módulo o un escenario: pase su archivo, o filtre por el identificador que llevan los nombres
(`-k 06dc`, `-k 07a`, `-k 07bb`, `-k 07bp`, `-k i09`). Con `--basetemp=<dir>` la evidencia queda
en un lugar conocido.

La suite completa (56 pruebas) tarda unos 11 minutos en macOS con OrbStack. Casi todo es 07B-B:
cada escenario hace una selección nueva sobre el catálogo completo del Merchant (cientos de
lecturas de Offer por fase) y el Merchant reconstruye el catálogo en cada lectura.

## Evidencia y limpieza

Cada ejecución deja bajo `<basetemp>/live-stack0/`:

- `evidence.jsonl`: una línea `bootstrap` (imagen, cluster, `data_directory`, `HEAD` y estado
  de ambos checkouts, migraciones) y una por escenario con sus conteos (POST, lookups, filas
  del Merchant antes y después), más `shutdown`.
- `merchant.log`, `merchant-control.log` y un log por proceso del Agent. Los workers solo
  exportan tipos y códigos de error, nunca mensajes, credenciales ni cuerpos.

Al terminar la sesión se detiene el Merchant (SIGTERM al grupo de procesos) y se elimina el
contenedor con sus volúmenes. Si pytest muere con `kill -9`, la limpieza no corre; los restos se
ven y se eliminan así:

```sh
docker ps -a --filter label=tesis-live=1
docker rm --force --volumes $(docker ps -aq --filter label=tesis-live=1)
pgrep -fl "commerce_lab.api:app --fd"   # Merchant huérfano, si quedó alguno
```

## Piezas

- `live_stack.py`: `PostgresContainer`, `MerchantServer`, `MerchantControl`, `FaultProxy`,
  `LiveStack`, `AgentProcess` y `JointCase` (episodio 06D-C/07A sobre el Merchant real).
- `live_merchant_control.py`: corre con el intérprete del Merchant. Aprovisiona con las
  funciones del propio Merchant (migraciones, episodios, sesiones, catálogo gestionado) y toma
  instantáneas y contadores de efectos. Rechaza cualquier DSN, base, rol o `cluster_name` que no
  sean los de la ejecución.
- `live_agent_worker.py`: proceso del Agent para reinicios y caídas. Un audit hook limita sus
  sockets al proxy y sus escrituras al directorio de la ejecución.
- `conftest.py`: `stack` (sesión) y `proxy` (por prueba). `proxy` reinicia los fallos y hace
  fallar la prueba si un hook del proxy falló o si alguien pidió una ruta fuera de la lista
  permitida (el proxy responde 405 y no la reenvía).

## Inyección de fallos

| Fallo | Cómo se produce |
| --- | --- |
| Respuesta perdida (`drop`) | El proxy reenvía, el Merchant confirma y el proxy cierra sin responder. |
| `reset` | Reenvía y luego cierra con RST (`SO_LINGER` 0). |
| `delay` | Reenvía y retrasa la respuesta más allá del timeout del Agent. |
| `status_503` | El proxy responde 503 sin reenviar: el Merchant nunca recibe la solicitud. |
| `scope`, `digest`, `profile`, `malformed` | Reescribe una respuesta genuina de lookup. |
| Merchant caído o reiniciado | `MerchantServer.stop/start`: proceso nuevo, mismo puerto y base. |
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
informes de `docs/b2-governance/` los citan como evidencia histórica; siguen en el historial de
Git.

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
