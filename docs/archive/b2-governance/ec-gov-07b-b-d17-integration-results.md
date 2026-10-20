# EC-GOV-07B-B — Integración D1/07A/D17

> **Nota 2026-10-20.** Rutas históricas, no se reescriben. El reporte estaba en
> `docs/b2-governance/` y hoy está en `docs/archive/b2-governance/`; los módulos live de
> `tests/integration/` se reemplazaron (`test_guarded_d1_live.py` → `test_live_07bb_guarded_d1.py`)
> y sus workers y arneses `gov*` se retiraron
> ([equivalencias](../../../tests/integration/README.md#equivalencia-con-la-suite-anterior));
> `ec-gov-07b-d17-offer-freshness-results.md` y `ec-gov-07b-b-d17-agent-integration-results.md`,
> listados como `??`, nunca se versionaron.

## A. Preflight y dependencias congeladas

2026-10-16. Agent base exacta d47b3da04b42b7c288162b8882b8ae59ef2b778a,
rama Q publicada y limpia. Origin https://github.com/Maldinho18/tesis-commerce-agent.git.
Rama y destino nuevos ausentes antes de crear
`governance/ec-gov-07b-b-d17-integration` en `.local/worktrees/ec-gov-07b-b-final`.
Merchant limpio en 9743c9f179c7ad5413e8818766ef3dceb326a917.
Baseline de 3248 archivos en ocho árboles protegidos, incluidos Q y B0:
`.local/07bb-validation/baseline.json`. No se leen .env operativos.

## B. Matriz de identidad y procedencia

Matriz previa a implementación; inspección de contratos y SQL, no solo narrativa.

| Enlace | Identidad y comprobación | Frontera |
| --- | --- | --- |
| P → selección | Recover del store P: HMAC, owner, UUID episodio, ranking, todas las observaciones, Offer y digest completo | Nunca recibir selección confiable del caller |
| Selección → Q | Recover Q del binding shadow original, mismos feed/intent/variante/owner/run y registro de emisión | Shadow no autoriza; base Q permanece congelada |
| Q → nueva D1 aplicada | Base experimental nueva, mismo episodio/run/owner, nueva decisión enforce y transición 07A, artefactos aprobados | No modificar registros/contextos shadow; claves backend persistentes |
| D1 → operación autorizada | UoW 07A, evidence digest, CAS received→selected, operation_id y clave únicos | Commit y lectura independiente antes de cualquier claim |
| Operación → intención D17 | Envelope auxiliar inmutable, enlazado al digest del envelope ACP original y a P/Q, oferta/revisión/digest, request canónico/fingerprint | No reinterpretar el path /checkout_sessions histórico |
| Intención → Merchant | Claim UoW único confirmado; transporte especializado a /lab/governance/checkout-creation/guarded | Sin retry ni fallback; error tras claim conserva incertidumbre |
| Merchant → prueba | GuardedProof y CreationFound: scope, hash clave, fingerprints, Offer, checkout, respuesta y digests | HTTP 200/201 solo no acredita creación |
| Prueba → recuperación | POST de lectura a /lab/recovery/guarded-checkout-creation/lookup | found/rejected/not_found/unrecoverable/error; nunca concede otro envío ni D2 |

Alternativas: promover shadow incumple C; adaptar el transporte 07A cambiando su
path incumple F; duplicar el evaluador/UoW introduce otra autoridad. Se elige
reservar con el gate 07A sin transporte, conservar su envelope original y persistir
un intento D17 auxiliar antes del único claim. Bases P/registro/Q separadas;
base nueva para D1 aplicado y D17; recuperación explícita de commits, sin reevaluar.

## C. Nueva autorización D1 y separación de shadow

`SqliteGuardedD1` exige un binding Q recuperable y resuelve de nuevo su registro
contra la credencial del transporte. Recupera directamente el sello P; coteja
owner/run/episodio, feed, intent, variante, observaciones, Merchant y recibos.
Q conserva su DecisionRecord `observed_shadow`, `operation_id=null` y base propia.
Su outcome, incluso allow, no se considera autorización comercial.

La nueva base aplicada empieza vacía. Bajo BEGIN IMMEDIATE se comprometen una
compra received y un intento HMAC que fija request P, digest del binding Q y de
configuración. Nunca se adopta historia D1 previa. El gate 07A original, **sin
ningún transporte**, captura exactamente feed/intent/ranking P y evalúa otra
decisión con el motor y artefactos aprobados. Su UoW compromete aplicación
`transition_committed`, CAS received→selected, nueva evidencia, operation_id,
clave única y envelope ACP original. Un deny real conserva evidencia blocked;
no produce intención D17 ni claim comercial. No hay evaluación LLM ni aprobación
humana fabricada; el perfil sigue siendo experimental backend-only.

Antes de enlazar o enviar se reproduce la decisión mediante el ledger original
y se recuperan fuentes/contexto/configuración autenticados del vault. Se exige
modo enforce, origen policy, outcome allow, transición real, identidad distinta
de la decisión shadow, referencias y owner exactos. El replay verifica un commit;
no reevalúa para crear otra decisión durable o cambiar términos.

La configuración no se conecta a Settings, PurchaseService público o runtime.
No se cambia rank_candidates, canonicalización, política, gate, UoW ni registros
históricos. La compra de Q permanece congelada; la transición nueva ocurre solo
en la base experimental aplicada, con el mismo UUID local y propietario.

## D. Envelope durable y modelo de estados

Contrato aditivo `commerce-d17-envelope/1`. Conserva el digest del envelope ACP
07A original, que sigue describiendo `/checkout_sessions`; no lo renombra ni
lo envía. La nueva intención declara su propio POST y path guardado, API-Version,
origen, operation/run/owner, hash de clave, decision/evidence/input/context,
binding Q completo y digest, Offer completo seleccionado y request D17 canónico.
La revisión y el digest proceden exclusivamente de la observación P sellada.
El fingerprint usa exactamente `merchant-guarded-acp-attempt/v1` y el algoritmo
de fingerprint ACP publicado. La clave privada sigue en el almacenamiento
operativo 07A: no entra al envelope auxiliar público ni al LLM.

Tablas nuevas: `b_d17_meta`, `b_d17_attempts`, `b_d17_envelopes`, `b_d17_events`.
Cada cuerpo queda ligado por HMAC con clave backend persistente. Triggers bloquean
UPDATE, DELETE y reemplazo por claves únicas incluso sin recursive_triggers.
Los eventos forman una cadena de secuencia y digest anterior, bajo write lock.
El estado reservado/claimed y su CAS son los existentes de 07A, no otra
implementación de autorización. Las observaciones no habilitan continuación.

Fronteras de atomicidad explícitas: P, registro y Q están separados de la base
aplicada. El intento firmado se compromete **antes** de la nueva D1. D1 y su
envelope ACP se comprometen juntos por 07A; la intención D17 se compromete en
otra transacción anterior al claim. El claim se compromete antes de HTTP; el
evento local de respuesta/lookup ocurre después de HTTP. No hay transacción
distribuida SQLite/PostgreSQL ni promesa de entrega exactamente una vez.

Si cae antes de que D1 comprometa su decisión, el intento no se reevalúa al
reiniciar: se bloquea y exige episodio nuevo. Si existe una reserva y envelope
07A completos, `recover_preparation` explícito puede enlazar sus commits, sin
capturar/evaluar/enviar otra vez. Nunca reconstruye intención para una operación
claimed sin intención D17 previa. Reservado con intención completa permite una
invocación explícita que gane el primer claim; claimed solo permite lookup.

## E. Despacho inicial, pruebas D17 y recuperación

Antes del POST se verifican registro, P/Q, autorización aplicada, replay real,
vault y ambos envelopes. Bajo BEGIN IMMEDIATE se revalida la evidencia y se
reclama la operación mediante el UoW original. Una segunda conexión debe leer el
claim y claimant exactos ya comprometidos. Solo la invocación que acaba de ganar
puede llamar al transporte. Otra llamada o worker observa claimed y se bloquea.
Tras el claim, incluso un error que parece anterior al envío consume ese derecho.

`HttpGuardedCheckout` usa Bearer del contexto comprobado, API-Version 2026-04-17,
misma Idempotency-Key durable, redirects deshabilitados, trust_env=False, timeout
explícito y límite 262144 bytes. No reintenta ni usa `/checkout_sessions` como
fallback. Un POST de creación y un POST de lookup tienen significado distinto:
solo el primero es comercial. El endpoint A2 usa transacción PostgreSQL
REPEATABLE READ, READ ONLY en el Merchant congelado.

HTTP 200/201 no basta. Se comprueban scope remoto, hash de clave, fingerprint
guard/ACP, oferta/revisión/perfil/digest, checkout, schema ACP, respuesta original
y su digest. También se contrastan subtotal, envío, total y unidad con el Offer
sellado. El timestamp Merchant tiene precisión de segundos; su comprobación
contra preparación local se hace a esa precisión, sin atribuir orden en
milisegundos o una atestación de reloj remota inexistente.

Lookup verifica adicionalmente el digest completo de la prueba o rechazo y sus
referencias exactas. `found` conserva prueba histórica; `rejected` conserva rechazo;
`not_found` no demuestra ausencia del efecto; `unrecoverable` bloquea por historia
insuficiente; timeout, error, corrupción o discrepancia conservan incertidumbre.
Se permiten consultas read-only explícitas repetidas. Una prueba histórica
verificada no puede ser reemplazada por otra contradictoria en los eventos locales.
Ningún resultado concede reenvío, D2, actualización, cancelación o pago.

## F. Archivos modificados y migraciones

Únicamente archivos nuevos en este worktree:

- `src/commerce_agent/governance/guarded_creation.py`: contratos y verificación D17.
- `src/commerce_agent/acp/guarded_creation.py`: transporte guardado y lookup acotados.
- `src/commerce_agent/persistence/guarded_d1_sqlite.py`: nueva D1 aplicada, intención,
  claim original y cadena durable de observaciones.
- `tests/governance/test_guarded_d1.py`: unitarias/SQLite/concurrencia y ASGI controlado.
- `tests/integration/gov07bb_agent_worker.py`: proceso Agent aislado de experimento.
- `tests/integration/gov07bb_merchant_worker.py`: proceso fixture Merchant con HTTP real.
- `tests/integration/test_guarded_d1_live.py`: clúster desechable y evidencia entre procesos.
- Este informe.

No hay cambios a módulos, contratos, migraciones o aserciones históricas. No se
añaden dependencias ni se modifica uv.lock. Las cuatro tablas auxiliares se crean
solo en una SQLite nueva; una base histórica sin marcador B se rechaza antes
de inicializar AgentStore. No se migra Merchant ni se abre su PostgreSQL desde
Agent: únicamente el worker fixture ejecutado con el intérprete Merchant posee
su clúster descartable y realiza setup/mutaciones/control de efectos.

## G. Pruebas, resultados y evidencia HTTP real

Python 3.12.6 y uv con intérprete Agent ya instalado; `--frozen --offline --no-sync`.
No se ejecuta uv sync ni se añaden dependencias. Las aserciones históricas quedan
intactas. Cada ejecución usa una etiqueta y scratch nuevos; se conserva su exit
pytest en JSON además del log. No se usa un resultado de Q como prueba actual B.

| Ejecución | Resultado y diagnóstico |
| --- | --- |
| Focalizadas iniciales | **24 failed, 7 passed**, 74.32 s; `focused-initial.txt`, `initial-exit.json`. Timestamp local no satisfacía UtcTime de milisegundos; se separó de checked_at Merchant, que tiene segundos. |
| Segunda focalizada | **31 passed**, 113.98 s; `focused-second.txt`, `second-exit.json`. |
| Focalizada ampliada | **39 passed, 1 warning**, 184.82 s; `focused-third.txt`, `third-exit.json`. Anterior a las referencias explícitas finales del envelope y al contraste de importes completo. |
| Focalizada de producción | **39 passed, 1 warning**, 159.52 s; `focused-final.txt`, `final-focused-exit.json`. Las tres fuentes de producción ya son definitivas; anterior al último ajuste del fixture HTTP. |
| Focalizada de entrega final | **39 passed, 1 warning in 199.22s (0:03:19)**, exit 0; `focused-delivery.txt`, `delivery-focused-exit.json`, tras el último ajuste del fixture. |
| Fronteras adicionales offline | **4 passed**, 12.93 s, exit 0; `extra-boundaries.txt` y `extra-boundaries-exit.json`. Sondas separadas del contador de la suite general. |
| HTTP real inicial | **6 failed**, 528.99 s; `real-initial.txt`, `real-initial-exit.json`. El assert del fixture confundía PID del redirector venv de Windows con PID del intérprete. Falló antes de prepare; no demuestra D1 aplicado/D17. Clúster 50578 detenido: `.local/07bp-real-ca9b24e3bc46/closed.json`. |
| HTTP real segundo | **1 failed, 5 passed in 1425.92s (0:23:45)**, exit 1; `real-second.txt`. Cinco escenarios aprobados. El fixture expiry introducía expires anterior a observed: produjo initial error y lookup not_found; no se interpreta como rechazo histórico probado. Se corrige solo el dato del fixture a observed+1 segundo, vencido pero estructuralmente válido. |
| HTTP real expiración final | **1 passed, 5 deselected in 299.79s (0:04:59)**, exit 0; `real-expiry-final.txt`, `real-expiry-final-exit.json`. Se repite solo expiry con Offer válido y vencido. Los cinco escenarios ya aprobados mantienen idénticas las tres fuentes de producción. |
| Contratos Merchant 07B-A/A2 sin modificaciones | **30 passed**, 0.82 s, exit 0; `merchant-contracts.txt`, `merchant-contracts-exit.json`. Intérprete Merchant independiente, perímetro offline; no PostgreSQL ni ASGI/HTTP en esta ejecución. |
| Suite offline previa al último ajuste del fixture | **981 passed, 51 skipped, 1 warning in 901.35s (0:15:01)**, exit 0; `full-final.txt`, `final-full-exit.json`, antes del ajuste final exclusivo del fixture HTTP. |
| Suite offline general final | **981 passed, 51 skipped, 1 warning in 686.06s (0:11:26)**, exit 0; `full-delivery.txt`, `delivery-full-exit.json`, contra los siete archivos congelados finales. |
| Ruff format/lint/Pyright finales | **193 files already formatted / All checks passed / 0 errors, 0 warnings, 0 informations**; `format-delivery.txt`, `lint-delivery.txt`, `types-delivery.txt`. |
| Diff y custodia final | Diff tracked/staged vacío y ocho archivos nuevos sin defectos de whitespace. Custodia 3248/3248 sin cambios; siete fuentes iguales a `code-frozen.json`. `delivery-diff.json`, `protection-final.json`. |

Diagnósticos estáticos previos, todos en archivos nuevos: Ruff inicial 33 errores
(1 corregido automáticamente, 32 pendientes); segunda 112 (5 corregidos, 107
pendientes); tercera 41 (1 corregido, 40 pendientes); cuarta 21; quinta 3; sexta
y finales limpias. Logs `lint-initial/second/third/fourth/fifth/sixth.txt`.
Pyright inicial 1 error de protocolo del gateway inaccesible; segunda 21 errores
de tipado en fixtures, narrowing y el import propio del intérprete Merchant;
posteriores y final 0. Se corrigieron sin suprimir diagnósticos ni cambiar tests
históricos. El worker Merchant importa Psycopg dinámicamente solo en su intérprete.

El primer diff no-index con autocrlf=false marcó los CR al final de las 241 líneas
del worker fixture como whitespace (Git exit 3; wrapper exit 1):
`diff-initial.txt` y `diff-initial-exit.json`. Es un archivo CRLF permitido por
la configuración Windows (core.autocrlf=true); no hay espacios o tabs sobrantes.
Se repite con las reglas normales de whitespace más cr-at-eol y una comprobación
directa de espacios/tabs finales, sin normalizar ni cambiar sus bytes congelados.

En el traceback del fixture HTTP inicial apareció la semilla de custodia sintética
efímera por la representación automática de pytest; no era una credencial
productiva. Se enmascararon sus seis apariciones y se conservaron resultados y
asserts. `initial-log-redaction.json` registra SHA-256 antes/después y motivo.
Los siguientes fixtures usan SecretStr para token y semilla; su serialización
privada solo se hace en memoria hacia el worker. No se afirma que ese diagnóstico
inicial hubiera estado libre de esa exposición sintética.

El runner offline bloquea red/DNS, .env operativo y escrituras/SQLite fuera del
scratch propio. Solo permite el socketpair interno exacto de CPython y los cuatro
scripts constantes de reinicio históricos inspeccionados. Sus hijos no heredan
el audit hook: se limita el comando y limpia el entorno, sin atribuirles otro
sandbox. La ejecución relativa B0 se redirige al scratch, no al B0 protegido.
Las cuatro sondas adicionales viven en `.local/07bb-validation/extra_adversarial.py`
y usan una copia del mismo perímetro offline. Son evidencia de validación local,
no cuatro casos incluidos silenciosamente en la suite general de tests.

Cobertura de los adversariales solicitados:

| Frontera | Comprobación |
| --- | --- |
| Shadow allow / D1 aplicada allow / deny | Shadow solo no puede despachar; nueva evidencia enforce y transición propias; deny POL-02 real sin intención D17. |
| Contexto y sello | Expectativas owner/run/feed/intent/variante/sello alteradas, registro revocado, credencial/actor ajenos y custodia corrupta bloquean antes del POST. Sondas separadas para sello físicamente ausente y episodio remoto ajeno. |
| Oferta posterior | Escenarios reales con mismo precio y revisión diferente, misma revisión y digest diferente, cambio de envío y expiración. Se preservan request y términos P. |
| Operación, clave y envelope | Triggers 07A impiden alterar operation/key; envelope ACP sin intención D17 no es enviable; guardia sin HMAC válido o clave B incorrecta bloquea antes de claim. |
| Concurrencia y caídas | Dos workers SQLite, caída antes del claim (rollback), después de claim antes de envío (no reenvío), tras commit remoto y respuesta perdida; reapertura de SQLite y procesos reales de reinicio. |
| Proof y lookup | Alteración de revisión/digest/fingerprint/actor/key/respuesta; lookup con scope/digest corruptos, timeout, redirect y tamaño excesivo. |
| Estados | found, rejected, not_found, unrecoverable y error cubiertos en transporte controlado; los estados demostrados por HTTP real se detallan aparte. Repetición de lectura nunca concede otro POST. |
| Efectos excluidos | Contadores SQLite de orders/completion en cero y estado selected/2 sin checkout_id/order_id; contadores PostgreSQL propios y trazas HTTP reales para efectos remotos. No se inventa un contador PSP externo. |

ASGI controlado se prueba con FastAPI TestClient y MockTransport: **no es HTTP
real**. El experimento real ejecuta otro intérprete Agent y otro Merchant; sus
peticiones comerciales y de lectura recorren HTTP loopback real. El pipe lleva
solo provisión/control de fixtures, nunca sustituye al canal HTTP comercial.
El worker fixture Merchant ejecuta sus contratos congelados y posee PostgreSQL
nuevo en un puerto aleatorio excluyendo puertos operativos. No se importa Merchant
desde Agent. Una middleware de fixture registra método/path/status sin headers
y puede retrasar solo la respuesta tras commit; no cambia las reglas D17.

Las mutaciones de catálogo de fixture se realizan en su propio clúster y se
restauran tras cada escenario; no son POST comerciales Agent. El prefijo físico
`07bp-real-` del directorio se hereda del harness, aunque el protocolo ejecutado
es 07B-B. Las evidencias públicas finales no contienen las claves efímeras.

Reproducción desde el worktree nuevo, con etiquetas nunca reutilizadas:

```powershell
$env:UV_PROJECT_ENVIRONMENT='C:/ANDES/Tesis/tesis-commerce-agent/.venv'
$env:UV_CACHE_DIR=(Join-Path (Get-Location) '.local/07bb-validation/uv-cache')
uv run --frozen --offline --no-sync ruff format --check .
uv run --frozen --offline --no-sync ruff check .
uv run --frozen --offline --no-sync pyright --venvpath C:/ANDES/Tesis/tesis-commerce-agent
uv run --frozen --offline --no-sync python -u -B .local/07bb-validation/run_offline.py focused review-focused
uv run --frozen --offline --no-sync python -u -B .local/07bb-validation/run_offline.py full review-full
uv run --frozen --offline --no-sync python -u -B .local/07bb-validation/run_extra.py extra review-boundaries
uv run --frozen --offline --no-sync python -u -B .local/07bb-validation/run_real.py review-real
uv run --frozen --offline --no-sync python -u -B .local/07bb-validation/run_real.py review-expiry expiry
```

El último comando crea procesos y un clúster nuevo y lo detiene al salir; no es
offline ni debe confundirse con una llamada a un servicio operativo existente.


Evidencia real consolidada de los seis escenarios válidos (cinco de real-second,
uno corregido de real-expiry-final). El código de producción es idéntico; entre
manifiestos solo cambió el constructor del dato de expiración del worker fixture.
No se presenta real-second como una suite real completa verde.

| Escenario | POST comercial | Lookup A2 read-only | Inicial local | Recuperación | PID Agent restaurado | PID Merchant |
| --- | ---: | ---: | --- | --- | ---: | ---: |
| received | 1 | 2 | found | found | 21364 | 13348 |
| lost | 1 | 2 | error | found | 13060 | 13348 |
| revision | 1 | 2 | error | rejected | 9008 | 13348 |
| digest | 1 | 2 | error | rejected | 11668 | 13348 |
| shipping | 1 | 2 | error | rejected | 524 | 13348 |
| expiry | 1 | 2 | error | rejected | 2328 | 2844 |

Los seis escenarios válidos suman **6 POST y 12 lookups, 2 checkouts aceptados y
4 rechazos históricos**. Incluyendo el intento adicional del fixture inválido,
el total observado es **7 POST y 13 lookups**. Las siete operaciones y hashes de
clave son distintos: no hubo segundo POST por operación ni fallback al ACP público.
El intento inválido adicional tiene state claimed, initial error y lookup
not_found, con permisos false; se conserva en `invalid-expiry-fixture-observation.json`.
No se promovió not_found a ausencia comprobada ni se reenvió esa operación.

En received se cambió la revisión del catálogo después del POST; el lookup
recuperó la prueba original. En lost se retrasó la respuesta tras commit Merchant,
se agotó el timeout Agent y el reinicio solo hizo consultas de recuperación.
Los contadores PostgreSQL de orders, checkout_mutations y
checkout_completion_attempts permanecen cero en ambos clústeres. No se inició PSP;
las fronteras de socket/endpoint del fixture excluyen esas llamadas, sin inventar
un contador de un servicio externo no ejecutado.

Directorios de evidencia real: `C:/ANDES/Tesis/tesis-commerce-agent/.local/worktrees/ec-gov-07b-b-final/.local/07bp-real-4553105e6394`; `C:/ANDES/Tesis/tesis-commerce-agent/.local/worktrees/ec-gov-07b-b-final/.local/07bp-real-c1237a238b5c`.
Cada agent-<escenario>/public-result.json conserva guard, pruebas, trazas y deltas;
merchant-final.json conserva todas las trazas método/path/status, sin headers.
Los POST de lectura de Offer y scope P se distinguen de la creación guardada.
Los cambios de catálogo por RPC/SQL son setup del fixture, no POST comerciales Agent.

Clústeres detenidos, con pg-stopped.json y closed.json en cada directorio:

| Directorio | Puerto PostgreSQL | pg_ctl status exit | Worker exit | postmaster.pid |
| --- | ---: | ---: | ---: | --- |
| `C:/ANDES/Tesis/tesis-commerce-agent/.local/worktrees/ec-gov-07b-b-final/.local/07bp-real-4553105e6394` | 52123 | 3 | 0 | ausente |
| `C:/ANDES/Tesis/tesis-commerce-agent/.local/worktrees/ec-gov-07b-b-final/.local/07bp-real-c1237a238b5c` | 59333 | 3 | 0 | ausente |

El fixture también comprueba cierre de los sockets PostgreSQL y HTTP. El clúster
inicial 50578 quedó detenido tras el fallo de PID. Se conservan los directorios,
sin limpieza destructiva.

Las 51 omisiones offline corresponden a 3 E2E, 6 opt-ins reales 07A, 32 de 06D-C,
4 P y los 6 escenarios B que se ejecutan por separado. La suite general final
sí incluye los 39 tests nuevos y las regresiones locales D1, procedencia, 06D-C,
07A, P y Q. No se alteraron pins/aserciones históricas para forzar otro Merchant.
La única advertencia es la deprecación heredada BlockingPortal de Starlette.
found y rejected tienen evidencia real; not_found también se observó en el
fixture inválido, sin presentarlo como escenario válido de oferta expirada.
unrecoverable y error, y las corrupciones de proof/transporte, se validan en
las pruebas controladas; no se atribuye HTTP real a esos casos unitarios.

## H. Garantías, riesgos y límites para D2-HP

El registro mantiene el perfil Q `test_backend_issuer/1` y declara
`test_backend_receipt_not_remote_authentication`. En el experimento real, el
worker de aprovisionamiento obtiene contexto del catálogo administrado y emite
una sesión Merchant real **del clúster sintético**, antes de P. El registro backend
liga su credencial, emisión, recibo y grant a origen/catálogo/merchant/run/actor/
owner/episodio. No declara autenticación productiva, firma Merchant o atestación
de despliegue; esta última sigue null. Los secretos viajan por canales de control
privados en memoria, nunca como argumentos LLM, evidencias o fixtures versionados.

El Offer P prueba lo observado en su consolidación; no vigencia futura. D17 en
Merchant es quien contrasta revisión y digest bajo la transacción comercial.
Una divergencia no actualiza el sello, el snapshot o la selección para lograr
éxito. El lookup prueba historia, incluso si el catálogo actual cambia después;
no promete que ese checkout continúe siendo utilizable ahora.

POL-01/POL-02 conservan su semántica original sobre las fuentes y hechos D1
aprobados. Revisión, digest y precio completo del Offer quedan ligados por el
contrato auxiliar; no se inventa una regla nueva dentro del artefacto D1. Un
allow no representa consentimiento humano a ese total ni evaluación D2; D17
comprueba la precondición comercial, sin sustituir esa autorización humana.

El límite es **como máximo un envío inicial por operación bajo custodia de la
misma SQLite**. Una caída tras claim puede dejar cero POST o un efecto remoto
desconocido; se acepta ese bloqueo para impedir reenvío. HMAC y triggers son
custodia local, no antirollback externo frente a copias completas del almacén,
control del host o compromiso de las claves. El HTTP loopback del experimento
confía en ese host y en sus procesos controlados; los digests no son firmas
criptográficas remotas.

El código recibe claves persistentes mediante inyección backend; no implementa
un gestor productivo de secretos. En el fixture, el controlador conserva las
claves efímeras en memoria durante el reinicio del worker Agent y las vuelve a
provisionar por el canal privado. Se demuestra ese reinicio, no recuperación
tras perder el controlador y sus claves. Al finalizar se descartan; una revisión
de las evidencias públicas dispone de hashes y pruebas, no de un archivo que
revele esas claves. Una operación backend real debe conservarlas por separado;
su pérdida bloquea la recuperación autenticada.

No se implementa D2-HP ni se fabrica consentimiento humano. Un checkout creado
queda `not_ready_for_payment`; purchases local queda selected, sin order ni
continuación habilitada. Retomar otra fase exige una autorización humana/D2
independiente y una integración revisada distinta. No hay HNP, PSP, completion,
actualización, cancelación o pago, ni enforce habilitado en el runtime público.

## I. Dictamen, hashes y estado Git

**READY_FOR_REVIEW** para revisión independiente del incremento experimental.
Hay nueva D1 aplicada, D17 real y recuperación histórica; no autorización D2-HP
ni habilitación de enforce público.

Worktree absoluto: `C:/ANDES/Tesis/tesis-commerce-agent/.local/worktrees/ec-gov-07b-b-final`.
Rama: `governance/ec-gov-07b-b-d17-integration`.
HEAD: `d47b3da04b42b7c288162b8882b8ae59ef2b778a`.
Origin: `https://github.com/Maldinho18/tesis-commerce-agent.git`.
Ocho archivos de entrega untracked; ningún cambio tracked o staged.
No hubo reset, stash, limpieza destructiva, stage, commit, push, merge o PR.

Estado completo de cambios del worktree:

```text
?? docs/b2-governance/ec-gov-07b-b-d17-integration-results.md
?? src/commerce_agent/acp/guarded_creation.py
?? src/commerce_agent/governance/guarded_creation.py
?? src/commerce_agent/persistence/guarded_d1_sqlite.py
?? tests/governance/test_guarded_d1.py
?? tests/integration/gov07bb_agent_worker.py
?? tests/integration/gov07bb_merchant_worker.py
?? tests/integration/test_guarded_d1_live.py
```

SHA-256 finales (rutas relativas al worktree absoluto anterior):

| Archivo | SHA-256 |
| --- | --- |
| `src/commerce_agent/acp/guarded_creation.py` | `55196a7d1098000bfc22a6d752ac5055e271139131a5969ba50ac555cc5405b0` |
| `src/commerce_agent/governance/guarded_creation.py` | `b8fab90870072fa3e459c81c58b2c68fa0d966613f3f2295f9ed636bf2c25730` |
| `src/commerce_agent/persistence/guarded_d1_sqlite.py` | `cdb832f0243cdd6886c546c598b2844d97c70701347aab2aaf0d45cb60dbae4f` |
| `tests/governance/test_guarded_d1.py` | `9654d86966bb50f7dedabb3291fe1790ed56fea7589dd8799934badc294c9339` |
| `tests/integration/gov07bb_agent_worker.py` | `6f4f867eb3e634a8ec3c373bdedf3a73996a231cd99e59ba5e8290f7fab82839` |
| `tests/integration/gov07bb_merchant_worker.py` | `812e7c3e7023d0ddc512491e470dc9b69e3f72e1e69b1393cb7eb6cdb7629890` |
| `tests/integration/test_guarded_d1_live.py` | `193a9bcc9ca410bc87d2950fcaca662a271952c48adb4c5cee4ce026922f76b0` |

El hash del informe se guarda fuera de su contenido en
`.local/07bb-validation/report-sha256.txt`; `delivery-sha256.json` incluye las
ocho rutas absolutas y hashes. `evidence-sha256.json` identifica los resultados
públicos, logs y runners; no se usa un hash autorreferencial del informe.

Custodia contra baseline previo a implementación:

| Árbol | Archivos | SHA-256 agregado | Cambios |
| --- | ---: | --- | --- |
| `C:\ANDES\Tesis\tesis-commerce-agent` | 1452 | `9e3141d186222b2ed2ff92dc4d1bab0d0856296d9db64fcff5eee5d159a01de6` | 0 |
| `C:\ANDES\Tesis\tesis-commerce-merchant` | 287 | `c7d1632c7cba0d0e495e30728e352e7c72a4f47b4354725668c13e78efece813` | 0 |
| `C:\ANDES\Tesis\tesis-commerce-lab` | 147 | `bb1866bf248a06afb535f4fe0f328a1a637fca0d49f89be2042d3924272b3ba8` | 0 |
| `C:\ANDES\Tesis\tesis-payment-provider-sandbox` | 17 | `cbf4083c8efe7ae789f16653ee6e4c8c5533e635f0a2b41bf3a777cf7d67428f` | 0 |
| `C:\ANDES\Tesis\tesis-commerce-agent\.local\worktrees\ec-gov-07b-b` | 336 | `17ab553554a9666fff96bb00c2f363e3ac9a38bdf898b3e3c240dc9bcdc44815` | 0 |
| `C:\ANDES\Tesis\tesis-commerce-agent\.local\worktrees\ec-gov-07b-p` | 343 | `006e537fadb2ea830e459661c789eec01d048d3ff423eee98f6580e471733305` | 0 |
| `C:\ANDES\Tesis\tesis-commerce-agent-hnp` | 317 | `659dc6ee030667ce70c78360d5f962dcbf534f5eae1c0461a753dfee37707dd5` | 0 |
| `C:\ANDES\Tesis\tesis-commerce-agent\.local\worktrees\ec-gov-07b-q` | 349 | `b8481bc4b0b758305af8afa56a8c5a2a128ce0e635e77216aee38a6a215738d9` | 0 |

El agregado usa líneas ruta_relativa:sha256 ordenadas, unidas con LF sin LF
terminal; también se compara cada archivo individual. Son 3248 archivos: todos
los tracked de los ocho árboles, informes 07B previos y los 1116 del B0 original
.local/gov05c. No es un sellado de todos los ignorados del disco ni se leen .env
operativos. Los archivos versionados de Lab quedan incluidos, independientemente
del colaborador que los haya creado. No hubo escrituras ordinarias fuera del
nuevo worktree; la creación Git autorizada sí añadió referencias y metadatos de
worktree comunes. `main-ignored-status.txt` y `worktree-ignored-status.txt`
conservan los inventarios de ignorados. Scratches y clústeres nuevos están aquí.

Referencias y estados previos conservados, sin refrescar índices:

| Árbol | HEAD | Rama | Estado |
| --- | --- | --- | --- |
| `C:\ANDES\Tesis\tesis-commerce-agent` | `35fd2d80e6f10150d5f0651b386adaa4eff2d40e` | `governance/ec-gov-07b-d17-offer-freshness` | `?? docs/b2-governance/ec-gov-07b-d17-offer-freshness-results.md`; índice vacío |
| `C:\ANDES\Tesis\tesis-commerce-merchant` | `9743c9f179c7ad5413e8818766ef3dceb326a917` | `merchant/ec-mer-07b-a2-readonly-guard-proof` | `limpio`; índice vacío |
| `C:\ANDES\Tesis\tesis-commerce-lab` | `b3be474e26ebbedc79dd4f3c3b133f42491f9542` | `main` | `limpio`; índice vacío |
| `C:\ANDES\Tesis\tesis-payment-provider-sandbox` | `ee19c06ecae3038b43d1fc7cb7a8822592fa1a08` | `main` | `limpio`; índice vacío |
| `C:\ANDES\Tesis\tesis-commerce-agent\.local\worktrees\ec-gov-07b-b` | `35fd2d80e6f10150d5f0651b386adaa4eff2d40e` | `governance/ec-gov-07b-b-d17-agent-integration` | `?? docs/b2-governance/ec-gov-07b-b-d17-agent-integration-results.md`; índice vacío |
| `C:\ANDES\Tesis\tesis-commerce-agent\.local\worktrees\ec-gov-07b-p` | `c145e97f8d34ed62fb6cef89d9896348c8125c31` | `governance/ec-gov-07b-p-preselection-authority` | `limpio`; índice vacío |
| `C:\ANDES\Tesis\tesis-commerce-agent-hnp` | `a633a043337286406f9a3e859809556ce46bcba1` | `hnp/ec-hnp-01-review-harness` | `limpio`; índice vacío |
| `C:\ANDES\Tesis\tesis-commerce-agent\.local\worktrees\ec-gov-07b-q` | `d47b3da04b42b7c288162b8882b8ae59ef2b778a` | `governance/ec-gov-07b-q-preselection-d1-binding` | `limpio`; índice vacío |

Logs/manifiestos citados: directorio absoluto
`C:/ANDES/Tesis/tesis-commerce-agent/.local/worktrees/ec-gov-07b-b-final/.local/07bb-validation`.
Las claves del fixture no forman parte de los manifiestos públicos.
