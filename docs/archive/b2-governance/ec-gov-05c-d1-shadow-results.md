# EC-GOV-05C — Integración D1 shadow

Dictamen: **READY_FOR_REVIEW**, en el perímetro offline probado.
Motor: **Python de referencia experimental**. D11 continúa abierto.
No se habilita enforce ni se afirma integración externa Merchant/PSP validada.

## Base y preparación

Fecha local: 2026-10-14, America/Bogota. Entorno real Windows, Python 3.12.6,
SQLite 3.45.3. No se ejecutó Linux.
Rama inicialmente limpia: `governance/ec-gov-05c-d1-shadow-integration`.
HEAD esperado/observado: `fa38ba246eca21de7c6d617921b4b7330bc2b78c`.
No se cambió HEAD ni se creó rama, commit, push, merge, rebase o PR.

Se inspeccionaron PurchaseService, selección, ConversationService, runtime,
Settings, identidad, store/CAS, contratos/evaluador/evidencia/artefactos/puertos,
procedencia y sus stores, policy-v1, aceptación, roadmap e informes 05/05A/05B.

## A. Arquitectura y configuración

PurchaseService acepta un puerto D1SelectionObserver opcional. Off usa None:
conserva una sola llamada al ranking y no captura ni evalúa datos D1.
Runtime construye el observador únicamente si se configura shadow; AgentRuntime
conserva su diagnóstico. No se cambiaron las rutas HTTP ni ConversationService.
Este último sigue entregando su intención, owner, feed del turno y variante real.

En shadow, se captura el feed/intención antes del ranking, sin enviar request_text,
buyer, destino, aprobación ni credenciales al observador. Se conserva una copia
privada y se contrasta con los objetos utilizados. Tras las guardas de selección
y la escritura B0 de selected, se observa el mismo SelectionResult y la variante
elegida, antes de obtener la clave y llamar create_checkout. No se vuelve a rankear
ni se sustituye una elección válida por el primer candidato.

Los archivos/paquetes de procedencia se miden realmente y se contrastan con lo
registrado en el vault. Las definiciones de funciones/métodos cargados de intent,
selection y feed se comparan con el código compilado de las fuentes capturadas,
sin ejecutar archivos archivados. Se comprueban también helpers sustituidos y
el alias real del ranker. Esto es una comprobación local de coherencia, no una
atestación del intérprete ni protección frente a un administrador del proceso.
Una mutación detectada de datos/código o un perfil no representable degrada la
observación. En particular, elegibles de otra moneda no se presentan como COP,
aunque B0 pueda procesarlos válidamente.

Configuración nueva, sin valores secretos incorporados al repositorio:

| Variable | Semántica |
| --- | --- |
| AGENT_GOVERNANCE_MODE | off por defecto; únicamente off/shadow |
| AGENT_D1_POLICY_SET_ID | Namespace explícito de configuración backend |
| AGENT_D1_POLICY_REVISION | Entero positivo expresado como string de configuración; validado al preparar shadow |
| AGENT_D1_KEY_ID | Identificador de las claves provisionadas |
| AGENT_D1_HMAC_KEY_HEX | SecretStr, clave HMAC hexadecimal >=32 bytes |
| AGENT_D1_AES_KEY_HEX | SecretStr, clave AES-256-GCM hexadecimal de 32 bytes, independiente |
| AGENT_D1_POLICY_PATH / AGENT_D1_POLICY_DIGEST | Opcionales conjuntamente: archivo de política revisado y pin SHA-256 confiable |
| AGENT_D1_COMMERCIAL_CLOCK_ID | Identifica un callback comercial inyectado; requerido si se usa ese callback en shadow |

Sin archivo, se construye el perfil POL-01/POL-02, prepare_checkout, COP/una unidad
y merchant configurado mediante la fábrica de 05B; namespace/revisión/claves son
obligatorios y explícitos. No se utiliza d1-policy.json. Con archivo, se verifican
bytes, digest, namespace, revisión y perfil D1; se puede observar una política
restrictiva que deniegue la acción o el merchant. El vault mantiene el registro
global de bytes únicos por policy_set_id/versión. No se añaden acciones D2/D3.

Autoridad usa UTC real del servidor. El reloj comercial es el mismo perfil que
el runtime comercial: sistema, sandbox fijo configurado o callback identificado,
con prioridad del callback existente. Configuración shadow ausente/incorrecta
produce UnavailableShadowObserver, diagnóstico degraded y log fijo, sin impedir B0.
Off no decodifica claves ni abre archivos de políticas ni prepara tablas B2.

Las pruebas inyectan claves efímeras. **No se provisionaron claves reales ni se
activó shadow en una configuración operativa existente.** Siguen pendientes la
custodia, ACL, backup, retención y rotación. AES-GCM cifra payloads de procedencia,
no el archivo SQLite completo ni las tablas B0 previas.

## B–C. Captura, transacciones, correlación y replay

1. PurchaseService crea/valida run/owner mediante B0 y ejecuta ranking una vez.
2. Tras validar candidato, el observador abre su conexión con FK y transacción.
   Registra episodio/vault/contexto/snapshots y lee state_version real.
   Prepara SelectionPolicyInput HP/shadow desde las fuentes recuperables y el
   conjunto elegible de esa invocación.
3. Conserva un intento durable con decision_id/evidence_id emitidos por backend.
   Se registran artefactos medidos y ámbito de fuentes; termina la transacción.
4. Invoca ReferencePolicyEvaluator fuera de transacción de escritura y mide su
   ejecución mediante perf_counter_ns. La latencia del registro es el entero
   medido en milisegundos, truncado; 0 sería válido para una invocación <1 ms.
   La metadata provisional requerida por la interfaz pura no se persiste: se
   completa con duración real y decided_at UTC después de la llamada.
5. Construye DecisionRecord observed_shadow, con transition_ref y versión
   resultante nulos. prepare_decision admite/reproduce el cuerpo fuera de
   transacción. Una transacción breve asigna secuencia y escribe la evidencia.
6. Se cierra la conexión antes de volver a la llamada comercial. El gateway espía
   comprueba que otra conexión puede adquirir BEGIN IMMEDIATE antes de create.

El orden comercial sigue siendo create → update → get; no hay llamadas nuevas
del observador a Merchant, Payment Provider, LLM ni otros servicios externos.
Un deny válido queda como observación y B0 continúa. Si falla configuración,
captura, motor, admisión o escritura, hay pérdida de observación, no una decisión
de política inventada. No se afirma persistencia cuando falló el ledger.
Las guardas B0 de candidato, presupuesto, ownership, HP/AP2 siguen existiendo.

### Extensión mínima y aditiva del ledger

El ledger original fijaba source_refs para toda la compra, impidiendo observar
otra versión de intención/feed sin sustituir historia. No se relajó register_episode
ni se sobrescribió el episodio: se añadió governance_d1_shadow_scopes, con un
ámbito inmutable por decision_id, exclusivo de shadow.

Solo pueden variar source_refs; run/owner/trace/fase/modo/context_version/binding
y artefactos deben coincidir con el episodio original. Registrar/reproducir/leer
resuelve el ámbito de la decisión; los registros legacy siguen usando el original.
No se puede asociar retrospectivamente un ámbito a una decisión legacy ni usar
la extensión como permiso enforce. Triggers rechazan cambio, borrado y reemplazo.

Antes de conectar PurchaseService se ejecutó el gate de migración: rollback de
DDL, inicialización repetida, preservación del episodio y evidencia histórica,
replay antiguo/nuevo y rechazo de cambios de identidad. **97 pruebas aprobaron**,
incluidas las 92 existentes del ledger. No se abrió una base operativa para ello.

La segunda tabla nueva, d1_shadow_attempts, es append-only y conserva input
canónico/evidence_id por clave de observación. No modifica purchases ni CAS.
Las tablas adicionales se inicializan solo en una observación shadow válida.

### Regla de reintentos

La clave separada de intento, con dominio commerce-d1-shadow-attempt/v1, incluye
todos los hechos/refs/configuración/selección/revisión real e identidad del motor,
excluyendo IDs emitidos y relojes de observación. Un retry equivalente reutiliza
el input original completo, sus tiempos realmente capturados y sus IDs; si ya hay
evidencia, no la sobrescribe ni vuelve a aplicar una transición.

Un cambio de intención, feed, selección o revisión real produce otra decisión.
Repetir PurchaseService.start después de que B0 avanzó etapas también cambia la
revisión real: no se finge que sea el mismo estado. Si falló la escritura, un retry
del mismo estado recupera su intento e identidad pendientes, sin inventar evidencia.
Dos observadores concurrentes producen un solo registro para el mismo intento.
Las secuencias se contrastan bajo bloqueo; una colisión se reintenta hasta tres
veces con nueva admisión fuera de transacción, conservando unicidad y sin huecos
por una reserva ficticia. Esta equivalencia es solo observacional POL-01/02;
no constituye un contrato de frescura/consentimiento ni de reutilización enforce.

## Ejemplo verificable del servicio

Artefactos generados por el test real de PurchaseService con gateway espía:

- `.local/gov05c/example.json`: DecisionRecord completo, replay, operaciones y diagnóstico.
- `.local/gov05c/example.sqlite`: copia de la base temporal generada por ese test.
- `.local/gov05c/latency.json`: cinco pares de muestras y métricas separadas.

Resultado final de la ejecución, no un fixture fabricado de gobernanza:

| Campo | Valor observado |
| --- | --- |
| run_id realmente persistido | run-real |
| decision_id emitido | decision:4841fb749a1ab4f0ffc1be06bde6dcf3 |
| evidence_id emitido | evidence:f0b05ce92805839e6958b4da5dcd9e9c |
| trace_id persistido | trace:27fcd1295818288a50d7a9eca6637b96 |
| Revisión capturada / final comercial | 2 / 4 |
| Outcome / reglas | allow; POL-01 pass, POL-02 pass |
| Aplicación | observed_shadow; ninguna transición reclamada |
| authority_now | 2026-10-15T03:21:57.584Z (14 de octubre en America/Bogota) |
| commercial_now | 2026-09-20T12:00:00.000Z, perfil sandbox explícito coherente con el gateway de prueba |
| Latencia registrada del evaluador | 2 ms |
| input_digest | sha256:3f9547218097e9e1ab8f815f3be14221328b6a449af17b97852e70858aaedaf2 |

SHA-256 del example.json final:
`79088947943334c4711c2b055837f8fd73fb4aa59af9dae77798c5dc1dd75da8`.
Se reabrió independientemente example.sqlite y se verificó get/replay mediante
SqliteGovernanceEvidenceStore: mismo outcome y resultados de ambas reglas.
Los snapshots de intención/feed se recuperaron y compararon bajo el owner correcto
dentro del test; el owner incorrecto fue rechazado. Las claves efímeras no se
conservan en estos artefactos: el replay público del PDP no las requiere; volver
a descifrar el vault después del test sí requeriría esas claves. No se presenta
esta copia como una base operativa recuperable sin custodia de claves.

## D–E. Comparación, pruebas y latencia

31 casos nuevos: **26 integración** y **5 extensión de ámbitos**; ninguno omitido.

| Requisito / caso | Resultado final |
| --- | --- |
| Off/shadow: respuesta y operaciones | PurchasePreparation iguales; create/update/get en ambos, solo una llamada a rank_candidates en shadow |
| Correlación y punto del control | Evidencia existe antes de create_checkout; run/trace/owner/ref/revisión vinculados |
| Selección real | Se conserva también una variante elegible distinta de la primera del ranking |
| Snapshots y privacidad | Intención/feed completos recuperados; PDP/evidencia sin request, títulos, PII, direcciones ni tokens sintéticos adversariales |
| Replay | Registro recuperado y replay lógico igual; archivo SQLite reabierto independientemente |
| Deny | Artefacto restrictivo fijado produce POL-02 deny; B0 termina awaiting_confirmation con las mismas llamadas |
| Fallos | Evaluador, vault, ledger, admisión y excepción inesperada del observador no bloquean B0; diagnóstico seguro y sin falsa evidencia |
| Guardas B0 | Selección inexistente/sin candidatos no crean checkout ni decisión; presupuesto excedido mantiene create/cancel y el error B0 |
| Reintentos/cambios/concurrencia | IDs estables en mismo estado, recuperación de intento sin registro, nuevos snapshots/versiones para hechos distintos, secuencias contiguas y un registro concurrente |
| Off/bootstrap | Sin claves/tablas/evaluaciones; fábrica off no mide fuentes; runtime shadow mal configurado informa degraded y conserva B0 |
| Runtime real | Inyección efectiva en runtime.purchases con gateway reemplazado por espía y perfil sandbox coherente |
| Coherencia | Mutación de feed, ranker/helper sustituidos y compra no COP provocan pérdida de observación; B0 continúa |

Resultados de comandos ejecutados:

| Control | Resultado |
| --- | --- |
| Gate ledger: pytest test_shadow_scopes.py test_evidence.py | 97 passed in 3.17s |
| Focalizada: uv run pytest tests/governance tests/test_purchase_revision.py | 326 passed in 44.91s; antes de los tres controles finales adicionales |
| uv run ruff format --check . | 132 files already formatted; código 0 |
| uv run ruff check . | All checks passed!; código 0 |
| uv run pyright | 0 errors, 0 warnings, 0 informations; código 0 |
| uv run pytest --junitxml=.local/gov05c/final.xml | 532 passed, 3 skipped, 1 warning in 62.32s; código 0 |
| git diff --check y revisión de nuevos archivos con índice temporal | Sin errores; índice real sin staging |

La suite final incluye D1 128, ledger 92, procedencia 42, CAS 36 y los 31 nuevos;
también las regresiones comerciales HP/AP2, API/ownership y protocolos existentes.
Solo se omitieron los tres E2E que requieren Merchant/PSP/PostgreSQL. Advertencia
heredada: alias anyio.abc.BlockingPortal deprecated en Starlette TestClient.
Los fallos iniciales fueron dos escenarios nuevos mal preparados (scope enforce
en un caso positivo shadow y feed con una sola variante disponible) y diagnósticos
Ruff/Pyright; se corrigieron sin eliminar aserciones ni modificar fixtures históricos.

### Medición offline final

Cinco pares en el mismo proceso Windows, gateway sin red y SQLite temporal por
muestra. El cronómetro de start excluye construcción del servicio/base y del
observador; bootstrap se mide aparte. El perfil de ejecución que cuenta llamadas
al ranking se usa en otro test, no en estas cinco muestras.

| Muestra | Off start ms | Shadow start ms | Bootstrap shadow ms |
| --- | ---: | ---: | ---: |
| 1 | 66.572 | 695.876 | 281.586 |
| 2 | 66.634 | 679.176 | 286.128 |
| 3 | 64.948 | 691.324 | 285.935 |
| 4 | 69.082 | 783.924 | 286.547 |
| 5 | 61.389 | 682.473 | 291.870 |

Medianas: off **66.572 ms**; shadow **691.324 ms**; diferencias de cada par
**626.376 ms**. Esta última no es la resta de las dos medianas.
Bootstrap shadow mediano **286.128 ms**; captura **283.190 ms**; observación posterior
**330.531 ms**; invocación original del evaluador **1.720 ms**. La observación incluye
persistencia y verificación/replay de admisión, además de la evaluación inicial.

El coste agregado es considerable y está dominado por las mediciones de fuentes
y captura/registro, no por POL-01/02. No se establece SLO, p95 poblacional ni una
comparación Python/OPA a partir de cinco pares. Tampoco se extrapola a Merchant,
PSP, LLM, PostgreSQL o tráfico real. Un bloqueo SQLite puede añadir espera hasta
el timeout local de 5 segundos; los fallos siguen sin convertirse en errores
comerciales añadidos. Los datos no justifican habilitar un experimento sin revisar
este coste y la contención. Se conservan en latency.json todos los valores crudos.

## F–G. Límites y decisiones pendientes

- D1 enforce sigue ausente: faltan UoW compartida evidencia/transición/CAS,
  revalidación del estado próximo al efecto y controles contra todas las carreras.
  La revisión leída es una observación y puede estar obsoleta después de devolverla.
- Los ámbitos conservan política, perfil de motor y contexto fijados al episodio.
  Cambiarlos bajo el mismo run degrada la observación; no se reescribe su historia.
- Los checks de objetos/fuentes detectan incompatibilidades observadas; no son
  aislamiento frente a mutadores arbitrarios del proceso ni atestación del host.
  Un contexto de operación inmutable y su atomicidad requieren revisión para enforce.
- D11 permanece abierto: Python es referencia experimental; OPA no se instaló,
  ejecutó ni seleccionó. Estas latencias son del observador, no del spike de motores.
- D17/frescura/consentimiento no se implementaron. Ningún allow shadow es permiso
  para firmar/completar ni puede reclamar transition_committed en el contrato actual.
- D23 de enforcement y coordinación de efectos remotos permanece pendiente.
  No se mantiene transacción de escritura durante Merchant/PSP en este incremento.
- HP existente conserva sus guardas; no se incorporan D2-HP, D2/D3-HNP, H01, D17,
  grants, reservas, delegación ni autonomía. No se afirma validación HNP ni E2E.
- Se requiere provisión real de configuración/claves, revisión de privacidad,
  límites/retención y presupuesto operativo antes de un episodio externo autorizado.

## H–I. Archivos, preservación y estado final

Modificados: application/purchase.py, config.py, runtime.py,
persistence/governance_sqlite.py y persistence/provenance_sqlite.py.
Nuevos: governance/shadow.py, governance/shadow_artifacts.py,
tests/governance/test_shadow_integration.py, tests/governance/test_shadow_scopes.py
y este informe. Los artefactos de ejecución están ignorados bajo .local/gov05c.

De los 292 archivos previamente versionados, **287 conservan exactamente sus bytes**;
las únicas cinco diferencias son las fuentes autorizadas anteriores. No se cambió
uv.lock, dependencia, .gitattributes, fuente ACP/AP2, algoritmo/normalizador, núcleo
del evaluador, contratos publicados, fixtures o resultados históricos B0.
No se ejecutó el baseline/piloto B0 ni se tocaron bases operativas u otros repositorios.
Se ejecutaron tests locales con UV_NO_SYNC/UV_OFFLINE, E2E/live calls desactivadas y
variables de credenciales de servicios retiradas del entorno del proceso de tests,
sin mostrar valores ni leer .env para preparar las pruebas nuevas.

HEAD/rama originales conservados; cambios sin commit ni push y sin staging real.
Detenido para revisión humana.
