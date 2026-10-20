# EC-GOV-07B-Q — Binding preselección a D1 sin despacho

## A. Preflight y SHAs

2026-10-16, Windows. Origin comprobado:
`https://github.com/Maldinho18/tesis-commerce-agent.git`.
Base exacta y rama de partida: `c145e97f8d34ed62fb6cef89d9896348c8125c31`,
`governance/ec-gov-07b-p-preselection-authority`. Rama y ruta Q ausentes antes
de crear `governance/ec-gov-07b-q-preselection-d1-binding` en
`.local/worktrees/ec-gov-07b-q` desde ese SHA. Se usaron permisos ampliados solo
para la creación autorizada de referencias/worktree. Merchant limpio y congelado
en `9743c9f179c7ad5413e8818766ef3dceb326a917`.

Principal conserva `35fd2d80e6f10150d5f0651b386adaa4eff2d40e` y su informe local;
07B-B conserva su informe bloqueado; 07B-P está limpio en c145e97. No se cambia
ninguno de sus checkouts. Baseline de los siete árboles protegidos, incluidos
Merchant, Lab, PSP, HNP y B0, en `.local/07bq-validation/baseline.json`.

## B. Matriz de correspondencia de procedencia

**Matriz redactada antes de implementar.** Se inspeccionaron los cuatro módulos
07B-P; enforce, provenance, models, dispatch_envelope, purchase; stores de vault,
UoW, envelopes y dispatch; observador shadow, ledger y pruebas 06D-C/07A/07B-P.

| Origen verificado | Campo D1/binding previsto | Comprobación y frontera |
| --- | --- | --- |
| `SqlitePreselectionStore.recover(id,owner)` | Snapshot AuthoritySelection completo + digest del sello | Se recupera del store, no se admite objeto recibido del comprador/modelo. HMAC, estado sealed, todas las observaciones y ranking se revalidan. |
| `Episode.episode_id` | `D1.run_id` idéntico | No se asigna otro run ni se reutiliza una compra/decisión histórica. |
| `Episode.owner_id` | owner backend + owner_ref del vault | Registro de grant independiente y comprobación de propietario; el pseudónimo D1 no se interpreta como identidad Merchant. |
| `episode_digest(Episode)` | Digest de episodio en intento y binding | Incluye feed, intent, origen y sesión esperados. |
| Observación cuya candidate es exactamente `selection.selected` | observation_id/digest, offer_id/revision, perfil/digest Offer | Un único match por identidad completa; nunca se consulta una oferta nueva para reemplazarla. |
| `Episode.feed` | `feed_snapshot_ref` del vault D1 | Se recupera por la referencia real y se compara su valor canónico exacto con el feed sellado. |
| `Episode.intent` | `IntentFacts.intent_ref` del vault | Igualdad exacta de intención recuperada, no reconstrucción desde un presupuesto parcial. |
| `AuthoritySelection.selection` | selected_variant_id y conjunto elegible | Recalculable con rank_candidates aprobado, intacto. |
| `Episode.binding` + registro externo de emisión y grant | Identidad remota esperada y procedencia resuelta | Recibo backend de pruebas explícito; no firma remota ni credencial productiva. Referencias en DAG, sin autorreferencia. |
| Contexto D1 registrado | context_ref, merchant_ref, política, algoritmo, clocks | Vault y verificación de fuentes reales existentes. merchant_ref es pseudónimo de origen, no merchant_id directo. |
| Ledger D1 comprometido | decision_id, evidence_id, digest del DecisionRecord | Solo se publican referencias tras lectura independiente del commit. |
| Ninguna operación comercial en Q | operation_id=null, dispatch_capability=false | No reserva, envelope, claim ni clave comercial; un allow observado no autoriza POST. |

Alternativas: llamar al enforce sin transporte dejaría reserva/envelope; copiar
sus reglas duplicaría autorización; ampliar contratos históricos expondría sus
artefactos. Se elige envolver el ShadowD1Observer existente: evaluación real
POL-01/POL-02 y ledger/vault originales, `observed_shadow`, sin transición.
Q no se conecta a Settings, PurchaseService ni runtime.

## C. Registro backend y frontera de confianza

Registro independiente `SqliteTestProvisioningRegistry`, SQLite propio y clave
HMAC persistente distinta de la custodia P/Q en las pruebas. Es un **emisor
backend de pruebas**, perfil `test_backend_issuer/1`, no una conexión a un registro
productivo ni una prueba criptográfica remota. `deployment_attestation=null`;
no se inventa identidad de despliegue verificada o firma Merchant.

El DAG verificable contiene cuatro objetos públicos tipados:

- `CatalogSource`: evento previo al descubrimiento, origen HTTP, merchant_id,
  catálogo administrado `merchant`, feed_id y run/actor remotos esperados.
  El run del catálogo administrado es su identidad de catálogo en este perfil.
- `CredentialIssuance`: evento de emisión, hora y referencia al CatalogSource.
  El hash de la credencial queda en columna privada, ligado al evento por HMAC;
  no aparece en su cuerpo público ni en el binding Q. No se guarda el Bearer.
- `CatalogReceipt`: referencia al origen y a la emisión, con copia contrastable
  del origen. Su ObjectRef apunta a sus propios bytes canónicos almacenados;
  sus referencias internas apuntan a **otros registros**, sin autorreferencia.
- `EpisodeGrant`: emisión backend separada que fija episodio local, propietario,
  run D1 idéntico al episodio, identidad persistente del SQLite D1, digest del
  sello P y referencia del recibo. Un grant ajeno no se acepta.

Los objetos y la relación episodio→grant son append-only. La resolución recupera
por referencias reales, verifica digest, tipo, HMAC, origen/catálogo/merchant,
contexto de emisión, hash de la credencial backend suministrada, revocación local,
propietario, run, store y sello. No deriva confianza simplemente de construir un
CatalogBinding, del feed, de un offer_id común o de un eco JSON. Un registro
ausente, sustituido o no demostrable bloquea antes de capturar D1.

Cada identidad de credencial/hash solo puede emitirse una vez en este registro:
no se admite reasignar el mismo Bearer a otro actor/run. Hay índice único y
guarda de inserción por hash, además de guardas contra INSERT OR REPLACE. Este
perfil de pruebas exige una credencial nueva para un ámbito nuevo; no simula
una rotación productiva o un rebinding remoto comprobado.

No hay endpoint, herramienta LLM, loader de Settings o API del comprador para
emitir/modificar estos registros. El emisor requiere la clave de custodia backend.
Las pruebas lo llaman explícitamente antes de ejecutar P, con datos sintéticos
controlados, y emiten después el grant local independiente. No se "resuelve" una
afirmación estática histórica mediante un recibo inventado desde esa misma
afirmación. Un sello P antiguo cuyo source_ref no resuelva en el registro sigue
bloqueado: se necesita aprovisionamiento acreditado y un episodio P nuevo.

La resolución declara literalmente
`test_backend_receipt_not_remote_authentication`. Comprobar su hash de credencial
prueba asociación en ese registro **de pruebas**; no verifica vigencia de una
sesión Merchant productiva. La revocación cubierta es la del registro backend de
pruebas. Q no consulta una revocación remota desconocida, ni realiza HTTP. Antes
de uso productivo se requiere un adaptador a emisión/autenticación reales con
procedencia verificable; no se atribuye ese resultado a este incremento.

## D. Binding durable y modificaciones

Se añaden únicamente cuatro módulos y un archivo de pruebas, además del informe:

| Archivo nuevo | Función |
| --- | --- |
| `governance/provisioning.py` | Contratos estrictos y versiones de origen, emisión, recibo, grant y resolución. |
| `persistence/provisioning_sqlite.py` | Emisor backend de pruebas, almacenamiento HMAC y resolución de referencias. |
| `governance/preselection_d1.py` | Request esperado y binding a evidencia D1 ya comprometida. |
| `persistence/preselection_d1_sqlite.py` | Protocolo de evaluación/recovery sin transporte ni operación comercial. |
| `tests/governance/test_preselection_d1.py` | 37 casos nuevos con SQLite real, política real, custodia, concurrencia y procesos de reinicio. |
| Este informe | Matriz previa, resultados y límites. |

Las rutas de módulos son relativas a `src/commerce_agent/`. No se modifica ningún
archivo versionado de la base: rank_candidates, política/canonicalización,
enforce, UoW, vault, envelopes, dispatch, PurchaseService, contratos P y pruebas
históricas quedan intactos. No hay dependencias nuevas o lockfile modificado.

`commerce-preselection-d1-request/1` contiene IDs de episodio/run/owner y digests
esperados de sello/feed/intent/variante. Son expectativas que se contrastan,
**no una AuthoritySelection confiada al caller**. `request(id,owner)` y `evaluate`
recuperan directamente `SqlitePreselectionStore.recover`: estado sealed, HMAC,
episodio, todas las observaciones, snapshot/digest Merchant, ranking, ámbito,
tiempos estrictos y vigencia en la consolidación P se verifican otra vez.

`commerce-preselection-d1-binding/1` conserva ID y digest del episodio, digest
del sello completo, observación elegida por igualdad exacta con selected,
identidad/digest de esa observación, Offer/revisión/digest D17, origen/merchant/
catálogo/sesión, resolución de registro, feed/intent y referencias reales del
vault, contexto, algoritmo, política, motor, decision_id, evidence_id y digest
del DecisionRecord. `evaluation_id` identifica un intento local realmente
comprometido. **operation_id=null**: no se inventa una operación comercial.

SQLite D1 dedicado: `q_d1_meta`, `q_d1_attempts`, `q_d1_bindings`, además de las
tablas existentes que necesitan AgentStore, vault y ledger shadow. El store_id
persistente y su HMAC impiden aplicar un grant a otro almacén nuevo. Intentos y
bindings tienen unicidad por run; evidence_id es único; snapshots, bindings y
meta no admiten UPDATE/DELETE ni INSERT OR REPLACE por triggers. Se contrastan también columnas de
identidad con sus payloads HMAC. Reabrir con otra clave falla cerrado.

No se migra una base histórica: si hay tablas pero falta el marcador Q, initialize
la rechaza antes de llamar a la inicialización Agent. La prueba conserva sus
bytes exactos. Dentro de una base Q, una compra/episodio/fuente/decisión D1 previa
sin el intento Q tampoco se promueve. Hay prueba con una decisión shadow real
preexistente, no solo con un marcador estructural.

Frontera de atomicidad explícita, sin transacción distribuida:

1. P y el registro están en bases separadas. Se recupera el sello P, se verifica
   el registro y se obtiene su resolución. Sus claves son custodia backend.
2. Bajo `BEGIN IMMEDIATE`, Q exige run completamente nuevo y compromete juntos
   un intento HMAC con copia exacta del sello/resolución/configuración y una fila
   purchase `received`, versión 1. No hay selección comercial en purchases.
3. ShadowD1Observer hace su captura real y sus commits existentes de procedencia,
   scope y decisión. Estas transacciones **no son** la del binding final Q.
4. Q lee la decisión realmente comprometida y admite su replay de verificación
   fuera de transacción. Luego toma `BEGIN IMMEDIATE`: recupera feed/intent y
   configuración privada con el vault autenticado, contrasta todos los hechos,
   exige una sola decisión del run, verifica versión/owner/estado de purchase y
   vuelve a leer el DecisionRecord antes de insertar el binding HMAC.
5. Si falla entre cualquiera de esas etapas, la evidencia parcial no es un
   binding válido. Otra llamada a evaluate rechaza el run; no retoma evaluación.
   `recover(request)` explícito solo puede enlazar una decisión existente y
   completa. Si falta commit de decisión, bloquea sin generarlo ni simularlo.

El claim es de **intento de evaluación**, no el claim comercial 07A. Los triggers
Q congelan la fila purchase asociada e impiden que pase a selected o cambie su
variante/versión. La captura concurrente tiene un único ganador; la vinculación
final revalida bajo lock y CAS los hechos persistidos. Un sello nuevo, aunque
tenga un HMAC P válido y un digest Offer recalculado, no sustituye el sello fijado
por el grant y por el intento. La copia original y el binding previo permanecen.

## E. Evaluación D1 sin capacidad comercial

Se usa **ShadowD1Observer**, no SqliteD1EnforceGate. La inspección confirma que
enforce puede crear reserva y envelope antes de devolver d1_dispatch_blocked;
no es un evaluador sin efectos y no se invoca, ni siquiera sin transporte.

El observador existente verifica fuentes y rank_candidates, captura feed/intent
originales, usa los registros y claves reales del vault, crea contexto y scope
D1 y ejecuta ReferencePolicyEvaluator POL-01/POL-02. El ledger existente valida
el resultado y conserva un DecisionRecord real con `observed_shadow`, sin
transition_ref o resulting_state_version. Q verifica que el D1 run sea el UUID
P, su owner sea el esperado, sus fuentes recuperadas sean idénticas a P, su
variante/conjunto elegible coincidan y su reloj de autoridad sea posterior al
sellado P. También contrasta la configuración comercial privada recuperada, no
su binding público opaco, con la configuración backend Q exacta.

Un allow es una observación D1; no hay `transition_committed`, reserva,
idempotency_key comercial, envelope o claim de envío. Una política restrictiva
válida produce deny POL-02 real y binding con outcome deny, conservando la misma
ausencia de capacidad. Las pruebas crean su propio artefacto restrictivo mediante
el contrato aprobado; no editan un artefacto o test histórico. Una política
inválida/artefacto incorrecto bloquea sin fabricar una decisión allow.

Q no recibe gateway, cliente HTTP ni factory de evaluador. No publica rutas ni
habilita enforce en Settings. La fila purchase continúa received/version=1 y no
puede pasar a selected en este almacén. Los consumidores deben leer literalmente
`operation_id=null`, `dispatch_capability=false`, `governance_mode=shadow`.

La recuperación **reproduce la política para verificar el resultado archivado**
mediante el ledger original; no es una nueva captura, autorización o decisión
durable. En las pruebas de otro proceso se prohíben capture/observe durante
recover. Sin decisión previamente comprometida, recover no produce binding.

## F. Pruebas y resultados

Python 3.12.6, Windows, intérprete instalado Agent reutilizado sin sincronizar ni
modificar dependencias. Los comandos finales usan `uv run --frozen --offline
--no-sync` con `UV_PROJECT_ENVIRONMENT` apuntando al .venv existente; sys.path del
runner coloca src y raíz del worktree Q primero. No se afirma ejecución de
`uv sync --frozen` ni de pruebas Linux.

| Ejecución | Resultado comprobado |
| --- | --- |
| Focalizadas iniciales | **7 failed, 23 passed**, 26.61 s; `focused-initial.txt`. Faltaba transacción explícita para recuperar el vault. |
| Segunda ejecución | **7 failed, 23 passed**, 22.06 s; `focused-second.txt`. Se intentó parsear el binding público commercial como configuración; se corrigió a recuperación privada autenticada existente. |
| Tercera ejecución, con adversariales adicionales | **2 failed, 32 passed**, 32.79 s; `focused-third.txt`. Los dos lectores de ledger en tests nuevos carecían de PRAGMA foreign_keys=ON; se habilitó sin cambiar las aserciones. |
| Cuarta ejecución | **34 passed**, 29.74 s; `focused-fourth.txt`. |
| Focalizadas finales | **35 passed**, 33.25 s, exit pytest 0 registrado en `final-focused-exit.json`; `focused-final.txt`. |
| Endurecimiento SQLite REPLACE | **36 passed**, 39.48 s, exit 0; `focused-hardened.txt`. |
| Focalizadas finales con identidad de credencial única | **37 passed**, 40.22 s, exit 0; `focused-credential-final.txt` y `credential-final-exit.json`. |
| Primera suite general, anterior al endurecimiento final | **940 passed, 45 skipped, 1 warning**, 642.93 s; `full-final.txt`, exit 0 en `final-full-exit.json`. Incluyó 35 casos Q; no se presenta como validación del código final reforzado. |
| Suite offline general final | **942 passed, 45 skipped, 1 warning**, 588.53 s, exit 0; `delivery-full.txt` y `delivery-full-exit.json`. Suite completa sobre los cinco archivos de código finales cuyos hashes constan en I. |
| Ruff format --check / Ruff lint / Pyright finales | **185 files already formatted / All checks passed / 0 errors, 0 warnings, 0 informations**; logs `delivery-format.txt`, `delivery-ruff.txt`, `delivery-pyright.txt`, contra el código final. |
| Diff y custodia final | Diff de archivos versionados e índice vacíos; diff --check también aplicado a los seis archivos nuevos. Los 2899 archivos protegidos conservan sus SHA-256 y los cinco archivos de código coinciden con el manifiesto congelado previo a la suite final; `protection-final.json` y `code-frozen-final.json`. |

No se debilitaron aserciones anteriores. Los errores de implementación anteriores
se corrigieron solo en archivos nuevos; activar la FK requerida en las conexiones
de los tests nuevos fortalece su configuración. Pyright inicial reportó 16 errores
de narrowing/atributo inexistente en tests nuevos, corregidos con comprobaciones
de tipo y una comprobación dinámica explícita del método comercial ausente.
Los diagnósticos Ruff iniciales de estilo/formato se conservan también en .local.

El baseline inicial restringido no pudo entrar con Git a HNP (`Permission denied`);
la repetición de lectura con permisos ampliados registró los siete árboles sin
modificarlos. La creación del worktree y la suite general usan permisos mínimos
ampliados necesarios; el perímetro offline se mantiene en el runner. No hubo
rechazo de aprobación automática. No se toma ningún resultado histórico 07B-P
o 07B-B como una ejecución actual de Q.

Antes de pruebas se inspeccionaron los sockets internos de asyncio, SQLite/URIs,
subprocesses y salidas shadow relativas. Runner `run_offline.py` en
`.local/07bq-validation`: bloquea red/DNS y .env operativos; admite solo el
socketpair interno exacto de CPython; SQLite y escrituras bajo ese directorio;
ejecución relativa shadow en un scratch nuevo, nunca B0 original. Elimina opt-ins
RUN_ y credenciales ambientales conocidas sin imprimir valores. Solo admite los
cuatro scripts de reinicio constantes inspeccionados (dos históricos, uno P y uno
Q) con argv/comando Windows exactos. Los subprocesses no heredan el audit hook;
su contenido y entorno están restringidos, sin atribuirles un sandbox inexistente.
El runner conserva el exit_code pytest en JSON, además de cada log completo.

Reproducción desde el worktree Q, con etiquetas nuevas para preservar ejecuciones:

```powershell
$env:UV_PROJECT_ENVIRONMENT='C:/ANDES/Tesis/tesis-commerce-agent/.venv'
$env:UV_CACHE_DIR=(Join-Path (Get-Location) '.local/07bq-validation/uv-cache')
$env:RUFF_NO_CACHE='true'
uv run --frozen --offline --no-sync ruff format --check .
uv run --frozen --offline --no-sync ruff check .
uv run --frozen --offline --no-sync pyright --venvpath C:/ANDES/Tesis/tesis-commerce-agent
uv run --frozen --offline --no-sync python -B .local/07bq-validation/run_offline.py focused review-focused
uv run --frozen --offline --no-sync python -B .local/07bq-validation/run_offline.py full review-full
```

Las **45 omisiones** corresponden a 3 E2E, 6 pruebas reales 07A, 32 pruebas reales 06D-C y 4 pruebas P con Merchant/PostgreSQL aislado. Ninguna prueba nueva Q fue omitida. La única advertencia es la deprecación heredada de BlockingPortal en Starlette TestClient; no es un fallo de Q. No se habilitaron esos opt-ins ni se iniciaron sus servicios.

## G. Casos adversariales

| Casos solicitados | Evidencia de Q |
| --- | --- |
| 1: binding correcto | Sello P real del store, vault/ledger/evaluador D1 originales, binding recuperado y cotejado tras commit. |
| 2–6: feed, intent, variante, owner y run diferentes | Expectativas alteradas rechazadas antes de captura; registro de intento permanece vacío. |
| 7–10: actor/run remoto, sesión, ausencia de registro y recibo alterado | Contexto P alterado falla HMAC; otra credencial backend realmente emitida para actor/run diferente no consume el sello; revocación local, Bearer ausente/erróneo, recibo borrado o sustituido bloquean. No son pruebas de autenticación remota productiva. |
| 11–13: oferta, revisión y digest alterados | Fallo de custodia; también un sello P nuevo válido con revisión/digest nuevos es rechazado por el grant y request originales. |
| 14: dos sellos para una operación/run | Unicidad del intento por run y bind a store/owner/seal; otro sello no adquiere el mismo run. No existe operación comercial Q. |
| 15: concurrencia y CAS | Threads con conexiones SQLite independientes, un intento/decisión ganador; estado purchase/version y decisión se revalidan bajo lock antes de vincular. |
| 16–19: crash/reinicio/fronteras | Fallo tras claim antes de capture; fallo de escritura del ledger; otro proceso antes/después del binding. Solo un commit D1 existente puede enlazarse explícitamente; no hay retry automático de evaluación. |
| 20: D1 anterior sin sello | Decisión shadow genuina anterior sin intento Q rechazada; bytes del DecisionRecord conservados. Base histórica también rechazada sin cambiar bytes. |
| 21–23: allow, deny, artefacto inválido | Evaluador real POL-01/POL-02, deny POL-02 restrictivo y dos artefactos inválidos; ninguna reserva comercial. |
| 24: POST | Durante evaluate/recover el transporte HTTP está prohibido por spy; no hay método create_checkout en Q. El avance CAS received→selected falla por trigger. |
| 25: regresiones | Incluidas en la suite final completa: D1, procedencia, custodia P y pruebas locales 06D-C/07A. No se reutiliza una ejecución histórica como resultado actual; los opt-ins reales siguen omitidos. |
| 26: ausencia de efectos | Cero filas nuevas en orders, completion_results, operation_keys, d1_checkout_operations, d1_enforce_inputs y d1_checkout_envelopes; checkout_id/order_id nulos; no HTTP de Q. Son verificaciones locales y de frontera, no contadores PostgreSQL remotos medidos. |

Las pruebas nuevas son unitarias de frontera con SQLite real y política D1 real;
la construcción P previa usa httpx.MockTransport. Las dos variantes de reinicio
usan otro proceso Python real. **No se ejecutó Merchant/ASGI/PostgreSQL ni HTTP
real entre procesos en Q**. No se exigen pruebas HTTP comerciales, prohibidas
por este ticket. Las regresiones históricas de sockets/servicios continúan opt-in;
no se cambian sus SHAs o aserciones para forzarlas contra otro Merchant.

## H. Garantías, límites y condiciones para retomar 07B-B

Logrado: selección P recuperada de su custodia, procedencia resuelta contra un
registro backend independiente de pruebas, nueva evaluación D1 real y binding
durable a sus commits, sin generar capacidad comercial. No hay una transacción
única entre las tres bases; el intento firmado anterior a captura y la recuperación
explícita fail-closed delimitan las fases. Conservar claves y archivos backend es
condición de recuperación; perderlos o cambiar datos/ref/modelos bloquea. HMAC
es custodia local: no es firma Merchant, antirrollback externo o protección ante
compromiso de todo el backend y sus claves.

Una resolución de registro tiene un instante histórico de comprobación. La
revocación o sustitución posterior puede bloquear la recuperación, nunca conceder
envío. No hay atomicidad remota de sesión/registro/SQLite; esto no autoriza dinero,
órdenes o checkout. La oferta es válida en su consolidación P, no necesariamente
en el momento de D1 o de un despacho futuro. El motor D1 aprobado no contiene las
reglas de revisión/digest D17: esas verificaciones siguen siendo binding auxiliar,
sin adulterar el veredicto POL-01/POL-02.

Condiciones precisas para retomar 07B-B, en otro incremento autorizado:

1. Adaptar el registro a aprovisionamiento/autenticación reales con evidencia
   verificable de catálogo, credencial, origen, scope y despliegue cuando exista.
   El emisor `test_backend_issuer/1` no se promueve a productivo.
2. Integrar la nueva selección en el flujo backend/UI manteniendo el ID P y este
   binding; si falta su procedencia, abrir un episodio nuevo, no completar historia.
   D2-HP debe presentar términos y obtener el consentimiento que corresponda;
   ninguna observación de backend se presenta como aprobación humana.
3. Diseñar una **nueva autorización de aplicación** y una operación real con
   referencia al sello/binding, CAS y estado comercial vigente. El DecisionRecord
   `observed_shadow` Q no se convierte en `transition_committed` ni se toma como
   reserva 07A. La base Q congela deliberadamente sus purchases; no es una cola
   de compras pendientes que pueda habilitarse por un flag.
4. Binding guardado aditivo a la operación/envelope, revisión y digest Offer
   originales, mismo claim/clave y prueba/lookup D17 verificados. Preservar bytes
   y semántica de envelopes/decisiones históricos. El guard Merchant atómico
   seguirá resolviendo cambios comerciales posteriores a la observación.
5. Verificación adversarial y de caída del nuevo transporte condicionado antes
   de cualquier activación. Este ticket no implementa esas capacidades.

No se habilita runtime enforce, HNP, 07A, D17, checkout, cancelación, completion,
pago, reenvío o continuación. No hay fuentes remotas de precios o LLM invocadas.

## I. Dictamen y estado Git

**READY_FOR_REVIEW** para revisión independiente de EC-GOV-07B-Q. No autoriza
despacho ni el avance operativo de 07B-B/D17. El emisor backend es de pruebas;
la evaluación D1 y su persistencia sí son las implementaciones reales existentes.

Worktree absoluto: `C:/ANDES/Tesis/tesis-commerce-agent/.local/worktrees/ec-gov-07b-q`.
Rama: `governance/ec-gov-07b-q-preselection-d1-binding`.
HEAD: `c145e97f8d34ed62fb6cef89d9896348c8125c31`.
No hay cambios tracked ni staged; los seis archivos de entrega siguen untracked.
No hubo reset, stash, stage, commit, push, merge o PR.

Estado completo de cambios del worktree (`git status --short`):

```text
?? docs/b2-governance/ec-gov-07b-q-preselection-d1-binding-results.md
?? src/commerce_agent/governance/preselection_d1.py
?? src/commerce_agent/governance/provisioning.py
?? src/commerce_agent/persistence/preselection_d1_sqlite.py
?? src/commerce_agent/persistence/provisioning_sqlite.py
?? tests/governance/test_preselection_d1.py
```

SHA-256 de bytes físicos finales (rutas relativas al worktree absoluto anterior):

| Archivo | SHA-256 |
| --- | --- |
| `src/commerce_agent/governance/preselection_d1.py` | `5d14364b613e5845f4ebe4d674a823c7930f4e18d9e046bfbb936633cb88616d` |
| `src/commerce_agent/governance/provisioning.py` | `fcf6541b1aab3e48d610f0f3ff4832a1c7585bbdb7edf832caaedcac5d337396` |
| `src/commerce_agent/persistence/preselection_d1_sqlite.py` | `4bf9b3c2484238e1e7c059f1ffe777173dbb5899f4abddce9ccba0cf48a0583d` |
| `src/commerce_agent/persistence/provisioning_sqlite.py` | `f624203e204d0083628018d61df2b639c63f2a6572493f71ca8705b1c23f2b5a` |
| `tests/governance/test_preselection_d1.py` | `7fea771c0a9ce88760dcc7d5152ec009b4cc121395fef391b8ae36ae3184600e` |

El hash del propio informe se registra fuera de su contenido para evitar una
autorreferencia imposible: `.local/07bq-validation/report-sha256.txt`.
`delivery-sha256.json` contiene las seis rutas absolutas y sus hashes finales.
Todos los logs y manifiestos citados están en el directorio absoluto
`C:/ANDES/Tesis/tesis-commerce-agent/.local/worktrees/ec-gov-07b-q/.local/07bq-validation`;
son evidencia local ignorada por Git, no archivos staged ni credenciales.

Custodia final de árboles protegidos, comparada contra baseline previo a cambios:

| Árbol | Archivos | SHA-256 agregado | Cambios |
| --- | ---: | --- | --- |
| `C:\ANDES\Tesis\tesis-commerce-agent` | 1452 | `9e3141d186222b2ed2ff92dc4d1bab0d0856296d9db64fcff5eee5d159a01de6` | 0 |
| `C:\ANDES\Tesis\tesis-commerce-merchant` | 287 | `c7d1632c7cba0d0e495e30728e352e7c72a4f47b4354725668c13e78efece813` | 0 |
| `C:\ANDES\Tesis\tesis-commerce-lab` | 147 | `bb1866bf248a06afb535f4fe0f328a1a637fca0d49f89be2042d3924272b3ba8` | 0 |
| `C:\ANDES\Tesis\tesis-payment-provider-sandbox` | 17 | `cbf4083c8efe7ae789f16653ee6e4c8c5533e635f0a2b41bf3a777cf7d67428f` | 0 |
| `C:\ANDES\Tesis\tesis-commerce-agent\.local\worktrees\ec-gov-07b-b` | 336 | `17ab553554a9666fff96bb00c2f363e3ac9a38bdf898b3e3c240dc9bcdc44815` | 0 |
| `C:\ANDES\Tesis\tesis-commerce-agent\.local\worktrees\ec-gov-07b-p` | 343 | `006e537fadb2ea830e459661c789eec01d048d3ff423eee98f6580e471733305` | 0 |
| `C:\ANDES\Tesis\tesis-commerce-agent-hnp` | 317 | `659dc6ee030667ce70c78360d5f962dcbf534f5eae1c0461a753dfee37707dd5` | 0 |

El agregado es SHA-256 de líneas `ruta_relativa:sha256` ordenadas, unidas con LF
sin LF terminal. Se compara cada archivo, no solo el agregado. El alcance incluye
todos los archivos tracked de cada árbol, informes 07B previos y los 1116 archivos
del B0 original `.local/gov05c`; no incluye credenciales operativas `.env`.
Son 2899 archivos en siete árboles; no es un sellado de todo archivo ignorado del disco.

Referencias históricas y estado final completos (sin refrescar índices):

| Árbol | HEAD | Rama | Estado |
| --- | --- | --- | --- |
| `C:\ANDES\Tesis\tesis-commerce-agent` | `35fd2d80e6f10150d5f0651b386adaa4eff2d40e` | `governance/ec-gov-07b-d17-offer-freshness` | `?? docs/b2-governance/ec-gov-07b-d17-offer-freshness-results.md`; índice vacío |
| `C:\ANDES\Tesis\tesis-commerce-merchant` | `9743c9f179c7ad5413e8818766ef3dceb326a917` | `merchant/ec-mer-07b-a2-readonly-guard-proof` | `limpio`; índice vacío |
| `C:\ANDES\Tesis\tesis-commerce-lab` | `b3be474e26ebbedc79dd4f3c3b133f42491f9542` | `main` | `limpio`; índice vacío |
| `C:\ANDES\Tesis\tesis-payment-provider-sandbox` | `ee19c06ecae3038b43d1fc7cb7a8822592fa1a08` | `main` | `limpio`; índice vacío |
| `C:\ANDES\Tesis\tesis-commerce-agent\.local\worktrees\ec-gov-07b-b` | `35fd2d80e6f10150d5f0651b386adaa4eff2d40e` | `governance/ec-gov-07b-b-d17-agent-integration` | `?? docs/b2-governance/ec-gov-07b-b-d17-agent-integration-results.md`; índice vacío |
| `C:\ANDES\Tesis\tesis-commerce-agent\.local\worktrees\ec-gov-07b-p` | `c145e97f8d34ed62fb6cef89d9896348c8125c31` | `governance/ec-gov-07b-p-preselection-authority` | `limpio`; índice vacío |
| `C:\ANDES\Tesis\tesis-commerce-agent-hnp` | `a633a043337286406f9a3e859809556ce46bcba1` | `hnp/ec-hnp-01-review-harness` | `limpio`; índice vacío |
