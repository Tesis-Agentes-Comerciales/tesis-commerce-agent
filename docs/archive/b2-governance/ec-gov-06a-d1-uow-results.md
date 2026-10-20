# EC-GOV-06A — Unidad de trabajo D1 local

## Alcance y base

Base verificada: `ab22563b2b0b3d684692f1db2b2fff71e8fc04a7`, rama
`governance/ec-gov-06a-d1-uow`, árbol inicialmente limpio. Sin commits, push,
merge, rebase ni PR. El incremento permanece aislado del ensamblado comercial.
Python sigue siendo referencia experimental; no se decide D11.

## A. Frontera transaccional

1. El backend registra snapshots operativos y un contexto explícito **enforce**
   en la bóveda existente; fija el artefacto de política y el motor medido.
   El registro del episodio del ledger continúa siendo inmutable.
2. Fuera de una transacción, el backend evalúa POL-01/POL-02 y construye la entrada
   canónica completa y el registro propuesto. `prepare` verifica el resultado por
   replay del evaluador real, los artefactos recuperables, el modo y el binding
   configurado. Admite esos bytes exactos y la identidad de operación/clave en
   memoria de esa instancia, sin conceder autoridad comercial.
3. El llamador abre `BEGIN IMMEDIATE` en la única conexión suministrada.
4. `apply` establece un savepoint. También obtiene bloqueo de escritura con un
   UPDATE de cero filas si el llamador utilizó BEGIN diferido. Revalida owner
   contra la compra, contexto, intención, feed, referencias, política, metadatos
   del episodio y el digest completo. Recupera snapshots autenticados, reconstruye
   los hechos de intención y recalcula elegibilidad con el algoritmo medido.
   Rechaza versiones anteriores de contexto/intención/feed frente al registro.
5. Para allow exige compra `received`, revisión esperada real, y selección,
   checkout y orden aún nulos. Ejecuta UPDATE condicional a `selected`, con la
   variante evaluada y `state_version + 1`. No añade stages ni usa timestamps o
   secuencia del ledger como revisión.
6. Conserva/crea la clave `checkout.create` del store comercial, rechazando otra
   clave previa. Inserta reserva durable y referencia diferida a la evidencia.
   Un verificador privado comprueba rowcount=1, compra resultante y recibo de
   operación en esa misma conexión antes de escribir `transition_committed`.
7. El llamador confirma. El resultado de `apply` es provisional hasta ese commit.
   La FK diferida impide confirmar una reserva sin evidencia. Un error de commit
   exige rollback del llamador, como demuestra la prueba correspondiente.
8. Un futuro worker podrá reclamar la reserva en otra transacción breve. `claim`
   vuelve a comprobar owner, stage, revisión, variante, ausencia de checkout/orden,
   clave y evidencia enforce allow. Solo un reclamo puede cambiar `reserved` a
   `claimed`. Debe confirmar **antes** de cualquier invocación de Merchant.
   Este incremento no implementa esa invocación ni el worker.

No evaluación de política, HTTP, firma ni operación PSP ocurre dentro de `apply`.
La reconstrucción de hechos usa únicamente SQL, descifrado y cálculo local.

## B. Interfaces y archivos

Nuevos:

- `src/commerce_agent/governance/uow.py`: `D1Application`, resultado provisional,
  error con decisión original y protocolo tipado `D1UnitOfWork`.
- `src/commerce_agent/persistence/d1_uow_sqlite.py`: `SqliteD1UnitOfWork` con
  `initialize`, `prepare`, `apply`, `record_failure`, `claim`, `recovery`.
- `tests/governance/test_d1_uow.py`: snapshots reales sintéticos, evaluador real,
  SQLite temporal y fallos/concurrencia observados directamente en SQL.
- Este informe.

Extensiones mínimas de contratos existentes, presentadas antes de implementarlas:

- `governance/provenance.py`: configuración backend `D1EnforceConfiguration`
  separada de `D1Configuration`, que **sigue rechazando enforce**. ContextData
  admite el modo explícito del contexto. Los campos comunes comparten una base.
- `persistence/provenance_sqlite.py`: registra el modo del tipo configurado; no
  convierte un contexto shadow en enforce ni modifica registros históricos.
  Recupera y autentica los contextos anteriores antes de registrar otro: rechaza
  cualquier cambio de modo del mismo run, incluso antes de su primera evidencia.
- `governance/shadow.py`: guarda explícita que mantiene al observador en shadow.

La API CAS comercial existente no se invoca ni modifica: posee su propia conexión
y commit. El adaptador aislado realiza el UPDATE condicional con la misma semántica
de revisión y los triggers originales, usando exclusivamente su conexión recibida.

## C. Atomicidad, rechazo y diagnósticos

Savepoint incluye hechos derivados que se registren, UPDATE, clave, reserva y
ledger. Si falla cualquier paso, se deshacen incluso cuando el llamador captura
la excepción y confirma el resto de su transacción. El adaptador nunca confirma.
Una segunda conexión no observa cambios pendientes de commit.

Deny normativo conserva exactamente la decisión y escribe `blocked`: cero CAS,
reserva o reclamo. Salidas inválidas o evaluación fallida no se admiten.
Los errores de aplicación llevan un código cerrado y la decisión original; no
reescriben un allow como deny del motor ni afirman evidencia persistida.

`record_failure` permite guardar, en una transacción posterior propiedad del
llamador, la misma decisión con aplicación blocked y un ApplicationFailure.
Requiere preparar esa forma fuera de SQL, autentica owner y contrasta la revisión
observada contra SQLite. No es persistencia automática del error: si el ledger
está caído tampoco puede guardar el diagnóstico. El ensamblado 06B debe hacer
explícito ese manejo posterior al rollback y contabilizar las pérdidas.

## D–E. Concurrencia, reserva e idempotencia

Una tabla opt-in `d1_checkout_operations` conserva identidad estable, run único,
efecto fijo `checkout.create`, evidencia única y su digest, digest de entrada,
merchant, variante, COP/una unidad, revisión autorizada y sucesora, clave y estado.
No almacena payloads privados del checkout. La clave es operativa, no evidencia
pública. No se exporta en `recovery` ni en el resultado de aplicación.

FK a compra y evidencia, unicidades, CHECKs y triggers protegen la asociación.
No se permite borrar, reemplazar, cambiar bindings ni devolver claimed a reserved.
La única actualización permitida es el primer reclamo. `initialize` comprueba la
definición SQL completa de tabla/triggers y rechaza esquemas debilitados incluso
con las mismas columnas. DDL es repetible y reversible mediante rollback.

Se ensayan dos conexiones concurrentes: mismo request produce un CAS y una
reutilización, nunca dos transiciones; identidades/claves distintas también
producen un solo ganador. Los dos trabajadores compiten por un único reclamo.
Reabrir SQLite conserva reserva y estado. Reintentar el request exacto no duplica
evidencia, clave ni reserva y devuelve `transitioned=False, reused=True`.
Otro request/operación/clave no sustituye el efecto ya reservado.

## F. Recuperación y límites

- Rollback previo al commit conserva la compra y no deja reserva ni evidencia
  autorizante. Un fallo de commit también debe revertirse por el llamador.
- Una reserva committed permanece `reserved` hasta un reclamo válido. Un estado
  `claimed` recuperado es incierto: no se libera automáticamente ni habilita otro
  envío. Un crash después del reclamo puede impedir progreso hasta reconciliar.
- No hay estados terminales, worker, protocolo de respuesta, consulta autoritativa
  ni redespacho. No se promete exactamente una ejecución remota.
- Persiste una frontera commit/HTTP: una cancelación posterior al reclamo requiere
  semántica de operación en curso y reconciliación revisadas en 06B.
- El perfil es D1 HP COP/una unidad. No soporta autoridad HNP, D2/D3 ni D17.
  Relojes provienen del backend; no se inventa aquí un TTL de autorización.
- Se reserva un único `checkout.create` por run. El ámbito enforce registrado
  permanece inmutable: cambiar sus fuentes/contexto exige otro episodio o un
  contrato futuro expresamente revisado; no se reutiliza la extensión de ámbitos
  shadow para sortear esa restricción.
- La integridad de fuentes y los registros autenticados no son atestación del
  proceso/host frente a un administrador o mutación arbitraria del intérprete.

## G. D23 y aceptación

| Control | Evidencia local y alcance |
| --- | --- |
| D23 | Replay fuera de SQL; decisión/CAS/clave/reserva en una transacción. Parcial: falta coordinación remota y recuperación comercial |
| C05 | Digest completo, binding configurado, referencias recuperadas, contexto/owner/run, revisión actual y elegibilidad reconstruida; sin trasplante de allow |
| C09 | Unicidades del ledger preservadas; carreras con dos conexiones y reintentos sin duplicación; los conflictos no sobrescriben historia |
| C11 | Caída de ledger después de allow, excepción tras UPDATE, transición ignorada y fallo después de reserva revierten todo el efecto local |

Matriz de pruebas nuevas (los nombres con parámetros representan varios casos):

| Requisito | Pruebas |
| --- | --- |
| 1, 13, 14 | allow_cas_ledger_reservation_and_reopen |
| 2 | normative_deny_preserved para POL-01 y POL-02 |
| 3, 7, 8 | tampered_request_or_engine_fails_closed; engine_exception_does_not_admit_or_transition; shadow_never_authorizes; configured_policy_binding_is_not_taken_from_input |
| 4, 9, 11, 12 | local_failure_rolls_back_even_when_caught; commit_failure_rolls_back_provisional_application |
| 5, 6 | state_and_binding_rejection; coherent_reevaluated_input_cannot_transplant_authority |
| 10 | concurrent_cas_and_claim_one_winner; concurrent_different_identities_do_not_authorize_second_effect |
| 15 | claim_rollback_integrity_and_effect_binding; existing_key_conflict_and_new_operation_retry_roll_back; new_operation_cannot_replace_committed_effect |
| 16 | evaluation_outside_transaction_and_network_never_invoked; inspección de imports/superficie. No prueba de integración remota |
| 17 | shadow_never_authorizes y suite shadow existente |
| 18, 19 | original_b0_migration_and_revision_guard_unchanged y suites comerciales/CAS existentes |
| 20 | no_network_or_runtime_authorization_surface y preservación SHA-256 de runtime/API/herramientas |
| Esquema y diagnóstico | initialization_idempotent_and_ddl_rollback; full_ddl_schema_definition_is_verified; diagnostics_preserve_original_allow_without_transition |
| Frontera de despacho y modo | transaction_required_and_recovery_cannot_export_uncommitted_state; claim_rechecks_owner_and_current_commercial_state; episode_mode_cannot_be_changed en ambas direcciones |

## H. Validación y preservación

Entorno realmente ejecutado: Windows 11 build 26220, Python 3.12.6, SQLite 3.45.3,
uv 0.12.15. No ejecución Linux ni servicios externos. Dependencias locales,
`UV_NO_SYNC=true`, `UV_OFFLINE=true`; sin inspeccionar ni imprimir archivos .env.
Credenciales retiradas
del entorno de los procesos de test, trazado desactivado y tres E2E sin habilitar.
Temporales y XML quedan ignorados en `.local/gov06a/`.

| Control | Resultado medido |
| --- | --- |
| Tests nuevos finales | 44 passed, 0 skipped, 54.02 s (`final44.xml`) |
| Gobernanza/CAS/compra/completion existentes | 352 passed, 0 skipped, 48.34 s (`compatibility.xml`); antes de la última guarda de modo, también cubiertos por la suite general final |
| Ruff format | 137 files already formatted |
| Ruff check | All checks passed! |
| Pyright | 0 errors, 0 warnings, 0 informations |
| Suite general final | 576 passed, 3 skipped, 1 warning, 111.49 s (`complete.xml`) |
| git diff --check | Código de salida 0; también se revisa el diff de archivos nuevos |

Las tres omisiones conocidas corresponden a `test_ap2_joint_purchase.py`,
`test_conversational_purchase.py` y `test_joint_purchase.py`: requieren Merchant,
Payment Provider y PostgreSQL. La advertencia conocida proviene de Starlette
TestClient: alias `anyio.abc.BlockingPortal` deprecado. Ninguna prueba nueva se omite.
Los avisos Git LF→CRLF de archivos no congelados no son errores de diff ni cambios
de configuración de atributos.

Comandos: tests nuevos; `pytest tests/governance tests/test_purchase_revision.py
tests/test_purchase_service.py tests/test_completion.py tests/test_completion_client.py
--ignore=tests/governance/test_d1_uow.py`; `uv run ruff format --check .`;
`uv run ruff check .`; `uv run pyright`; `uv run pytest`; `git diff --check`.
Los tests se ejecutan con `uv run`, sin sincronizar ni cambiar dependencias.

La primera corrida nueva se detuvo por schema_version ausente en la construcción
del fixture; se corrigió el fixture. Ruff/Pyright detectaron inicialmente estilo,
tipos y un acceso a compra posiblemente None; se corrigieron sin relajar checks.
Corridas intermedias: 26 passed (29.67 s), después 38 passed (48.01 s), después
41 passed (50.14 s). Son resultados previos al último refuerzo de claim.
La primera suite general, antes del último refuerzo de claim, pasó con 573 passed,
3 skipped y 1 warning en 108.32 s; la tabla anterior identifica la ejecución final.
Tras reforzar claim: 42 pruebas nuevas aprobadas en 51.06 s y suite de 574 passed,
3 skipped, 1 warning en 110.29 s. La inspección posterior añadió el rechazo
explícito de cambios de modo por episodio, comprobado con dos pruebas adicionales.

Manifest SHA-256 previo de 297 archivos versionados: solo cambian los tres archivos
existentes enumerados en B; los otros 294 conservan sus bytes. No se modifica
PurchaseService, ApprovalService, CompletionService, runtime, API, store comercial,
uv.lock, dependencias, ACP/AP2, fixtures ni resultados históricos B0. No se ejecuta
el piloto/baseline. Las nuevas inicializaciones y operaciones de base se ejercen
solo en SQLite temporal. Settings continúa admitiendo únicamente off/shadow.
La identidad medida del motor continúa siendo
`sha256:b1b5286deaf7741804bb0efda0017f1162d87cfc2de0efdacae545ab38457a14`.

## I. Pendiente exacto para EC-GOV-06B

1. Revisar y autorizar el ensamblado de un episodio enforce y su configuración,
   claves y artefactos; no convertir episodios shadow históricos.
2. Capturar snapshots operativos e identificar la operación desde el backend antes
   de D1; mantener la evaluación/admisión fuera de SQL y resolver la secuencia del
   ledger con su política explícita de conflicto/reintento.
3. Sustituir exclusivamente el paso de selección del camino enforce por UoW;
   conservar off/shadow y todas las guardas comerciales existentes.
4. Implementar el llamador con BEGIN IMMEDIATE, commit/rollback y tratamiento
   explícito de fallos de commit y diagnósticos posteriores; ningún resultado
   provisional podrá iniciar I/O.
5. Diseñar y probar worker/gateway con reclamo confirmado, misma identidad/clave,
   asociación al request remoto exacto y registro del checkout resultante.
6. Definir estados terminales, cancelación, incertidumbre, consulta autoritativa,
   recuperación y reconciliación antes de permitir un reenvío. Resolver el gap
   commit/HTTP sin prometer una transacción distribuida.
7. Probar el flujo comercial y cada frontera de crash con gateways observables;
   después preparar por separado un entorno externo aislado y expresamente
   autorizado. Estas pruebas sintéticas no sustituyen esa integración.
8. Revisar contención/coste del cálculo local bajo el bloqueo y presupuestos de
   tiempo/retención/provisión antes de un episodio externo. D11 y D17 permanecen
   abiertos y no se añaden D2/D3/HNP a este incremento.

## J. Dictamen

**READY_FOR_REVIEW**, exclusivamente para la UoW aislada y sus garantías locales.
No habilita enforce comercial, envío remoto ni cierre completo de D23.

Estado final: HEAD conserva la base exacta y la rama inicial. Tres archivos
versionados modificados y cuatro nuevos, incluido este informe; índice real
vacío. No commits, push, merge, rebase, PR ni cambios en otros repositorios.
