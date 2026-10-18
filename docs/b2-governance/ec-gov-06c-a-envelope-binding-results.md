# EC-GOV-06C-A — Envelope durable y binding esperado

## A. Preparación Git

Árbol inicialmente limpio en `governance/ec-gov-06b-d1-enforce-integration`, HEAD
`5df389fe9ed6fc1fedcd2e9af4c61079abdf0e44`. Origin verificado como
`https://github.com/Maldinho18/tesis-commerce-agent.git`; remoto `org` conservado.
Se creó únicamente la rama local `governance/ec-gov-06c-a-envelope-binding` desde
esa base. HEAD sin cambios; sin stage, commit, push, merge o PR.
Estado final: dos archivos versionados modificados y cinco nuevos. De los 308
archivos inicialmente versionados, solo cambiaron cliente ACP y gate enforce;
los otros 306 conservan sus SHA-256 iniciales, incluidos informes R1, contratos,
fixtures, dependencias y resultados históricos. Working tree pendiente de revisión.

## B. Contratos inspeccionados

06B: `enforce.py`, `purchase.py`, `d1_dispatch_sqlite.py`, `d1_uow_sqlite.py`,
`governance_sqlite.py`, `provenance_sqlite.py`, modelos/cliente ACP y pruebas 06B.
Se revisaron los resultados 06B/R1 y el contrato D1 vigente.
Merchant se inspeccionó mediante `git show` del objeto exacto
`71f9dba4de4fec96ceee0385c53f797ef9c99cb4`, sin importar su paquete, modificarlo,
consultar su base ni ejecutar HTTP contra él. No se acredita un despliegue remoto.

## C. Diseño y alternativas

Se añade una tabla privada `d1_checkout_envelopes`, con PK/FK `operation_id`,
bytes canónicos y digest del envelope. Reutiliza la reserva, clave, decisión,
evidencia y contexto existentes. No genera otra clave ni otra identidad de operación.
No modifica las tablas 06A/06B ni sus triggers; no duplica el ledger.

Se descartó ampliar destructivamente los DDL históricos, que se verifican de forma
estricta. Se descartó reconstruir la solicitud al recuperarla: las omisiones y los
campos explícitos pueden tener fingerprints distintos para Merchant.

`CheckoutCreationEnvelope` es estricto, inmutable y versionado. Conserva operación,
run/owner locales, origen y referencia Merchant, versión/perfil ACP, método/ruta,
cuerpo completo canónico, clave original, referencias/digests D1, contexto y tiempo
de preparación ligado al registro autorizante. La clave se oculta en repr/dumps
ordinarios; `storage_bytes()` es una serialización privada deliberada. No exportar
ese contenido al modelo, logs o evidencia pública.

Hay dos digests independientes del digest de input D1:

- `body_digest`: SHA-256 con dominio `merchant-acp-create/v1`, versión, perfil
  `merchant-p0-create/1` y cuerpo canónico, equivalente al fingerprint del commit
  Merchant, con prefijo local `sha256:`.
- Digest del envelope: dominio `commerce-acp-create-envelope/v1`, sobre todos sus
  bytes canónicos privados, incluida la asociación de clave e identidades.

Se extrae `build_checkout_create` del cliente. Cliente y gate usan ese mismo
constructor puro, conservando defaults y normalización USD/COP del camino previo.
El perfil durable se limita al Merchant actual: COP, una variante/unidad y handlers
vacíos. Valida también el esquema ACP congelado sin modificarlo. Conserva omisiones
del cuerpo almacenado y orden de arrays; el orden de claves se canonicaliza.
Estos son bytes canónicos JSON, no una captura de los bytes TCP originales.

El binding tiene únicamente dos estados: `unavailable` y
`backend_expected_unverified`. La inyección backend opcional conserva run/actor
esperados y `source_ref`, con assurance fija `backend_expected_not_authenticated`.
Esa referencia es procedencia declarada de la expectativa, no prueba de token,
sesión remota, destino real o despliegue. No existe variante autenticada/verificada
ni una fuente conectada a un emisor real en este incremento.

## D. Archivos

Modificados:

- `src/commerce_agent/acp/client.py`: extraer el constructor sin cambiar el cuerpo
  observable del cliente ordinario.
- `src/commerce_agent/governance/enforce.py`: preparar/persistir/verificar envelope
  y derivar los argumentos experimentales del original comprometido.

Nuevos:

- `src/commerce_agent/acp/creation.py`.
- `src/commerce_agent/governance/dispatch_envelope.py`.
- `src/commerce_agent/persistence/d1_envelope_sqlite.py`.
- `tests/governance/test_dispatch_envelope.py`.
- Este informe.

## E. Atomicidad e invariantes

La evaluación y preparación del envelope ocurren fuera de la transacción
autorizante. Dentro de un mismo BEGIN IMMEDIATE se ejecutan UoW/CAS, clave, reserva,
ledger e INSERT del envelope. Un error al insertar el envelope revierte también
transición, clave y reserva. Los diagnósticos bloqueados posteriores siguen siendo
otra transacción, según 06B; no equivalen a una autorización persistida.

Antes del claim, una conexión independiente compara evidencia, reserva y envelope
confirmados. Antes del puerto experimental se vuelve a recuperar el envelope,
verificar su identidad/clave/contexto y contrastar el cuerpo con el constructor
compartido. Todos los SQL de lectura terminan antes del I/O. La recuperación utiliza
un savepoint de lectura porque el vault exige una transacción para resolver sus
fuentes; se libera antes de retornar al transporte.

La lectura valida digest, representación canónica, propietario, reserva, input D1,
evidencia y origen Merchant recuperado del contexto operativo autenticado local.
Esto verifica el contexto local; no autentica la identidad remota esperada.

UPDATE, DELETE y REPLACE se rechazan mediante triggers. Repetir una preparación
idéntica es idempotente; otro contenido para la misma operación se rechaza. El
esquema se verifica y su inicialización es aditiva y reversible por el llamador.

No hay backfill automático para reservas antiguas sin envelope. El camino de
reutilización exige que ya exista el original. El almacén rechaza crear por primera
vez un envelope para una operación ya claimed.

El puerto experimental conserva su interfaz anterior. Solo puede representar el
cuerpo que construye el cliente actual; una omisión distinta en un envelope se
conserva, pero ese puerto no puede despacharla mediante defaults nuevos: la comparación
de cuerpo bloquea la divergencia. Un transporte productivo que consuma el original
queda fuera del alcance. No se añade API de replay.

## F. Validación

Windows, Python 3.12.6 y SQLite temporal; no se declara validación Linux.
Sin instalación de dependencias ni modificación de `uv.lock`.

| Control | Resultado |
| --- | --- |
| Nuevos contratos/envelopes + 06B | 77 passed in 110.79s; ninguna omitida |
| Comando oficial `uv run pytest tests/governance/test_dispatch_envelope.py -q` con caché desactivada y basetemp nuevo | 35 passed in 48.79s |
| `uv run ruff format --check .` | 148 files already formatted |
| `uv run ruff check .` | All checks passed! |
| `uv run pyright` | 0 errors, 0 warnings, 0 informations |
| Suite general protegida | 653 passed, 3 skipped, 1 warning in 212.16s |
| `git diff --check` | Sin errores; aviso existente LF→CRLF en enforce.py |

Las 35 pruebas nuevas cubren: persistencia antes del puerto; cierre/reapertura de
SQLite con los mismos bytes; conformidad del cuerpo y fingerprint independiente;
presencia/omisión; variante/moneda/capacidades; identidad/clave/Merchant;
binding esperado sin autenticación; campos secretos rechazados; redacción de clave;
UPDATE/DELETE/REPLACE; corrupción de bytes; rollback de INSERT incompleto;
preparaciones concurrentes idempotentes y carrera de dos preparaciones iniciales;
aislamiento de lectura por run/owner e inicialización reversible del esquema.
Las pruebas 06B conservan off/shadow, CAS, claims y fallos de despacho.
Las tres omisiones son los E2E joint, conversational y AP2 joint, que requieren
Merchant, PSP y PostgreSQL. Ningún test nuevo se omitió. La advertencia es la
deprecación de anyio BlockingPortal usada por Starlette TestClient.

Corridas intermedias: 21 fallidas/21 aprobadas en 66.36 s al intentar recuperar el
vault sin una transacción de lectura; se corrigió con el savepoint descrito y luego
pasaron 42/42 de 06B en 65.89 s. La primera matriz nueva pasó 32 casos en 48.07 s;
después se añadieron los tres casos finales. Se corrigió un orden de imports.

La suite general usa un runner en memoria que desactiva dotenv, variables de
proveedores/trazado y E2E; permite solo SQLite bajo un basetemp nuevo, bloquea
escrituras al repositorio y sockets externos/operativos. Dos tests históricos de
shadow que exportan `.local/gov05c` se ejecutan con cwd temporal mediante una fixture
del runner, sin editar sus fuentes ni sobrescribir esos artefactos históricos.
El runner llama a `pytest.main`, con configuración oficial, `-p no:cacheprovider`
y basetemp nuevo. Los comandos uv usan `UV_NO_SYNC=true` y `UV_OFFLINE=true`.

## G–H. Límites y siguiente incremento

Solo se ejecutaron transportes simulados y SQLite temporal. La comparación con
Merchant es de código/contrato y algoritmo de fingerprint; no es una prueba real
Agent–Merchant, de credenciales renovadas ni de pertenencia de un checkout remoto.
Reapertura SQLite no significa ensayo de caída del SO o proceso remoto.

06C-B sigue necesitando binding autenticado del ámbito real y verificación del
contrato desplegado, transporte del original persistido, protocolo explícito para
los POST que todavía podrían crear, eventos/CAS de reconciliación y separación entre
creación histórica y estado comercial vigente. No se modifica la tabla de resultados
06B ni se habilita convertir uncertain en confirmed.

No hay worker, reenvío tras uncertain, liberación de claim, estados nuevos,
activación en Settings/API/runtime, autorización de completion, D2/D17 o HNP.
No se modifican Merchant, PSP, UI/SSE, políticas publicadas, informes anteriores,
fixtures ni baseline B0. Las ejecuciones off/shadow son unitarias y sintéticas;
no se reejecutó el baseline experimental.

## I. Dictamen

**READY_FOR_REVIEW** para EC-GOV-06C-A. El despacho comercial permanece bloqueado.
No se implementa ni se autoriza EC-GOV-06C-B en este incremento.
