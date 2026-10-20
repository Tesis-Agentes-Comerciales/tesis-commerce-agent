# EC-GOV-05A — Revisión persistente de purchases y CAS

> **Nota 2026-10-20.** Rutas históricas, no se reescriben. `scripts/evaluation_mutate_offer.py` está
> en `evaluation/scripts/`.

Dictamen: **READY_FOR_REVIEW**, dentro del alcance de la fila local persistente.
Validación real: Windows, Python 3.12.6, SQLite 3.45.3, 2026-10-14.
No se ejecutó Linux. No se implementó D1 shadow ni enforcement.

## Base y fuentes

Rama `governance/ec-gov-05a-purchase-revision`, inicialmente limpia.
HEAD `08582af94a15c90bd6b41b0b1e62740252adceaa`;
`git merge-base --is-ancestor 578d257 HEAD` terminó con código 0.
Incluye EC-GOV-04 y el informe de readiness EC-GOV-05.

Se leyeron policy-v1.md, policy-acceptance-v1.md,
ec-gov-05-shadow-readiness.md, system-design-v1/roadmap.md, persistence/sqlite.py,
application/purchase.py, completion.py y persistence/governance_sqlite.py.
Se buscaron referencias a purchases y SQL INSERT/UPDATE/DELETE/REPLACE en el
código Python y SQL del repositorio, incluidos scripts y tests.

## A. Inventario de escritores

Todos los escritores comerciales encontrados están en
`src/commerce_agent/persistence/sqlite.py`:

| Método | Escritura y llamadores |
| --- | --- |
| create_purchase | INSERT recibido, reintento ON CONFLICT DO NOTHING; PurchaseService.start |
| update_purchase | Etapa, variante, checkout y orden; start, approve, cancel, abandono por error y cancelación por presupuesto de PurchaseService |
| claim_completion | Reserva approved/uncertain → completing; CompletionService.complete y reconciliación |
| release_completion | completing → approved/uncertain/failed; recuperación de CompletionService.complete |
| record_completion | completing → completed y order_id; finalización/reconciliación de CompletionService |
| compare_and_set_purchase | Nuevo escritor explícito condicionado por run_id y revisión; todavía sin llamadores comerciales |

No se encontraron escritores adicionales directos de purchases en scripts,
otros módulos ni SQL de producción. Los tests existentes usan los métodos del store.
El nuevo test de revisión incluye SQL directo deliberado sobre bases temporales.
`scripts/evaluation_mutate_offer.py` opera sobre el entorno comercial externo;
se inspeccionó su código, no se ejecutó.
El ledger de gobernanza no escribe purchases. API, conversación y runtime llegan
al store a través de los servicios; no se modificaron esos módulos.

## B–C. Semántica y migración

La revisión corresponde al contenido persistente de **una fila de purchases**.
La migración añade:

```sql
state_version INTEGER NOT NULL DEFAULT 1
CHECK(typeof(state_version) = 'integer' AND state_version >= 1)
```

Una fila nueva empieza en 1. Una fila histórica empieza en 1 en el momento de
migración: representa su estado real entonces, sin reconstruir ni fingir el
número de transiciones anteriores.

Campos materiales: run_id, owner_id, request_text, stage, selected_variant_id,
checkout_id, order_id y created_at. Cualquier cambio de uno o varios de ellos
en un UPDATE requiere exactamente +1. `IS NOT` compara también cambios desde/hacia
NULL. updated_at es bookkeeping y no incrementa por sí solo. No se consulta reloj,
timestamp ni secuencia del ledger para calcular la revisión.

Dos triggers persistentes exigen versión inicial 1 e incremento exacto según el
cambio material. Los escritores actualizados calculan el incremento en el mismo
UPDATE. Una sentencia SQL directa que omita la revisión al cambiar contenido
falla con `purchase_revision_mismatch`; no queda una mutación sin contabilizar.
También se rechazan decrementos, saltos e incrementos sin cambio material.
No hay un trigger que haga una segunda actualización oculta.

La columna y los triggers se instalan en una transacción BEGIN IMMEDIATE,
después de las migraciones heredadas que usan executescript. No se afirma
atomicidad de todas las migraciones históricas del inicializador.
La inicialización repetida no reinicia la revisión ni duplica triggers.
Una columna existente con tipo, nulabilidad o default incompatibles se rechaza;
valores existentes no enteros o menores de 1 también se rechazan.
No se reescribe ninguna columna histórica para incorporar state_version.

La monotonía se refiere a la vida de la fila conservada y a estos escritores
INSERT/UPDATE. No se introduce retención, tombstone ni protección contra un
administrador que borre/recree la fila, use REPLACE, modifique el esquema o quite
triggers. No existen caminos B0 de borrado/reemplazo de purchases en el inventario.
Si se incorporan, deberán conservar identidad/revisión o definir explícitamente
una nueva generación antes de reutilizar run_id; no son operaciones soportadas
por esta primitiva CAS.

## D. CAS y límites transaccionales

`purchase_state_version(run_id) -> int | None` lee la revisión SQL real.
La proyección legacy de `purchase()` conserva sus nueve campos originales.

`compare_and_set_purchase` recibe expected_state_version entero positivo estricto,
etapa y los mismos campos opcionales de update_purchase. None conserva el campo;
no incorpora una API para borrarlo. Devuelve PurchaseCASResult tipado:

| status | Significado |
| --- | --- |
| applied | Coincidió la revisión y hubo cambio material; versión resultante = esperada + 1 |
| unchanged | Coincidió, pero no cambió contenido material; no reclama transición |
| stale | No coincidió; no escribió la fila; informa revisión observada bajo el mismo bloqueo |
| not_found | No existe la fila |

BEGIN IMMEDIATE abarca el UPDATE condicionado por run_id/state_version y la
lectura que clasifica su resultado. Una excepción revierte la transacción.
Dos conexiones que intentan cambiar la misma revisión producen un aplicado y
un obsoleto. Varios no-ops pueden devolver unchanged: ninguno aplica transición.
Los errores SQL/busy/timeout se propagan, no se convierten en applied.

La primitiva posee su conexión y commit. **Todavía no es una UoW compartida con
el ledger**; no se puede componer su commit independiente con record_decision y
afirmar atomicidad. La integración futura deberá compartir conexión/transacción.
No valida autoridad ni la máquina de estados del servicio y no es una herramienta
expuesta al modelo. Los escritores B0 mantienen revisión, pero no comparan una
versión esperada; siguen siendo susceptibles a carreras en sus precondiciones.

## E. Pruebas y resultados medidos

Ejecuciones offline con el entorno existente, UV_NO_SYNC/UV_OFFLINE,
PYTHONDONTWRITEBYTECODE, E2E deshabilitadas y variables de credenciales de servicios
retiradas únicamente del proceso de tests, sin mostrar valores. Bases de tests y
temporales bajo `.local/gov05a`; no se abrió una base operativa real.

| Corpus nuevo | Casos aprobados |
| --- | ---: |
| Compra nueva, lectura/proyección y creación idempotente/conflictiva | 1 |
| Migración histórica, con/sin owner legacy, repetición y escritura posterior | 2 |
| Esquema previo incompatible | 1 |
| Escritores update/claim/release, no-op y fallo | 1 |
| SQL directo por cada campo material, omisión rechazada y +1 aceptado | 8 |
| NULL, timestamps y rollback, recursive_triggers 0/1 | 2 |
| Revisión inicial incorrecta | 5 |
| Conflicto UNIQUE, compra ausente y CAS fallido | 1 |
| CAS aplicado, obsoleto, unchanged y ausente | 1 |
| Revisión CAS inválida/coercible/fuera de rango | 8 |
| Concurrencia de dos conexiones, un ganador | 1 |
| Escritor B0 invalida CAS antiguo, pero continúa sin CAS propio | 1 |
| PurchaseService y aprobación con gateway fake | 1 |
| Cancelación y retry sin nueva transición/llamada | 1 |
| Completion/replay y límite de revisión de approvals | 1 |
| Fallo tras UPDATE de completion: rollback de revisión y tablas relacionadas | 1 |
| **Total nuevo** | **36** |

| Comando | Resultado real |
| --- | --- |
| uv run pytest tests/test_purchase_revision.py --junitxml=.local/gov05a/unit.xml | 36 passed in 2.71s, ninguna omitida |
| uv run ruff format --check . | 122 files already formatted; código 0 |
| uv run ruff check . | All checks passed!; código 0 |
| uv run pyright | 0 errors, 0 warnings, 0 informations; código 0 |
| uv run pytest --junitxml=.local/gov05a/full.xml | 459 passed, 3 skipped, 1 warning in 19.30s; código 0 |
| git diff --check | Sin diagnósticos; código 0 |

La suite completa incluye de nuevo los 36 casos nuevos, tras corregir dos
diagnósticos iniciales de Ruff en el test (SIM117 y E501).
Regresiones existentes: PurchaseService 10, approval 6, completion 12,
completion_client 1 y persistence 6, todas aprobadas. Gobernanza D1: 128;
ledger: 92. El test de integridad ACP también aprobó.

Únicas omitidas: test_ap2_joint_purchase, test_conversational_purchase y
test_joint_purchase; requieren Merchant/Payment Provider/PostgreSQL.
Advertencia: Starlette TestClient usa el alias deprecated
anyio.abc.BlockingPortal; no se modificaron dependencias para ocultarla.
Los fakes verifican las llamadas dentro del perímetro de tests; no se presenta
esta ejecución como integración comercial ni como benchmark externo.

## F–I. Compatibilidad, pendientes y entrega

Único archivo previamente versionado modificado: persistence/sqlite.py.
Nuevos archivos: tests/test_purchase_revision.py y este informe.
La comparación SHA-256 previa/posterior de los 285 archivos versionados deja
284 idénticos y solo ese store cambiado. Por tanto, contratos, cuatro fuentes
del fingerprint del motor, ACP congelado, fixtures, uv.lock, configuración,
servicios comerciales y resultados históricos B0 conservan sus bytes.
No se reejecutó el baseline/piloto B0 ni se modificaron otros repositorios.

La revisión no cubre mutaciones independientes en approvals, approval_terms,
orders, webhook_events, completion_results, operation_keys, conversaciones,
checkpoints ni graph threads; tampoco contenido remoto del checkout o pagos.
record_completion ya escribe proyección de orden, resultado, etapa de purchase
y consumo de aprobación en una transacción existente; su incremento participa
en ese rollback, pero no convierte state_version en revisión general de esas
tablas ni de recibos. Guardar una aprobación por separado no incrementa purchase.

Se resuelve la ausencia de revisión real, no todos los bloqueos EC-GOV-05:
siguen pendientes snapshots protegidos/versionados de intención/feed, bindings
operativos de identidad/política/relojes, ensamblador y observación shadow.
D2 y HNP requieren sus contratos/guardas, reservas, revocación, criptografía y
atomicidad propias; no se implementaron ni se deducen de esta revisión.
No se implementó OPA, D1 shadow, D2/D3, HNP ni D17.

HEAD y rama se conservaron. Working tree contiene los tres archivos descritos;
no se hizo commit, push, PR ni merge. Detenido para revisión humana.
