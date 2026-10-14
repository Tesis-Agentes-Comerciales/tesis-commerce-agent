# EC-GOV-05 — Contraste D1 con runtime real

Estado: **BLOCKED en fase A; integración shadow no implementada**.
Base verificada: `578d257384f7a1427c6129465514ea46587f229b`.
Rama: `governance/ec-gov-05-d1-shadow`; working tree inicialmente limpio.

## 1. Dictamen y alcance

Se contrastaron PurchaseService.start, rank_candidates, PurchaseIntent/ProductFeed,
SqliteAgentStore, build_runtime, Settings, autenticación y transporte del owner,
modelos/evaluador de gobernanza y ledger EC-GOV-04, junto con las pruebas comerciales.
No se conectó un observador ni se activó shadow. No hay una decisión D1 de producción
ni una comparación integrada off/shadow que se pueda presentar como ejecutada.

El bloqueo principal es `expected_local_state_version`: el contrato v1, sección 3,
lo define como la revisión CAS actual del estado, pero la persistencia comercial no
la mantiene. Tampoco existen snapshots operativos versionados de intención/feed,
un perfil de reloj recuperable o configuración del binding de política operativo.
El ledger acepta un contexto ya resuelto por backend; no genera esas autoridades.

Estos componentes pueden desarrollarse, pero su ausencia no se subsana rellenando
el ensamblador con `1`, digests de fixtures, un contador de observaciones o un alias
de merchant inventado. No se ha demostrado aquí la seguridad de una nueva migración
y de todos sus escritores bajo los modos off/shadow. Se aplica la condición del ticket
de detenerse sin fabricar evidencia cuando no se puede resolver un hecho esencial.

## 2. Fuentes inspeccionadas y punto correcto de integración

- `application/purchase.py:PurchaseService.start`: crea la compra, llama una vez a
  rank_candidates sobre `feed or self._feed`, rechaza selección inválida, persiste
  selected, obtiene clave idempotente y crea checkout. El punto futuro es después
  de las guardas de elegibilidad y antes de create_checkout, reutilizando ese
  SelectionResult y el mismo feed; nunca otra lectura del feed ni otro ranking.
- `application/conversation.py:_prepare_checkout`: entrega intención validada,
  selected_variant_id, owner backend, graph_thread_id y feed del contexto del turno.
  run_id procede de turn_run_id sobre el turno procesado por backend.
- `api.py:current_buyer` y `buyer_auth.py`: identidad autenticada o modo local;
  el owner se transporta al servicio. JWT `sub` es un string de hasta 256 caracteres,
  no una garantía de pseudonimización ni del alfabeto/longitud de Id de gobernanza.
- `domain/selection.py`: elegibles reales en selection.ranked; selection.selected
  es el mejor candidato, pero el elegido por el router puede ser otro elegible.
  No se debe reemplazar la elección validada por selection.selected silenciosamente.
- `acp/feed.py`: ProductFeed es una proyección validada pero sus objetos internos
  incluyen listas/diccionarios y extra=allow. FeedMetadata.id no es una revisión
  inmutable del contenido. La proyección tiene títulos, URLs y opciones libres.
- `runtime.py:build_runtime`: configura destino merchant, feed y reloj comercial
  de aprobaciones, pero no registra un contexto B2 ni carga política D1 configurada.
- `persistence/sqlite.py`: tabla purchases sin revisión CAS ni snapshot de la
  PurchaseIntent. Los checkpoints y request_text no equivalen a esa intención
  validada/versionada. Cada método comercial abre su conexión y transacción.
- `governance/evidence.py:EvidenceScope` y `persistence/governance_sqlite.py`:
  comprueban un contexto previamente registrado, no resuelven referencias operativas.

## 3. Mapa de todos los campos obligatorios de SelectionPolicyInput

"Falta" describe la base actual, no afirma que sea imposible incorporarlo.
Las propuestas de emisión/persistencia siguientes no se ejecutaron.

| Campo | Fuente autoritativa real / adquisición sin LLM | Persistencia y versión/digest | Si no se resuelve |
| --- | --- | --- | --- |
| schema_version | Contrato `commerce-policy/1.0` del núcleo | Literal normativo existente; no generar otra versión | Rechazo de esquema |
| decision_id | Emisión backend por evaluación lógica | Falta registro que conserve ID y cuerpo en retry; emitir otro solo en reevaluación explícita | No inventar decisión |
| run_id | Argumento backend de start; en conversación, turn_run_id | Ya persistido como PK purchases; reutilizarlo sin regenerar | No asociar evidencia a compra inexistente/ajena |
| trace_id | Correlación emitida por backend | Falta asociación persistente trace/run/decisión; graph_thread_id no es automáticamente ese contrato | Bloquea input completo |
| owner_ref | Owner autenticado propagado a start y purchases.owner_id | Owner real persistido; falta mapping opaco gobernanza/owner validado. No copiar sub sin validar ni aplicar SHA-256 a PII | No exponer identidad ni inventar ownership |
| purchase_mode | Rama HP realmente implementada | Fijar HP por contexto backend, no leer al modelo; falta contexto B2 persistido | No prometer HNP |
| governance_mode | Configuración explícita del runtime | Falta setting off/shadow y fijación por episodio; off debe evitar toda inicialización B2 | No activar por inferencia |
| phase | Ubicación D1 del control en start | Literal D1 normativo | No evaluar otra fase |
| action | Operación real previa a create_checkout | Literal prepare_checkout normativo | Rechazar otras acciones, sin efectos de autoridad |
| context_version | Versión de configuración normativa fijada para el episodio | Falta objeto de contexto versionado y recuperable; no usar timestamp ni versión del estado | No asignar 1 por conveniencia |
| expected_local_state_version | Revisión comercial CAS del backend | **No existe** en purchases ni es mantenida por sus escritores | **Bloqueo de ensamblado**; no usar etapa, updated_at o secuencia del ledger |
| expected_policy | Perfil configurado de confianza, verificable con verify_artifact | No hay ruta/pin operativo en Settings/build_runtime; el JSON encontrado es fixture offline-d1/merchant-1 | No usar la política de tests en producción |
| authority_now | Reloj real del servidor, UTC | Capturar RFC3339 Z/milisegundos al evaluar y conservar en la entrada; falta perfil asociado | No sustituir por reloj sintético del usuario |
| commercial_now | Reloj comercial efectivo, incluido sandbox si está configurado | runtime recibe clock o agent_sandbox_clock_at; falta registro de su perfil y binding por episodio | No describir un callback desconocido como reloj validado |
| clock_profile_ref | Configuración backend de ambos relojes y normalización temporal | Falta objeto real registrado, ID/revisión y digest de sus bytes seguros | No usar clock-1 ni un digest constante |
| experiment_run_id | Contexto experimental backend si aplica | No se transporta hoy en start; null solo si backend declara no aplicable, no para encubrir un dato requerido desconocido | No copiar IDs de resultados B0 |
| conversation_id | Conversación/thread ya resuelto por backend | Existe vínculo purchase_graph_threads; falta proyección que conserve semántica y valide Id sin truncar. No es sinónimo de run/trace | Null solo para ruta sin conversación |
| intent | PurchaseIntent validada que recibe start | Hace falta snapshot operativo protegido y proyección IntentFacts descrita debajo | Sin snapshot/binding no fabricar intención versionada |
| feed_snapshot_ref | Exacto ProductFeed usado por ese rank_candidates | Falta snapshot real y resolver/versionado; metadata.id por sí solo no fija bytes ni garantiza privacidad | No releer feeds/current para completar evidencia |
| eligibility_algorithm_ref | Código real de ranking, filtros y proyección de candidatos | Falta artefacto/perfil recuperable de selection.py y dependencias relevantes. Hash solo de nombre de función no basta | No registrar algoritmo ficticio |
| eligible_variant_ids | selection.ranked de la misma llamada en start | Tomar item.candidate.variant_id; validar unicidad e IDs y usar canonicalización del núcleo | Las guardas B0 siguen rechazando selección inválida |
| selected_variant_id | candidate.variant_id tras pertenencia comprobada por start | Ya persistido en purchases; no sustituir por otro candidato | Rechazo B0 previo a evaluación; no fabricar allow |
| merchant_ref | Destino ACP configurado en runtime, no URLs del catálogo | Falta binding de origen/destino validado a ID opaco y allowlist del artefacto verificado | No confiar en discovery ni merchant-1 del fixture |

### Proyección obligatoria IntentFacts

| Campo | Fuente y persistencia necesarias | Regla de generación / ausencia |
| --- | --- | --- |
| intent_ref | PurchaseIntent validada; snapshot protegido ligado a run/owner | ID/revisión de snapshot realmente guardado; digest solo de representación no sensible acordada. El request original no reemplaza la intención normalizada |
| currency | hard.currency y perfil comercial explícito | Núcleo admite COP. No convertir USD en COP ni inferir moneda de autoridad del LLM; rechazar proyección no representable |
| max_unit_minor | hard.max_unit_amount_minor | Copia de entero estricto, sin conversión de unidades; null tiene semántica HP explícita |
| max_total_minor | hard.max_total_amount_minor | Mismo criterio; no usar el precio observado como presupuesto autorizado |
| quantity | Perfil comercial de una unidad | Literal 1 normativo, no inventar campo de PurchaseIntent |
| hard_constraint_refs | Restricciones hard estructuradas del snapshot validado | Emitir refs opacas respaldadas por correspondencia protegida; no escribir términos/textos privados como Id ni hashearlos en claro |
| normalization_version | Implementación y perfil real de normalización de domain/intent.py | Fijar artefacto/perfil y versión recuperable; falta registro. No usar normalizer-1 del fixture |

Todos los ObjectRef requieren ID, versión y digest ligados al mismo objeto; una fila
de refs sin contenido recuperable no demuestra snapshot. Los tipos obligatorios ya
existen; lo que falta es su resolución y custodia operativa.

## 4. Verificación de la persistencia real y alternativas mínimas

Probe con SqliteAgentStore real sobre SQLite temporal, sin abrir bases operativas:
se crearon estados selected, checkout_prepared y awaiting_confirmation. PRAGMA
table_info(purchases) devolvió run_id, owner_id, request_text, stage,
selected_variant_id, checkout_id, order_id, created_at y updated_at. Sin versión CAS.
La base B0 inicializada tampoco requirió tablas governance_. Evidencia auxiliar:
`.local/gov05/cas-probe-2.json` y su base temporal aislada.

Las escrituras comerciales están repartidas entre update_purchase, claim_completion,
release_completion y record_completion. No sería correcto incrementar una revisión
solo al observar D1 y presentarla como revisión de todos los estados comerciales.

Alternativas a revisar, antes de conectar un ensamblador:

| Alternativa | Evaluación |
| --- | --- |
| Constante 1, número de decisiones o hash convertido a entero | Rechazada: no es la revisión CAS exigida por policy-v1 |
| Columna de revisión en purchases, migración explícita y mantenimiento transaccional en todos los escritores pertinentes | Candidata mínima para una revisión de fila real; requiere definición de inicialización de filas antiguas, cobertura de escritores/reintentos y pruebas off/shadow. No ejecutada ni declarada segura aquí |
| Tabla lateral de revisión ligada al estado comercial | Evita alterar columnas existentes, pero solo es CAS real si se coordina transaccionalmente con todos los escritores; un contador solo del observador no sirve |
| Proyectar desde updated_at o número ordinal de stage | Rechazada: no prueba revisión monotónica ni identifica escrituras repetidas/ABA |

Recomendación de continuación: concretar primero D18/D23 para la revisión local y
una inicialización aditiva opt-in, con compatibilidad en SQLite temporal; definir
snapshots protegidos/refs y perfil de privacidad D30; cargar el artefacto y binding
operativos por configuración verificada. Solo después conectar el observador D1.
No basta que los tests del ledger pasen para afirmar segura una migración no probada.

Off debe conservar constructor/inicialización/consultas B0 y no consultar tablas B2.
Shadow no puede bloquear B0 por un fallo de la inicialización o de cualquier escritura
añadida. No se implementó una migración parcial para intentar satisfacer estos puntos
sin demostrarlos. Tampoco se altera el contrato para rebajar la revisión CAS a una
"versión de observación" ni se selecciona motor definitivo.

## 5. Comparación off/shadow y evidencia del episodio

| Verificación solicitada | Base existente | Shadow integrado |
| --- | --- | --- |
| Operaciones y datos comerciales | Tests actuales con gateway espía; prueba de base | Pendiente |
| D1 correlacionado, legible y replayable en una compra | Solo núcleo/ledger aislados existentes | Pendiente |
| Deny adicional no bloquea B0 | No hay observador añadido | Pendiente |
| Fallo de evaluación/evidencia no altera B0 | No se inyectó tal integración | Pendiente |
| Candidato inválido sigue bloqueado | Guarda B0 existente cubierta por sus tests | Pendiente de comparación con observador |
| Reintentos/idempotencia de evidencia integrada | Ledger aislado probado; no es prueba de compra | Pendiente |
| Episodio shadow real sin datos sensibles | No se ejecutó ni se fabricó | Pendiente |

No se presenta un fixture de EC-GOV-02/04 como episodio del runtime. La identidad
Python del evaluador es verificable, pero no reemplaza un PolicyBinding operativo,
las referencias de hechos ni la revisión CAS de una compra.

## 6. Validación de la base y preservación

Controles ejecutados realmente en Windows, sobre la base sin cambios de código:

| Control | Resultado |
| --- | --- |
| `uv run pytest tests/test_purchase_service.py tests/test_feed_refresh_graph.py tests/governance` | **231 passed in 8.37s**, código 0 |
| `uv run ruff format --check .` | `120 files already formatted`, código 0 |
| `uv run ruff check .` | `All checks passed!`, código 0 |
| `uv run pyright` | `0 errors, 0 warnings, 0 informations`, código 0 |
| `uv run pytest` | **423 passed, 3 skipped, 1 warning in 20.02s**, código 0 |
| `git diff --check` y revisión del informe nuevo con índice temporal | Sin errores; índice real vacío |

Los tests específicos anteriores son controles existentes de compra, feed y núcleo/
ledger; **no son tests nuevos de integración shadow**. No se añadieron estos últimos
porque la fase A no quedó satisfecha. Ningún resultado verde de esta tabla resuelve
los bindings o demuestra segura una migración de estado aún no implementada.

Se usaron `UV_NO_SYNC=true`, `UV_OFFLINE=true`, dependencias existentes y
`RUFF_NO_CACHE=true`, sin regenerar lockfile. Pytest recibió
`-p no:cacheprovider --basetemp=C:/ANDES/Tesis/tesis-commerce-agent/.local/gov05/base-specific`
o `base-full` mediante PYTEST_ADDOPTS y produjo los XML respectivos en
`.local/gov05/`. Se retiraron del entorno del proceso de tests las variables de
proveedores/servicios y se desactivaron E2E/live calls. No se imprimieron secretos.

Omisiones: los tres E2E conocidos que requieren merchant, proveedor y PostgreSQL.
Advertencia: deprecación de `anyio.abc.BlockingPortal` en Starlette TestClient.
No se ejecutó Linux ni se utilizaron servicios comerciales externos.

Incidencia del probe inicial: las comprobaciones SQL se completaron, pero el
TemporaryDirectory no pudo borrar la base por un handle abierto (WinError 32).
Se repitió en otro archivo temporal con cierre explícito y retención de la evidencia,
con código 0. No fue un fallo comercial ni se tocó una base operativa.

Los **284 archivos previamente versionados conservan exactamente sus bytes SHA-256**
tras las verificaciones. Ningún escenario, resultado histórico B0, código o fixture
fue modificado. Los tests se ejecutaron únicamente sobre bases/temporales de prueba.

## 7. Estado Git y límites

Único archivo nuevo: este informe de readiness/bloqueo. No se modificaron fuentes,
tests, fixtures, configuración, bases operativas, escenarios/resultados históricos B0,
protocolos ACP/AP2, dependencias ni otros repositorios. Sin commit, push ni merge.
Fases B–E no implementadas; no D2/D3/HNP/H01/D17, OPA ni enforce.
