# EC-GOV-07C — Consentimiento humano D2-HP vinculado a D17

Estado: **READY_FOR_REVIEW**. Implementación experimental backend-only, validada offline
con humanos sintéticos autenticados. No constituye integración con humanos reales en UI,
autorización para ejecutar comercio ni habilitación del runtime enforce.

## A. Preflight y SHAs

Base exacta comprobada: `3ec19f58cfa57236336c2722addae53ebacd8b4b`, rama base
`governance/ec-gov-07b-b-d17-integration`. Origin local comprobado:
`https://github.com/Maldinho18/tesis-commerce-agent.git`.
Nuevo worktree: `C:/ANDES/Tesis/tesis-commerce-agent/.local/worktrees/ec-gov-07c`.
Nueva rama: `governance/ec-gov-07c-d2-hp-consent`. HEAD permanece en la base exacta.
Merchant congelado: `9743c9f179c7ad5413e8818766ef3dceb326a917`.
No se hizo fetch, stage, commit, push, merge ni PR.

Inventario protegido, medido por bytes al inicio y recomputado al cierre:

| Árbol | HEAD comprobado | Archivos protegidos | Estado Git |
| --- | --- | ---: | --- |
| `tesis-commerce-agent` (C:\ANDES\Tesis\tesis-commerce-agent) | `35fd2d80e6f10150d5f0651b386adaa4eff2d40e` | 1452 | ?? docs/b2-governance/ec-gov-07b-d17-offer-freshness-results.md |
| `tesis-commerce-merchant` (C:\ANDES\Tesis\tesis-commerce-merchant) | `9743c9f179c7ad5413e8818766ef3dceb326a917` | 287 | limpio |
| `tesis-commerce-lab` (C:\ANDES\Tesis\tesis-commerce-lab) | `b3be474e26ebbedc79dd4f3c3b133f42491f9542` | 147 | limpio |
| `tesis-payment-provider-sandbox` (C:\ANDES\Tesis\tesis-payment-provider-sandbox) | `ee19c06ecae3038b43d1fc7cb7a8822592fa1a08` | 17 | limpio |
| `ec-gov-07b-b` (C:\ANDES\Tesis\tesis-commerce-agent\.local\worktrees\ec-gov-07b-b) | `35fd2d80e6f10150d5f0651b386adaa4eff2d40e` | 336 | ?? docs/b2-governance/ec-gov-07b-b-d17-agent-integration-results.md |
| `ec-gov-07b-p` (C:\ANDES\Tesis\tesis-commerce-agent\.local\worktrees\ec-gov-07b-p) | `c145e97f8d34ed62fb6cef89d9896348c8125c31` | 343 | limpio |
| `tesis-commerce-agent-hnp` (C:\ANDES\Tesis\tesis-commerce-agent-hnp) | `a633a043337286406f9a3e859809556ce46bcba1` | 317 | limpio |
| `ec-gov-07b-q` (C:\ANDES\Tesis\tesis-commerce-agent\.local\worktrees\ec-gov-07b-q) | `d47b3da04b42b7c288162b8882b8ae59ef2b778a` | 349 | limpio |
| `ec-gov-07b-b-final` (C:\ANDES\Tesis\tesis-commerce-agent\.local\worktrees\ec-gov-07b-b-final) | `3ec19f58cfa57236336c2722addae53ebacd8b4b` | 357 | limpio |

Resultado de custodia: **3605/3605**
archivos idénticos. El alcance comprende archivos tracked de nueve árboles, informes históricos
07B y 1116 archivos del B0 `.local/gov05c` del Agent principal. No equivale a un inventario
global de todos los archivos ignorados, entornos virtuales o directorios PostgreSQL anteriores.
No hubo escrituras ordinarias en esos árboles; la creación autorizada del worktree actualizó
la metadata Git compartida. Evidencias: `.local/07c-validation/baseline.json` e
`integrity-final.json`. No se modificó Merchant, Lab, PSP, HNP ni la UI de Yair.

Inspección contractual: `approval.py`, `application/purchase.py`, contratos ACP,
`governance/guarded_creation.py`, `acp/guarded_creation.py`,
`persistence/guarded_d1_sqlite.py`, contratos/stores P y Q, provisioning y pruebas de
07A/P/Q/B. Se reutilizan verificadores, sin modificar ninguno de esos archivos.

## B. Frontera D1 / D17 / D2

D1 aplicada de B autoriza la creación inicial D17; Q sigue siendo shadow histórico,
sin promoción de sus permisos. D17 precede a este consentimiento. D2 conserva una evidencia
de términos y decisión posterior y no reescribe esa cronología.

`ApprovalService.present/authorize` exige `ready_for_payment`; su `ApprovalTerms.action`
es `complete_purchase`. `approve` puede persistir una aprobación consumible por completion
y emitir mandatos si tiene signer AP2. No se reutiliza para el estado D17
`not_ready_for_payment`, ni se cambia artificialmente ese estado. Tampoco se llama
`PurchaseService.approve`, `ApprovalService.authorize` o un signer AP2.

La acción futura de esta versión es únicamente **`request_checkout_preparation_review`**:
solicitar una revisión futura de preparación del checkout con los términos presentados.
No es aprobación de `prepare_checkout`, actualización o `complete_purchase`.
Se eligió este alcance porque la evidencia inicial carece de comprador, dirección detallada,
selección final de envío y preparación de pago. Esos faltantes impiden aprobar una acción
comercial concreta que los requiera. No se inventan esos datos ni se deja un alcance abierto.
Una aceptación no aprueba retroactivamente la creación ni obliga a conservar precios futuros.

`VerifiedD17Source` verifica de nuevo P, resolución de provisioning, binding Q, nueva D1
aplicada, vault/ledger/07A, envelope legacy y guard durable. Requiere operación claimed y
último evento durable **lookup/found**. Comprueba HMAC y encadenamiento de todos los eventos,
digest del guard y `verify_proof`, que incluye respuesta ACP original y su schema/digest.
Los resultados ausente, rejected, not_found, unrecoverable o error bloquean. Un found inicial
sin recuperación histórica tampoco basta. No hay recaptura ni decisión D1 nueva: el replay
del evaluador comprueba la decisión archivada. No hay HTTP ni POST comercial en D2.

El adaptador local instala un authorizer SQLite que permite lecturas, transacciones y
solo PRAGMA de metadatos requeridos, y deniega DML/DDL. El verificador histórico B toma un
`BEGIN IMMEDIATE` que revierte; no modifica filas ni bytes. Los stores P/Q/provisioning
conservan sus verificadores originales. Los tests comparan los bytes de las cuatro bases.

## C. Contrato de presentación comercial

`commerce-d2-hp-presentation/1` es extra-forbid, estricto y frozen, con ID UUID,
episodio/run/owner backend, timestamps UTC, límite de validez y `continuation_allowed=false`.
Su `CommercialTerms` no contiene diccionarios/listas mutables: incluye identidad y origen
Merchant, actor/episodio remotos, checkout real y estado not_ready_for_payment, oferta,
producto, variante, revisión, nombre de oferta y nombre exacto del ítem ACP, cantidad 1,
unitario/envío/total enteros en unidades minor COP, impuestos incluidos, condición,
destino conocido CO/Bogota/110111 y plazo delivery_days. No se confunde ese destino
comercial con una dirección de entrega completa. Las opciones ACP conocidas se conservan
como JSON canónico textual, sin seleccionarlas ni transformarlas en código HTML.

Se conservan ambas expiraciones (Offer y checkout), acción futura fija, descripción
comprensible, requisitos pendientes y referencias/digests de sello P, binding Q, decisión
y evidencia D1 aplicada, operación/envelope D17, prueba, historial y respuesta ACP original.
No se reconstruyen términos del feed o del modelo. Los dos nombres se distinguen para no
sustituir silenciosamente el nombre efectivamente recibido por ACP.

Fingerprint: SHA-256 de `commerce-d2-hp-terms/1` + NUL + JSON canónico de **todos** los
términos. Se valida otra vez al recuperar la presentación. El ID y los metadatos completos
se vinculan además al recibo mediante digest de presentación y HMAC de custodia.
La vigencia es el mínimo de cinco minutos desde emisión, expiración Offer, expiración ACP
y expiración de sesión humana. Los términos vencidos no permiten una presentación nueva.
Una presentación es un registro de emisión backend, no una prueba de renderizado/lectura
humana de pantalla. La API devuelve `delivery_assurance=backend_issued_not_ui_seen`.

## D. Identidad humana y consentimiento

`HumanAuthenticator` es un puerto backend confiable: verifica sesión de IdP, clasificación
humana, revocación, pertenencia al propietario y ACL del episodio. No recibe una identidad
inventada por el frontend o LLM. No se proporciona todavía un adaptador de IdP de producción.
El emisor suministrado es `SyntheticHumanIssuer`: token HMAC de pruebas, clave backend
independiente, principal, owner, run, autenticación, expiración e ID de autenticación.
Su assurance es exclusivamente **`synthetic_test_human/1`** y no puede emitir assurance de UI real.
Una eventual implementación de `authenticated_human_ui/1` deberá aportar esa verificación;
la mera existencia de ese valor en el protocolo no demuestra que esté implementada.

El servicio revalida estrictamente la identidad y su vigencia, incluso si un adaptador
devuelve un objeto construido sin validación. Exige el mismo principal, issuer, assurance,
owner y run que recibieron la presentación; renovar la sesión no permite cambiar de humano.
No ofrece un parámetro de identidad en el comando UI ni herramientas de modelo.

`ConsentCommand` exige presentación registrada, checkout exacto, fingerprint exacto,
acción fija, challenge, `decision=accept|reject` y `affirmative=true` para aceptar
(`false` para rechazar). No basta una recomendación de política/modelo ni la prueba D17.
Challenge aleatorio de 256 bits: solo se guarda SHA-256; el valor sale una vez por canal
backend privado y se mantiene redactado en representaciones ordinarias. No es un permiso
de comercio. Cada presentación admite un único resultado terminal, sea aceptación o rechazo.

## E. Persistencia, CAS y recuperación

SQLite D2 separado de P/Q/registry/B; constructor rechaza esas rutas y la inicialización
rechaza un esquema ajeno. Metadata vincula request original y ruta de la base aplicada.
Tablas `d2_meta`, `d2_presentations`, `d2_decisions`; PK de decisión por presentación,
ID de evento único, FK y `synchronous=FULL`. Se verifican esquema exacto y triggers
en cada operación. UPDATE, DELETE e INSERT OR REPLACE quedan bloqueados, también sin
depender de recursive_triggers.

HMAC-SHA256 separado por dominio/tipo protege metadata, snapshot canónico con recipient
y hash de challenge, y recibo terminal. La clave backend de al menos 32 bytes se inyecta:
el servicio no la genera ni la escribe en SQLite. Debe conservarse/provisionarse por el
backend entre reinicios; una clave diferente bloquea. La clave del emisor humano es distinta.
La operación presente se confirma antes de devolver propuesta/nonce; una falla al escribir
o confirmar no entrega éxito. La decisión usa `BEGIN IMMEDIATE`, lectura verificada del
estado y unicidad terminal: dos aceptaciones concurrentes producen un ganador y un bloqueo.
Un rechazo consume la presentación igual que una aceptación.

`recover` verifica fuente, snapshot, identidad, MAC, índices, digest, scope y tiempos del
evento, y devuelve propuesta + pendiente/recibo. No recupera el nonce en claro, no vuelve
a decidir y no ejecuta comercio. Un recibo vencido permanece histórico; no revive permisos.
Si avanza/cambia el historial D17, el fingerprint del historial deja de coincidir y se
bloquea esa presentación; una revisión posterior requiere una nueva propuesta.
Perder la respuesta inicial/nonce deja un registro pendiente sin capacidad de aceptar con
otro nonce. Se puede presentar otra propuesta, sin reintentar creación D17.

El sondeo independiente `restart_probe.py` pasó cinco procesos reales distintos:
[6328, 10700, 9728, 8272, 16608]. Etapas: emitir, recuperar pending, aceptar, recuperar el
mismo event_id, bloquear replay. Claves sintéticas conservadas solo en memoria del padre
y provisionadas por stdin privado a cada proceso; no quedaron en artefactos públicos.
Las cuatro bases origen permanecieron idénticas, HTTP D2=0 y efectos comerciales=0.
No se cuenta este sondeo como casos adicionales de pytest.

Límite de custodia: HMAC/triggers/SQLite no proporcionan antirollback externo frente a una
copia completa anterior del almacén, pérdida del almacén, control del host o compromiso de
claves. Un digest Merchant no es una firma del Merchant ni no repudio humano. La garantía
de un uso se aplica al almacén persistente configurado, no a clones/rollbacks hostiles.

## F. Pruebas y validación

Entorno: Python 3.12.6 y uv, entorno instalado del Agent reutilizado con
`UV_PROJECT_ENVIRONMENT=C:/ANDES/Tesis/tesis-commerce-agent/.venv` y
`uv run --frozen --offline --no-sync`. No se instalaron dependencias, no hubo pip ni cambios
de lockfile. No se afirma una instalación limpia con `uv sync`.

| Ejecución | Resultado original y evidencia |
| --- | --- |
| Focalizada inicial | 48 errores de setup por atributo de ruta P incorrecto; `focused-initial.txt`. |
| Primer arreglo | 34 failed, 14 passed; authorizer bloqueaba lectura de PRAGMA foreign_keys; `focused-repair1.txt`. |
| Segundo arreglo | 48 passed en 124.46s; `focused-repair2.txt`, `repair2-exit.json`. Anterior a las nuevas pruebas ASGI/custodia. |
| Focalizada ampliada en sandbox | Interrumpida en inicialización de socketpair de Windows; sin resumen final ni claim verde; `focused-final1.txt`. |
| General inicial en sandbox | Interrumpida; sin resumen final ni claim verde; `full-final.txt`. |
| Diagnóstico ASGI | `asgi-diagnostic.txt`: stack en socket._fallback_socketpair/accept, antes de ejecutar la API. Procesos propios cerrados en `aborted-owned-processes.json`. |
| ASGI repetido con socket interno disponible | 2 passed, 52 deselected en 11.32s; `asgi-host.txt`. No cambió código para esta repetición. |
| **Focalizada sobre entrega final** | **54 passed in 101.37s (0:01:41)**, exit 0; `delivery-exit.json`. |
| **General sobre entrega final** | **1035 passed, 51 skipped, 1 warning in 795.69s (0:13:15)**, exit 0; `delivery-full-exit.json`. Incluye regresiones 07A, P, Q y B. |
| Ruff format --check . | 198 archivos ya formateados; `format-final.txt`. |
| Ruff check . | All checks passed; `lint-final.txt`. |
| Pyright | 0 errors, 0 warnings, 0 informations; `types-delivery.txt`, con --venvpath del Agent. |
| Reinicio en procesos | PASS; `restart-results.json` y `restart-probe.txt`. |
| Diff/custodia | Seis archivos nuevos, tracked/index sin cambios; `diff-final.json`, `integrity-final.json`. Cinco fuentes/test coinciden con `code-frozen.json`. |

Se conservan también diagnósticos estáticos originales: atributo P incorrecto, tres
anotaciones de tipo de la fábrica API y un E501 inicial, corregidos; `types-initial.txt`,
`types-second.txt`, `types-third.txt`. Una invocación intermedia combinó opciones
incompatibles de Pyright (`types-final.txt`); la invocación de entrega concluyó sin errores
ni avisos. El aviso de entorno .venv en primeras invocaciones se resolvió usando --venvpath.
Los mensajes WARNING de aplicación de escenarios negativos se conservan aparte en
`delivery-full.log`; no deben confundirse con el contador de warnings de pytest.

El ejecutor limpia variables de proveedores/provisioning sin imprimir sus valores,
desactiva tracing y opt-ins, bloquea DNS/conexiones y escrituras/SQLite fuera del directorio
de validación. Solo permite el socketpair interno exacto de CPython y cuatro scripts
históricos constantes inspeccionados para pruebas de reinicio. Los hijos históricos no
heredan el audit hook; no se presenta ese mecanismo como sandbox del sistema operativo.
Las repeticiones host conservan estos interlocks y no autorizan red Merchant/PSP/LLM.
Cada corrida usa su propio scratch `.local/gov05c`; nunca el B0 histórico.

Omisiones y warnings de la ejecución general final, transcritos sin ocultar su alcance:

```text
============================== warnings summary ===============================
..\..\..\..\..\..\.venv\Lib\site-packages\starlette\testclient.py:53
  C:\ANDES\Tesis\tesis-commerce-agent\.venv\Lib\site-packages\starlette\testclient.py:53: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
SKIPPED [1] ..\..\..\tests\e2e\test_ap2_joint_purchase.py:32: requires the AP2 merchant, payment provider, and PostgreSQL
SKIPPED [1] ..\..\..\tests\e2e\test_conversational_purchase.py:74: requires the merchant, payment provider, and PostgreSQL
SKIPPED [1] ..\..\..\tests\e2e\test_joint_purchase.py:95: requires the merchant, payment provider, and PostgreSQL
SKIPPED [6] ..\..\..\tests\integration\test_guarded_d1_live.py:226: requires new isolated 07B-B real HTTP/PG experiment
SKIPPED [4] ..\..\..\tests\integration\test_initial_dispatch_live.py:268: requires RUN_GOV07A_REAL=1 and a new isolated PostgreSQL/Merchant cluster
SKIPPED [2] ..\..\..\tests\integration\test_initial_dispatch_live.py:358: requires RUN_GOV07A_REAL=1 and a new isolated PostgreSQL/Merchant cluster
SKIPPED [1] ..\..\..\tests\integration\test_live_creation_recovery.py:50: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [9] ..\..\..\tests\integration\test_live_creation_recovery.py:127: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [1] ..\..\..\tests\integration\test_live_creation_recovery.py:183: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [2] ..\..\..\tests\integration\test_live_creation_recovery.py:192: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [2] ..\..\..\tests\integration\test_live_creation_recovery.py:215: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [6] ..\..\..\tests\integration\test_live_creation_recovery.py:240: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [1] ..\..\..\tests\integration\test_live_creation_recovery.py:275: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [1] ..\..\..\tests\integration\test_live_creation_recovery.py:292: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [2] ..\..\..\tests\integration\test_live_creation_recovery.py:311: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [1] ..\..\..\tests\integration\test_live_creation_recovery.py:354: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [2] ..\..\..\tests\integration\test_live_creation_recovery.py:376: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [1] ..\..\..\tests\integration\test_live_creation_recovery.py:410: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [1] ..\..\..\tests\integration\test_live_creation_recovery.py:430: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [1] ..\..\..\tests\integration\test_live_creation_recovery.py:473: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [1] ..\..\..\tests\integration\test_live_creation_recovery.py:538: requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster
SKIPPED [1] ..\..\..\tests\integration\test_preselection_merchant.py:262: requires new isolated Merchant/PostgreSQL cluster
SKIPPED [1] ..\..\..\tests\integration\test_preselection_merchant.py:285: requires new isolated Merchant/PostgreSQL cluster
SKIPPED [1] ..\..\..\tests\integration\test_preselection_merchant.py:302: requires new isolated Merchant/PostgreSQL cluster
SKIPPED [1] ..\..\..\tests\integration\test_preselection_merchant.py:316: requires new isolated Merchant/PostgreSQL cluster
1035 passed, 51 skipped, 1 warning in 795.69s (0:13:15)
```

No se iniciaron Merchant, PostgreSQL, PSP ni servidores HTTP. El setup del módulo D2
produce una historia **sintética controlada** con MockTransport (una creación y una lectura
histórica, antes de D2); no son POST comerciales reales. Para cada test se clonan esas
bases y se bloquea todo envío HTTP de D2. Las dos pruebas de contrato usan ASGITransport
en memoria, no TCP/HTTP real ni React. Las alteraciones adversariales se restringen a
copias de SQLite bajo el scratch de cada prueba.

Integración con una captura Merchant real anterior: **no ejecutada**. El informe B publicado
explica que sus claves de fixture eran efímeras y no están en la evidencia pública; esta
entrega no dispone de una provision backend persistente de esa cadena P/Q/B para reabrirla
con garantía. Usar solo JSON público no cumpliría la verificación requerida. No se buscó
una credencial operativa ni se hizo un POST nuevo para suplir esa ausencia. Los casos
verifican los mismos contratos y funciones de custodia, pero no son autenticación remota
Merchant ni una nueva validación HTTP/DB end-to-end de producción.

## G. Casos adversariales

| Grupo | Evidencia / resultado |
| --- | --- |
| Cadena D17 | Found sintético verificado con cadena real P/Q/D1/07A SQLite; absent y ausencia de lookup bloquean. |
| No-found | Observaciones rejected/not_found/unrecoverable/error válidas y re-selladas en copias se bloquean; no se convierten en propuesta. |
| Scope remoto | Actor y episodio ajenos, checkout diferente, revisión y digest alterados bloquean aunque se vuelva a sellar el evento de prueba. |
| Importes | Precio unitario, envío y total alterados bloquean en prueba D17 y al sustituir snapshot de términos. |
| Términos | Oferta, revisión, digest, destino, checkout, expiración y acción futura distintos no coinciden con la presentación registrada. |
| Identidad | Principal/owner/run incorrectos, token falsificado/vencido y kind LLM en salida de adaptador se rechazan; body extra de identidad da 422. |
| Presentación/nonce | ID nunca registrado, fingerprint falso, nonce incorrecto, cruce entre presentaciones y replay bloquean. |
| Temporal | Checkout vencido no se presenta; aceptación al límite exacto queda vencida; una sesión humana vencida no puede operar. |
| Terminal/CAS | Un accept y un bloqueo concurrentes; ningún segundo accept/reject después de accept o reject. |
| Persistencia | SQLite bloqueado durante presentación/decisión no devuelve éxito; estado queda pendiente. |
| Alteración | UPDATE/DELETE/REPLACE, trigger ausente, MAC adulterado incluso tras restaurar esquema, clave distinta y base ajena bloquean. |
| Reinicio | Nuevas instancias antes/después, más cinco procesos reales con misma clave persistente de prueba; recibo estable y nonce consumido. |
| Efectos | HTTP de D2 prohibido; hashes P/Q/B/registry sin cambios; purchases conserva selected/version2 sin checkout_id/order_id; sin authorize/complete en servicio D2. |
| API/UI | ASGI verifica pending/accept/reject, errores sanitizados y /complete y /checkout_sessions ausentes. Assurance sintética y backend-issued explícitas. |

No se agregaron permisos comerciales, mandatos AP2, Payment Mandate, órdenes, pagos,
actualización, cancelación, completion ni remisión de creación. El recibo contiene
`continuation_allowed=false`, y la fuente sigue bloqueando cualquier continuación D17.
El runtime original y la lógica de bloqueo de PurchaseService permanecen idénticos.

## H. Límites y contrato futuro con la UI

`create_consent_app(store)` crea una aplicación experimental separada, no montada en
Settings, grafo, herramientas o API operativa del Agent. Una instancia tiene un episodio
backend preconfigurado; el frontend no puede elegir otro store/request, merchant o identity.
No hay CORS, cookies de autenticación ni rutas de comercio en esta fábrica.
Esquemas JSON exportados sin datos de sesión: `.local/07c-validation/contracts/`
(`Presentation.schema.json`, `ConsentCommand.schema.json`, `ConsentReceipt.schema.json`).

1. `POST /experimental/d2/presentations`, body `{}`, cabecera de sesión humana Bearer
   verificada por el adaptador backend: 201 con `presentation`, challenge de un uso y
   delivery_assurance. `Cache-Control: no-store`. La identidad y términos vienen del backend.
2. La futura UI debe mostrar exactamente nombre(s), cantidad, unitario, envío, total y COP,
   destino conocido y su límite, condición/plazo/opciones conocidas, expiraciones, acción
   descrita y requisitos pendientes. Debe renderizar texto como texto, sin interpretar
   contenido Merchant como HTML, ni marcar aceptación por defecto. Debe conservar el
   fingerprint recibido; no calcular nuevos términos o deducir direcciones/métodos de pago.
3. `POST /experimental/d2/decisions`: presentation_id, checkout_id, terms_fingerprint,
   future_action, challenge, decision y affirmative. Ningún campo principal/owner permitido.
   201 devuelve receipt; no devuelve un token de ejecución. Accept exige true, reject false.
4. `GET /experimental/d2/presentations/{presentation_id}`: proposal y state
   pending/accept/reject, receipt o null y continuation_allowed=false. Un pending cuyo
   valid_until pasó se muestra vencido; no puede aceptarse. Una respuesta perdida de decisión
   se recupera por GET; no se reenvía accept para inferir éxito. No se redistribuye el nonce.
5. Error de identidad/autenticación: 401; conflicto/vencimiento/no-found/custodia: 409;
   body inválido: 422 sin eco del payload; persistencia indisponible: 503. Todos bloquean
   continuación y llevan no-store. Un error no autoriza inventar términos ni reenviar D17.

Queda pendiente integrar un IdP humano y ACL de episodios reales, un canal UI autenticado
y confidencial, interacción afirmativa observable y su evidencia de entrega/renderizado.
El emisor de pruebas no acredita una persona real. No se modificó React ni trabajo de Yair.
El adaptador reutiliza métodos internos de la base exacta P/Q/B; no se afirma compatibilidad
genérica con otras revisiones. Un cambio de esos verificadores exige revisión y regresiones.
Los hashes son custodia local, no firma remota, prueba criptográfica de voluntad humana,
frescura comercial actual ni garantía de precio futuro. Un incremento posterior debe volver
a comprobar identidad/autorización, estado comercial, términos, vigencia y datos indispensables
antes de introducir cualquier capacidad de ejecución. Esta entrega no habilita ese incremento.

## I. Dictamen, archivos y estado Git

**READY_FOR_REVIEW** del experimento backend de registro de términos y consentimiento
sintético. **Pendiente** la integración con humanos autenticados en UI y validación de
producción. No se declara D2-HP completamente integrado con humanos reales.

Archivos nuevos de implementación y pruebas; ninguno de los archivos existentes fue modificado:

| Archivo | SHA-256 de bytes |
| --- | --- |
| `src/commerce_agent/d2_consent_api.py` | `9ae4f19619bf202894a5b70fdb114d2840abd2d635456db538391832dfaa2eb5` |
| `src/commerce_agent/governance/human_consent.py` | `63f40afea052103205d31d64391e870b743c9c611e0e769394708a887845902a` |
| `src/commerce_agent/persistence/d2_d17_source.py` | `a822d6903f618a8139003e4c76c56a2610b6c16be33f9279f8e10269502cf591` |
| `src/commerce_agent/persistence/human_consent_sqlite.py` | `aaebecff33f86f21f99c2f742431477d22d2915efc838e91a529a0f9540f040f` |
| `tests/governance/test_human_consent.py` | `0d27cc1bc89a6d8ae9d4662c3782618418702ffc7ee63279c09771f423793f8d` |

Sexto archivo nuevo: este informe `docs/b2-governance/ec-gov-07c-d2-hp-consent-results.md`.
Su SHA-256 se entrega externamente en `.local/07c-validation/delivery-sha256.json` para evitar
una autorreferencia imposible. El manifiesto cubre los seis archivos. Los scripts, logs,
copias SQLite y manifiestos de prueba permanecen ignorados bajo `.local/07c-validation`;
su inventario de evidencia está en `evidence-sha256.json`. No contienen claves backend
efímeras de este incremento ni tokens/challenges en claro en informes públicos.

HEAD/base y rama coinciden con A, diff tracked y staged vacío, seis archivos untracked
exactos, sin stage/commit/push/merge/PR. La custodia protege 3605 archivos anteriores;
el informe no altera evidencia histórica ni promueve permisos de fases futuras.
