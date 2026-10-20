# EC-GOV-06D-C — Integración real de recuperación Agent–Merchant

## A. Git y preflight

Agent: rama inicial `governance/ec-gov-06d-b-remote-creation-verification`, HEAD
`4b890cb468d29e658d4c79987ec59d86394c4582`, árbol limpio, índice vacío,
sin stashes, rama de destino ausente y sin locks Git observados. El inventario
de tareas solo mostró esta conversación activa; no se afirma exclusión de editores
externos. Se creó exclusivamente `governance/ec-gov-06d-c-live-recovery-integration`
desde ese HEAD. Origin sigue en `https://github.com/Maldinho18/tesis-commerce-agent.git`
y org en `https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent.git`.

Merchant: `merchant/ec-mer-rec-02-readonly-lookup`, HEAD
`c5bd8113998f26928f7d2b62771285fe4040d07f`, árbol limpio y origin
`https://github.com/Maldinho18/tesis-commerce-merchant.git`. No tiene remoto org.
Se ejecuta ese checkout exacto, sin checkout alternativo, fetch, copia mutable,
instalaciones, stage, commit, push, merge, rebase o PR en ninguno de los repositorios.

Los hashes iniciales cubren los 326 archivos versionados de Agent, los 273 de
Merchant y 1116 archivos previos de `.local/gov05c`. Los datos Merchant/SQLite y
la evidencia principal nueva se crean bajo `.local/gov06dc-<nonce>` que no existía;
Pytest y las suites offline usan también basetemp nuevos del directorio temporal OS.

## B. Contratos reales revisados

Se revisaron las APIs de 06D-B: BackendLookupSession, CreationLookupClient,
SessionScope, RemoteCreationVerifier, VerificationRequest y SqliteRemoteCreationStore;
envelope, reconciliación, post-create, su persistencia y gate enforce; config,
pruebas de 06A–06D-B y el informe 06D-B, incluida la corrección H1.

Merchant: api, context, acp, creation_lookup, creation_lookup_api, sus contratos,
db/core, settings, payment_sandbox, fixtures y migraciones originales; pruebas
de lookup y recuperación. Se comprobaron las firmas de seed, issue_lab_session,
authenticate_lab_session, migrate, prepare_in_transaction y ACPCheckoutAdapter.

El lookup privado usa POST `/lab/recovery/checkout-creation/lookup`, versión
`merchant-acp-creation-lookup/1`, API-Version `2026-04-17`, Bearer real de laboratorio
y únicamente el hash de clave en el cuerpo. No lleva Idempotency-Key comercial.
La respuesta original procede del POST real y del JSONB preservado por Merchant;
el cliente verifica perfil `merchant-acp-original-response/1`, digest de dominio,
fingerprint `merchant-acp-create/v1`, checkout, producto, moneda, cantidad y scope.
El handler real `tesis_sandbox` y shipping se validan sin exigir igualdad con las
capacidades del request. No se modificó ni debilitó ese verificador.

Merchant fija `tesis_lab` en su validador DSN: se conserva ese nombre dentro de
un clúster totalmente nuevo. No se alteró el validador para inventar otro nombre.
Un episodio solo pertenece a un actor: las pruebas de otro actor usan otro run
legítimo; las de otro run conservan el actor cuando corresponde.

## C. Arquitectura de integración

La activación explícita `RUN_GOV06DC_REAL=1` crea PostgreSQL 17.2 nuevo con initdb,
solo 127.0.0.1, puerto libre no perteneciente a 55432/55433/62281, UTF-8 y timezone UTC.
No se elimina ni reutiliza una carpeta previa. El bootstrap usa gov06dc_admin;
la aplicación usa gov06dc_app, propietario de su base, sin SUPERUSER/CREATEDB/CREATEROLE.
Se le concede pg_read_all_settings para comprobar data_directory. La guía de
PostgreSQL se aplicó al aislamiento y privilegios, sin introducir ORM ni índices.

La identidad real de host/puerto/base/rol/directorio se verifica antes de migrar
y en cada conexión de los procesos Merchant. Se rechaza otro DSN. Primera migración
sobre cero tablas públicas y segunda ejecución idempotente. PGCONNECT_TIMEOUT=2
limita las conexiones fallidas del entorno de prueba; no cambia código aprobado.
HBA trust se limita a los roles sintéticos y loopback del clúster descartable;
el password sintético exigido por el formato DSN no acredita seguridad de producción.

Procesos separados:

- PostgreSQL propio, arrancado/detenido por pg_ctl con su directorio exacto.
- Worker de aprovisionamiento bajo Python/venv de Merchant, con IPC autenticado.
- Merchant FastAPI/Uvicorn real bajo su venv, en otro proceso y con HTTP loopback.
- Pytest/Agent bajo su propio venv; un proceso Agent adicional prueba crash y restart.

Agent nunca importa commerce_lab. El worker extranjero lo importa solo dentro del
proceso Merchant. Su guard añade comprobaciones del destino PostgreSQL y prohíbe
escrituras en ambos repositorios fuera del nuevo directorio; no sustituye handlers
ni resultados de negocio. El observador ASGI solo conserva tipos/ubicaciones de
excepciones sin sus valores. El app FastAPI servido es el original congelado.

En Windows el launcher .venv/python y CPython tienen PIDs distintos. El hijo publica
su PID/parent PID en un archivo de arranque nuevo; el padre verifica el vínculo
con el Popen propio. El padre reserva un socket exclusivo y lo comparte mediante
socket.share con ese PID real. El hijo verifica su dirección y ejecuta Uvicorn
con ese socket; conserva un marcador PID/dirección. No hay intervalo de rebind ni
se atribuye identidad por una cadena devuelta en el lookup. Este mecanismo se usa
porque CIM no estaba disponible y netstat no listó ni un listener propio de prueba.

El worker ejecuta seed en la base aislada, emite la sesión con issue_lab_session y
la comprueba con authenticate_lab_session antes de devolverla por IPC autenticado.
Run/actor/session hash son el origen del binding esperado y SessionScope, antes de
comprometer el envelope. Nunca se rellenan desde la respuesta de lookup. Local run
y Merchant run difieren. Bearer, claves de procedencia y credenciales permanecen
en memoria/canales privados; no se escriben tokens en stdout, fixtures o informes.

El setup crea ProductFeed nuevo con un offer ID efectivo del Merchant y hechos
observados de su catálogo. El gate real compromete D1/envelope con dispatch=None;
NoEffectsGateway rechaza invocaciones comerciales. Solo después un componente
de fixture envía exactamente un POST sintético con el cuerpo canónico y clave
del envelope. La recuperación nunca envía ese POST. En el caso principal no se
usan MockTransport, TestClient ni JSON found fabricado.

## D. Implementación

Solo se agregan:

- `tests/integration/gov06dc_harness.py`: clúster, procesos, identidad, fixture
  conjunto, snapshots, proxy de fallos y mediciones.
- `tests/integration/gov06dc_merchant_worker.py`: aprovisionamiento/observador/servidor
  extranjero y ejecución secuencial de regresiones del Merchant congelado.
- `tests/integration/gov06dc_agent_worker.py`: inspección tras restart y caída real
  os._exit(81) antes del commit, después de recibir y validar HTTP.
- `tests/integration/test_live_creation_recovery.py`: 32 casos opt-in.
- Este informe.

No se modifican archivos publicados de Agent, Merchant, PSP, UI/SSE, Yair, B0,
ACP/AP2, dependencias, uv.lock, Settings/runtime ni endpoints públicos. El harness
es infraestructura de pruebas; no es un worker de recuperación instalado.

## E. Invariantes y evidencias

La clave y cuerpo comprometidos se usan sin reconstrucción ni cambio posterior
de binding. El resultado original se compara con el POST real, fingerprint,
digest SHA-256, checkout y referencias D1. Se conserva replay_allowed=false y
continuation_allowed=false, incluso para found_verified. La compra local no cambia
de estado ni recibe autorización vigente a partir de esa evidencia histórica.

Cada verify_zero toma snapshots de todas las tablas públicas Merchant, con counts
y hashes deterministas, después del setup. Antes/después deben ser idénticos:
cero nuevos checkouts, órdenes, pagos o mutaciones comerciales durante la verificación.
En el positivo principal: checkout_sessions 1→1, orders 0→0,
checkout_mutations 0→0 y checkout_completion_attempts 0→0; hashes de las 12 tablas
públicas idénticos. El esquema no tiene una tabla payments independiente; se
comprueba la tabla de sesiones completa, las de completion y orders. No se
infiere una consulta a un PSP externo: no hubo comunicación con ninguno.
El POST de setup y la cancelación explícita de fixture se cuentan fuera de ese intervalo.
Las pruebas de lookup sin creación y creación pendiente demuestran que not_found
no prueba ausencia histórica ni autoriza replay. Unrecoverable usa prepare legítimo,
sin inventar ni borrar el par ACP.

La prueba de pendiente usa prepare_in_transaction, _response y _save_creation
originales en un proceso Merchant que mantiene la transacción abierta: lookup HTTP
observa not_found; después de commit explícito observa found. Es API interna real
de fixture, no POST HTTP de creación; se distingue del caso principal, cuyo setup
y lookup sí usan HTTP real. No hay respuesta found manual en ninguno.

El catálogo se modifica válidamente tras creación y también se cancela un checkout
en setup. La consulta conserva exactamente la respuesta original. La implementación
congelada del lookup selecciona solo checkout_sessions, sin catálogo/reconstrucción;
esto se inspeccionó en código y se contrasta con esas modificaciones reales. No se
afirma haber capturado un trace SQL exhaustivo de cada petición.

SQLite es nuevo por caso. Se verifica commit, reapertura y un proceso independiente
que recupera digests del historial sin hacer HTTP. Crash antes de commit deja cero
eventos; fallo de commit no informa durabilidad; un intento explícito posterior
solo repite la consulta. CAS permite un evento y un rechazo conservador concurrente.
Con el mismo query_id, correlaciones servidor diferentes pueden causar conflicto:
no se sobrescribe el ganador; repetir su inspección no envía otro HTTP.

## F. Resultados de pruebas y reproducción

Validaciones completadas en Windows el 15 de octubre de 2026:

| Control | Resultado |
| --- | --- |
| 06D-B + regresiones 06A/06B/06C | 309 passed in 652.10s: 102 específicas y 207 regresiones; cero omitidos. |
| Suite general offline protegida | 841 passed, 35 skipped, 1 warning in 729.86s. |
| Matriz real 06D-C | 32 passed in 332.08s (0:05:32), cero omitidos y cero advertencias propias. |
| Regresiones Merchant en clúster aislado | 105 passed, 0 failed, 0 skipped, 2 warnings en 117.6214567s; ejecutadas secuencialmente dentro del último caso. No se suman nuevamente a los 32 casos Agent. |
| Ruff format / check | 167 files already formatted; All checks passed! |
| Pyright | 0 errors, 0 warnings, 0 informations. |
| Diff checks | git diff --check y whitespace de los cinco archivos nuevos, sin stage. Resultado final consignado en I. |

Los 35 skips offline son 32 casos nuevos por activación explícita ausente y los
tres E2E previos: test_ap2_joint_purchase.py:32, test_conversational_purchase.py:74
y test_joint_purchase.py:95, que requieren otros servicios. Las 32 omisiones nuevas
no se presentan como validación real: la matriz se ejecuta por separado con el flag.
La advertencia offline conocida es DeprecationWarning de starlette/testclient.py:53
por el alias anyio.abc.BlockingPortal. No se cambian dependencias para ocultarla.

Versiones registradas: Windows 11, Python Agent y Merchant 3.12.6; PostgreSQL 17.2;
Merchant psycopg 3.3.5, FastAPI 0.141.1, Uvicorn 0.53.0, httpx 0.28.1 y Pydantic 2.12.5.
uv 0.12.15 (d35f1f270 2026-09-15 x86_64-pc-windows-msvc). No hubo instalación
ni sincronización de dependencias. No se ejecutó validación Linux.
Agent: pytest 9.0.2, Ruff 0.16.8, Pyright 1.1.414, httpx 0.28.1,
Pydantic 2.12.5, cryptography 46.0.5 y FastAPI 0.141.1.

El resumen inicial Merchant preservó solo el conteo de advertencias. Para conocer
su origen sin guardar valores potencialmente sensibles se añadió únicamente
captura de categoría/archivo/línea al reporter auxiliar y se repitió ese caso:
`-k merchant_regressions`, basetemp nuevo. Resultado externo: 1 passed,
31 deselected in 121.41s; interno: 105 passed, 0 failed, 0 skipped,
2 warnings en 101.9855978s. No son pruebas adicionales distintas ni se suman
al corpus principal. Advertencias de colección: StarletteDeprecationWarning
en testclient.py:1 y DeprecationWarning en testclient.py:53. No se guardó su texto.
Ruff y Pyright finales incluyen ese cambio de observabilidad; no cambió la lógica
validada de recuperación. Las suites de 309/841 y la matriz de 32 corresponden
a las ejecuciones indicadas, anteriores a esa captura adicional de metadatos.

Evidencia de esa repetición: `.local/gov06dc-e526cf83aa04`, PG/HTTP/IPC
65239/65240/65241, PID real Merchant 23616, mismo par de commits. Su bootstrap
verificó el directorio pgdata nuevo, base tesis_lab, gov06dc_app y 127.0.0.1.
SHA-256 merchant-validation.json:
`f6b1c1c17e3ec495844e82ef8c7951880c73b50de8cf05787020f160a3898fa3`;
evidence.json: `2cc4464347b8ab026704926ded850620ef8636f95a682d5f805adb10d85ff48d`.
Los ocho hijos terminaron solos; servidor/control/PG detenidos, tres puertos cerrados.
Un primer comando auxiliar fue rechazado por Pytest al recibir basetemp vacío
(exit 4, sin colección ni servicios); se corrigió la sintaxis PowerShell pasando
una variable de ruta nueva. Un script de inventario también falló por decodificar
el informe UTF-8 con cp1252; se repitió con encoding explícito, sin editar datos.

Ejecución principal conservada en `.local/gov06dc-60ae4c7b8d42`:
PostgreSQL 63297, Merchant HTTP 63298 e IPC 63299; data_directory verificado
`C:\ANDES\Tesis\tesis-commerce-agent\.local\gov06dc-60ae4c7b8d42\pgdata`,
base tesis_lab, rol gov06dc_app y host 127.0.0.1. Migraciones: 11 aplicadas por
primera vez, 11 ya aplicadas en segunda ejecución, incluida 013 exactamente una vez.
El PID real inicial de Merchant fue 24888 y tras restart 12032; sus marcadores
de arranque/socket y la relación con el launcher están en ese directorio.

De 37 invocaciones registradas por verify_zero, 37 tuvieron la clasificación
esperada (100% de este corpus sintético, no estimación de precisión poblacional):
13 found_verified; 4 authentication_error; 3 unattributable; 2 identity_conflict;
3 local_invalid; 2 not_found; 1 unrecoverable; 1 response_invalid;
2 malformed_response; 1 timeout; 3 transport_error; 1 cas_conflict y
1 persistence_error. Algunas son lecturas locales idempotentes; no son 37 HTTP.
Concurrencia, 503, crash y fases medidas tienen además registros/aserciones
separados. Ninguna verificación creó checkout ni mutó tablas comerciales.

Medición puntual de fases, con bootstrap y seed excluidos: lookup HTTP y
validación wire 82.4263 ms; revalidación/append SQLite 8.4041 ms; commit SQLite
2.7160 ms; total del verificador 119.3306 ms. Las fases no cubren todo el total
(también hay lectura local y validaciones). En el caso positivo principal,
HTTP incluyendo construcción del cliente midió 387.9246 ms. Son muestras reales,
no percentiles, SLO o medición de rendimiento comercial.

Referencias SHA-256 de evidencia pública, sin Bearer ni clave idempotente cruda:

- `evidence.json`: `5c08f4b7e2284ea9422340b2951b363c36baa6889b8c4cafbee114e1ed1fffe5`.
- `merchant-validation.json`: `d6b4b0cdfbc2f02210c811ba83d2d7408fceb2b9ac11c51b786925fab31fec62`.

El digest histórico de respuesta del positivo es
`e983ebe7b82dc55d1a698de2d67287090ead7fdf80132084a597ef9710eba8e6`,
fingerprint `3c5b8515d5e5c117e4d53572b95de0eafd20c82e5ba42e373218cfb7fde010b2`
y correlación servidor `6a8bf421-1f71-41ee-9822-05a9af6d218b`. Cada evento
completo, referencias D1/envelope, count/hash antes/después y su base SQLite
están en la evidencia local. No se publica el material de firma efímero.

Incidencias conservadas, sin modificar criterios ni desactivar casos:

| Ejecución de desarrollo | Resultado / corrección de infraestructura |
| --- | --- |
| Primer smoke | 1 error en 97.90s: pipe heredado de pg_ctl. Se detuvo solo el clúster propio y se capturaron comandos PG en archivos nuevos. |
| Segundo smoke | 1 error en 6.97s: inet::text incluye /32; se usa host(inet_server_addr()), conservando igualdad exacta de host. |
| Smokes de diagnóstico | 1 failed en 16.60s; 1 failed en 16.10s; 1 failed en 17.99s. Se localizó ValueError de parse_timestamp con timezone local. Clúster nuevo configurado/verificado en UTC; no se relajó el contrato temporal. |
| Primera matriz | 1 failed, 13 passed en 66.57s: borrar catálogo violaba FK. Se conserva la FK y se prueban cambios válidos de revisión/nombre/precio/disponibilidad. |
| Segunda matriz | 1 failed, 29 passed en 179.88s: la conexión PG caída excedía el timeout HTTP. Se fija PGCONNECT_TIMEOUT=2 en el entorno descartable. |
| Tercera matriz | 1 failed, 30 passed en 281.39s: los 30 casos reales pasaron; auxiliar Merchant dio 104 passed, 1 failed, 0 skipped, 2 warnings en 104.55s, al bloquear el guard un puerto efímero del test HTTP de restart. Se registran puertos reservados por el propio test y se sigue cada proceso hijo para limpieza. |
| Validaciones de atribución OS | 32 errores de setup en 21.31s (CIM no disponible), 32 en 18.05s (netstat sin listener), 32 en 22.77s (PID launcher distinto del real). Son fallos de una fixture común, no 32 fallos funcionales diferentes. Se sustituyen por transferencia del socket al PID real comprobado. |

Hubo errores estáticos transitorios de imports, firmas y Optional en el harness;
se corrigieron con imports, anotaciones y comprobaciones explícitas, sin ignores,
cambios de configuración o debilitamiento de aserciones. Todos los directorios
de diagnóstico se conservan; no se reutilizan ni se borran para obtener verde.

Reproducción Windows PowerShell desde la raíz Agent, con ambos checkouts exactos:

```powershell
$env:UV_NO_SYNC = 'true'
$env:UV_OFFLINE = 'true'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:RUFF_NO_CACHE = 'true'
$env:RUN_GOV06DC_REAL = '1'
$tempRun = Join-Path $env:TEMP ('gov06dc-' + [guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $tempRun) { throw 'El destino temporal debe ser nuevo' }
uv run pytest tests/integration/test_live_creation_recovery.py -q --tb=short -p no:cacheprovider --basetemp=$tempRun
Remove-Item Env:RUN_GOV06DC_REAL
uv run ruff format --check .
uv run ruff check .
uv run pyright
git diff --check
# Para cada uno de los cinco archivos nuevos, sin stage:
git -c core.autocrlf=false diff --no-index --check -- NUL <ruta_nueva>
```

La opción -c es exclusiva de esa invocación de lectura: no cambia configuración
local/global ni bytes. Para un archivo nuevo diff --no-index devuelve 1 por la
diferencia contra NUL; se exige stdout/stderr vacío, sin errores de whitespace.
Con la configuración normal Git advirtió que LF se convertiría en CRLF cuando
lo tocara; no se renormalizó ningún archivo ni se cambiaron atributos.

El test prepara y detiene sus servicios; no hay DSN externo que apuntar ni puertos
operativos que arrancar. La ejecución final usó un basetemp distinto de todos los
anteriores. Para las validaciones offline se utilizó el runner protegido descrito
en 06D-B por stdin: elimina RUN_ y variables de proveedores, desactiva dotenv,
prohíbe sockets operativos, SQLite fuera de su basetemp y escrituras al repositorio;
redirige dos exportaciones shadow a cwd temporal. Los argumentos fueron los seis
archivos governance de 06D-B/06A–06C para las 309, y ninguno para la suite general.
La fuente exacta del runner protegido está en el bloque Python de la sección F
del informe `ec-gov-06d-b-remote-creation-verification-results.md`, sin editarlo.
Se pasa por stdin con `$runner | .venv/Scripts/python.exe -B -`; para las 309 se
agregan estos argumentos, en una sola invocación:

```text
tests/governance/test_remote_creation.py tests/governance/test_d1_uow.py tests/governance/test_enforce_integration.py tests/governance/test_dispatch_envelope.py tests/governance/test_reconciliation.py tests/governance/test_post_create.py
```

Para reproducir solo una fila de la matriz se usa el mismo comando real con
`-k <nombre_del_test>` (los parámetros están en test_live_creation_recovery.py).
Siempre se crea un clúster nuevo. Las suites offline no comparten PostgreSQL
con la matriz real; el auxiliar Merchant
se ejecuta secuencialmente y tiene su propio basetemp nuevo dentro del mismo clúster.

## G. Matriz adversarial

| Escenario | Medio y resultado exigido/observado por las aserciones |
| --- | --- |
| Creación real y found | POST explícito de fixture + HTTP real; original_response exacta, fingerprint/digests/ID correctos, capacidades tesis_sandbox y shipping reales; found_verified durable. |
| Sesión renovada | Emisor/authenticator reales; token distinto, mismo scope, found_verified. |
| Token inexistente, expirado, revocado | 401 real no-store; authentication_error, sin acceso al checkout. Expiración/revocación se preparan solo en lab_sessions del clúster nuevo. |
| Otro run / otro actor | Sesiones legítimas de otros episodios: HTTP real not_found en su propio scope; verificador rechaza el desacuerdo antes de consultar. |
| Registro de sesión mentiroso | Token real ajeno con metadatos deliberadamente erróneos del fixture: HTTP genuino devuelve otro scope y Agent rechaza identity_conflict. No es un aprovisionamiento válido. |
| Binding ausente / run local incorrecto | unattributable / local_invalid antes de HTTP; no autoridad inferida desde el token ni el response. |
| Request-Id del cliente | Merchant emite un ID diferente; body/header coinciden. Solo correlación, no identidad. |
| Not_found y posterior creación | Primera consulta real sin fila; un único POST posterior de fixture; nueva consulta found, sin replay por Agent. |
| Creación sin commit | Transacción real abierta en worker Merchant; not_found concurrente, found después del commit, sin deducir ausencia histórica. |
| Interna sin par ACP | prepare legítimo conserva registro histórico; unrecoverable, sin backfill ni reconstrucción. |
| Catálogo posterior cambiado | Precio/nombre/revisión/disponibilidad distintos; respuesta original exacta. |
| Checkout cancelado en setup | Histórico original not_ready_for_payment permanece; no se interpreta como vigencia actual. |
| Scope/digest/perfil/JSON alterados | Proxy de sockets real altera explícitamente una respuesta tras lookup genuino; identity_conflict/response_invalid/malformed_response. No se atribuye la respuesta modificada al Merchant genuino. |
| Timeout / pérdida de respuesta | Proxy espera o corta TCP tras completar lookup real; timeout/transport_error conservadores, sin efectos. |
| Merchant caído y reiniciado | Proceso propio detenido, error de transporte; restart con otro PID; inspección idempotente y nueva consulta explícita, sin replay. |
| 503 real | PG propio detenido, endpoint original devuelve lookup_unavailable; se reinicia el mismo clúster propio y se conserva la evidencia. |
| Dos consultas / sesiones | HTTP concurrente real, scopes legítimos, dos correlaciones servidor; tablas invariantes. |
| Mismo/distinto query_id, misma versión | Dos HTTP reales; un ganador y un CAS conflictivo; un único evento, sin sobrescritura. |
| Revocación entre solicitudes | Sesión vieja rechazada en consulta nueva; renovada aceptada. No se promete revocación a mitad de una petición. |
| Envelope / evidencia anterior corruptos | Corrupción solo en SQLite descartable; local_invalid, sin nuevo HTTP; history falla cerrado. |
| Purchase modificada durante HTTP | Callback de fixture después del lookup real; CAS rechaza resultado obsoleto. No modifica Merchant. |
| Crash Agent antes de commit | Proceso real sale con 81 después de recibir/validar HTTP e insertar provisionalmente; otro proceso recupera cero eventos. |
| Commit fallido y recuperación | Excepción inyectada solo en SQLiteConnection de fixture; no durable; consulta explícita posterior confirma un evento recuperable por otro proceso sin HTTP. |
| Sin efectos durante verificación | Snapshots completos, contadores y rutas del proxy; cero checkouts/órdenes/pagos/mutaciones nuevos después del setup. |

No se utilizan mocks en la prueba principal. Las alteraciones de respuestas,
callback de purchase y fallo de commit son inyecciones auxiliares explícitas;
timeout/drop usan procesos/sockets reales. Las regresiones Merchant incluyen
TestClient como pruebas auxiliares de su repositorio, nunca como sustituto del
servicio independiente en la matriz Agent–Merchant.

## H. Alcance y limitaciones

Se comprueba la frontera con sesión real y almacenamiento real, no TLS remoto,
identidad de un despliegue de producción, retención ilimitada, exactly-once
distribuido, vigencia actual del consentimiento/oferta o autorización de continuación.
No HNP, D17, D2, firma AP2, PSP, completion, worker automático ni enforce comercial.
El host y sus procesos de prueba son confiables; HBA/IPC no son un modelo para cloud.

Las métricas son mediciones locales excluyendo bootstrap/seed, sin SLO ni comparación
B0 contra gobernanza. El sample HTTP del caso principal incluye creación del cliente;
la medición dedicada separa lookup+validación wire, revalidación/append SQLite,
commit y total del verificador. No representa latencia pura de red ni un benchmark
de carga. Las pruebas no garantizan revocación concurrente durante una consulta.

Los procesos originales se controlan por Popen y el socket por transferencia al PID
real. Se conservan los datos nuevos para auditoría; limpieza significa detener
exclusivamente esos procesos, cerrar listeners y no borrar evidencia previa.

## I. Dictamen y estado final

READY_FOR_REVIEW. Se demuestra la consulta autenticada real y durable del
contrato publicado, sin alterar su implementación ni autorizar continuación.
Las respuestas históricas siguen sin acreditar vigencia comercial.

Rama Agent: `governance/ec-gov-06d-c-live-recovery-integration`.
HEAD Agent: `4b890cb468d29e658d4c79987ec59d86394c4582`.
Índice vacío; sin stage, commit, push, merge ni PR.
git diff --check terminó sin salida y con código 0. Los cinco controles de
whitespace contra NUL terminaron sin diagnósticos, código 1 por archivos nuevos.

Solo los cinco archivos nuevos enumerados en D. Estado detallado:

```text
## governance/ec-gov-06d-c-live-recovery-integration
?? docs/b2-governance/ec-gov-06d-c-live-recovery-integration-results.md
?? tests/integration/gov06dc_agent_worker.py
?? tests/integration/gov06dc_harness.py
?? tests/integration/gov06dc_merchant_worker.py
?? tests/integration/test_live_creation_recovery.py
```

Los bytes iniciales/finales coinciden: Agent 326 archivos versionados, agregado
`6798c3f8a1f31690553adf82b801d7ccddf7b7b73b2ca6a345779d6dd97e31e4`;
Merchant 273, `cf7e564e9b82b083e72a4ffd14b15d10415edcf6c2946b038953570a92b9eb31`;
histórico `.local/gov05c` 1116 archivos,
`def9c7416a05abae4597c23c369a54ed6cf2704ffc201334717e855fcdc810cd`.
Método: ordenar rutas; SHA-256 de cada archivo real; SHA-256 del texto
`ruta:hash` separado por LF. Merchant permanece limpio en su HEAD requerido.

La evidencia shutdown confirma servidor, control y PostgreSQL propios detenidos;
los puertos 63297/63298/63299 están cerrados. Los ocho hijos de las regresiones
Merchant terminaron; ninguno requirió terminación forzada. Se conservan todos los
directorios nuevos para auditoría, sin borrar ni sobrescribir datos anteriores.
El cierre final volvió a comprobar los seis puertos de las dos ejecuciones
satisfactorias y la ausencia de postmaster.pid en todos los clústeres gov06dc
creados. La última comparación byte a byte y los cinco controles de whitespace
pasaron después de la repetición auxiliar.
No se inicia otro incremento.
