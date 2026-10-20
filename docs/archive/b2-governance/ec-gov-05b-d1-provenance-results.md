# EC-GOV-05B — Procedencia operativa D1

Dictamen del incremento aislado: **READY_FOR_REVIEW**.
Uso operativo: pendiente de claves/configuración confiables y del enlace EC-GOV-05C.
No se activó shadow ni se seleccionó motor definitivo; D11 permanece abierto.

## Base y alcance

Windows, 2026-10-14; Python 3.12.6 y SQLite 3.45.3.
Rama verificada inicialmente limpia: `governance/ec-gov-05b-d1-provenance`.
HEAD esperado y observado: `9218d4b596e0748e4d29757d917a65495b1d1ee2`.
No se cambió rama ni HEAD. No commit, push, merge, rebase ni PR.

Se inspeccionaron los informes EC-GOV-05/05A, policy-v1, policy-acceptance-v1,
roadmap, intent.py, selection.py, feed.py, purchase.py, conversation.py,
config.py, runtime.py, ambos stores SQLite existentes y los contratos,
canonicalización, evaluador y artefactos del ledger. El código vigente confirma
que state_version ya existe, pero no hay captura D1 integrada en PurchaseService.

## A. Mapa de hechos D1

El registro prepara fuentes y contexto. **No existe un ensamblador de producción
de SelectionPolicyInput en este incremento**. Los hechos ligados a la selección
comercial futura se mantienen pendientes, sin sustituirlos por un fixture.

| Hecho | Fuente y representación | Versión, digest y recuperación |
| --- | --- | --- |
| schema_version | Contrato aprobado commerce-policy/1.0 | Literal normativo existente; no una revisión comercial inventada |
| decision_id | Debe emitirlo el backend por evaluación lógica en 05C | Pendiente del registro de la operación/evaluación; no se fabrica en esta capa |
| run_id | PK existente de purchases; se valida Id y existencia | Episode y FK de fuentes a esa compra; no se crean compras desde procedencia |
| trace_id | Emisión backend con secrets.token_hex, guardada una vez por run | Episode append-only; mismo trace tras retry/reapertura |
| owner_ref | HMAC con clave y dominio owner sobre el owner de purchases | Cada operación contrasta el owner autenticado suministrado por backend con la fila; no autentica un JWT por sí misma |
| purchase_mode | Contexto HP de esta implementación | ContextData registrado; no soporte HNP implícito |
| governance_mode | D1Configuration explícita para preparación shadow | Contexto registrado; sin cambio de Settings/runtime ni activación comercial. Off evita la fábrica completamente |
| phase / action | Punto D1 / prepare_checkout del contrato | Normativos; el enlace con esa operación real corresponde a 05C |
| context_version | Revisión del snapshot de configuración y sus fuentes | RegisteredContext.ref.version; recuperar con recover_context |
| expected_local_state_version | SELECT state_version FROM purchases | observe_context devuelve el entero SQL actual; no contador de fuentes/ledger |
| expected_policy | D1PolicyArtifact construido desde configuración confiable | PolicyBinding verificado por bytes; registro global set/versión; recover_context y public_source(policy) |
| authority_now | ClockReader: system_utc o callback explícitamente identificado | UTC Z/milisegundos; perfil registrado y contrastado; lectura tipada, no fecha del usuario |
| commercial_now | Sistema, fixed_sandbox explícito o callback identificado | Perfil separado; se rechazan clocks ingenuos o perfil incompatible |
| clock_profile_ref | ClockProfile estricto | Fuente recuperable clock con versión y digest; no un nombre sin contenido |
| experiment_run_id | Argumento backend explícito de register_episode | Id validado o None declarado no aplicable; retry incompatible se rechaza |
| conversation_id | purchase_graph_threads.thread_id realmente asociado | Referencia HMAC por owner/thread, conservada en episode; no se exporta thread privado |
| intent.intent_ref | PurchaseIntent validada proporcionada por backend | Snapshot privado cifrado, vínculo HMAC público, ObjectRef; recover_intent |
| intent.currency | hard.currency COP o perfil COP explícito si falta | USD se rechaza para D1; no conversión de importes ni moneda |
| intent.max_unit_minor / max_total_minor | Enteros originales de HardConstraints | Sin conversión; None conserva ausencia explícita HP. Snapshot completo recuperable |
| intent.quantity | Perfil COP/una unidad registrado | Constante normativa 1, no campo inventado en PurchaseIntent |
| intent.hard_constraint_refs | Categorías, condiciones, opciones y términos estructurados | IDs HMAC por restricción tipada; recover_constraint devuelve el contenido privado y comprueba ámbito |
| intent.normalization_version | ObjectRef del manifiesto de normalización | Token id:vN resuelve exactamente la revisión guardada; no normalizer-1 ficticio |
| feed_snapshot_ref | ProductFeed exacto recibido del backend | JSON del modelo completo cifrado, incluida metadata/extra/listas y floats finitos; recover_feed |
| eligibility_algorithm_ref | Manifiesto medido de fuentes, lock y dependencias | Bytes archivados recuperables; public_source(algorithm), ObjectRef y digest |
| eligible_variant_ids | Mismo SelectionResult.ranked utilizado en la operación | Pendiente de 05C; no se vuelve a consultar ni rankear desde este módulo |
| selected_variant_id | Variante validada en esa operación por PurchaseService | Pendiente de 05C; no se reemplaza silenciosamente por selection.selected |
| merchant_ref | HMAC del merchant_base_url confiable y validado | Configuración comercial cifrada recuperable internamente; mismo ref en allowlist del artefacto, nunca URL del feed |

Los tiempos observados serán conservados en la entrada/evidencia por 05C; esta capa
todavía no registra una evaluación comercial ni promete replay de un reloj externo.
Los modelos reales se revalidan **antes** de serializarlos, preservando los tipos
Python para detectar bool en campos enteros y atributos extra introducidos con
model_copy. Feed extra=allow se conserva como parte del snapshot privado y no se
convierte en un modelo enviado al PDP.

## B. Arquitectura y reglas de versionado

- governance/provenance.py: contratos estrictos, claves inyectadas, configuración
  D1, perfiles/lectores de reloj, contexto, observación y constructor de política.
- governance/provenance_artifacts.py: captura de fuentes y verificación de
  dependencias instaladas; no ejecuta código archivado ni descarga artefactos.
- persistence/provenance_sqlite.py: registro, recuperación, asociación run/owner,
  proyección IntentFacts y cifrado de payloads operativos.
- tests/governance/test_provenance.py: pruebas con modelos comerciales reales,
  feed cargado mediante load_product_feed, ranking real y fuentes locales medidas.

Para cada compra/owner/kind/slot se emite una identidad estable mediante HMAC.
La primera captura es revisión 1: significa primer objeto efectivamente registrado
en ese slot, no su número de cambios anteriores ni una versión remota del feed.
Repetir los mismos bytes de la última captura conserva ObjectRef; cambiar contenido
añade revisión anterior +1. Volver a un contenido anterior añade otra revisión,
aunque el digest de ese contenido pueda coincidir con uno histórico. Los originales
siguen recuperables. Se verifica la última captura antes de permitir otro registro,
de modo que corrupción no se interprete como una versión nueva legítima.

Las restricciones usan identidad HMAC del contenido tipado y ámbito: cambiar una
restricción crea una identidad distinta; cada identidad es inmutable/revisión 1.
Se exportan referencias únicas ordenadas; el snapshot operativo mantiene el orden
y las duplicaciones originales de las secuencias del modelo.

El artefacto de política utiliza una regla distinta y explícita: namespace y
revisión de release suministrados por configuración confiable. No hay defaults de
despliegue. En toda la base, un mismo policy_set_id/version identifica exactamente
los mismos bytes. Otro contenido necesita una revisión superior o un namespace
distinto. Recuperar un contexto histórico no cambia su binding.

## C. Persistencia y privacidad

Cuatro tablas aditivas, inicializadas solo mediante llamada explícita:

| Tabla | Contenido |
| --- | --- |
| d1_provenance_schema | Versión 1 del esquema, key_id y comprobación HMAC de las claves suministradas |
| d1_provenance_episodes | run existente, owner_ref, trace estable, conversación opaca y experimento opcional |
| d1_provenance_sources | PK source_id/version, FK run/owner, kind, digest, cuerpo público seguro, nonce y ciphertext |
| d1_provenance_policies | PK policy_set_id/version, digest y bytes públicos del artefacto verificado |

Se requieren conexión del llamador, foreign_keys=ON y transacción explícita.
No se abre una conexión ni se hace commit en el adapter. SAVEPOINT por operación
revierte escrituras parciales si hay error, incluso si el llamador lo captura.
Triggers rechazan UPDATE, DELETE y reemplazo de identidades ya registradas.
No son una protección contra un administrador que modifique el esquema.

Decisión de privacidad: SHA-256 del contenido privado en evidencia permitiría
probar diccionarios; se rechaza esa alternativa. Solo HMAC más SQLite en claro
dejaría los snapshots expuestos a lectores del archivo. Se usa HMAC con separación
de dominio y cifrado de payloads con AES-256-GCM de cryptography **ya instalada**.
No se añadieron dependencias. Claves independientes: HMAC >=32 bytes, AES 32 bytes.
El nonce se genera criptográficamente en backend; AAD vincula run, owner, kind y
ObjectRef. La clave nunca entra en la base, fixtures persistidos ni ledger.

ObjectRef.digest de snapshots privados es SHA-256 de una proyección pública con
schema, kind, key_id, ámbito opaco y vínculo HMAC; no del texto privado. Recuperar
verifica digest público, AEAD y HMAC del payload descifrado. Fuentes públicas de
código, perfil temporal, contexto y política usan sus propios bytes recuperables;
el digest de política coincide exactamente con PolicyBinding.artifact_digest.

Se preserva el **modelo ProductFeed usado por ranking**, no el formato ni los
bytes originales del transporte HTTP/JSONL. El serializer operativo privado admite
floats finitos de campos extra; no se cambia commerce-canonical-json/1 del PDP.
No se lee ni copia purchases.request_text. Descripciones, términos y atributos
adversariales del feed/intención quedan exclusivamente en payloads cifrados.
IntentFacts y DecisionEvidence reciben referencias y hechos estructurados seguros.

**No hay cifrado integral de SQLite, ACL nuevas ni gestión de claves implantada.**
La base B0 conserva su almacenamiento previo de owner/request. El cifrado nuevo
protege los payloads de procedencia; no borra ni cifra otras tablas, metadatos o
memoria del proceso. La provisión, custodia, backup, retención y rotación de claves
siguen pendientes. Perder/cambiar claves sin migración provoca rechazo, no regeneración
silenciosa. En tests se inyectan claves efímeras generadas, sin leer .env.

## D. Política, algoritmo y reproducibilidad medidos

build_d1_policy construye exclusivamente POL-01/POL-02, prepare_checkout, merchant
configurado, COP y cantidad 1. Se serializa, calcula binding y ejecuta verify_artifact;
la recuperación verifica de nuevo bytes y registro global. No se carga d1-policy.json
ni se instala un artefacto de producción desde un fixture. El operador todavía debe
proporcionar namespace/revisión, destino comercial y claves confiables reales.
Python continúa como referencia experimental; OPA sigue candidato no seleccionado.

Archivos que acreditan ranking/proyección: domain/intent.py, domain/selection.py,
domain/__init__.py, acp/feed.py y el schema.feed.json ACP congelado.
El manifiesto conserva su contenido, uv.lock completo, Python/implementación,
versión Unicode y diez dependencias instaladas. Sus versiones se contrastan con
el lock; se leen los archivos listados en RECORD y se verifican sus SHA-256 reales.
No se incluyen .pyc ni se afirma atestación del intérprete/proceso. Los binarios de
dependencias no se archivan: queda su identidad verificada, no una distribución
autocontenida ejecutable. Recuperar el manifiesto no requiere ejecutar ese código.

| Dependencia medida | Versión |
| --- | --- |
| pydantic / pydantic-core | 2.12.5 / 2.41.5 |
| annotated-types / typing-extensions / typing-inspection | 0.8.0 / 4.16.0 / 0.4.4 |
| jsonschema / attrs | 4.26.0 / 26.1.0 |
| jsonschema-specifications / referencing / rpds-py | 2025.9.1 / 0.37.0 / 2026.6.3 |

Perfil explícito project-source-utf8-lf/1: sustituye CRLF por LF **al capturar**,
rechaza CR suelto y decodificación no UTF-8. No reescribe archivos ni usa read_text
con una normalización implícita no documentada. Los cuatro Python originales
están en CRLF en este Windows; su captura normalizada coincide exactamente con
los blobs Git. schema.feed.json ya está en LF. No se modificó .gitattributes ni
core.autocrlf, ni se renormalizó el repositorio.

| Manifiesto real | Bytes | SHA-256 |
| --- | ---: | --- |
| Elegibilidad | 242373 | 770271e14547e5b1ad8cc705930b91dfeb269d00e8e3fbda79dafae7bbe6e20c |
| Normalización | 201456 | 3c904a5cb01f156ee9fe6125e48c32e531420f4a448f0420cbf091dc68d5fa43 |

Validación real solo en Windows. Se probó LF/CRLF y equivalencia con blobs Git;
no se ejecutó Linux. Los hashes de dependencias binarias pueden diferir entre
plataformas: se identifican entornos distintos, no se promete un digest global
idéntico para instalaciones con binarios diferentes.

## E. Pruebas ejecutadas

42 casos nuevos, ninguno omitido. Cobertura de los quince requisitos del ticket:

| Requisito | Evidencia |
| --- | --- |
| Intención/feed registrados y recuperados | Modelos reales, igualdad completa; ranking recuperado igual al original; mutación posterior no cambia archivo histórico |
| Versiones y digest | Snapshot cambiado, retry idéntico, reversión histórica, configuración/perfil cambiado |
| Relaciones run/owner/fuentes | Otra compra del mismo owner, otro owner, compra ausente y cambio posterior de propietario rechazados |
| Correlación en retry | Trace y contexto estables tras commit/reapertura; experimento incompatible rechazado |
| Algoritmo/normalización | Fuentes y diez dependencias reales medidas; deriva de código/dependencia simulada y detectada sin editar archivos |
| Política válida/inválida/alterada | Binding correcto, versión/set distinto, bytes alterados y conflicto global entre compras |
| Relojes/perfiles | Sistema real, callbacks identificados, sandbox fijo, UTC milisegundos; naive/mismatch rechazados |
| Privacidad adversarial | PII/token sintéticos en modelos; ausentes de proyección/ciphertext/logs/evidencia; restricciones recuperables solo por resolver autorizado |
| Ledger/contratos | Evaluador real de referencia + DecisionRecord + registro SQLite existente con refs de estas fuentes |
| Off | Fábrica prohibida nunca invocada; B0 funciona sin tablas/llaves de procedencia |
| state_version/B0 | Observación lee versión SQL que cambia por update B0; no incrementa por observar; suite CAS existente completa |
| Fallos/corrupción/recuperación | Error de INSERT, rollback, reapertura con claves correctas; ciphertext/nonce/digest/proyección corruptos y clave errónea rechazados |
| APIs estrictas | Extra, cantidades/modos/monedas no soportadas y enteros coercibles rechazados; revalidación previa a serializar |

La prueba de compatibilidad arma **solo en tests** una entrada D1 a partir de su
operación sintética con rank_candidates real, snapshots recuperables y relojes
etiquetados. Persiste/reproduce evidencia con el ledger existente. No es un episodio
del runtime ni una compra integrada con Merchant/PSP ni una prueba de shadow real.
El archivo feed fixture aporta datos de prueba al loader real; los artefactos de
código y dependencias se midieron en esta instalación y no son manifiestos ficticios.

| Comando | Resultado final real |
| --- | --- |
| uv run pytest tests/governance/test_provenance.py --junitxml=.local/gov05b/unit3.xml | 42 passed in 9.72s; código 0 |
| uv run ruff format --check . | 127 files already formatted; código 0 |
| uv run ruff check . | All checks passed!; código 0 |
| uv run pyright | 0 errors, 0 warnings, 0 informations; código 0 |
| uv run pytest --junitxml=.local/gov05b/full.xml | 501 passed, 3 skipped, 1 warning in 45.56s; código 0 |
| git diff --check | Sin diagnósticos; comprobación adicional de archivos nuevos con índice temporal |

Regresiones aprobadas dentro de la suite: D1 128, ledger 92, revisión/CAS 36,
PurchaseService 10, approval 6, completion 12 y completion_client 1; además ACP.
Las únicas omisiones son los tres E2E conocidos que requieren Merchant/PSP/PostgreSQL.
Advertencia heredada: anyio.abc.BlockingPortal deprecated en Starlette TestClient.

Las primeras ejecuciones detectaron corrupción de digest tratada como nueva captura,
una prueba que preparaba evidencia dentro de transacción y coerción bool→int por
serialización de un model_copy inválido; se corrigieron sin debilitar aserciones.
También se resolvieron diagnósticos de Ruff/Pyright en los módulos/tests nuevos.
Todos los controles finales de la tabla se ejecutaron sobre la versión corregida.

Se utilizó UV_NO_SYNC/UV_OFFLINE, el entorno existente y temporales .local/gov05b.
Se desactivaron E2E/live calls y se retiraron del entorno del proceso de tests las
variables de credenciales de servicios, sin mostrar valores. No se abrió una base
operativa ni se ejecutaron servicios externos o el baseline/piloto B0.

## F–I. Garantías, pendientes y preservación

Preparado: snapshots recuperables, integridad/autenticidad de payloads privados,
relaciones run/owner, correlación persistente, perfiles de fuentes/tiempo,
configuración D1 verificable, proyección IntentFacts y lectura de revisión real.
La observación no aplica CAS ni valida una transición ni elimina TOCTOU.

Pendiente para uso operativo y 05C:

1. Provisionar claves independientes y su lifecycle; verificar almacenamiento,
   ACL/backup/retención. No existe aprovisionamiento real ni se eligió una clave aquí.
2. Suministrar/revisar configuración y namespace/revisión de política reales;
   enlazar destino y callbacks de tiempo con el backend autenticado vigente.
3. Capturar exactamente intención/feed/SelectionResult/elección de la misma operación,
   impedir mutación entre ranking y snapshot, emitir decision_id durable por evaluación
   y fijar el contexto que se recupera al reintentar. No registrar otro ranking como
   si fuera el utilizado por PurchaseService.
4. Conectar observación shadow sin bloquear/cambiar B0 ante fallos de inicialización,
   captura, motor o evidencia; preparar la admisión del ledger fuera de transacción.
5. Preparar fuentes/medición fuera de la ruta comercial y revisar costos de I/O,
   límites de tamaño/retención y contención antes de integrarlas. El registro actual
   es setup explícito con transacción; no se declara un SLO ni un benchmark comercial.
6. Para enforcement futuro, compartir UoW/CAS/evidencia y revalidar autoridad/estado.
   Ninguna lectura independiente de state_version concede autorización comercial.

No se implementaron D1 shadow, D2, D3, HNP, D17, OPA ni un motor definitivo.
No se cambiaron contratos aprobados, Settings/runtime, PurchaseService, selección,
aprobación, pagos, completion, SQLite B0/CAS, protocolos, fixtures ni dependencias.
Los **287 archivos previamente versionados conservan exactamente su SHA-256**.
Incluye resultados históricos B0, uv.lock, fuentes ACP y el fingerprint Python.
Otros repositorios permanecieron fuera del trabajo.

Cinco archivos nuevos: tres módulos, un módulo de pruebas y este informe.
Índice real sin staging; sin commit/push/merge/PR. Detenido para revisión humana.
