# EC-GOV-06C-B — Núcleo transaccional de reconciliación segura

## A. Estado Git inicial y final

Base comprobada: `c1202a5a0a2e62137beca8c75c0369f5765b49db`.
La rama inicial `governance/ec-gov-06c-a-envelope-binding` estaba limpia.
Se creó desde esa base `governance/ec-gov-06c-b-reconciliation`.
HEAD permanece en la base; no hay staging, commit, push, merge ni PR.
Origin permanece en `https://github.com/Maldinho18/tesis-commerce-agent.git`;
el remoto `org` permanece en la organización Tesis-Agentes-Comerciales.

La corrección posterior a la auditoría parte de esa misma rama y HEAD, con los
cuatro archivos conocidos del incremento sin seguimiento y el índice vacío.
No se recreó la rama ni se incorporaron cambios ajenos.

## B. Contratos inspeccionados

Se revisaron `governance/enforce.py`, `governance/dispatch_envelope.py`,
`persistence/d1_dispatch_sqlite.py`, `persistence/d1_envelope_sqlite.py`,
`persistence/d1_uow_sqlite.py`, `application/purchase.py` y las pruebas de
06A, 06B y 06C-A. Merchant se consultó exclusivamente mediante `git show`
del objeto `71f9dba4de4fec96ceee0385c53f797ef9c99cb4`, sin importar su paquete,
modificar su checkout ni acceder a sus bases.

La recuperación remota de ese Merchant depende del contexto autenticado,
clave y fingerprint, y conserva la respuesta original. Esto no prueba que
el despliegue futuro tenga ese contrato ni autentica las observaciones
recibidas por este núcleo local. No se ejecutó integración Agent–Merchant.

## C. Diseño y esquema

`SqliteD1Reconciler` recibe una conexión SQLite, claves de procedencia del
backend y un reloj explícito. No tiene transporte, ruta de base por defecto,
worker ni acceso desde runtime/API. `inspect` recupera hechos locales;
`reconcile` agrega una observación; `history` consulta el historial por ámbito.

Dos tablas aditivas evitan duplicar el ledger D1:

| Tabla | Función |
| --- | --- |
| `d1_reconciliation_events` | Evento canónico, digest con dominio, solicitud, operación, versión, resultado y referencias a evidencia/envelope originales. Unicidad por operación/solicitud y operación/versión. |
| `d1_reconciliation_projection` | Último evento y versión CAS de cada operación. No representa un nuevo estado comercial. |

Los triggers impiden UPDATE/DELETE y REPLACE de eventos, incluyendo conflictos
en claves únicas secundarias. La proyección solo avanza una versión y debe
referir al evento de la misma operación, versión y resultado. La inicialización
verifica las definiciones SQL esperadas y rechaza definiciones debilitadas.

Inicialización y escritura exigen transacción explícita del llamador y claves
foráneas activas. Un savepoint agrupa evento y proyección; un error revierte
ambos aunque el llamador capture la excepción y confirme su transacción.
Se adquiere el bloqueo de escritura antes de leer la versión. El commit sigue
siendo responsabilidad del llamador: un evento devuelto antes de commit es provisional.

La misma solicitud exacta devuelve el evento ya persistido, incluso con un reloj
posterior. Reutilizar su identificador con contenido diferente falla. Solicitudes
distintas que compiten por la misma versión no generan dos avances.

### Corrección de integridad solicitada por auditoría

Defecto confirmado: `_version()` verificaba el evento final y count/max(version),
sin comprobar los bytes/digests de eventos anteriores. Con v1 corrupto y v2
intacto, `inspect()` podía aceptar la proyección y una nueva reconciliación podía
avanzarla. La prueba de regresión se ejecutó antes de corregirlo: falló con
`DID NOT RAISE EvidenceError` (1 failed in 6.50s), reproduciendo la aceptación
incorrecta durante `inspect()`.

La corrección incorpora `_validated_history()` dentro del mismo store. Lee los
identificadores en orden de versión y reutiliza `_event()` para validar cada
evento: modelo estricto, bytes canónicos, digest y correspondencia con columnas
SQL. Además exige operación coincidente y versiones exactamente 1..N; finalmente
comprueba versión, último identificador y resultado de la proyección.
`_version()` obtiene N exclusivamente de ese historial validado. Tanto inspección
como avance y reejecución idempotente verifican todo el historial existente.
`history()` devuelve directamente los eventos ya validados, evitando un segundo
recorrido completo. El coste es O(N) en el historial de la operación; no se añade
un caché de confianza que pueda ocultar corrupción posterior.

No se modifica SQL, triggers, transacciones, savepoints, CAS, claves de idempotencia
ni resultados comerciales. Un error de integridad sucede antes de insertar un
evento o actualizar la proyección; se mantienen los mecanismos de rollback.

## D. Archivos añadidos

- `src/commerce_agent/governance/reconciliation.py`: contratos estrictos y
  versionados, observaciones explícitamente no verificadas, razones cerradas,
  separación de resultados históricos/actuales y requisitos de replay.
- `src/commerce_agent/persistence/d1_reconciliation_sqlite.py`: recuperación
  acotada por propietario/episodio, verificación de referencias, historial y CAS.
- `tests/governance/test_reconciliation.py`: 48 casos parametrizados offline.
- Este informe.

No se modifica ningún archivo preexistente, dependencia, lockfile, fixture
histórico, política, migración ni integración comercial.

En esta corrección se modifican exclusivamente el store, sus pruebas y este
informe. `governance/reconciliation.py` conserva sus bytes del incremento previo.

## E. Invariantes de seguridad

La recuperación comprueba ámbito local, procedencia del propietario, referencias
al registro de autorización y consistencia del envelope original. Un envelope
ausente o corrupto queda identificado y nunca se reconstruye con defaults.
Una autorización válida se etiqueta exclusivamente `historical_allow_only`.

Se distinguen `reserved`, `claimed_without_observation`, `uncertain`, `confirmed`
y el resultado de fallo definitivo ya existente. Claim no demuestra envío HTTP;
reserva no autoriza un POST durante recuperación. Los resultados originales y
los claims permanecen intactos.

`historical_confirmation_only` describe únicamente una confirmación que ya
existía localmente, con referencias verificables. No confirma una operación
incierta a partir de una respuesta arbitraria. Todos los eventos llevan
`replay.eligible=false` y `continuation_allowed=false`, con tipos literales que
rechazan intentos de conceder permisos. El backend debe conservar esa frontera.

Las observaciones externas contienen resúmenes/digests/referencias tipados;
su procedencia solo puede ser `offline_fixture` o `unverified_external`.
No se admite un campo `trusted` o `authenticated`. El binding esperado sigue
siendo `backend_expected_unverified`; coincidencia de campos no es autenticación.
Los eventos no guardan credenciales ni el valor original de la clave idempotente.

Se separan creación histórica, estado actual observado y permiso de continuar.
Un checkout cancelado, expirado, completado o con snapshot diferente no adquiere
autoridad por coincidir con una creación histórica. Incluso un estado observado
`ready_for_payment` no habilita pagos, update ni completion.

## F. Validación ejecutada

Entorno real: Windows 11 `10.0.26220`, Python 3.12.6, SQLite 3.45.3,
uv 0.12.15. No se declara validación Linux.

### Referencia anterior a la corrección de auditoría

| Control | Resultado |
| --- | --- |
| Primera validación de pruebas nuevas, previa al caso adicional de proyección ausente | 41 passed in 63.31s con runner protegido; 41 passed in 88.58s con el comando oficial. |
| Comando oficial final `uv run pytest tests/governance/test_reconciliation.py -q --tb=short -p no:cacheprovider --basetemp=<nuevo>` | 42 passed in 84.47s; sin omisiones ni advertencias. |
| Regresiones `test_d1_uow.py`, `test_enforce_integration.py`, `test_dispatch_envelope.py` | 121 passed in 219.62s; sin omisiones ni advertencias. |
| `uv run ruff format --check .` | 153 files already formatted. |
| `uv run ruff check .` | All checks passed! |
| `uv run pyright` | 0 errors, 0 warnings, 0 informations. |
| Primera suite general, previa al caso adicional | 694 passed, 3 skipped, 1 warning in 341.52s. |
| Suite general protegida final | 695 passed, 3 skipped, 1 warning in 301.17s. |
| `git diff --check` | Sin errores. |
| Revisión adicional de los cuatro archivos nuevos con `git diff --no-index --check -- NUL <ruta>` | Sin errores de whitespace; aviso de conversión futura LF a CRLF por la configuración Git existente. No se cambió esa configuración. |
| Integridad física de archivos preexistentes | 313/313 SHA-256 iguales al inicio. |

La suite final conserva las tres omisiones E2E conocidas:
`test_ap2_joint_purchase.py:32`, `test_conversational_purchase.py:74` y
`test_joint_purchase.py:95`, que requieren Merchant, PSP y PostgreSQL.
La advertencia procede de `starlette/testclient.py:53`:
`The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.`
No se corrigen dependencias ni se reducen verificaciones para ocultarla.
La referencia de 06C-A era 653 aprobadas y 3 E2E omitidas: se conservan las
653 pruebas anteriores y se añaden 42 aprobadas; no hay nuevas omisiones.

Las pruebas generales/regresiones se ejecutan mediante `pytest.main` con
`-q --tb=short -p no:cacheprovider --basetemp=<directorio temporal nuevo>`.
Un runner externo a los archivos versionados elimina variables de proveedores,
desactiva dotenv/tracing/E2E y bloquea SQLite fuera del directorio temporal,
escrituras al repositorio y conexiones a servicios externos/operativos.
Solo permite sockets efímeros creados por el mismo proceso para el soporte
interno de asyncio en Windows. Dos tests históricos de shadow que exportan
`.local/gov05c` usan cwd temporal, sin alterar sus fuentes ni artefactos originales.
Los comandos uv emplean `UV_NO_SYNC=true`, `UV_OFFLINE=true` y caché temporal.
No se sincronizan dependencias ni se ejecuta B0.

### Ejecución de la corrección de auditoría

| Control posterior a la corrección | Resultado |
| --- | --- |
| Reproducción previa al fix, caso de v1 corrupto | 1 failed in 6.50s: `inspect()` no lanzó EvidenceError. Fallo esperado que confirma el defecto. |
| `uv run pytest tests/governance/test_reconciliation.py -q --tb=short -p no:cacheprovider --basetemp=<nuevo>` | 48 passed in 92.47s; sin omisiones ni advertencias. |
| Regresiones 06A/06B/06C-A protegidas | 121 passed in 190.73s; sin omisiones ni advertencias. |
| `uv run ruff format --check .` | 153 files already formatted. |
| `uv run ruff check .` | All checks passed! |
| `uv run pyright` | 0 errors, 0 warnings, 0 informations. |
| Suite general protegida | 701 passed, 3 skipped, 1 warning in 333.55s. |
| `git diff --check` | Sin errores. |
| Diff check adicional de los tres archivos autorizados sin seguimiento | Sin errores de whitespace; avisos existentes de conversión futura LF a CRLF. |
| Comparación SHA-256 de los 317 archivos presentes al iniciar esta corrección | Solo cambian los tres archivos autorizados; no se añade ni se elimina ningún archivo. |

Se reutiliza el mismo aislamiento descrito arriba. La suite general y las
regresiones usan el runner protegido; las pruebas específicas usan el comando
oficial uv sobre bases SQLite nuevas y descartables. No se relajan omisiones ni
se actualizan dependencias para obtener un resultado verde.
Frente a la referencia de 695 aprobadas y 3 omitidas, se conservan las pruebas
anteriores y se añaden seis aprobadas. No hay nuevas omisiones. Las tres E2E y
la advertencia Starlette/AnyIO son exactamente las detalladas en la referencia
anterior. Estas cifras corresponden a ejecuciones reales posteriores al fix.

## G. Matriz de cobertura, crashes y concurrencia

| Requisito | Evidencia de pruebas nuevas |
| --- | --- |
| 1. Reinicio | Cierre/reapertura y proceso Python nuevo real: mismo digest de evento durable. |
| 2–5. Estados locales | Cuatro casos reserved/claimed/uncertain/confirmed; ningún permiso ni cambio comercial. |
| 6. Envelope | Tabla ausente, fila ausente y bytes corruptos; bloqueo sin reconstrucción. |
| 7. Ámbito | Propietario, run y operación incorrectos rechazados antes de leer/devolver historial ajeno. |
| 8–9. Confianza/replay | Binding esperado y fixture coincidente siguen sin autenticar; ALLOW histórico insuficiente. |
| 10. Conflictos | Origin, run remoto, actor, digest de clave, fingerprint y checkout incompatible. |
| 11. Historia/actualidad | Cuatro estados actuales distintos de la creación original; continuación prohibida. |
| 12. Concurrencia | Dos hilos, conexiones SQLite distintas y barrera: solicitud idéntica comparte un evento; solicitudes distintas con CAS inicial producen un solo avance. |
| 13. Crash | Rollback antes de commit; commit real seguido de excepción sintética y reapertura/reintento sin duplicación. |
| 14. Atomicidad | Fallos inyectados en INSERT y UPDATE de proyección revierten también el evento. |
| 15. Inmutabilidad | Siete intentos SQL de modificación/borrado/REPLACE y versión inválida rechazados. |
| 16. Reejecución | Mismo evento inmutable; contenido diferente con mismo request_id falla. |
| 17. Sin segundo POST | Contadores del gateway sintético no aumentan; una operación incierta conserva su único create original. |
| 18. Regresiones | Suites 06A/06B/06C-A y suite general conservadas. |

También se prueban historial corrupto o proyección ausente con eventos existentes,
requisitos de replay no eliminables,
rechazo de booleanos de confianza y definiciones SQL debilitadas. Las pruebas
de crash son inyección determinista de fallos transaccionales; no son cortes
de energía ni terminación abrupta del sistema operativo. El reinicio en otro
proceso sí se ejecutó realmente. Ningún doble pretende autenticar Merchant.

La corrección añade seis casos (sin retirar los 42 anteriores):

| Caso adicional | Comprobación |
| --- | --- |
| v1 con body corrupto, v2/proyección intactos | `inspect`, `history`, nuevo reconcile v3 y reejecución de v2 fallan cerrado. |
| v1 con digest corrupto | Mismas cuatro fronteras; journal y proyección quedan exactamente iguales tras el rechazo. |
| v1 con bytes JSON no canónicos | No se acepta equivalencia semántica como integridad de bytes. |
| v1 con operación distinta y digest recalculado | Se rechaza la discrepancia entre contenido y columnas; no basta un hash coherente. |
| Historial válido v1/v2/v3 | Consultable, ampliable e idempotente en cada avance. |
| Falta de versión intermedia | v1/v3 y última proyección intacta no satisfacen continuidad; no puede añadirse v4. |

En los cuatro casos de corrupción se retira solo el trigger de UPDATE de la base
descartable; el último evento se verifica por separado como intacto. Se comparan
los bytes/filas completos de eventos y proyección antes y después de los intentos,
además de las tablas comerciales protegidas. La prueba de discontinuidad retira
solo el trigger de DELETE de otra base descartable. No se alteran definiciones
SQL de producción. Las pruebas previas de concurrencia, rollback y reejecución se
conservan sin cambios.

## H. Contrato pendiente y limitaciones

Un futuro adaptador verificable deberá demostrar conjuntamente:

1. Emisor/servicio remoto autenticado y versión del contrato realmente desplegado.
2. Identidad remota de episodio y actor, vinculada al ámbito autorizado original;
   no basta Request-Id, UUID declarado, credencial renovada sin binding ni HTTP 200.
3. Correspondencia de clave idempotente y fingerprint ACP con el envelope durable.
4. Pertenencia del checkout y su respuesta original a esa creación autenticada,
   manteniendo separado cualquier snapshot comercial actual y su procedencia.
5. Garantía verificable de conservación del registro idempotente durante todo el
   intervalo de incertidumbre y hasta la eventual recuperación.

Este incremento no implementa esa prueba ni un puerto que permita autodeclararla.
Para un POST que todavía podría ser el primero, además faltaría reautorización
vigente de backend conforme a un contrato posterior. No se inventa TTL, D17,
renovación D2 ni liberación de claim. Los tres requisitos permanecen explícitos
y no satisfechos en todo `ReplayAssessment`.

Los hashes y triggers detectan corrupción y escrituras ordinarias no permitidas;
no garantizan no repudio frente a un administrador capaz de alterar esquema y
reescribir todos los hashes. La autenticación del llamador backend queda fuera
de este objeto local: no hay endpoint que acepte identidad de herramientas o LLM.

Ningún GET/POST real, adaptador de producción, reconciliación remota, nuevo pago,
consentimiento, worker, activación enforce, HNP o continuación se implementa.
No se tocaron Merchant, PSP, Lab, UI/SSE, AP2, Notion ni archivos de Yair.

## I. Dictamen

**READY_FOR_REVIEW** para el núcleo local de EC-GOV-06C-B.
La corrección de auditoría valida todos los eventos antes de aceptar o avanzar
la proyección y está cubierta por las 48 pruebas específicas y la suite de 701
aprobadas. El defecto se considera corregido para una nueva revisión independiente;
este dictamen no sustituye esa revisión ni autoriza un commit.
La autenticación remota, replay y continuación comercial siguen bloqueados.
Se detiene para revisión independiente sin commit ni push.

Estado final:

```text
## governance/ec-gov-06c-b-reconciliation
?? docs/b2-governance/ec-gov-06c-b-reconciliation-results.md
?? src/commerce_agent/governance/reconciliation.py
?? src/commerce_agent/persistence/d1_reconciliation_sqlite.py
?? tests/governance/test_reconciliation.py
```

El diff de archivos versionados y el índice están vacíos. Los cuatro archivos
son nuevos, sin añadir al índice. Se recompararon los 313 archivos originalmente
versionados por SHA-256: ninguno cambió. La suite protegida bloqueó escrituras
al repositorio y bases ajenas a temporales; B0 y sus artefactos históricos no se
reejecutaron ni se sobrescribieron.

Para esta corrección se compararon también los 317 archivos existentes al inicio
(los 313 versionados y los cuatro archivos conocidos del incremento): solo cambian
el store, sus pruebas y este informe. El contrato de gobernanza y todos los archivos
del commit base conservan sus bytes. No se cambió el esquema SQL ni otro módulo.
