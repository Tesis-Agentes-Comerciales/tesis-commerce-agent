# EC-GOV-06C-C — Seguridad posterior a creación

## A. Git y alcance

Base exacta comprobada: `982c41ff39fe3df25ba7b70c921cc049650ee7a5`.
La rama `governance/ec-gov-06c-b-reconciliation` estaba limpia y seguía a origin.
Se creó `governance/ec-gov-06c-c-post-create-safety` desde esa base.
HEAD no cambia. Sin stage, commit, push, merge ni PR.
Remotos inspeccionados: origin en `Maldinho18/tesis-commerce-agent` y org en
`Tesis-Agentes-Comerciales/tesis-commerce-agent`; no se modificaron.

## B. Inspección de efectos y defecto

Se revisaron PurchaseService, gate D1, envelope, journal de reconciliación,
dispatch, cliente/modelos ACP, persistencia de purchases y pruebas 06A/06B/06C-A/B.
También se revisaron las rutas de aprobación, completion y sus puntos de entrada
en API/runtime; estos dos últimos archivos permanecen intactos.

| Ruta previa | Efectos y clave | Persistencia anterior / riesgo |
| --- | --- | --- |
| `enforce._send` | POST create con clave 06A/envelope, seguido de GET de verificación sin clave idempotente | Claim antes del POST; confirmación y checkout_prepared después del GET. El GET carecía de intención propia. |
| `start → _apply_details` | POST update con `checkout.update.details`, después GET sin clave | Clave en operation_keys, pero sin intención/resultado durable de cada efecto. |
| Presupuesto inicial y posterior al GET | Evaluación local de total; POST cancel con `checkout.cancel.budget` si excede | Cancelación sin intención; stage canceled después de responder, sin verificar el resultado en la ruta anterior. |
| `approval_terms/_current_checkout/approve` | GET; en AP2 también GET del JWT y posible GET de JWKS | Rutas posteriores al start; fuera de la autorización D1 de selección. |
| Cancelación manual | GET y POST cancel con `checkout.cancel.user` | Fuera de preparación inicial. |
| Completion separado | GET y POST complete HP/AP2 con `checkout.complete`; delegación PSP con `payment.delegate`, recibos/JWKS | Aceptaba uncertain y hacía GET antes de verificar aprobación. Su reconciliación HP de completed podía registrar un resultado histórico. Ahora los episodios D1 se rechazan antes de esa ruta. |

El cliente ACP realiza una sola llamada HTTP por invocación, valida los modelos
y traduce errores de transporte/timeout. No se añadieron reintentos. Request-Id
se genera para correlación; no es identidad remota ni autorización.

La inspección adicional encontró una vía de reentrada al reiniciar sin gate:
una reserva D1 persistida podía caer en start/approve/cancel legados o en completion.
Se cerró con una consulta local de pertenencia D1 antes de cualquier efecto.
Esto justificó dos cambios pequeños adicionales: método de lectura en SqliteAgentStore
y guard al inicio de CompletionService.complete. No modifica el protocolo de
completion ni los flujos de episodios off/shadow sin operación D1.

El defecto posterior a confirmación estaba en `_abandon_on_error`: cualquier
excepción en checkout_prepared podía convertir la compra en failed, aun después
de un UPDATE/cancel que pudo producir efectos. Ahora esa lógica anterior se usa
exclusivamente en off/shadow. D1 conserva el ID y registra incertidumbre.

## C. Máquina de estados y autorización

| Estado anterior | Evento permitido | Estado siguiente |
| --- | --- | --- |
| Sin checkpoint, operación claimed/selected | Respuesta POST validada estructuralmente | creation_observed |
| creation_observed | Intención GET de verificación | intent(verify_creation_get) |
| intent(X) | Respuesta validada del mismo efecto | observed(X) |
| observed(verify_creation_get) | Confirmación 06B persistida en la misma transacción | creation_confirmed |
| creation_confirmed | Intención UPDATE o cancelación por presupuesto | intent(update_details/cancel_budget) |
| observed(update_details) | Intención GET actual | intent(get_current) |
| observed(get_current) | Preparación experimental terminada, o intención de cancelación por presupuesto | awaiting_confirmation / intent(cancel_budget) |
| observed(cancel_budget) | Cancelación observada | canceled |
| Cualquier estado no terminal | Error o respuesta ambigua | uncertain |
| intent pendiente tras crash | Solo inspección local | Sin transición automática ni reenvío |
| uncertain / canceled / awaiting_confirmation | Ningún efecto nuevo desde este núcleo | Terminal para este incremento |

`creation_observed` es una observación estructural de una respuesta, sin
autenticación remota demostrada. No sustituye a la confirmación 06B.
Los estados awaiting_confirmation/canceled describen resultados del experimento;
no conceden autoridad comercial. Todos los eventos fijan replay_allowed y
continuation_allowed a false. El binding sigue unavailable o backend_expected_unverified.

Los efectos requieren un puerto backend `ExperimentalPostCreateDispatch`
inyectado explícitamente, cuyo valor por defecto es None. Solo existe un doble
de prueba; no se implementó adaptador de producción. El ALLOW histórico D1 no
satisface ese puerto ni autoriza nuevos efectos. Las rutas de términos,
aprobación, cancelación manual y lectura indirecta de PurchaseService se bloquean
explícitamente cuando hay gate D1. Runtime/API no ensamblan ni activan enforce.
El bloqueo también reconoce la operación D1 persistida aunque el servicio reinicie
sin gate. Completion se bloquea independientemente del ensamblaje actual, antes de
GET, lectura de resultado completado, delegación PSP o invocación de complete.
Un ALLOW histórico no puede convertirse en autoridad mediante un cambio de modo.

## D. Journal, checkpoint y atomicidad

Dos tablas nuevas, solo en el entorno opt-in del gate:

- `d1_post_create_events`: eventos inmutables por operación/versión, bytes canónicos,
  digest con separación de dominio y enlace al digest anterior.
- `d1_post_create_checkpoint`: última versión/digest con FK al evento y avance CAS.

Se reutilizan EnvelopeModel, canonicalización, validación de envelope/procedencia
y comprobación estricta de DDL. No se duplica el ledger D1: los eventos conservan
referencias/digests originales. No se modifican d1_checkout_results, los eventos
06C-B ni su esquema. La inserción original de confirmación 06B permanece append-only;
el nuevo checkpoint creation_confirmed se añade en esa misma transacción.

Cada intención se confirma en SQLite antes de invocar el puerto. La llamada
ocurre sin bloqueo de escritura. El retorno se valida y produce otra observación.
No se guardan payloads con datos personales, credenciales ni claves idempotentes
en claro: se registran digests, ámbito, checkout ID y referencias. Los digests de
petición son identidad local del intento, no una prueba de fingerprint wire ACP.
El journal no conserva un payload de replay ni existe método de reenvío automático.

Eventos y checkpoint avanzan dentro de savepoint y transacción explícita con FK.
Los cambios de etapa de purchase a uncertain, canceled o awaiting_confirmation
son atómicos con esos registros y usan CAS de versión/etapa/propietario. Se verifica
también la revisión esperada de purchase antes de admitir un nuevo efecto.
Los triggers rechazan UPDATE/DELETE/REPLACE de eventos, borrado/reemplazo del
checkpoint y avances incompatibles. Se valida todo el historial, sus versiones,
digests, enlaces, ámbito y referencias antes de aceptar el checkpoint.

`PostCreateFlow.inspect()` abre una lectura transaccional, sin HTTP ni inicializar
tablas. Un intent pendiente significa resultado potencialmente incierto; no prueba
envío ni ausencia de efectos. No se reconstruyen defaults ni estados remotos actuales.

## E. Archivos

Modificados:

- `src/commerce_agent/application/purchase.py`: ruta D1 con journal, excepción segura
  y bloqueo de continuación; ramas off/shadow conservadas.
- `src/commerce_agent/governance/enforce.py`: intención del GET inicial, confirmación
  atómica del checkpoint y nuevo puerto experimental opcional.
- `src/commerce_agent/persistence/sqlite.py`: detección de operación D1 persistida,
  solo lectura y sin requerir ni crear tablas D1 para episodios ordinarios.
- `src/commerce_agent/completion.py`: rechazo D1 antes del primer GET o consulta de
  resultado completado; necesario porque uncertain era aceptado por la ruta legada.
- `tests/governance/test_enforce_integration.py`: cinco líneas de fixture para
  inyectar explícitamente el doble posterior a creación; ninguna aserción eliminada.

Añadidos:

- `src/commerce_agent/governance/post_create.py`: modelo estricto, puerto y coordinación.
- `src/commerce_agent/persistence/post_create_sqlite.py`: historial/checkpoint transaccional.
- `tests/governance/test_post_create.py`: 38 casos offline.
- Este informe.

## F. Matriz de fallos y evidencia

| Escenario | Resultado probado |
| --- | --- |
| UPDATE produce efecto y pierde respuesta | Intención durable; uncertain; checkout ID y creación confirmada conservados. |
| GET posterior falla o devuelve estado incompatible | uncertain; sin inferir vigencia comercial ni ausencia de UPDATE. |
| Respuesta UPDATE cambia identidad | Rechazo y uncertain. |
| Excepción local de presupuesto | uncertain; no cancela ni despacha por inferencia. |
| Cancelación por presupuesto con timeout o respuesta no verificable | uncertain; no se presume cancelación ni ausencia de efectos. |
| Cancelación observada correcta | Etapa canceled y evento atómicos; creación 06B sigue confirmed. |
| Crash después de intención, antes del efecto | Subproceso real terminado con os._exit; intención recuperable, sin efecto ni reenvío. |
| Crash después de efecto sintético, antes del retorno | Mismo cierre abrupto, marcador local confirma solo el efecto sintético; no hay repetición al recuperar. |
| Reinicio/reinvocación de start o del efecto | Bloqueo antes del transporte; journal e identidad conservados. |
| Dos conexiones concurrentes | Un solo ganador para la versión CAS; un único intent nuevo. |
| Purchase modificada concurrentemente | Rechazo antes del transporte. |
| Fallo de checkpoint capturado por el llamador | Rollback de evento y modificación de purchase, aunque se confirme la transacción externa. |
| Checkpoint, evento antiguo, tabla o envelope corruptos | Inspección/efectos bloqueados; sin reconstrucción ni HTTP. |
| Run/owner ajenos | Rechazo antes de efectos o lectura de historial ajeno. |
| Éxito experimental | Misma secuencia create/get/update/get y revisión final 4; nueve eventos del journal nuevo. |
| Rutas posteriores de aprobación/cancelación | Bloqueadas para D1 sin HTTP; no se habilita completion. |
| Reinicio con/sin gate y compra uncertain | Diez casos de start/términos/approve/cancel/complete; cero GET, POST o delegación PSP. |

Las pruebas inspeccionan la intención desde otra conexión antes de cada efecto,
comparan la evidencia de creación antes/después y comprueban que no aparecen
email, texto privado ni clave en claro en los eventos. No simulan autenticación
remota verificada. Los crashes de proceso son reales; sus efectos son dobles
offline. No son pruebas de caída eléctrica, pérdida de disco o integración Merchant.

## G. Validación

| Control | Resultado |
| --- | --- |
| Pruebas nuevas iniciales, antes del guard persistente adicional | 28 passed in 55.23s. |
| Pruebas nuevas finales, comando oficial `uv run pytest tests/governance/test_post_create.py -q --tb=short -p no:cacheprovider --basetemp=<nuevo>` | 38 passed in 102.74s; sin omisiones ni advertencias. |
| Regresiones específicas 06A/06B/06C-A/06C-B, antes del guard adicional de reentrada/completion | 169 passed in 283.99s; sin omisiones ni advertencias. Se vuelven a incluir todas en la suite final. |
| Primera suite protegida, previa al guard adicional | 729 passed, 3 skipped, 1 warning in 434.76s. |
| `uv run ruff format --check .` final | 157 files already formatted. |
| `uv run ruff check .` final | All checks passed! |
| `uv run pyright` final | 0 errors, 0 warnings, 0 informations. |
| Suite general protegida final | 739 passed, 3 skipped, 1 warning in 391.95s. Incluye nuevamente las 169 regresiones 06A/06B/06C-A/B y las 38 nuevas. |
| `git diff --check` | Sin errores de whitespace; avisos existentes LF a CRLF en enforce.py y su fixture de tests. |

Se preserva la referencia de 701 pruebas aprobadas, sin retirar aserciones ni
pruebas anteriores; los nuevos casos son 38. Las tres omisiones conocidas son
`tests/e2e/test_ap2_joint_purchase.py:32`, `test_conversational_purchase.py:74` y
`test_joint_purchase.py:95`, que requieren Merchant, PSP y PostgreSQL.
La advertencia existente es DeprecationWarning de `starlette/testclient.py:53`
por el alias `anyio.abc.BlockingPortal`, que recomienda `anyio.from_thread.BlockingPortal`.
No se cambian dependencias para ocultarla.
Durante la construcción hubo una ejecución con 27 passed y 1 failed in 52.83s:
el test de privacidad intentaba canonicalizar una colección de modelos sin
convertir sus elementos a JSON. Se corrigió únicamente esa serialización de test,
manteniendo las aserciones; no se modificó canonical.py para aceptarla.

Entorno Windows, uv/Python local existente; sin dependencias nuevas ni cambios al
lockfile. No se declara ejecución Linux. La suite protegida elimina variables de
proveedores, desactiva dotenv/tracing/E2E, impide SQLite fuera de un basetemp nuevo,
escrituras al repositorio y sockets externos/operativos. Dos exportaciones históricas
shadow se redirigen a cwd temporal sin modificar sus tests ni artefactos. El runner
invoca pytest.main con `-q --tb=short -p no:cacheprovider --basetemp=<nuevo>`.
Las pruebas específicas usan `uv run pytest` con bases temporales y transportes
sintéticos; uv se ejecuta con UV_NO_SYNC/UV_OFFLINE y caché temporal.

## H. Límites y riesgos pendientes

Este núcleo no verifica autenticación remota, idempotencia/retención del Merchant,
vigencia de oferta/consentimiento ni autorización de continuación. No concede
payments, completion, D2, D17 ni HNP. Ningún estado histórico prueba vigencia actual.
La recuperación únicamente permite inspección. Un nuevo efecto necesita otro
contrato y autorización; no basta reenviar una clave conocida ni un ALLOW previo.

El puerto experimental es código backend confiable del ensayo, no una capacidad
que el comprador/LLM pueda declarar ni evidencia de autorización humana. No hay
implementación de red, worker, endpoint ni opción pública nueva para habilitarlo.
Si una observación no puede persistirse, la intención anterior sigue siendo la
evidencia durable de incertidumbre. El error no sustituye ni borra esa intención.
El diagnóstico secundario puede fallar; nunca convierte la compra en failed.

Los hashes/triggers no protegen contra un administrador que reescriba conjuntamente
esquema, historial y hashes. La carga de lectura es lineal en los eventos locales.
No se implementa limpieza, caducidad, liberación de claims ni reconciliación de red.
No se inventan checkpoints para confirmaciones antiguas que carezcan de este
journal. La concurrencia probada es local entre conexiones; no valida despliegues
comerciales con múltiples runtimes y modos diferentes.
No se modifican Merchant, PSP, ACP/AP2 congelado, UI/SSE, API/runtime, baseline B0,
resultados históricos, Notion ni archivos de Yair.

## I. Dictamen y estado final

**READY_FOR_REVIEW** para el incremento local EC-GOV-06C-C.
No es autorización para desplegar ni activar enforce comercial. El replay,
payment, completion y continuación permanecen bloqueados para episodios D1.
Se detiene sin commit ni push para revisión humana.

La suite final conserva las 701 aprobadas de referencia y agrega 38 aprobadas,
sin nuevas omisiones. No hubo ejecución real Agent–Merchant, PSP o PostgreSQL.
Se verificaron los SHA-256 de los 317 archivos de la base: 312 permanecen idénticos
y solo cambian los cinco archivos enumerados en E. En particular, uv.lock, los
contratos de políticas, las fuentes congeladas ACP y los módulos/eventos 06C-B
no se modificaron. Los cuatro archivos nuevos son exclusivos de este incremento.
B0 y sus artefactos históricos no se reejecutaron ni se sobrescribieron.

Estado final:

```text
## governance/ec-gov-06c-c-post-create-safety
 M src/commerce_agent/application/purchase.py
 M src/commerce_agent/completion.py
 M src/commerce_agent/governance/enforce.py
 M src/commerce_agent/persistence/sqlite.py
 M tests/governance/test_enforce_integration.py
?? docs/b2-governance/ec-gov-06c-c-post-create-safety-results.md
?? src/commerce_agent/governance/post_create.py
?? src/commerce_agent/persistence/post_create_sqlite.py
?? tests/governance/test_post_create.py
```

HEAD: `982c41ff39fe3df25ba7b70c921cc049650ee7a5`; índice vacío.
Diff versionado: cinco archivos, 149 inserciones y 22 eliminaciones.
Los archivos nuevos se revisaron además con diff check contra NUL; sin errores
de whitespace, con avisos existentes de conversión futura LF a CRLF. No se cambió
core.autocrlf ni se normalizó el repositorio.
