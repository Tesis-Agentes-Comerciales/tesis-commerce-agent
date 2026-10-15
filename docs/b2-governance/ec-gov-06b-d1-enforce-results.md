# EC-GOV-06B — Integración local D1 y frontera de despacho

## Dictamen y preparación

**Integración local: READY_FOR_REVIEW. Activación comercial: BLOCKED.**
No se instala ni habilita enforce en Settings, build_runtime, API o herramientas.
No hay implementación de despacho de producción ni reenvío automático. El puerto
de primer envío se inyecta explícitamente en las pruebas con un gateway espía.
No se promete idempotencia remota validada, exactamente una ejecución ni atomicidad
distribuida. D11 sigue abierto y Python es referencia experimental.

Base y rama verificadas con árbol inicialmente limpio:
`678ce5f7f3c3132afc3f1fa27a5e287f5af0df5d`,
`governance/ec-gov-06b-d1-enforce-integration`. Sin commit, push ni merge.

## A. Arquitectura y puntos de integración

`PurchaseService` recibe un `D1EnforceGate` opcional, distinto del observador
shadow. Configurar ambos produce error. El runtime existente no inyecta la
compuerta y Settings continúa admitiendo off/shadow, con off predeterminado.

Camino experimental enforce:

1. Store comercial crea la compra y verifica owner/request. Un run cuyo stage ya
   avanzó requiere recuperación explícita; start no vuelve a crear el efecto.
2. Gate captura copias exactas del feed/intención y comprueba artefactos/código
   cargado. PurchaseService ejecuta su ranking primario una sola vez y conserva
   las guardas B0 para no candidato/selección inválida.
3. La preparación registra procedencia, snapshots y contexto enforce reales,
   política configurada y archivo medido del motor. Una tabla append-only fija la
   entrada canónica y las identidades backend de decisión/evidencia/operación/clave.
   Reintentar después de un rollback previo al commit no genera otra identidad.
4. POL-01/02 se evalúan fuera de SQL mediante la frontera existente. Después se
   valida/admite la evidencia; las decisiones de política se verifican por replay.
5. Una transacción BEGIN IMMEDIATE ejecuta UoW: hechos actuales, owner, revisión,
   CAS real received→selected, clave, reserva y ledger. En enforce no ocurre el
   update a selected previo del camino off/shadow. El llamador confirma y otra
   conexión comprueba que la evidencia y reserva son visibles y coinciden.
6. Sin implementación explícita del puerto de envío, se detiene aquí con reserva
   durable. Con el puerto de prueba, otra transacción reclama la operación y
   confirma; otra conexión comprueba estado y claimant antes de invocarlo.
7. Fuera de SQL, se invoca una sola vez create y luego get por el ID retornado.
   Se validan modelos, variante, moneda, unidad y concordancia del snapshot/ID.
   Una transacción posterior asocia checkout_id y observación confirmed a la compra
   mediante revisión condicional. El índice único comercial rechaza un ID ya ligado
   a otra compra. Después continúan las guardas B0 de presupuesto y datos de entrega.

La secuencia de preparación/DDL no es la transacción autorizante. Evaluación y
verificación previa tampoco autorizan despacho. La autorización durable se forma
con CAS+ledger+reserva en una sola transacción y el reclamo confirmado posterior.

La única ampliación de UoW admite un runtime_guard deny validado como evidencia
bloqueada: nunca como allow. También elimina el segundo ranking redundante dentro
de `_facts`. Se conserva un cálculo independiente bajo SQL para contrastar los
snapshots y el conjunto de elegibles. Son **una llamada primaria en PurchaseService
y una revalidación en UoW**, comprobadas por perfil de ejecución; no una única
llamada total en todo el sistema. El replay del PDP no ejecuta rank_candidates.

## B–C. Bloqueo previo y atomicidad

Sin commit y reclamo confirmados no se invoca el puerto de envío. Se prueban
fallos antes y después del commit, incluido un commit que no hace persistencia.
Una respuesta local allow o un objeto de resultado UoW no se devuelve como compra
comercial exitosa. La ausencia del puerto produce d1_dispatch_blocked, no éxito.

Las denegaciones POL-02 conservan razón normativa y aplicación blocked. Errores de
motor, salida inválida o intento de devolver allow shadow se conservan como deny
operativo según el contrato, sin transición ni reserva.

**POL-01 en PurchaseService:** un seleccionado fuera de elegibles ya falla la guarda
B0 antes del PDP. No es posible producir un deny POL-01 auténtico después de pasar
esa misma guarda con los mismos hechos. Se prueba ese rechazo sin invocar el motor
y el rechazo de un resultado POL-01 de otra selección como GOV_OUTPUT_INVALID.
La denegación normativa POL-01 se sigue probando en PDP/UoW existentes. No se cambia
la semántica ni se fabrica una decisión normativa para simular un caso integrado.

Errores de UoW revierten transición/clave/reserva. Cuando es posible, otra transacción
guarda el mismo allow con aplicación blocked y un ApplicationFailure, sin convertirlo
en deny del motor. Si el ledger no está disponible se contabiliza pérdida de
diagnóstico y se emite un mensaje cerrado, sin payload ni excepción remota.
Un registro ya confirmado nunca se reescribe tras una excepción posterior al commit.

## D. Concurrencia

- Dos PurchaseService.start sobre el mismo run y hechos producen una compra
  preparada y un rechazo, una sola llamada create y una sola decisión autorizante.
- Dos conexiones/workers compitiendo por la reserva producen un único claim.
- La reserva conserva operación, evidencia y clave. Un crash antes del commit de
  autorización permite reintentar los hechos originales con esas mismas identidades;
  la prueba compara las filas de entrada y reserva persistidas.
- Un run ya seleccionado, reclamado, confirmado o incierto no vuelve a start.
  No existe lease, liberación automática ni worker que redespache tras reiniciar.

## E. Contrato Merchant inspeccionado y bloqueo comercial

Inspección **de código y pruebas existentes**, sin ejecutar Merchant/PostgreSQL:
Merchant HEAD `70ce92b97cee3d7aabf8d287f0bf700816dc584b`, main, árbol limpio.

- `src/commerce_lab/checkout.py`, CheckoutService.prepare/_existing: guarda
  fingerprint y create_snapshot, con unicidad de run/actor/clave hash. Un replay
  interno con el mismo fingerprint devuelve el snapshot original; otro contenido
  produce IDEMPOTENCY_CONFLICT.
- `src/commerce_lab/acp.py`, ACPCheckoutService.create: **antes** de ese replay
  consulta la oferta actual y reconstruye expected_offer_revision/delivery_context.
  Una oferta cambiada o ausente puede impedir recuperar el resultado con el mismo
  cuerpo ACP original. No se afirma que esto cree un duplicado: el punto no probado
  es la recuperación autoritativa del checkout ya creado.
- `tests/integration/test_acp_http.py` contiene un caso de misma clave/mismo body y
  conflicto de body, con catálogo estable. No se ejecutó esa prueba: necesita la
  preparación PostgreSQL de otro ticket.
- La API observada ofrece GET por checkout_id, no una consulta autoritativa por
  clave cuando se perdió ese ID. ACPClient.create_checkout envía Idempotency-Key;
  get_checkout exige un ID conocido. No se inventa un endpoint de recuperación.

Dependencia precisa para activar comercialmente: ticket separado Merchant y prueba
aislada de pérdida de respuesta tras commit, reinicio, concurrencia y cambio/borrado
de oferta. Debe demostrar **una** alternativa revisada:

1. Lookup autenticado por scope run/actor y clave/fingerprint del request original,
   que recupere ID/resultado durable sin crear otra operación; o
2. Replay ACP basado en la solicitud original antes de reinterpretar el catálogo
   actual, con scope, persistencia y conflicto definidos y ensayados.

También falta probar la correlación autoritativa checkout↔clave/solicitud/episodio.
GET del ID retornado permite verificar coherencia de contenido y rechazar un ID
distinto o ya usado localmente; **no prueba** que un ID ajeno desconocido, pero con
la misma variante, pertenezca a esta creación. Por ello no existe adaptador de
producción de InitialCheckoutDispatch y no se abre enforce mediante configuración.

## F. Estados y escenarios de crash

La tabla original 06A permanece intacta; dos extensiones opt-in append-only guardan
la identidad de entrada y una observación de resultado por operación.

| Estado operativo | Semántica |
| --- | --- |
| reserved | CAS/evidencia/reserva confirmados; aún sin reclamo de envío |
| claimed sin resultado | Posible envío; al recuperar se informa uncertain, nunca permiso de reenvío |
| confirmed | ID y snapshot coherentes verificados; compra y resultado asociados en SQL |
| uncertain | Timeout, respuesta inválida o persistencia posterior incierta; sin ID no verificado |
| definitive_failure | Definición reservada para prueba autoritativa de no creación; no se produce ni se ofrece API para inventarla en este incremento |

| Frontera ejercitada | Resultado observado |
| --- | --- |
| Crash después de UPDATE, antes de commit autorizante | Rollback; cero reserva/evidencia autorizante/create; entrada previa conserva identidad |
| Commit falla antes/después de persistir o no persiste | Cero HTTP; el estado durable se inspecciona y no se supone rollback si ya confirmó |
| Crash tras autorización, antes del claim | reserved durable; segundo start bloqueado |
| Crash tras claim, antes de HTTP | uncertain al reabrir; cero create y ningún reenvío |
| Timeout después de posible creación simulada | Un create; uncertain; mismo ID de operación/clave; cero segundo create |
| Respuesta malformada, variante/moneda/ID discrepantes | uncertain; no checkout_id asociado a esa compra |
| ID ya asociado a otra compra | Restricción comercial revierte la asociación; otra compra intacta; uncertain |

Las caídas se inyectan como excepciones/interrupciones en el proceso de test y se
reabre SQLite; no son ensayos de corte de energía o crash de procesos remotos.
Si falla la persistencia del diagnóstico, un claim durable sin resultado sigue
siendo incierto. No hay recuperación automática del resto de la preparación
comercial tras confirmed ni resolución automática de decisiones blocked ya
registradas: requieren revisión/backend explícito y un contrato posterior.
Nunca se propone otra compra/clave para resolver una incertidumbre remota.

## G. Comparación off/shadow/enforce y ausencia de SQL durante I/O

Las pruebas comparan el PurchasePreparation completo: resultado comercial igual
en off, shadow allow, shadow deny no bloqueante y enforce permitido simulado.
Off/shadow conservan create→update→get. Enforce añade un get de verificación antes
de update/get. El observador shadow sigue sin autoridad y no se mezcla con gate.

En cada método comercial del gateway espía otra conexión debe adquirir BEGIN
IMMEDIATE y liberarlo. Las pruebas fallan si existe un bloqueo de escritura del
agente. Es evidencia sobre los gateways efectivamente utilizados por PurchaseService
en el test, no una prueba de red real ni de ausencia de contención en producción.

## H. Medición offline reproducible

Comando: `uv run python scripts/measure_d1_enforce.py --output
.local/gov06b/latency-final.json --repetitions 5` (en una sola línea).

Entorno verificado: Windows 11 build 26220, Python 3.12.6, SQLite 3.45.3,
uv 0.12.15. Cinco tripletas, orden de modos rotado; cada caso usa SQLite temporal
nuevo, fixture sintético local y gateway espía. Claves criptográficas efímeras
inyectadas en la prueba; ninguna credencial externa. Medición serial sin suite
general simultánea. No se fuerza caché fría del SO. GC entre casos fuera del timer.

| PurchaseService.start | Mediana ms | Mínimo ms | Máximo ms |
| --- | ---: | ---: | ---: |
| off | 46.7378 | 39.3124 | 117.4043 |
| shadow | 572.1302 | 556.1018 | 587.8147 |
| enforce simulado | 1045.2166 | 1039.8246 | 1059.6517 |

Añadido **pareado** mediano frente a off: shadow **523.3623 ms**, enforce
**1000.5122 ms**. Incluye verificaciones y el get adicional simulado; no equivale
a latencia añadida contra un Merchant real. Los cinco valores por modo/diferencia
y metadatos completos quedan en latency-final.json, no en resultados históricos B0.

| Fase enforce | Mediana ms |
| --- | ---: |
| Bootstrap/captura de artefactos al construir gate | 238.8576 |
| Captura de hechos y nueva medición de artefactos | 237.4821 |
| Ensamblado/recuperación de snapshots y commit preparatorio | 246.6411 |
| Preparación UoW y DDL opt-in | 247.2714 |
| Evaluación del motor POL-01/02 | 0.3977 |
| Frontera de evaluación, incluida configuración/verificación | 2.6402 |
| Admisión/verificación de evidencia y replay | 231.2265 |
| BEGIN IMMEDIATE autorizante hasta commit | 15.3029 |
| BEGIN de claim hasta commit | 2.3241 |

Bootstrap queda fuera del timer de start. Las fases no son una partición aditiva
de medianas: la evaluación pura está incluida en su frontera y no se suman ambas.
La admisión incluye medición/verificación real de fuentes, no solo SHA de la
decisión. El coste dominante es captura/verificación repetida, no POL-01/02.
No se eliminan checks de integridad para esconder ese coste. La optimización del
ranking se acredita por número de llamadas, sin atribuirle una mejora causal en ms
que no se midió antes/después. No se fija SLO ni se extrapola a integración real.

El registro de contextos y artefactos también mantiene una transacción preparatoria
de escritura dentro de snapshots_ms; no se aisló su hold time del resto del
ensamblado. Los 15.3029 ms corresponden solo a la transacción autorizante, no al
máximo de todos los bloqueos SQLite. Esta contención preparatoria requiere revisión
antes de activar el camino real. La variación visible en off confirma que cinco
muestras no acreditan una distribución estable de producción.

Primera medición: terminó los casos pero falló la limpieza Windows con WinError 32
por manejadores SQLite pendientes; no produjo métricas publicables. El medidor
ahora recolecta conexiones legacy fuera del intervalo medido antes de limpiar su
directorio aislado. No se modifica el store comercial para resolverlo. Las tablas
anteriores corresponden a la repetición completa exitosa.
La medición previa a las dos últimas guardas se conserva en latency.json; la tabla
publicada usa latency-final.json, obtenida después de ellas. No se atribuye a esas
guardas la diferencia entre las dos corridas.

## I. Validación y preservación

| Control | Resultado |
| --- | --- |
| Tests nuevos PurchaseService | 42 passed, 0 skipped, 56.40 s (`final-targeted.xml`) |
| Regresiones compra/shadow/CAS/evidencia/procedencia/UoW/completion | 396 passed, 0 skipped, 102.44 s (`regressions.xml`) |
| Ruff format | 142 files already formatted |
| Ruff check | All checks passed! |
| Pyright | 0 errors, 0 warnings, 0 informations |
| Suite general | 618 passed, 3 skipped, 1 warning, 152.72 s (`full-final.xml`) |
| git diff --check | Sin errores; incluye revisión con índice temporal para los archivos nuevos |

Ninguna prueba nueva se omite. Las tres omisiones conocidas son
`tests/e2e/test_ap2_joint_purchase.py`, `test_conversational_purchase.py` y
`test_joint_purchase.py`: requieren Merchant, Payment Provider y PostgreSQL.
La advertencia es DeprecationWarning de Starlette TestClient por el alias
`anyio.abc.BlockingPortal`. Los avisos Git LF→CRLF de archivos no congelados no
son errores de diff y no se cambió la configuración de atributos. Solo se ejecutó
Windows; no se declara validación Linux.

Comandos pytest nuevos y de regresión se ejecutaron mediante uv run, seguidos de
`uv run ruff format --check .`, `uv run ruff check .`, `uv run pyright`,
`uv run pytest` y `git diff --check`. UV_NO_SYNC/UV_OFFLINE activos; credenciales
externas retiradas del entorno del proceso; trazado desactivado. Temporales/XML
en `.local/gov06b/`. No se inspeccionaron ni imprimieron archivos .env.

Primera matriz nueva: 32 passed en 46.32 s; la ampliación añadió comparación de
modos, workers, pin de artefacto, inmutabilidad y retry. Incidencias iniciales de
estilo/tipos fueron corregidas sin relajar pruebas ni cambiar contratos publicados.
La matriz ampliada inicial pasó 40 casos en 70.70 s y la primera suite general
616 passed, 3 skipped, 1 warning en 167.03 s. La revisión posterior añadió el rechazo
explícito de un resultado nulo del gate, sin assert como guarda de autoridad, y la
revalidación de campos crudos de modelos devueltos por el gateway; una instancia
construida con id booleano tampoco puede confirmarse. La tabla identifica la
validación final que incorpora esas dos pruebas adicionales.

Las 20 exigencias se ejercitan en test_enforce_integration.py y regresiones:
allow; deny/guardas B0; motor inválido/shadow; ledger; CAS; carreras de start y claim;
crashes/commit; timeout; respuesta malformada/ajena; retry de identidad; bloqueo de
recuperación; comparación de modos; reapertura/integridad; privacidad y probes de
bloqueo en las llamadas del gateway. También se prueban DDL repetible/rollback,
esquema incompatible y rechazo de mutación/reemplazo de las nuevas tablas.

Manifest previo SHA-256 de 301 archivos versionados: solo cambian PurchaseService
y el adaptador UoW; los otros 299 conservan sus bytes. Sin cambios en código core
de políticas, uv.lock, dependencias, ACP/AP2, Settings/runtime/API, fixtures ni
documentos/resultados históricos. No se ejecutó baseline/piloto B0, migraciones
Merchant, PostgreSQL, PSP, E2E externos ni LLM. Todas las bases abiertas por el
incremento y la medición son temporales de prueba.

## J. Pendientes

- **D17:** vigencia, refresco, términos materiales y correlación del consentimiento;
  este D1 no define TTL nuevo ni resuelve la frescura para aprobar/completar.
- **D2-HP:** intento humano, firma/aprobación/estado atómicos y recuperación propios;
  siguen las guardas HP B0, sin atribuirles una nueva garantía transaccional D2.
- **D11:** comparar motores con evidencia revisada. No se selecciona OPA ni se
  presenta Python como decisión definitiva.
- **HNP/D2/D3/D17:** no implementados; requieren sus tickets y dependencias.
- **Despacho comercial:** contrato y ensayos Merchant descritos en E; adaptador
  verificado, conciliación y estados terminales, retención/provisión, recuperación
  de preparaciones y política de reintento local que preserve historia/operación.
- La frontera commit→HTTP permanece distribuida: no se elimina una carrera con
  cancelación posterior al claim. No activar por observar solo una suite offline verde.

## K–L. Archivos y estado final

Modificados: `src/commerce_agent/application/purchase.py` y
`src/commerce_agent/persistence/d1_uow_sqlite.py`.

Nuevos: `src/commerce_agent/governance/enforce.py`,
`src/commerce_agent/persistence/d1_dispatch_sqlite.py`,
`tests/governance/test_enforce_integration.py`, `scripts/measure_d1_enforce.py` y
este informe. Sin cambios en otros repositorios.

HEAD y rama conservan la base de preparación. Índice real vacío; diff para revisión,
sin commit/push/merge/PR. **READY_FOR_REVIEW local; BLOCKED para activación comercial.**
