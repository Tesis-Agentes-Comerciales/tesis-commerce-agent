# EC-GOV-07A — Despacho inicial HP seguro

## A. Preflight y referencias Git

Agent inició limpio, índice vacío, sin stashes, en governance/ec-gov-06d-c-live-recovery-integration,
HEAD 9f15d20906f576b7d28138ff9f5708022b405ae8.
Origin https://github.com/Maldinho18/tesis-commerce-agent.git y
org https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent.git conservados.
La rama governance/ec-gov-07a-hp-initial-dispatch estaba ausente y se creó desde ese HEAD.
Merchant limpio en merchant/ec-mer-rec-02-readonly-lookup, c5bd8113998f26928f7d2b62771285fe4040d07f.

## B. Camino existente — inspección anterior a cambios

| Etapa | Implementado antes de 07A | Frontera pendiente |
| --- | --- | --- |
| Captura | capture verifica artefactos y copia feed/intent; selección determinista | Feed no es autoridad final comercial |
| D1 | POL-01/02, procedencia y contexto backend HP | No D17 ni D2-HP |
| UoW | Ledger, CAS, reserva y envelope en una transacción; lectura tras commit | Sin commit no hay despacho |
| Claim | CAS reserved→claimed, commit y lectura independiente | Claim no prueba entrega; nunca se libera automáticamente |
| Envío | Puerto experimental variante/moneda/clave | No transporta directamente bytes del envelope |
| Respuesta | Validación y creation_observed durable en PostCreateFlow | No acredita binding remoto autenticado |
| Poscreación | GET con intención durable y puerto experimental independiente | Sin puerto, bloqueado; UPDATE/cancel/pago no se habilitan |
| Recuperación | Reconciliación local, lookup HTTP autenticado y journal independiente | Solo historia; no replay ni continuación |
| Runtime | off/shadow; Settings no admite enforce | Se conserva sin activar |

Decisión de diseño: añadir un puerto backend separado que recibe el envelope
comprometido, reutiliza UoW/claim y registra creation_observed. Se detiene antes de
GET; no modifica PostCreateFlow ni declara creation_confirmed sin sus requisitos.
Se conserva la interfaz experimental antigua y sus pruebas. No se presenta el
despacho inicial como flujo HP completo.

## C. Fronteras de confianza y contratos

La inyección utiliza BackendLookupSession y SessionScope de 06D-B: origin, token,
run/actor y source_ref proceden del aprovisionamiento backend, no del comprador
ni del lookup. El binding del envelope continúa siendo backend_expected_unverified.
La respuesta inicial no incluye una prueba de scope; no se inventa autenticación
remota a partir de Request-Id. El lookup posterior sí utiliza el verificador aprobado.

El nuevo puerto valida de nuevo CheckoutCreationEnvelope.storage_bytes(), sus
bytes canónicos, fingerprint merchant-acp-create/v1, perfil COP/una unidad y
scope provisionado. Se valida antes del claim y otra vez al enviar. El gate lee
el envelope durable y sus vínculos a evidencia/contexto antes de invocar el puerto.
El cuerpo del POST es directamente body_canonical.encode('utf-8'); no se llama
ACPClient.create_checkout ni se reconstruye un JSON con defaults.

El transporte solo dispone de POST /checkout_sessions. Sin redirect, proxies del
entorno, reintentos, worker, lookup implícito ni operaciones poscreación. Exige
201, clave reflejada exactamente, correlación servidor de formato válido, JSON
identity acotado, contrato ACP congelado y modelo estricto; comprueba variante,
moneda, cantidad y estado inicial. Una respuesta rechazada sigue siendo ambigua,
incluido un HTTP no exitoso: no se convierte en prueba de ausencia de efectos.
La respuesta puede contener capacidades Merchant diferentes del request (H1).

El método de transporte no es una autorización independiente. La admisión ocurre
solo dentro del gate, después de una UoW nueva y claim confirmado. La configuración
es código backend confiable; esto no protege contra un backend Python malicioso.
No se exporta un endpoint o herramienta del modelo que pueda invocarlo.

## D. Archivos y justificación

- Nuevo src/commerce_agent/acp/initial_dispatch.py: protocolo de envelope y HTTP
  inicial, validación de binding y respuesta, errores sin datos sensibles.
- Modificado src/commerce_agent/governance/enforce.py: keyword initial_dispatch
  separado e incompatible con el puerto legacy; comprobación previa al claim;
  envío del envelope; detención tras creation_observed. Se extrae el diagnóstico
  de incertidumbre existente para compartirlo entre excepciones, sin cambiar SQL.
- Nuevo tests/governance/test_initial_dispatch.py: 19 casos offline con
  respuestas sintéticas conformes al contrato y MockTransport explícito.
- Nuevo tests/integration/test_initial_dispatch_live.py: seis casos con
  PostgreSQL/HTTP/SQLite reales y proxy de pérdida/latencia controlado.
- Modificado tests/integration/gov06dc_harness.py: perfil opcional de HEAD y
  allowlist de archivos Agent para reutilizarlo en 07A sin desactivar preflight.
  Los defaults anteriores se conservan; Merchant continúa exigiendo HEAD exacto
  y árbol limpio sin excepciones. La evidencia registra el HEAD efectivo del perfil.
- Nuevo este informe. Total: cuatro archivos nuevos, dos modificados.

Los escenarios históricos test_live_creation_recovery.py no se editaron. El
harness anterior fijaba un HEAD previo incluso al commit 06D-C aprobado; se
necesita el perfil explícito para ejecutar escenarios intactos con código 07A.
No se copia un servidor, contrato Merchant o journal de reconciliación.

## E. Invariantes

Secuencia nueva: captura→D1→UoW/evidencia/envelope/reserva→commit→lectura
independiente→validación de sesión→claim CAS→commit→lectura→envelope
durable→único POST→validación→creation_observed durable→bloqueo poscreación.

La detención controlada usa d1_initial_creation_observed; no devuelve
PurchasePreparation ni conduce a _apply_details. La purchase queda selected,
sin checkout_id comercial utilizable; el ID conocido y digest de respuesta
están en PostCreateFlow. No se emite creation_confirmed ni CHECKOUT_VERIFIED,
que requieren el camino anterior con su GET protegido. El estado de despacho
conservador permanece claimed/sin resultado, mostrado como uncertain por 06B.
06D recupera el ID histórico del journal post-create y lo contrasta con Merchant.
Esta distinción es deliberada: creación observada no equivale a flujo HP completo.

Un error de transporte o respuesta lleva a uncertain; si falla persistir el
diagnóstico, queda el claim irrevocable. Una caída después del claim y antes
del envío puede dejar cero efectos o uno: nunca se resuelve con un nuevo POST.
Si la respuesta se recibió pero falla la escritura de creation_observed, se
conserva la incertidumbre y se requiere lookup explícito. Las reservas antiguas
(applied.reused), incluso no enviadas, no se admiten como primer envío nuevo.

Un segundo start con estado selected/uncertain se bloquea con
d1_recovery_required. El lookup conserva replay_allowed=false y
continuation_allowed=false. No se liberan claims ni se sobrescriben eventos.
El POST queda fuera de transacciones SQLite abiertas. La correlación HTTP no
constituye identidad autoritativa. Los tokens solo viajan por memoria/IPC
privado; no se leen archivos .env ni se registran headers o claves crudas.

## F. Validaciones

| Control | Resultado |
| --- | --- |
| Nuevas unitarias, ejecución aislada | 19 passed in 34.97s; transporte sintético |
| Integración real 07A inicial | 6 passed in 47.65s |
| Integración real 07A con comprobación previa al envío | 6 passed in 56.23s, sin skips ni warnings |
| Integración real final con comparación inicial/histórica/snapshot | 6 passed in 46.92s, sin skips ni warnings |
| 06A–06D-B + nuevas unitarias | 328 passed in 578.04s; cero skips ni warnings |
| General offline protegida | 860 passed, 41 skipped, 1 warning in 656.71s (0:10:56) |
| 06D-C histórico real, perfil 07A | 32 passed in 300.53s; cero skips ni warnings propios |
| Regresiones Merchant dentro del último caso 06D-C | 105 passed, 0 failed, 0 skipped, 2 warnings en 108.3736922s; no se suman otra vez a los 32 casos Agent |
| Ruff format/check | 171 files already formatted; All checks passed! |
| Pyright | 0 errors, 0 warnings, 0 informations |

Windows real; Python Agent/Merchant 3.12.6, PostgreSQL 17.2, uv 0.12.15;
httpx 0.28.1, Pydantic 2.12.5, FastAPI 0.141.1, Uvicorn 0.53.0,
psycopg 3.3.5, pytest 9.0.2, Ruff 0.16.8 y Pyright 1.1.414.
No se instaló ni actualizó software. No se declara ejecución Linux.

Los 41 skips offline son seis escenarios 07A y 32 escenarios 06D-C por sus
gates explícitos, más tres E2E anteriores que requieren Merchant/PSP/PostgreSQL:
tests/e2e/test_ap2_joint_purchase.py:32, test_conversational_purchase.py:74 y
test_joint_purchase.py:95. Los seis nuevos y 32 anteriores se ejecutaron también
por separado con servicios reales; no se validaron a partir del skip.
La advertencia offline es DeprecationWarning en starlette/testclient.py:53
por anyio.abc.BlockingPortal, que remite a anyio.from_thread.BlockingPortal.
No se ocultó ni se cambiaron dependencias para eliminarla.

La ejecución principal nueva quedó en .local/gov06dc-53caa9490a49:
PG 54286, HTTP 54287, IPC 54288; PID real Merchant 18604.
data_directory verificado C:/ANDES/Tesis/tesis-commerce-agent/.local/gov06dc-53caa9490a49/pgdata;
host 127.0.0.1, base tesis_lab y rol gov06dc_app. Clúster recién creado,
11 migraciones originales aplicadas y segunda ejecución idempotente; no se usa
PostgreSQL operativo. Se heredan guards DSN y de sockets/escrituras, socket
Windows compartido al PID real, UTC, roles sintéticos y HBA loopback de 06D-C.

SHA-256 evidence.json:
74edc96f5ba97eb74167c96274d32f5c71e1611d92c4afb88fc44e59dd5c9afc.
Los cuatro casos con envío produjeron cuatro checkouts distintos (uno por
operación), cero POST de fixture y cuatro verificaciones found_verified durables.
Las dos negativas produjeron cero POST, con snapshots Merchant idénticos.
Se conserva el historial en SQLite y se inspecciona con un proceso Agent nuevo
usando el worker 06D-C, sin HTTP. No equivale a reiniciar un runtime activo.

El primer ensayo quedó en .local/gov06dc-83c44c3e2ad6, puertos
53786/53787/53788, PID Merchant 12412; evidencia
c1643640d09e62d1e6d0faaee1d8666a81521250a8c3827a73deed5f75e8ca14.
Se conservaron ambos directorios nuevos y sus procesos fueron detenidos.

La última repetición reforzó solo aserciones de la prueba real: el digest
original_response_digest de 06D debe corresponder a la respuesta del 201 observado
por el proxy; en los positivos recibidos, el digest del snapshot creation_observed
debe corresponder al modelo de esa misma respuesta. Se aprobaron los seis casos
en 46.92s. El código comercial/puerto no cambió tras sus regresiones anteriores.
La suite general ya terminada había omitido estos seis casos por su gate; no se
afirma que ejecutó las nuevas aserciones de servicios reales.

Evidencia final 07A: .local/gov06dc-97bc35827fb9, puertos PG/HTTP/IPC
55035/55036/55037, PID real Merchant 13736; data_directory es su pgdata nuevo,
verificado con host 127.0.0.1, rol gov06dc_app y base tesis_lab antes de migrar.
SHA-256 evidence.json:
c8dfc58266f94b4fb615b9bf10b82ff29d9371798e68b467d0ed4f9fe2a29847.
Cuatro POST D1, cero de fixture; cuatro found_verified durables y dos bloqueos
previos al envío; snapshots invariables durante cada lookup. Procesos propios
detenidos y los tres listeners cerrados; datos conservados para revisión.

El histórico real utilizó otro directorio nuevo .local/gov06dc-b9f0ff4e6df4.
Puertos PG/HTTP/IPC 54447/54448/54449, PID inicial Merchant 24628,
reiniciado con PID 16212; mismo host, rol, base y verificación del directorio
pgdata exclusivo. SHA-256 evidence.json:
133f382d57cd65aec60f0bec50008b052d0bb924f8528aebb2823caaa87b6a66;
merchant-validation.json:
5fd66850f70ad8806f50ce2e59e02b0b2dcf192966dc3c3fbe0202af9cc1a6c2.
Sus 37 verificaciones registradas tuvieron clasificación esperada, incluyendo
cached/local y fallos conservadores (no equivalen a 37 solicitudes HTTP).
Warnings auxiliares Merchant: StarletteDeprecationWarning (testclient.py:1) y
DeprecationWarning (testclient.py:53), ambas en colección; no se captura su texto
para evitar datos accidentales. Sus ocho procesos hijos terminaron sin limpieza
forzada. El crash/restart de verificación 06D-C sigue probado con un proceso real.

Incidencias resueltas: primera ejecución unitaria dio 2 failed, 17 passed en
34.53s porque la respuesta antigua FakeCheckoutGateway no cumplía el esquema
ACP wire. Se reutilizó la respuesta sintética representativa del Merchant de
06D-B, sin relajar el esquema. Hubo errores estáticos de longitud, Optional,
tipos de callback/timeout y firma de override; se corrigieron sin ignores.
El primer launcher de regresiones históricas dio 32 errores de setup en 1.15s:
el módulo importado por nombre de paquete no era la instancia recolectada por
Pytest y no recibió el perfil de HEAD; el guard rechazó el HEAD anterior.
No se arrancó ningún clúster en ese intento. Se aplica el perfil al módulo
efectivamente recolectado mediante un hook, sin editar escenarios ni quitar guards.
Git advierte LF→CRLF futuro en los dos archivos modificados; no se cambian
atributos ni core.autocrlf y no se renormaliza el repositorio.

Comando real, desde la raíz Agent (el harness provisiona y detiene servicios):

```powershell
$env:UV_NO_SYNC='true'
$env:UV_OFFLINE='true'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:RUFF_NO_CACHE='true'
$env:RUN_GOV07A_REAL='1'
$newTemp = Join-Path $env:TEMP ('gov07a-' + [guid]::NewGuid().ToString('N'))
uv run pytest tests/integration/test_initial_dispatch_live.py -q --tb=short --assert=plain -p no:cacheprovider --basetemp=$newTemp
Remove-Item Env:RUN_GOV07A_REAL
uv run ruff format --check .
uv run ruff check .
uv run pyright
git diff --check
# Para cada uno de los cuatro archivos nuevos, sin stage:
git -c core.autocrlf=false diff --no-index --check -- NUL <ruta_nueva>
```

Unitarias/regresiones y suite offline usan el runner protegido por stdin de la
sección F del informe 06D-B, cuya fuente no se modificó. Elimina credenciales y
RUN_ del entorno, desactiva dotenv, prohíbe sockets operativos y escrituras al
repositorio, limita SQLite al basetemp nuevo y redirige las dos exportaciones
shadow históricas a cwd temporal. Invocación: $runner | .venv/Scripts/python.exe
-B - seguida de los argumentos de abajo; sin argumentos para la suite completa.

```text
tests/governance/test_initial_dispatch.py tests/governance/test_remote_creation.py tests/governance/test_d1_uow.py tests/governance/test_enforce_integration.py tests/governance/test_dispatch_envelope.py tests/governance/test_reconciliation.py tests/governance/test_post_create.py
```

Regresión real de los escenarios 06D-C intactos, en otro clúster nuevo,
después de que termina la suite 07A:

```powershell
@'
import os, functools, tempfile, uuid, pytest
from pathlib import Path
from tests.integration.gov06dc_harness import LiveEnvironment
from tests.integration.test_initial_dispatch_live import HEAD, ALLOWED
os.environ['RUN_GOV06DC_REAL']='1'
os.environ.pop('RUN_GOV07A_REAL',None)
class Profile:
 def pytest_collection_modifyitems(self,items):
  for item in items:
   if item.path.name=='test_live_creation_recovery.py':
    item.module.LiveEnvironment=functools.partial(LiveEnvironment,agent_head=HEAD,allowed_agent_changes=ALLOWED)
raise SystemExit(pytest.main(['tests/integration/test_live_creation_recovery.py','-q','--tb=short','--assert=plain','-p','no:cacheprovider','--basetemp='+str(Path(tempfile.gettempdir())/('gov07a-06dc-'+uuid.uuid4().hex))],plugins=[Profile()]))
'@ | .venv/Scripts/python.exe -B -
```

El perfil admite exclusivamente las dos rutas modificadas de D. Todo otro diff
versionado o cualquier cambio Merchant provoca fallo de preflight. No admite
otro HEAD, DSN o destino. No se comparten conexiones PostgreSQL entre suites;
las offline corren solo sobre SQLite temporal separado. Ninguna dependencia nueva.

## G. Matriz adversarial

| Escenario | Medio | Esperado y observado |
| --- | --- | --- |
| Primer POST válido | PostgreSQL/HTTP real, puerto D1 | d1_initial_creation_observed, un checkout, un evento creation_observed, sin GET posterior |
| Respuesta perdida | Proxy TCP corta después de recibir 201 real | d1_dispatch_uncertain; un checkout; consulta explícita found_verified |
| Timeout | Proxy retrasa .8s tras el 201 real; cliente .5s | d1_dispatch_uncertain; sin segundo POST; found_verified histórico |
| Dos starts concurrentes | Threads, HTTP y SQLite reales | Un ganador observado, otro rechazo conservador; un POST, un checkout |
| Token ausente | Backend configurado sin token; Merchant real disponible | d1_unavailable antes del claim; reserva conservada, cero HTTP, tablas iguales |
| Scope contradictorio | Registro de sesión sintético alterado en fixture | d1_unavailable antes del claim; cero HTTP, tablas iguales |
| Envelope/D1 antes de POST | Proxy inspecciona SQLite desde otra conexión antes de reenviar | Claim committed, envelope verificable, body/key exactos; nunca se registra su valor |
| Reinicio e inspección | Proceso Agent independiente y reapertura SQLite | Un evento de verificación íntegro recuperado sin HTTP adicional |
| Denegación D1 | Offline con PDP real, MockTransport auxiliar | d1_denied; no se llama transporte |
| JSON/currency/quantity/variant/status/clave contradictorios o redirect | Respuesta sintética controlada | Incertidumbre; no segundo POST, sin llamadas al gateway comercial |
| Corrupción de envelope con model_copy | Offline | Rechazo antes de red |
| Crash después del claim | SystemExit inyectado en MockTransport, no caída real de proceso en esta prueba | Claim conservado; start posterior bloqueado |
| Fallo al persistir observación | Excepción OSError inyectada en append de SQLite descartable | Incertidumbre y sin reenvío; no se afirma fallo real de disco |
| Flujo anterior off/shadow y 06A–06D | Regresiones protegidas e histórico real | Resultados de cierre en F |

Durante las cuatro verificaciones nuevas, snapshots completos de las 12 tablas
públicas antes/después del lookup permanecieron idénticos: cero nuevos checkouts,
órdenes, completion_attempts y mutaciones. Los POST iniciales se contabilizan
por separado, antes del intervalo del lookup. La ausencia de llamadas GET,
UPDATE, cancel y complete se comprueba mediante NoEffectsGateway y rutas del proxy;
no existe conexión a PSP ni LLM. No se sustituye el positivo con MockTransport.

## H. Límites

D17, D2-HP, autorización vigente, continuación y activación runtime permanecen fuera.
No se implementa consentimiento HP, compra autónoma, actualización de datos,
presupuesto autoritativo D2, pago, completion, HNP o liberación de reservas.
La respuesta de creación no permite seguir: precio/oferta, términos, expiración,
revocación y checkout vigente requieren sus controles posteriores. Los controles
actuales de continuación de PurchaseService y CompletionService no se modifican.

La identidad es la sesión real emitida/autenticada por el Merchant de laboratorio
y el proceso/socket controlado; HTTP loopback no demuestra TLS de producción ni
identidad de un despliegue remoto. No se afirma exactly-once distribuido, SLO,
retención ilimitada, rendimiento comercial, validación de AP2-HNP o Linux.
La caída inicial SystemExit es auxiliar; el reinicio inspector es un proceso real,
y los fallos de entrega usan sockets reales. El runtime no se arrancó.

## I. Dictamen

READY_FOR_REVIEW. El despacho inicial real quedó demostrado con D1, envelope
durable, sesión provisionada, HTTP y PostgreSQL reales, un intento y recuperación
autenticada sin nuevos efectos. No es un flujo HP completo y no habilita runtime,
D17, D2-HP o continuación. Se detiene para auditoría independiente.

Rama: governance/ec-gov-07a-hp-initial-dispatch.
HEAD conservado: 9f15d20906f576b7d28138ff9f5708022b405ae8.
Índice vacío; sin stage, commit, push, merge ni PR. Merchant permanece limpio
en c5bd8113998f26928f7d2b62771285fe4040d07f y sus remotos no se alteran.

La comparación de bytes mantiene intactos 329 archivos versionados Agent fuera
de las dos modificaciones autorizadas: agregado
840dca3e1c14d2abeb4555cd10b046ae007ebad0404609ddd976428f8f340666.
Los 273 archivos Merchant conservan
cf7e564e9b82b083e72a4ffd14b15d10415edcf6c2946b038953570a92b9eb31.
Los 1116 archivos históricos de .local/gov05c conservan
def9c7416a05abae4597c23c369a54ed6cf2704ffc201334717e855fcdc810cd.
Método: ordenar rutas relativas, SHA-256 de bytes reales por archivo, SHA-256
de líneas ruta:hash unidas con LF. Baseline total Agent inicial: 331 archivos,
173df11db5a016159fa476192a2eb39b17740185998f219e56c91bf4f01bd2ad.
Esto incluye escenarios 06D-C, documentos anteriores, código comercial,
runtime/Settings, ACP/AP2, uv.lock y B0 versionado sin cambios. No se ejecutó B0.

Estado final comprobado:

```text
## governance/ec-gov-07a-hp-initial-dispatch
 M src/commerce_agent/governance/enforce.py
 M tests/integration/gov06dc_harness.py
?? docs/b2-governance/ec-gov-07a-hp-initial-dispatch-results.md
?? src/commerce_agent/acp/initial_dispatch.py
?? tests/governance/test_initial_dispatch.py
?? tests/integration/test_initial_dispatch_live.py
```

git diff --check: sin errores de whitespace; los cuatro archivos nuevos también
se verifican contra NUL sin stage. El código 1 de --no-index significa que el
archivo es nuevo; se exigen diagnósticos vacíos. -c core.autocrlf=false afecta
solo a esa lectura para suprimir avisos de conversión futura, no a la configuración.
Los clústeres nuevos y procesos propios se detienen y sus evidencias se conservan;
no se borran datos anteriores ni se detienen servicios ajenos.
La comprobación final verificó los 12 puertos de las cuatro ejecuciones reales
cerrados y ningún postmaster.pid en los directorios gov06dc creados. Se repitió
la comparación de hashes y el control de whitespace después de todas las suites.
