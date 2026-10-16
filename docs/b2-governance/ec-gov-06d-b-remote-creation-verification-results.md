# EC-GOV-06D-B — Verificación remota de creación histórica ACP

## A. Git y preflight

Base exacta: `95cdcf12d86230206c8f17605214ceb3630219d3`, rama inicial
`governance/ec-gov-06c-c-post-create-safety`. Árbol limpio, índice vacío,
sin stashes ni rama de destino existente. El inventario de tareas de Codex
solo mostró esta tarea activa; no había otra tarea activa sobre Agent ni locks
Git observados. Esto no es un bloqueo exclusivo contra editores externos.
Se creó `governance/ec-gov-06d-b-remote-creation-verification` desde esa base,
y se comprobó nuevamente el HEAD completo.

Remotos conservados, fetch y push:

- origin: `https://github.com/Maldinho18/tesis-commerce-agent.git`.
- org: `https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent.git`.

Solo se añaden cinco archivos; no se modifica ningún archivo de la base:

- `src/commerce_agent/governance/remote_creation.py`.
- `src/commerce_agent/acp/creation_lookup.py`.
- `src/commerce_agent/persistence/remote_creation_sqlite.py`.
- `tests/governance/test_remote_creation.py`.
- Este informe.

Sin stage, commit, push, merge, rebase ni PR. No cambios en otros repositorios.

Corrección focalizada H1 posterior a auditoría CHANGES_REQUIRED: se confirmó la
misma rama, HEAD exacto e índice vacío, con los cinco archivos nuevos anteriores.
Solo se retocan remote_creation.py, test_remote_creation.py y este informe.
El cliente de lookup y el store SQLite conservan sus bytes previos; no cambia
la arquitectura, el ensamblado ni ningún módulo comercial/histórico.

## B. Contratos inspeccionados

Agent en la base exacta: envelope, canonicalización, enforce, reconciliación,
post-create, sus stores SQLite, dispatch, procedencia, cliente/modelos/esquema
ACP y configuración. Se revisaron pruebas e informes 06A, 06B, 06C-A/B/C.

Merchant se leyó mediante `git show` del objeto congelado
`c5bd8113998f26928f7d2b62771285fe4040d07f`, en la rama publicada
`merchant/ec-mer-rec-02-readonly-lookup`: `contracts/creation_lookup.py`,
`creation_lookup.py`, `creation_lookup_api.py`, informe 06D-A, primitivas y
soporte de creación. No checkout, fetch, import de su paquete, servicio o DB.

El POST privado absoluto es `/lab/recovery/checkout-creation/lookup`.
Su cuerpo contiene exactamente versión `merchant-acp-creation-lookup/1` y
SHA-256 hexadecimal de la clave; no recibe run/actor/checkout/autoridad ni clave
original. Headers: Bearer backend, API-Version 2026-04-17 y JSON. No se envía
Idempotency-Key de mutación ni Request-Id elegido por cliente.

Se replicó exactamente el perfil de respuesta original: UTF-8 estricto, claves
Unicode ordenadas sin normalización NFC, secuencias conservadas, null/bool/string,
integers seguros ±9007199254740991, sin floats y con las doce claves sensibles
rechazadas recursivamente. Se comprueba el vector publicado de café/z.
Fingerprint request y digest response usan dominios diferentes. El primer
algoritmo reutiliza el envelope íntegro y su cuerpo original, no defaults nuevos.
El fingerprint Merchant no trae un campo creation_profile separado: ese perfil
se verifica en el envelope y forma parte del algoritmo de fingerprint.

### H1 — Causa confirmada y corrección

La inspección directa del mismo objeto Merchant añadió acp.py y payment_sandbox.py:
ACPCheckoutCreateRequest usa ACPPaymentCapabilities.handlers con max_length=0;
ACPCheckoutAdapter._response no copia ese valor, sino que publica
checkout_payment_capabilities(), cuyo payment_handler completo tiene id
tesis_sandbox, requiere delegación, no requiere PCI y declara sus schemas/config.
Por tanto, igualdad entre capabilities del request y del response no es una
garantía del Merchant. El verificador anterior confundía capacidades solicitadas
por el agente con capacidades publicadas por el vendedor.

El schema congelado describe expresamente esa diferencia de contexto. PaymentHandler
valida estructura, tipos, campos requeridos, versión y URIs; FulfillmentOptionShipping
requiere type/id/title/totals y admite fechas de entrega. Se compararon los blobs
del schema Merchant con los bytes locales Agent: son exactamente iguales, SHA-256
`d0e4290617d66bf05d002b8ace388732be2b3eb9a92a1003db7a2daa1e0436f2`.
No se importó ni se ejecutó Merchant ni se cambió el contrato congelado.

Corrección productiva mínima: retirar exclusivamente
`checkout.capabilities != body["capabilities"]` y añadir un comentario explicativo
junto a validate_acp. Las capacidades de respuesta siguen sujetas al esquema ACP
congelado dentro del CheckoutSession completo y al digest histórico recalculado.
No se agrega una política de selección de handlers ni se interpreta su presencia
como permiso para delegación, pago, continuación o completion. Las otras
comparaciones y verificaciones permanecen intactas.

## C. Arquitectura y confianza

Se descartó reinterpretar eventos 06C-B: sus observaciones siguen sin autenticar.
Se descartó ampliar ACPClient, que expone efectos comerciales. Se añade un cliente
privado con una sola operación y un journal independiente, reutilizando los
verificadores de envelope, D1, procedencia, historial y definiciones DDL existentes.

`BackendLookupSession` solo se inyecta desde backend confiable. Contiene destino,
SecretStr y un `SessionScope` del registro de aprovisionamiento backend, con referencia
versionada. Ese registro debe vincular **esa credencial** con el run/actor remoto;
no se obtiene copiando el binding esperado del envelope ni de campos del comprador.
El contrato del registro exige `backend_session_registry/1`, distinto de
`backend_expected_not_authenticated`; el tipo esperado anterior se rechaza.

El código no implementa emisor ni introspección de sesiones. Construir un modelo
SessionScope o señalar una referencia no demuestra autenticación. El backend que
inyecta la pareja credencial/ámbito y la integridad de su registro son supuestos
explícitos de confianza. Una respuesta válida debe además provenir del destino
configurado, presentar el ámbito esperado y superar todas las verificaciones.

En transporte de producción, HTTPS usa verificación estándar de certificados;
atribuiría la conexión al host configurado, no certifica el código desplegado ni
firma la evidencia histórica. Bearer y el contexto devuelto se interpretan bajo
el contrato del servidor confiable que valida la sesión. Un servidor comprometido
puede mentir. HTTP loopback depende de la confianza en el host/proceso local, sin
autenticación TLS. Estos límites se conservan en `basis`:
`https_backend_session`, `loopback_backend_session` o `controlled_transport`.
Todo transporte inyectado se marca controlado; las pruebas nunca acreditan TLS,
validación real de Bearer ni consulta real al Merchant.

El cliente exige HTTPS fuera de loopback, rechaza credenciales/query/fragmento y
destinos ambiguos, elimina deliberadamente el prefijo ACP al resolver la ruta
absoluta, no sigue redirecciones, no usa proxies ambientales, no tiene fallback,
no lee dotenv y no reintenta. Timeout explícito de 5 s por fase httpx, límite por
defecto de 262144 bytes, lectura por bloques y solo encoding identity/JSON.
El timeout es por fase, no un deadline global contra un servidor que gotea datos.
No se añaden logs de cuerpos, claves o credenciales; los errores son códigos cerrados.

## D. Implementación

`VerificationRequest` solo identifica operación, run/owner local, query y versión
esperada; no tiene parámetros de red ni autoridad. Es una API interna backend,
sin tool, endpoint, worker ni opción nueva de Settings/runtime.
`Verification`, `VerificationEvent` y sus solicitudes son estrictos, inmutables,
versionados y rechazan campos desconocidos/coerciones. Replay y continuación son
literales false. La referencia de owner persistida es la pseudónima D1.

`CreationLookupClient.lookup` valida el wire exacto y diferencia found verificado,
conflicto de identidad/fingerprint, respuesta original inválida, not_found,
unrecoverable, autenticación, timeout, transporte, respuesta malformada y falta de
atribución. Versiones/perfiles desconocidos son contrato wire malformado, nunca found.
Los resultados locales distinguen además integridad, persistencia y CAS.

`RemoteCreationVerifier.verify`:

1. Abre lectura SQLite, verifica envelope/procedencia/D1 e historiales previos.
2. Recupera ámbito local y checkout conocido, también de post-create si corresponde.
3. Valida historial nuevo y versión esperada; una query ya persistida se inspecciona
   sin repetir HTTP. Cierra la conexión antes de consultar.
4. Ejecuta únicamente el lookup con el cliente privado.
5. Abre BEGIN IMMEDIATE, revalida el snapshot local completo y todo el historial,
   verifica CAS y agrega evidencia con su head en savepoint/transacción.
6. Confirma, cierra y reabre para comprobar el registro durable. Solo entonces
   retorna durable=true. Un commit fallido/no-op no acredita persistencia.

`inspect` abre exclusivamente una lectura y no inicializa tablas ni llama HTTP.
El store de bajo nivel documenta que `append` devuelve un evento provisional.

Dos tablas aditivas opt-in, sin cambios de esquema histórico:

| Tabla | Función |
| --- | --- |
| d1_remote_creation_events | Operación/versión y operación/query únicas; bytes canónicos, digest de dominio y enlace al evento anterior. |
| d1_remote_creation_head | Última versión/digest, FK al evento y avance CAS de una versión. |

Triggers impiden UPDATE/DELETE/REPLACE de eventos, borrado/reemplazo de head y
avances inválidos. Se comprueba todo evento, versiones 1..N, contenido/digest,
correspondencia SQL, cadena, ámbito, referencias D1/envelope y head antes de leer
o avanzar. Un cambio del primer evento con el último intacto se rechaza.

Se persisten resultado, motivos cerrados, base de confianza, referencia de sesión,
correlación remota, digest del wire de lookup, digest de original_response validado
y checkout ID verificado. El digest del wire es referencia de contenido, no un
archivo local recuperable. No se duplica la respuesta ACP completa: el evento
atestigua qué validó este backend y con qué digests; una auditoría que necesite
revalidar el cuerpo original requerirá su retención en Merchant u otro archivo
aprobado. El journal solo no permite reconstruirla. No contiene payload de replay,
Bearer ni clave original. Los hashes no sustituyen una firma remota ni protegen
contra un administrador capaz de alterar coherentemente esquema, contenido y hashes.

## E. Invariantes

Se comprueban envelope canónico/digest, referencias D1/contexto/owner/operación,
origen configurado, binding backend de sesión distinto de expectativa, run/actor
remotos, hash de clave, fingerprint de request, versión y perfil, formato/coincidencia
de checkout ID, esquema ACP congelado, digest/perfil de respuesta, moneda/variante/
cantidad y estado original de creación; capabilities se valida estructuralmente
contra el contrato de respuesta ACP, sin exigir igualdad con el request. Se contrasta el checkout ya
conocido por resultados 06B o post-create. Local y remote run pueden diferir.

La revalidación transaccional detecta cambios durante HTTP en compra, reserva,
resultado, envelope y los historiales incluidos en el snapshot. Una respuesta
vieja no sobrescribe otra: CAS falla o devuelve la misma evidencia ya registrada
cuando la identidad y contenido coinciden exactamente. Los eventos son históricos;
la inspección de un evento antiguo no lo convierte en estado comercial vigente.

not_found solo expresa ausencia observada en ese snapshot; unrecoverable no permite
backfill; found no concede vigencia, consentimiento, retención de idempotencia,
reautorización ni continuación. Fallos locales previos no producen consulta; si no
hay envelope íntegro no se fabrica una referencia de evidencia para el error.
Un fallo posterior al lookup devuelve resultado no durable y exige inspección
explícita; no se promete persistir un error cuando la persistencia está averiada.

No se escriben purchases, claims, reservas, ledger D1, envelopes, resultados 06B
ni eventos 06C-B/C. No hay create/update/cancel/complete, PSP, liberación de claims,
enforce comercial, HNP, AP2, UI/SSE o cambios de protocolos/dependencias.

## F. Pruebas y controles

Entorno medido: Windows 11 10.0.26220, Python 3.12.6, uv 0.12.15
(d35f1f270), Ruff 0.16.8, Pyright 1.1.414, Pytest 9.0.2, httpx 0.28.1,
Pydantic 2.12.5. No se instalaron paquetes ni se ejecutó sync.

Resultados anteriores a H1 (referencia histórica, no una nueva ejecución):

| Control | Resultado |
| --- | --- |
| Tests nuevos, corrida específica | 93 passed in 163.07s, cero omitidos. |
| Regresiones 06A/06B/06C-A/B/C | 207 passed in 506.38s, cero omitidos. |
| Suite general protegida final | 832 passed, 3 skipped, 1 warning in 721.28s. |
| Ruff format --check . | 162 files already formatted. |
| Ruff check . | All checks passed! |
| Pyright | 0 errors, 0 warnings, 0 informations. |
| Diff check, incluidos nuevos contra NUL | Sin errores de whitespace; avisos de conversión futura LF a CRLF por configuración existente. |

La corrida específica anterior de 93 casos precedió únicamente al añadido del campo
literal de versión del resultado y de constraints de tipos en el head; la suite
general final valida también esos ajustes, incluyendo los mismos 93 casos.
Se conservan las 739 aprobadas anteriores y se agregan 93, sin omisiones nuevas.
Los tres E2E omitidos requieren Merchant, Payment Provider y PostgreSQL reales:
`tests/e2e/test_ap2_joint_purchase.py:32`,
`tests/e2e/test_conversational_purchase.py:74` y
`tests/e2e/test_joint_purchase.py:95`.
La única advertencia es DeprecationWarning en `starlette/testclient.py:53` por
`anyio.abc.BlockingPortal`, que recomienda `anyio.from_thread.BlockingPortal`.
No se cambian dependencias para ocultarla. Los tiempos son duración de suites,
no un benchmark ni un SLO; parte de regresiones/general corrió en paralelo
con bases temporales separadas. No hubo fallos en esas dos ejecuciones protegidas.

### Corrección H1 — Nuevas ejecuciones de validación

| Control post-H1 | Resultado |
| --- | --- |
| Regresión positiva antes de corregir | 1 failed in 4.07s: response_invalid en lugar de found_verified, después de validar satisfactoriamente el esquema congelado del handler, shipping y CheckoutSession. |
| Pruebas específicas de 06D-B post-H1 | 102 passed in 159.34s, cero omitidos. |
| Regresiones 06A/06B/06C-A/B/C post-H1 | 207 passed in 328.25s, cero omitidos. |
| Suite general protegida post-H1 | 841 passed, 3 skipped, 1 warning in 570.09s. |
| Ruff format --check . post-H1 | 162 files already formatted. |
| Ruff check . post-H1 | All checks passed! |
| Pyright post-H1 | 0 errors, 0 warnings, 0 informations. |
| Diff checks post-H1 | Sin errores de whitespace, incluidos los cinco archivos nuevos contra NUL; avisos existentes de conversión futura LF a CRLF. |

Se añaden nueve casos: una regresión positiva y ocho negativos estructurales.
El fixture representativo se usa en las pruebas anteriores, conservando sus
aserciones de identidad, integridad, aislamiento y ausencia de efectos comerciales.
Durante la preparación, Pyright señaló 10 errores de inferencia del helper found
al combinar strings y un diccionario sintético: se anotó su retorno dict[str, Any],
sin ignorar errores ni alterar las verificaciones. Ruff detectó el bloque Python
del runner previamente agregado a este informe sin formato; se formateó solo ese
bloque documental. No cambia el runner ejecutado ni sus protecciones.

La suite post-H1 conserva las 832 aprobadas anteriores y añade nueve: 841 en total.
Las tres omisiones E2E y la advertencia Starlette/AnyIO son exactamente las mismas
enumeradas arriba; no hay omisiones nuevas ni pruebas negativas desactivadas.
Ninguna de las tres ejecuciones post-H1 (específicas, regresiones y general) falló.

Incidencias de desarrollo, sin ocultar resultados: la primera corrida se detuvo
con 1 failed in 7.56s porque el fake comercial heredado incluía una opción shipping
abreviada inválida para el esquema congelado. Se completó exclusivamente el nuevo
fixture sintético (fulfillment_options vacío); no se modificó ACP ni el fake histórico.
La segunda corrida produjo 2 failed, 79 passed in 128.60s: una mutación de revisión
sin cambio comercial violaba correctamente el trigger previo, y una aserción de
excepción esperaba ValidationError en vez de ValueError por clave JSON duplicada.
Se corrigió el estímulo de concurrencia (cambio real de etapa con revisión) y la
aserción de tipo, conservando las verificaciones. Luego se ampliaron escenarios,
incluido truncamiento, commit no-op y fallos de head, hasta los 93 aprobados.
El desarrollo estático tuvo 28 errores iniciales de inferencia de kwargs heterogéneos
y después 1 error del helper de headers en tests; se resolvieron mediante validación
estricta del diccionario de resultado y un header explícito en el fixture, sin
ignorar errores, reducir typeCheckingMode ni cambiar dependencias.

La suite protegida elimina variables de proveedores, desactiva dotenv/tracing/E2E,
impide SQLite fuera de basetemp nuevo y bloquea sockets externos/operativos y
escrituras al repositorio. Solo permite loopback efímero creado por el propio
proceso para mecanismos de asyncio en Windows. Dos pruebas shadow que exportan
evidencia cambian temporalmente cwd al tmp_path; no sobrescriben .local/gov05c.
Las suites paralelas usan bases SQLite temporales distintas, no un PostgreSQL compartido.
Los subprocesos históricos de tests heredan el entorno desactivado, pero no el
audit hook Python; sus destinos se limitan a fixtures descartables ya cubiertos
por las regresiones. No se declara sandbox de procesos hostiles.

Comandos estáticos y corrida específica, con UV_NO_SYNC=true, UV_OFFLINE=true,
PYTHONDONTWRITEBYTECODE=1 y RUFF_NO_CACHE=true:

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest tests/governance/test_remote_creation.py -q --tb=short -p no:cacheprovider --basetemp=$env:TEMP/ecgov06db-third
git diff --check
# Cada archivo nuevo, sin añadirlo al índice:
git diff --no-index --check -- NUL <ruta_nueva>
```

Para regresiones se invocó el siguiente runner por stdin con
`tests/governance/test_d1_uow.py tests/governance/test_enforce_integration.py tests/governance/test_dispatch_envelope.py tests/governance/test_reconciliation.py tests/governance/test_post_create.py`.
Para la suite general se invocó sin argumentos. La llamada fue
`$runner | .venv/Scripts/python.exe -B - <argumentos>`, con la fuente siguiente
como contenido de $runner (equivalente salvo formato); no escribe un runner al
repositorio ni utiliza una base existente. Las nuevas corridas post-H1 específicas
usan ese mismo runner con tests/governance/test_remote_creation.py; las regresiones
y la general conservan los argumentos indicados y basetemp nuevo en cada proceso.

```python
import os, sys, tempfile, socket
from pathlib import Path

for key in tuple(os.environ):
    if key.startswith(
        ("OPENAI_", "MERCHANT_", "PAYMENT_", "AGENT_", "LANGSMITH_", "LANGCHAIN_", "RUN_")
    ):
        os.environ.pop(key)
os.environ["RUN_JOINT_E2E"] = "0"
os.environ["RUN_JOINT_AP2_E2E"] = "0"
os.environ["RUN_LIVE_LLM"] = "0"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ.pop("PYTEST_ADDOPTS", None)
from commerce_agent.config import Settings

Settings.model_config["env_file"] = None
base = Path(tempfile.gettempdir()) / ("ec-gov-06ca-" + __import__("uuid").uuid4().hex)
root = Path.cwd().resolve()
listeners = set()
original_listen = socket.socket.listen


def listen(self, *args, **kwargs):
    result = original_listen(self, *args, **kwargs)
    addr = self.getsockname()
    if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1"):
        listeners.add((addr[0], addr[1]))
    return result


socket.socket.listen = listen


def inside(path, parent):
    return path == parent or parent in path.parents


def guard(event, args):
    if event == "socket.connect":
        addr = args[1]
        if not isinstance(addr, tuple) or (addr[0], addr[1]) not in listeners:
            raise RuntimeError("External/operational sockets blocked")
    if event == "sqlite3.connect":
        value = args[0]
        if str(value) != ":memory:":
            if isinstance(value, str) and value.startswith("file:"):
                from urllib.parse import urlsplit
                from urllib.request import url2pathname

                path = Path(url2pathname(urlsplit(value).path)).resolve()
            else:
                path = Path(value).resolve()
            if not inside(path, base):
                raise RuntimeError("SQLite outside isolated test directory blocked")
    if event == "open":
        name, mode, flags = args
        write = (mode is not None and any(c in mode for c in "wax+")) or (
            flags is not None
            and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        )
        if (
            write
            and isinstance(name, (str, bytes, os.PathLike))
            and inside(Path(os.fsdecode(name)).resolve(), root)
        ):
            raise RuntimeError("Repository writes blocked during tests")
    if event in ("os.mkdir", "os.remove", "os.rmdir", "os.rename"):
        values = args[:2] if event == "os.rename" else args[:1]
        for value in values:
            if isinstance(value, (str, bytes, os.PathLike)) and inside(
                Path(os.fsdecode(value)).resolve(), root
            ):
                raise RuntimeError("Repository mutations blocked during tests")


sys.addaudithook(guard)
import pytest


class SafeExports:
    @pytest.fixture(autouse=True)
    def isolate_legacy_exports(self, request, monkeypatch, tmp_path):
        if request.node.name in (
            "test_same_commercial_result_operations_and_real_evidence_before_gateway",
            "test_measured_off_shadow_latency_separate_from_bootstrap",
        ):
            (tmp_path / ".local/gov05c").mkdir(parents=True)
            monkeypatch.chdir(tmp_path)


print(
    "Offline; fresh SQLite; repository writes blocked; historical exports redirected to temporary cwd"
)
raise SystemExit(
    pytest.main(
        [*sys.argv[1:], "-q", "--tb=short", "-p", "no:cacheprovider", "--basetemp=" + str(base)],
        plugins=[SafeExports()],
    )
)
```

## G. Escenarios adversariales

H1 añade una regresión positiva dedicada: petición con handlers vacíos, respuesta
con PaymentHandler tesis_sandbox completo y shipping con title, fechas y totales.
Se valida cada estructura y el CheckoutSession con el esquema congelado antes de
esperar found_verified durable. Este fixture es una construcción sintética desde
el código Merchant publicado, no una captura real; sustituye la respuesta abreviada
en todas las pruebas de lookup para que las validaciones previas también cubran
esa forma representativa. No cambia fixtures históricos ni importa Merchant.

Ocho casos nuevos recalculan correctamente el digest sobre respuestas inválidas y
exigen response_invalid: handlers con tipo incorrecto, falta de psp, flag booleano
como string, campo extra de handler, URI inválida, versión inválida, shipping sin
title y shipping con totals de tipo incorrecto. Las pruebas previas de moneda,
variante, cantidad, checkout ID, estado, fingerprint, ámbito, perfil y digest
se conservan con el nuevo fixture; no se rebajan aserciones.

La matriz ejecutable está en test_remote_creation.py. Incluye consulta correcta,
reinicio por reapertura/coordinador nuevo, conservación de creación comercial,
scope local y remoto, sesión ausente/no autenticada, Bearer ausente/rechazado,
origen, endpoint absoluto desde /api/acp, campos de destino/autoridad prohibidos,
redirección, perfiles/versiones, claves/fingerprints/IDs y respuestas inválidas.

También incluye canonicalización Unicode/orden/tipos/claves sensibles, envelope,
D1, historial 06C-B y post-create corruptos, corrupción de un evento antiguo,
truncamiento de cola, guards SQL, CAS y dos consultas concurrentes; misma query
devuelve un evento, queries distintas producen un solo avance desde la misma versión.
Un cambio local durante HTTP invalida la respuesta. Se prueban rollback, fallo de
inserción/head, commit antes/después de persistir y commit que no hace nada.

Timeout antes/después de lookup, error de red, 503, 401, JSON inválido/duplicado,
respuesta excesiva y no-store ausente conservan semántica cerrada. SystemExit en
el transporte simula terminación después de consultar y antes de persistir;
la nueva inspección no hace HTTP. Es inyección controlada de fallo, no una caída
real de red ni prueba conjunta de ambos procesos. Los snapshots comparan tablas
comerciales y los dobles comprueban cero efectos causados por el verificador.

## H. Límites y preparación para 06D-C

Solo Windows/Python/SQLite real con transporte HTTP simulado. No validación Linux,
PostgreSQL, TLS real, revocación remota, despliegue Merchant o integración conjunta.
La corrección H1 demuestra compatibilidad estructural y semántica con una respuesta
sintética derivada del código congelado; no sustituye la prueba Agent–Merchant real
con ese handler y ese despliegue, que sigue correspondiendo a 06D-C.
El registro real que aprovisiona la pareja token/ámbito sigue siendo una dependencia
de ensamblaje backend; no se configura aquí ni se declara validado por los mocks.

06D-C deberá fijar exactamente el despliegue Merchant c5bd811, preparar PostgreSQL
aislado, emitir sesiones sintéticas desde el emisor confiable y registrar su binding
independiente del envelope. Validará scopes distintos local/remoto, renovación,
revocación, ruta fuera de ACP, contexto realmente derivado de Bearer, pérdida de
respuesta y fallos de persistencia con ambos servicios. Necesita autorización propia;
no debe añadir replay ni derivar autoridad de este found histórico.

Las lecturas de historial son lineales. No se implementa retención, archivado,
garantía frente a administradores, reautorización, reserva remota ni exclusión de
otros procesos que muten el entorno fuera del contrato SQLite. Los timeouts y
crashes probados solo acreditan el perímetro local descrito.

## I. Dictamen y preservación

El incremento se limita a cliente privado de consulta, verificación y evidencia
aditiva. No activa enforce ni avanza a 06D-C. Se detiene para revisión humana,
sin stage, commit, push, merge ni PR.

Sello de preservación, calculado sobre rutas ordenadas y SHA-256 real de cada
archivo (ruta + ':' + hash hexadecimal, unidos con newline, luego SHA-256):

- 321 archivos originales versionados:
  `51868fd4e0328c8e74d028628cb41e9e141e2bdb68befd3183f48c4b99a27b1a`.
- 1116 archivos previos de `.local/gov05c`:
  `def9c7416a05abae4597c23c369a54ed6cf2704ffc201334717e855fcdc810cd`.

Ambos coinciden con el preflight. Los archivos nuevos no entran en el primer
sello, porque el índice permanece vacío. Se preservan exactamente ACP/AP2
congelado, motor Python medido, uv.lock, políticas, fixtures y resultados B0
versionados. No se reejecutó el baseline experimental ni se tocó su etiqueta.
Las pruebas ejecutaron dobles comerciales y SQLite sintético, no B0 ni servicios.

Estado Git:

```text
## governance/ec-gov-06d-b-remote-creation-verification
?? docs/b2-governance/ec-gov-06d-b-remote-creation-verification-results.md
?? src/commerce_agent/acp/creation_lookup.py
?? src/commerce_agent/governance/remote_creation.py
?? src/commerce_agent/persistence/remote_creation_sqlite.py
?? tests/governance/test_remote_creation.py
```

HEAD continúa en `95cdcf12d86230206c8f17605214ceb3630219d3`.
Diff de archivos previamente versionados vacío; los cinco nuevos permanecen
sin stage para revisión. No se modificó core.autocrlf ni se normalizó el repositorio.

La revisión H1 comprobó además el alcance exacto contra sus hashes previos:
restituir en memoria la comparación retirada y quitar el comentario nuevo produce
SHA-256 `9da013296856e8e64b8b5b4f9d5f5504b00a30828f2f62466257cd2f85db3f19`,
idéntico al remote_creation.py anterior a H1. No se restauraron archivos físicos.
CreationLookupClient conserva SHA-256
`4f0d64eeff594118e1e7f97e2940ca5d80e81a56d2a71b1b6e7d0c99978980be`;
el store/coordinador SQLite conserva
`4c893e5e911bd19623fe3217354c3aab368632b6f97f4649b87a94a4ef0927b5`.
Los otros cambios focalizados son fixture/pruebas e informe; no hay refactorización.

Los controles estáticos y las suites exigidas finalizaron satisfactoriamente en
el perímetro offline descrito. No acredita autenticación real ni autoriza activación
comercial; esas comprobaciones conjuntas siguen pendientes de 06D-C y su revisión.
H1 queda corregido con reproducción roja previa y validación verde posterior:
la respuesta representativa con tesis_sandbox y shipping real deja de rechazarse
por una igualdad que Merchant no promete. El esquema, digests, fingerprint y
las restantes verificaciones de identidad y ámbito permanecen efectivos.
Se preservan íntegros 321 archivos versionados y 1116 archivos previos de gov05c,
recomprobados después de la suite general. Cinco archivos nuevos para revisión,
sin stage, commit, push, merge ni PR. Se detiene este incremento.

READY_FOR_REVIEW
