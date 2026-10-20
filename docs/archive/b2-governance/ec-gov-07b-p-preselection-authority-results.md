# EC-GOV-07B-P — Observación autoritativa anterior a una nueva selección

> **Nota 2026-10-20.** Rutas históricas, no se reescriben. Los módulos live de `tests/integration/`
> se reemplazaron (`test_preselection_merchant.py` → `test_live_07bp_preselection.py`) y sus workers
> y arneses `gov*` se retiraron
> ([equivalencias](../../../tests/integration/README.md#equivalencia-con-la-suite-anterior)).

## A. Preflight y aislamiento de worktrees

16 de octubre de 2026, Windows. Agent base exacta
`35fd2d80e6f10150d5f0651b386adaa4eff2d40e`; referencia base
`governance/ec-gov-07a-hp-initial-dispatch` comprobada en ese SHA. Origin:
`https://github.com/Maldinho18/tesis-commerce-agent.git`.
Merchant limpio en `9743c9f179c7ad5413e8818766ef3dceb326a917`.

La rama solicitada no existía. Se creó desde el commit exacto:
`governance/ec-gov-07b-p-preselection-authority`, worktree
`C:/ANDES/Tesis/tesis-commerce-agent/.local/worktrees/ec-gov-07b-p`.
El primer `git worktree add -b ...` falló por permiso al crear el lock de la
referencia; repetir exclusivamente esa operación con permisos ampliados tuvo
éxito. No hubo rechazo de revisión automática, reset, stash o cambio de checkout.

Árbol principal: rama `governance/ec-gov-07b-d17-offer-freshness`, con su informe
local previo. 07B-B: worktree `.local/worktrees/ec-gov-07b-b`, mismo SHA base,
con su informe bloqueado. HNP: `tesis-commerce-agent-hnp`. Todos se conservaron.
Se leyó el informe 07B-B completo; no se copiaron sus resultados como pruebas nuevas.

Antes de ejecutar pruebas se guardó un baseline de bytes de los seis árboles
protegidos y del histórico `.local/gov05c`. Inventario: Agent principal 1452
archivos, Merchant 287, Lab 147, PSP 17, 07B-B 336, HNP 317. El mapa está en
`.local/07bp-validation/baseline.json`; el inventario del principal incluye sus
335 archivos versionados, el informe previo y 1116 archivos históricos B0.

## B. Análisis del bloqueo y origen de las ofertas

Hechos inspeccionados en los SHAs congelados:

| Eslabón | Contrato y límite comprobados |
| --- | --- |
| `ProductFeed.candidates` | Proyección de Product/Variant: IDs, títulos, URLs, precio observado, moneda, disponibilidad resumida, categorías, marcas, condiciones y opciones. No incluye revisión, envío, total, destino, días de entrega o expiración Offer. |
| `PurchaseIntent` / `rank_candidates` | Restricciones y ranking aprobados sobre la proyección del feed. El ranking no valida el total autoritativo. No se modifican sus fuentes o artefactos. |
| `PurchaseService.start` | Recibe `selected_variant_id` desde una elección previa; no puede convertir una consulta posterior en observación anterior a esa elección. Permanece intacto. |
| D1 capture / source refs | Congela feed e intento; el vault SQLite conserva el feed validado con cifrado y referencias verificables. Su integridad no reconstruye términos ausentes del feed. |
| Vault / CAS / ledger / UoW | Vinculan decisión, contexto, fuentes, transición y reserva durables. La evidencia experimental nueva no se inserta ni se presenta como un veredicto de ese motor. |
| 07A | Envelope durable conserva bytes ACP, contexto, evidencia, clave y ruta literal `/checkout_sessions`; commit y claim anteceden el primer despacho. No cambia su formato ni transporte. |
| Merchant `current_feed` | `/feeds/current` publica exclusivamente el run administrado `variant='merchant'`. `build_feed` es una proyección no inyectiva. |
| Merchant `/lab/offers/get` | Autentica el Bearer, resuelve run/actor y lee `catalog_offers` del episodio autenticado. Devuelve `Success[Offer]` o `Failure`; registra `offer.read`. Es lectura comercial observable, no transacción PostgreSQL enteramente READ ONLY. |
| Aprovisionamiento | `managed_catalog_context` obtiene el run/actor administrado. `scripts/railway_bootstrap.py` puede asociar una credencial a ese contexto. La mera existencia del script no acredita la configuración desplegada ni una credencial operativa actual. |
| D17 publicado | Creación privada `merchant-guarded-acp-create/1` y lookup `merchant-guarded-creation-lookup/1`; guard exige revisión y digest de Offer completa. No producen retrospectivamente la observación inicial que faltaba. |

**No se presume equivalencia por ID.** La prueba nueva con Merchant y PostgreSQL
aislados creó el catálogo administrado y otro episodio B2 con Offer IDs comunes.
La credencial del segundo episodio pudo leer el mismo ID con expiración distinta;
la comprobación de ámbito la rechazó. La credencial emitida desde el contexto
administrado sí observó y vinculó las 285 ofertas disponibles del feed publicado.
Esto acredita ese entorno descartable, no la sesión de una instalación operativa.

No inyectividad probada con las funciones reales Merchant en un intérprete
separado: incrementar revisión y envío/total conserva el feed completo pero cambia
el SHA-256 Offer. El espejo independiente Agent produce ambos digests idénticos
a Merchant. Las pruebas unitarias también cubren cambios de expiración y cambios
de digest manteniendo revisión. Términos nuevos pueden observarse antes de una
selección nueva; nunca se atribuyen al feed original o a una selección antigua.

Alternativas evaluadas: enriquecer el feed requeriría otro contrato Merchant y no
acreditaría por sí mismo el ámbito autenticado; añadir una lectura dentro de
`start` después de recibir la elección mantendría el defecto temporal; ampliar
directamente D1/envelopes mezclaría dos incrementos y expondría historia aprobada.
Se eligió una interfaz nueva de preselección y un almacén auxiliar dedicado para
demostrar el orden sin cambiar esos contratos. La vinculación transaccional con D1
sigue siendo trabajo posterior explícito.

## C. Contrato de observación autoritativa

Contratos nuevos, estrictos y versionados:

- `commerce-preselection-episode/1`: episodio UUID nuevo, owner backend, binding
  de catálogo/sesión, feed completo validado, PurchaseIntent completo, destino y
  momento de descubrimiento. No recibe una variante ya seleccionada.
- `commerce-authority-observation/1`: UUID de observación, episodio y digest de
  episodio, candidato exacto, Offer completa validada con defaults, cantidad 1,
  hora local de recepción y Request-Id Merchant. Incluye revisión, actividad,
  cantidad disponible, precio de ítems/unidad, envío, total, moneda, destino,
  condiciones, días de entrega, observación de origen y expiración.
- `commerce-authority-selection/1`: episodio, conjunto completo de observaciones
  de candidatos disponibles, dos respuestas de comprobación de ámbito,
  comprobación final, nueva selección, tiempos de consolidación/sellado y control
  complementario de términos. `human_approval=false`,
  `commercial_continuation=false` son literales obligatorios.

`CatalogBinding` es una **afirmación de registro backend**, no una firma Merchant:
origen HTTP exacto, merchant_id, catálogo `merchant`, feed_id conocido y
SessionScope con run/actor y referencias de aprovisionamiento. La aplicación que
ensamble el experimento debe resolver esas referencias contra el registro de
emisión real. No se implementa una API que permita al LLM o comprador crearlas.
Sin evidencia de registro no debe ensamblarse el cliente. En la prueba real el
worker Merchant crea un recibo de aprovisionamiento sin secretos, conservado con
digest en `catalog-provisioning-receipt.json` y vinculado a la selección sellada.

Con el mismo Bearer, antes y después de las lecturas Offer, se hace un challenge
aleatorio al lookup histórico **existente** de 06D, sin creación ni envelope
ficticio. Solo se acepta `not_found` con challenge, Request-Id, run y actor exactos.
Ese resultado se usa exclusivamente para atribuir el ámbito autenticado; no prueba
ausencia comercial ni concede permiso de envío. Ambos resultados completos quedan
en el sello. Se confía en la estabilidad del aprovisionamiento y en el servidor
Merchant durante la captura; no se promete atomicidad remota entre esos requests,
ni una prueba criptográfica remota de la lectura. El Offer endpoint no devuelve
por sí mismo run/actor ni firma de observación.

Snapshot: perfil literal `merchant-offer-snapshot/1`; revalidación completa de
Offer, defaults incluidos, JSON UTF-8 con claves ordenadas y arrays sin reordenar,
enteros seguros, rechazo de claves sensibles, dominio
`merchant-offer-snapshot/v1` + NUL y SHA-256 hexadecimal. Se conservan **snapshot y
digest**, no una sustitución del primero por el segundo. Ningún proceso Agent
importa Merchant o abre su DB; el espejo de contrato es local e independiente.

## D. Implementación y persistencia

| Archivo nuevo | Responsabilidad |
| --- | --- |
| `src/commerce_agent/acp/offer_snapshot.py` | Espejo estricto Offer congelado y digest compatible. |
| `src/commerce_agent/acp/authority_observation.py` | Descubrimiento y lecturas privadas acotadas, registro backend y comprobación de ámbito. |
| `src/commerce_agent/governance/preselection.py` | Flujo nuevo de observación → compatibilidad → selección, sin puertos comerciales. |
| `src/commerce_agent/persistence/preselection_sqlite.py` | SQLite auxiliar, claim durable único, snapshots append-only, sello y recuperación. |
| `tests/governance/test_preselection.py` | Pruebas unitarias/adversariales, SQLite real, concurrencia y proceso de reinicio. |
| `tests/integration/gov07bp_merchant_worker.py` | Proceso extranjero con Python Merchant y acceso exclusivo a su clúster descartable. |
| `tests/integration/test_preselection_merchant.py` | Clúster aislado y cuatro pruebas de Merchant real mediante transporte controlado a ASGI. |
| Este informe | Evidencia, límites y dependencia futura. |

SQLite separado: tablas `pa_episodes`, `pa_observations`, `pa_selections`.
`initialize` rechaza una DB que contenga tablas ajenas **antes de migrar/escribir**;
el test histórico conserva exactamente sus bytes. No hay migración de bases D1,
operativas o históricas. Triggers impiden cambiar/borrar snapshots o decisiones.
Cada payload canónico lleva HMAC-SHA256 con dominio de tipo; una alteración aun
tras quitar un trigger es detectada al recuperar. Pérdida/cambio de clave bloquea.
La clave de custodia debe conservarse por el backend entre procesos; no se guarda
en evidencia pública. HMAC representa custodia local, no firma Merchant.

Estados: `open → capturing → sealed|blocked`. El claim se confirma antes del
primer request autenticado. Otra captura concurrente, repetición tras sellado o
reintento después de un crash no recupera el permiso de leer/seleccionar otra vez.
Los snapshots parciales permanecen como historia; sin sello no se devuelven como
selección válida. La recuperación revalida firmas, modelos, binding, conjunto de
observaciones, ranking y tiempos, en una transacción local consistente; no hace HTTP.

Límites: origen HTTP(S) normalizado sin path, userinfo, query, fragmento o escapes;
HTTP remoto no loopback rechazado; `trust_env=False`, sin redirecciones. Requests
fijados a tres rutas; feed hasta 8 MiB y 512 candidatos disponibles, Offer hasta
64 KiB y scope challenge hasta 4 KiB. Timeout HTTP configurable (5 s por defecto,
máximo 30 s), más deadline acumulado del stream para evitar goteo indefinido.
Errores sanitizados sin cadenas de excepción; payloads con claves sensibles o
reflejos de la credencial se rechazan antes de conservarlos.

## E. Procedencia temporal y binding con selección

Secuencia ejecutable, sin reutilizar una selección anterior:

1. `begin(owner_id, intent, destination)` descubre el feed y abre un UUID nuevo.
2. `capture` reclama durablemente el episodio y comprueba el ámbito de la sesión.
3. Obtiene cada Offer disponible, registra su snapshot y verifica su proyección
   **completa** contra el candidato: producto/variante, títulos, URLs, precio,
   moneda, disponibilidad, taxonomías, marcas, condiciones y opciones.
4. Comprueba merchant, destino, actividad, stock y ventana temporal; repite el
   scope challenge y la comprobación de todas las ofertas.
5. Ejecuta `rank_candidates` intacto. El resultado es provisional hasta comprobar
   el total autoritativo contra `max_total_amount_minor`. Consolida la nueva selección.
6. Persiste el conjunto sellado y verifica su recuperación independiente.
7. Cualquier D1/D17/despacho futuro sigue fuera de este incremento.

Se observa a todos los candidatos disponibles (máximo 512), evitando introducir
un ranking alternativo previo. Es conservador: un candidato disponible con términos
incompatibles bloquea el episodio entero. Si el candidato que gana el ranking
excede el presupuesto total, se bloquea; no se reranquea con un algoritmo nuevo.
La prueba mide 285 lecturas en el catálogo real aislado; no es una optimización
de latencia ni una recomendación de despliegue de ese patrón sin más trabajo.

`Offer.observed_at` pertenece al snapshot Merchant; el `observed_at` exterior es
la muestra UTC local después de recibirlo. No se confunden. Para muestras iguales
en Windows se espera una muestra de reloj **realmente posterior**, hasta un segundo;
no se suman tiempos artificiales. Un reloj detenido/atrasado bloquea. Las pruebas
conservan la exigencia de orden estricto y verifican que el ranking solo ocurre
tras conservar los snapshots completos y comprobar de nuevo el scope.

No se acepta un `selected_variant_id` anterior ni una D1 histórica. Una elección
humana/modelo previa solo puede conducir a **otra selección backend nueva** en
otro episodio; no adquiere nuevos términos ni nuevas fechas. Mostrar términos
completos y obtener aprobación humana es requisito de integración D2-HP pendiente.

## F. Pruebas ejecutadas y resultados

Python 3.12.6. Se reutilizó `.venv` instalado de Agent sin sincronizarlo ni
modificar dependencias; el runner coloca `src` y raíz del worktree nuevo primero
en `sys.path`. Merchant usa exclusivamente su propio intérprete y sus fuentes
congeladas. Validación final con `uv run --frozen --offline --no-sync`,
`UV_PROJECT_ENVIRONMENT=C:/ANDES/Tesis/tesis-commerce-agent/.venv` y cache uv bajo
el directorio descartable nuevo. No se afirma haber ejecutado `uv sync --frozen`.

| Ejecución realmente realizada | Resultado / evidencia |
| --- | --- |
| Específicas iniciales | **11 failed, 28 passed**, 5.87 s. `focused-initial.txt`. Falló el orden estricto porque muestras UTC consecutivas podían ser iguales en Windows. Se corrigió la captura para esperar reloj real, conservando las aserciones. |
| Repetición específicas tras corrección | **39 passed**, 4.47 s, `focused-second.txt`. |
| Específicas tras endurecer transporte/custodia | **43 passed**, 9.49 s, `focused-hardened.txt`. |
| Específicas con proceso de reinicio y spy de orden | **45 passed**, 6.92 s, `focused-restart.txt`. |
| Real Merchant/PG, primer intento restringido | **4 errors de setup**, 42.73 s, cero casos aprobados. `initdb` agotó 40 s; no llegó a arrancarse servidor PG ni a ejecutarse los tests. `real-first.txt`; no se atribuye una causa raíz funcional. |
| Real Merchant/PG, repetición con permisos ampliados | **4 passed**, 94.44 s, `real-second.txt`, 285 Offer; clúster nuevo `07bp-real-2ec18993b94f`, puerto 63575, worker exit 0 y puerto cerrado al finalizar. |
| Real Merchant/PG, repetición del código final endurecido | **4 passed**, 90.54 s, `real-third.txt`, otras 285 Offer, clúster nuevo `07bp-real-66158bd799e0`; transporte a ASGI controlado. |
| Suite general completa, ejecución anterior al endurecimiento final | **899 passed, 45 skipped, 1 warning**, 615.53 s, exit 0. `full-final.txt` conserva su nombre de lanzamiento; no representa la versión final posterior. |
| Suite general completa del código final | **905 passed, 45 skipped, 1 warning**, 573.27 s, resumen completo y proceso finalizado correctamente. `delivery-complete.txt`; incluye las 45 pruebas nuevas y todas las regresiones generales. Fuentes finales sin cambios durante la ejecución. |
| Ruff format --check / Ruff check / Pyright finales con uv | **179 files already formatted / All checks passed / 0 errors, 0 warnings, 0 informations**. Logs `format-delivery.txt`, `ruff-delivery.txt`, `pyright-delivery.txt`. |
| Diff y custodia finales | **PASS**: diff versionado e índice sin diagnósticos; ocho archivos nuevos comprobados contra NUL sin problemas de whitespace; los 2556 archivos protegidos conservan sus bytes exactos. Mapa `protection-final.json`. |

Los logs completos, incluidos fallos originales y repeticiones, se conservan en
`.local/07bp-validation/`. La suite general que se lanzó primero ya estaba
coleccionada cuando se añadieron pruebas de endurecimiento/reinicio; por eso no
se presenta como validación del código final. Se conserva y se repite completa.
No se suman aprobados de retries para inventar una ejecución general verde.
Los números históricos de 07B-B (797/63/41 y retry 63) no son resultados de 07B-P.

Las 45 omisiones de la suite general son 41 históricas (3 E2E, 6 reales 07A,
32 reales 06D-C) y 4 nuevas opt-in Merchant/PG, que se ejecutaron separadamente.
El warning general es la deprecación `anyio.abc.BlockingPortal` de Starlette.
El stderr de cada worker real también conserva un `StarletteDeprecationWarning`
por FastAPI/TestClient con httpx; no se ocultó ni se cuenta como caso fallido.
No se ejecutaron las 38 pruebas reales históricas contra el Merchant nuevo:
sus harnesses siguen fijados a su SHA anterior, sin retocar sus expectativas.

Otras incidencias conservadas: el primer Ruff de archivos nuevos detectó problemas
de formato/estilo corregidos mediante formateo y cambios locales; Pyright inicial
reportó tres errores de inferencia en el parser del store, corregidos con un
parser genérico tipado. No se cambiaron aserciones o fuentes históricas para lograr
verde. Algunas búsquedas exploratorias usaron rutas inexistentes y fueron
corregidas; no son ejecuciones de pruebas.

Antes de tests se inspeccionaron sockets, SQLite y subprocesses existentes. Runner
offline: conexiones/DNS bloqueados, con excepción del socketpair interno exacto
de CPython; SQLite solo bajo `.local/07bp-validation`, incluyendo URIs locales
decodificadas; `.env` operativos prohibidos; salidas relativas shadow bajo un
directorio de ejecución nuevo, nunca el B0 original. Solo permite tres scripts
Python de reinicio, por contenido constante exacto inspeccionado en AST y comando
Windows exacto: dos históricos y uno nuevo. Los subprocesses no heredan el hook
Python; se limita su contenido y se limpia su entorno, sin atribuirles un sandbox
de proceso inexistente. No se habilitan opt-ins históricos ni servicios externos.

Las pruebas reales crean una DB vacía `tesis_lab`, rol propio sin superusuario,
puerto aleatorio no operativo, directorio nuevo y migraciones Merchant congeladas.
Cada conexión del worker contrasta DB/usuario/puerto/data_directory. No recibe DSN
de desarrollador; no abre PostgreSQL operativo. El worker rechaza escrituras de
archivos fuera del directorio nuevo y sockets ajenos; credenciales solo por pipe
privado en memoria, nunca por evidencia, argv público o logs. Todos los procesos
PostgreSQL iniciados por pruebas se detienen sin borrar sus directorios.

Comandos de reproducción, desde el worktree aislado, usando una etiqueta nueva
para no reutilizar directorios temporales de resultados previos:

```powershell
$env:UV_PROJECT_ENVIRONMENT='C:/ANDES/Tesis/tesis-commerce-agent/.venv'
$env:UV_CACHE_DIR=(Join-Path (Get-Location) '.local/07bp-validation/uv-cache')
$env:PYTHONDONTWRITEBYTECODE='1'
$env:RUFF_NO_CACHE='true'
uv run --frozen --offline --no-sync ruff format --check .
uv run --frozen --offline --no-sync ruff check .
uv run --frozen --offline --no-sync pyright --venvpath C:/ANDES/Tesis/tesis-commerce-agent
uv run --frozen --offline --no-sync python -B .local/07bp-validation/run_offline.py full review-new
```

Integración opt-in independiente (requiere permisos de procesos PostgreSQL propios;
el runner offline general desactiva deliberadamente ese opt-in):

```powershell
$env:RUN_GOV07BP_REAL='1'
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
uv run --frozen --offline --no-sync python -B -m pytest tests/integration/test_preselection_merchant.py -q -s -p no:cacheprovider --basetemp=.local/07bp-validation/real-review-new
```

## G. Casos adversariales

| Caso | Evidencia de este incremento |
| --- | --- |
| Snapshot completo / candidato exacto | SQLite y transporte controlado; también 285 Offer del Merchant real aislado. |
| Precio unitario, variante, producto, moneda, opciones, merchant o destino diferentes | Bloqueo sin selección sellada. |
| Inactiva, agotada, futura, vencida, total inconsistente o payload incompleto | Bloqueo conservador; snapshots parciales no se promueven. |
| Revisión diferente, mismo precio con distinto envío/expiración, digest diferente con igual revisión | Snapshot nuevo distinto preservado antes de selección nueva; no se iguala a la observación anterior. Dos Offer distintas proyectan el mismo feed. |
| Episodio/actor ajeno y sesión incorrecta/revocada | Simulación controlada y Merchant/PG reales aislados; ID común no basta. |
| Merchant inaccesible, errores HTTP, redirect, timeout, respuesta excesiva y JSON duplicado | Rechazo conservador. Deadline acumulado probado con stream de goteo controlado. |
| Observación posterior a selección cerrada | Segunda captura rechazada antes de HTTP; fechas alteradas fallan validación. |
| Concurrencia | Dos threads, conexiones SQLite independientes: un único claim/captura ganadora. |
| Reinicio | Nueva instancia y proceso Python nuevo recuperan el mismo sello sin recaptura. Estado claimed incompleto no se reanuda. |
| Sustitución | Triggers, HMAC, owner/contexto, digest Offer y binding de episodio; incluso una Offer recalculada con digest válido no sustituye la selección sellada. |
| Historia incompleta | DB histórica rechazada sin cambios de bytes, ausencia de selección/claim cerrado bloquea. |
| Credenciales | Bearer fuera de modelos/evidencia; reflejo en respuesta rechazado antes de persistencia. |
| 07A / 06D / D1 | Fuentes intactas y suite de regresión; ninguna afirmación de integración D17. |

Las pruebas reales nuevas usan PostgreSQL real y FastAPI/TestClient Merchant en
un proceso extranjero. El canal Agent→Merchant es pipe privado + transporte httpx
controlado que llama ASGI: **no es una prueba de sockets HTTP reales**. El fixture
solo admite GET feed y los dos POST de lectura por lista fija; no permite rutas
de checkout. Se comparan seis contadores comerciales antes/después; permanecen
iguales. `offer.read` sí puede incrementar eventos. Las pruebas históricas
de publicación Merchant no se ejecutaron completas porque una crea/cancela
checkout; no se invocaron en este ticket.

## H. Garantías y dependencia exacta para retomar 07B-B

Logrado en el experimento: evidencia completa previa a una selección nueva,
digest compatible, correspondencia con feed/intento/contexto, persistencia y
custodia local inmutables, fallo cerrado ante sustitución/reinicio y demostración
de origen en un catálogo administrado aislado. No se verificó un deployment o
Bearer operativo actual, ni se estableció un registro productivo de credenciales.

Para retomar 07B-B hacen falta, en este orden:

1. Ensamblaje backend con un registro real, resoluble y protegido que vincule
   catálogo administrado, origen, run, actor y credencial. Una instancia de
   `CatalogBinding` o un ID común no sustituye ese registro.
2. Integrar explícitamente este episodio/selección nuevos antes de D1, preservando
   la referencia y el digest del sello. `PurchaseService.start` actual no consume
   esa evidencia; no debe rellenarse con un snapshot consultado después.
3. Diseñar el binding aditivo entre observación, nueva selección, fuentes D1,
   contexto y operación durable, con comprobación transaccional/CAS apropiada.
   El SQLite auxiliar no se presenta como UoW D1 ni como commit distribuido.
4. Diseñar la extensión durable D17 sin reinterpretar el envelope 07A histórico;
   conservar la misma identidad de operación/clave y claim único antes de enviar.
   Implementar transporte guardado, verificación de prueba y lookup específico
   únicamente en el ticket autorizado posterior.
5. Integración D2-HP para presentar envío/total/destino/expiración y obtener la
   aprobación humana que corresponda. Observación backend no es consentimiento.

La evidencia sellada es histórica. No acredita vigencia futura de stock/precio,
autoridad actual, aprobación humana o permiso de pago. El guard atómico Merchant
de D17 sigue siendo necesario frente a cambios posteriores; no está conectado aquí.
No hay runtime enforce, continuación comercial, creación, update, cancelación,
completion o pago reales nuevos. Los caminos históricos de la suite general usan
sus simulaciones originales; no son efectos comerciales remotos de esta tarea.

## I. Dictamen, SHAs y estado Git

**READY_FOR_REVIEW** para este incremento experimental de observación y selección nueva. D17 y su integración con D1/07A continúan pendientes; no se concede autorización de despacho ni de continuación comercial.

HEAD del worktree permanece en
`35fd2d80e6f10150d5f0651b386adaa4eff2d40e`; Merchant permanece en
`9743c9f179c7ad5413e8818766ef3dceb326a917`. Rama nueva:
`governance/ec-gov-07b-p-preselection-authority`. Índice sin cambios; no hay
archivos anteriormente versionados modificados. Entregables: **ocho archivos
nuevos**, todos sin stage, enumerados en D. Los runners, logs, snapshots sintéticos
y clústeres de validación quedan ignorados bajo `.local/` del worktree nuevo.

SHA-256 de bytes finales del código y tests (el informe conserva su propio hash
externamente para evitar autorreferencia):

| Archivo | SHA-256 |
| --- | --- |
| `src/commerce_agent/acp/authority_observation.py` | `cca77b766972d5dc7f2f319c6fa7f8c7bfb1178bfe4c049b6742524a1cbde21d` |
| `src/commerce_agent/acp/offer_snapshot.py` | `34b63754109edd70cb782c476f4eaa61193272a69db92af6ce9c1378279276c4` |
| `src/commerce_agent/governance/preselection.py` | `1d37583c529e77125989387d41091cbdeb93aca1ba9707d98a41cf54ff9c59f4` |
| `src/commerce_agent/persistence/preselection_sqlite.py` | `44348c75f9cbdcaa705c7052a2de0131b59c6851f6701b6c2e039e7eb5dfdd48` |
| `tests/governance/test_preselection.py` | `e3fc96394f731cca50fa1236732fe894fe8dbdf380889cdcb8f08974669323ba` |
| `tests/integration/gov07bp_merchant_worker.py` | `e0991875d74645d054274605f18f9f26050ed8246a305ec8093098c2acf78aa3` |
| `tests/integration/test_preselection_merchant.py` | `d4cbb40ab3b55b962926b86377bd03371d5b9497af9e023969cac10447cb9834` |

El manifiesto final de los ocho entregables, incluido este informe, queda en
`.local/07bp-validation/delivery-sha256.json`; también se conserva
`report-sha256.txt`. Ningún hash de la tabla representa conversión EOL de Git.

Comparación final contra el baseline (rutas relativas ordenadas, líneas `ruta:sha256` unidas con LF, sin LF final, SHA-256 del conjunto):

| Árbol protegido | Archivos | Agregado SHA-256 sin cambios |
| --- | --- | --- |
| `C:/ANDES/Tesis/tesis-commerce-agent` | 1452 | `9e3141d186222b2ed2ff92dc4d1bab0d0856296d9db64fcff5eee5d159a01de6` |
| `C:/ANDES/Tesis/tesis-commerce-merchant` | 287 | `c7d1632c7cba0d0e495e30728e352e7c72a4f47b4354725668c13e78efece813` |
| `C:/ANDES/Tesis/tesis-commerce-lab` | 147 | `bb1866bf248a06afb535f4fe0f328a1a637fca0d49f89be2042d3924272b3ba8` |
| `C:/ANDES/Tesis/tesis-payment-provider-sandbox` | 17 | `cbf4083c8efe7ae789f16653ee6e4c8c5533e635f0a2b41bf3a777cf7d67428f` |
| `C:/ANDES/Tesis/tesis-commerce-agent/.local/worktrees/ec-gov-07b-b` | 336 | `17ab553554a9666fff96bb00c2f363e3ac9a38bdf898b3e3c240dc9bcdc44815` |
| `C:/ANDES/Tesis/tesis-commerce-agent-hnp` | 317 | `659dc6ee030667ce70c78360d5f962dcbf534f5eae1c0461a753dfee37707dd5` |

Los informes previos, los archivos aprobados y el B0 original conservan sus hashes. `protected-git-final.json` registra ramas/SHAs/estados finales de los seis árboles. El clúster real final usó el puerto 59423; `closed.json` confirma worker exit 0 y puerto cerrado tras detener PostgreSQL. El primer clúster satisfactorio también terminó con worker exit 0 y puerto cerrado. No se borraron directorios de evidencia.

La inspección Git final restringida tuvo dos incidencias conservadas: una lectura por subprocess/cwd devolvió `this operation must be run in a work tree`; al repetir con `git -C`, HNP devolvió `Permission denied`. Se repitió exclusivamente la inspección y el cierre de artefactos con permisos ampliados y `--no-optional-locks`; los seis estados se leyeron correctamente, sin modificar configuración, índices o árboles protegidos.

No stage, commit, push, merge ni PR. Sin publicación, sin D17 ejecutable,
sin runtime enforce y sin continuación comercial. La revisión humana de este
incremento no debe interpretarse como autorización del siguiente.
