# P0 — Intención condicional (UX03, UX04, UX05, UX06, UX07)

Estado: propuesta para aprobación de Yair. Base: `main` 343f099. Escenarios de la matriz
`evaluation/scenarios/p0/matrix.json` del lado de Yair. El contrato de evaluación de UX03/UX05 es
el de Maldo, que todavía no está en `main`: protocolo `docs/design/p3-ux03-protocol.md` (#158) y
validador `evaluation/src/commerce_evaluation/p3_protocol.py` (#176). Este producto tiene que ser
compatible con él (§3.11).

## 1. Objetivo

El comprador puede dar una instrucción condicional, por ejemplo «A si el total es ≤ X; si no, B si
tiene stock y el total es ≤ Y», «prefiero A, pero nunca compres B» o «si A se agota, compra B». El
agente la convierte en una intención tipada y la evalúa contra el Merchant de forma determinista:
lee la oferta de cada rama en orden, crea **como máximo un checkout**, y solo para la rama
permitida. Si ninguna rama cumple, no compra nada y explica por qué. Si no puede verificar una rama
por un fallo técnico, tampoco compra y no pasa a la siguiente. El modelo interpreta la instrucción,
pero nunca decide la rama.

## 2. Comportamiento actual (verificado en 343f099)

### 2.1 HP: cómo se procesa hoy una instrucción condicional (leído)

- **Router** (`llm/conversation_router.py`): `CommerceDecision` (:291-306) solo admite un
  `PurchaseIntent`, con un bloque `hard` y otro de `preferences` (`domain/intent.py:30-79`). Hay
  un solo `max_total_amount_minor` y no existen ramas, orden, límites por rama, exclusiones ni
  fallbacks. Las instrucciones del router (:60-73) no mencionan condiciones.
- **Búsqueda:** `rank_candidates` (`domain/selection.py:100-141`) filtra disponibilidad, moneda,
  precio **unitario** observado, categoría, condición, opciones y términos. **No compara el total**:
  `rejection_reason` (:54-74) nunca lee `max_total_amount_minor`. El orden es «más preferencias
  coincidentes, luego el más barato» (:128-135). El feed solo trae precio unitario, sin envío
  (`acp/feed.py:68-86`).
- **`select_product`** (`application/conversation.py:724-784`): el LLM elige dentro de la lista
  corta. El enum de Structured Outputs solo admite IDs de esa lista
  (`conversation_router.py:333-350`), pero el modelo puede elegir **cualquiera** de ellos. Si
  todas las preferencias coinciden, la compra es forzada (:476-478). Las instrucciones de selección
  dicen «no aclares solo porque el total es desconocido; el backend lo compara» (:111-112).
- **`prepare_checkout`** (:786-842) llama a `PurchaseService.start`
  (`application/purchase.py:230-372`):
  1. vuelve a rankear el feed y exige que la variante esté en la lista (:281-296);
  2. crea el checkout;
  3. `_enforce_authoritative_budget` (:575-617) **cancela** si el total supera el presupuesto y
     lanza `budget_exceeded`.

  O sea: el agente conoce el total con envío solo después de **crear** un checkout.
- **`governed_recovery`** (`application/governed_recovery.py`) solo existe en enforce con flujo
  gobernado (:74-75). Explica `no_candidate`, la elección fuera de la lista y `budget_exceeded`
  después del checkout, con evidencia en `hp_evidence`. No conoce ramas. Su «opción más cercana»
  puede nombrar cualquier variante del feed (:249-286).

### 2.2 Experimento con el modelo guionado (ejecutado)

Corrí cuatro pruebas temporales, fuera del repo y borradas después, con:
- el SDK real sobre `ModelServer` (`tests/support/model_server.py`);
- SQLite real, B0-HP y un Merchant doble que devuelve la variante pedida con precio + envío de
  25.000;
- el feed `tests/fixtures/assistant_feed`: A = Sony WH-1000XM6 negro (1.899.000), B = Bose QC
  Ultra negro (1.799.000, más barato) y C = Sennheiser negro (1.249.000).

Las cuatro pasaron:

| Caso | Router guionado (lo único que el esquema expresa) | Observado |
|---|---|---|
| Esquema | — | `HardConstraints` y `SoftPreferences` no tienen campos de ramas, prioridad ni exclusión, y `CommerceDecision` rechaza una clave `branches` (`extra=forbid`) |
| UX04 (A y B cumplen, prioridad A) | un límite total de 1.950.000 y A y B como términos preferidos | la lista corta llega al modelo como `[B, A, C]`: hay empate de preferencia y gana el más barato. El modelo elige el primero, **se crea el checkout de B** y hubo 2 llamadas al modelo |
| UX03-B (A excede su límite) | el límite de B (1.850.000) como único límite total | A sigue en la lista corta. `max_unit_amount_minor` no estaba fijado, y `rank_candidates` solo usa `max_total_amount_minor` en la guarda de moneda (`selection.py:101-111`), nunca como filtro. Con `max_unit` en 1.850.000, A (1.899.000) habría quedado fuera. Si el modelo elige A: `POST /checkout_sessions` para A, total 1.924.000 > límite, cancelación y **REST 409 `budget_exceeded`**. Se creó un checkout y no hubo fallback a B |
| UX07 (nunca B) | A como preferencia, sin dónde poner la prohibición | la lista corta es `[A, C, B]`. Si el modelo elige B, **se crea el checkout de B** |

En B2-HP, POL-11 habría negado el caso UX03-B antes de crear, porque el precio unitario 1.899.000
supera 1.850.000 (`governance/evaluator.py:106-116`). Pero si el precio unitario cabe y el envío lo
pasa del límite, el checkout se crea y se cancela igual. Esto lo leí; no lo ejecuté.

### 2.3 HNP (leído)

- **Grant:** `DelegationScope` (`governance/hnp_models.py:66-76`) tiene `variant_ids`, que debe ser
  un conjunto **ordenado alfabéticamente** (:56-63), y un solo `total_max_minor`. `ConsentScope`
  (:114-141) agrega `preference_text`. No hay prioridad, ni límites por variante, ni exclusiones.
  El grant se crea con un formulario tipado (`delegation/service.py:87-103`) y no se edita: para
  cambiarlo hay que revocarlo y consentir de nuevo. El intent del grant es solo moneda + total
  (`service.py:290-293`).
- **Ejecutor** (`application/hnp_execution.py:479-522`):
  1. `_shortlist` = `rank_candidates` ∩ `variant_ids` (:563-574). No filtra el total.
  2. `_select` (:576-603) elige con el selector determinista, que toma el más barato, o con el LLM,
     que sigue `preference_text` (`llm/delegated_choice.py:33-45`).
  3. Revisor opcional. Con `AGENT_HNP_REVIEWER=llm`, que exige enforce
     (`delegation/bootstrap.py:242-244`), revisa lo que devuelva `_select`, también la elección
     determinista (:493).
  4. `start` crea **un solo** checkout y no hay reintento con otra variante.
- Hoy UX04-HNP no garantiza A. Para UX05/UX06-HNP con el total por encima por el envío, se crea un
  checkout y lo cancela el guardia de presupuesto (`budget_guard`, que **no** escala:
  `_ESCALATIONS` :106-116). UX07-HNP ya es determinista si B no está en `variant_ids`: lo impiden
  la lista corta, POL-01, POL-08 y el mandato abierto. Pero el borrador del chat **marca todas** las
  ofertas encontradas (`ui/src/lib/delegation-draft.ts:94-96`), así que B entra al grant si el
  comprador no la desmarca.
- **El borrador del chat trae texto:** `chatDraft` llena `preference_text` con el mensaje del
  comprador (`conversation.py:693`, `delegation-draft.ts:102`). Con revisor, ese texto se revisa.

### 2.4 Merchant y oferta (leído en Merchant abf30df, el SHA fijado)

- `ACPClient.get_offer` (`acp/client.py:116-146`) hace un solo intento sin reintentar y exige el
  mismo `id` y el mismo `delivery_context` (#178). Errores:
  - `offer_expired` es tipado;
  - `OFFER_NOT_FOUND` llega como `invalid_offer`, porque `ok:false` no valida;
  - un timeout, un error de transporte o un HTTP 401/429/5xx dan `timeout`, `transport_error` o
    `http_error` (:357-398).
- `OfferPricing` valida `total_minor = items_total_minor + shipping_total_minor` en COP
  (`acp/offer_snapshot.py:39-50`). El envío está incluido y la oferta trae `availability`,
  `available_quantity` y `product_status` (:53-92).
- El checkout **copia el pricing de la oferta**:
  - el Merchant arma `totals` desde `checkout.pricing` (`commerce_lab/acp.py:597-625`);
  - `compare_offer` ya exige `checkout.total_minor == offer.pricing.total_minor`
    (`application/offer_freshness.py:44-64`).

  Así que el total de la oferta es el total con el que se creará el checkout, salvo que la oferta
  cambie en medio.
- **Leer una oferta deja rastro en el Merchant:** `PersistentCatalogReader.get` inserta un evento
  `offer.read` en `run_events`, con `offer_id` y resultado
  (`commerce_lab/persistent_catalog.py:47-64`). El oráculo de 0 efectos del bloque A compara
  **todas** las tablas del Merchant
  (`evaluation/src/commerce_evaluation/stack/merchant_control/core.py:170-185`), así que hay que
  ajustarlo (§4.4).

### 2.5 Restricciones que condicionan el diseño (leído)

- **D1 mide su código fuente:** `domain/intent.py`, `domain/selection.py`, `domain/__init__.py` y
  `acp/feed.py` entran en el digest del algoritmo de elegibilidad
  (`governance/provenance_artifacts.py:38-44`). Tocarlos cambia la identidad del algoritmo de D1
  en **todas** las corridas, no solo en las condicionales. Por eso los tipos nuevos van en módulos
  nuevos que `domain/__init__.py` no importa.
- **El motor de D1, D2 y D3 mide `hnp_models.py`:**
  - `ENGINE_SOURCES` (`governance/evaluator.py:43-50`) incluye `governance/hnp_models.py`;
  - `measure_reference_engine()` (:72-86) hashea esos archivos, y ese `artifact_digest` va en el
    `engine` de cada decisión D1, D2 y D3 (`governance/models.py:406`);
  - agregar `priority` a `ConsentScope` y `ScopeDisplay` lo cambia. Hoy es `sha256:e47356ef…`, y
    el revisor midió `sha256:6b0d65b4…` con un cambio de ejemplo. El valor final depende de los
    bytes del PR 2.
- **El replay exige ese mismo digest:**
  - `replay_reference` (`governance/evidence_artifacts.py:96-116`) da `GOV_FACTS_UNAVAILABLE` si
    el motor instalado no es el archivado;
  - `evaluation/src/commerce_evaluation/verify_evidence_hnp.py:1114-1120` lo informa como
    `unavailable (GOV_FACTS_UNAVAILABLE)`;
  - los seis archivos del motor en `hnp-pilot-v1-base` y `hnp-review-pilot-v1-base` son hoy
    iguales a los de `main`. Con el cambio, el replay de esos pilotos con `main` da `unavailable`;
  - el piloto HP v2 (`pilot-v2-base`) ya corrió con otro motor, `reference-d1-d2:2`.

  D13 decide qué se hace (§8).
- **El consentimiento HNP se vuelve a serializar byte a byte:** `parse_consent_scope` rechaza un
  scope cuya forma canónica cambie (`hnp_models.py:153-158`). Un campo nuevo que se serialice como
  `null` rompería todos los grants guardados y cambiaría su `scope_digest`. Por eso se omite
  cuando no hay valor, como `canonical_without_absent_facts` (`governance/models.py:233-243`).
- **`hp_evidence` tiene una lista CHECK cerrada** (`persistence/hp_evidence_sqlite.py:18-24`) y solo
  existe en el flujo gobernado. **`hnp_tasks`** genera su CHECK desde `OutcomeCode`
  (`delegation/models.py:48-75`). Un código nuevo exige migrar bases viejas, con el patrón
  `admits_*` de `persistence/delegation_sqlite.py:588-609`. Para evitarlo, se reutilizan códigos y
  se usa una tabla nueva.
- Precedente: `PreselectionAuthority` (`governance/preselection.py`) observa ofertas antes de
  seleccionar, pero es un experimento sin cableado. Observa **todas** las candidatas (hasta 512) y
  tiene su propio almacén, así que no se reutiliza.

## 3. Diseño

### 3.1 Intención tipada

Va en un módulo nuevo, `domain/conditional.py`, que `domain/__init__.py` no importa (§2.5).
`CommerceDecision` gana `conditional: ConditionalIntent | None = None`, solo admitido con
`action="purchase"`.

| Tipo | Campos | Reglas |
|---|---|---|
| `BranchMatch` | `variant_id: str \| None`, `categories`, `conditions`, `required_options`, `required_terms` (mismos tipos normalizados que `HardConstraints`) | al menos un criterio. Identifica **qué es** la variante, no su precio. `variant_id` se compara por igualdad y no existe en `HardConstraints` (§3.5) |
| `PurchaseBranch` | `match: BranchMatch`, `max_total_amount_minor: int \| None` (≥ 0, entero, COP), `only_if_previous_out_of_stock: bool = False` | la rama 0 no puede tener `only_if_previous_out_of_stock` |
| `ConditionalIntent` | `branches: tuple[PurchaseBranch, ...]` (0 a 3, en orden de prioridad), `max_total_amount_minor: int \| None` (límite global), `currency: Literal["COP"] \| None`, `excluded: tuple[BranchMatch, ...]` (0 a 5) | debe tener al menos una rama o una exclusión |

- **Intent efectivo de una rama.** Es `intent.hard` completo, endurecido con el `match` de la rama:
  - se conservan `max_unit_amount_minor`, `categories`, `conditions`, `required_options` y
    `required_terms` de `intent.hard`, y se les suman los del `match`, sin duplicados;
  - la moneda es COP;
  - el límite de total es el mínimo entre el de la rama, el global de `conditional` e
    `intent.hard.max_total_amount_minor`, sin contar los nulos. Si los tres son nulos, la rama no
    tiene límite de total y queda solo la condición de stock;
  - las preferencias van vacías.

  El efectivo **nunca es más amplio que el original**: toda variante que lo cumple también cumple
  `intent.hard`, y su límite no supera ninguno de los tres. Si el `match` de una rama contradice un
  filtro de `intent.hard`, la rama no es elegible (§3.2, paso 5), y si va antes de la elegida, la
  decisión es `clarify` (§3.3, paso 2).
- **Por qué se parte de `intent.hard`:** el prompt del router dice «Use max_total_amount_minor
  when the budget includes shipping or the final total» (`conversation_router.py:65`). Así que el
  modelo probablemente ponga el límite global en `intent.hard`, no en `conditional`. Si la rama
  descartara `intent.hard`, ese límite se perdería y la rama compraría sin él.
- **Las instrucciones del router ganan una sección corta.** Hay que llenar `conditional` cuando el
  usuario da:
  - alternativas ordenadas con condiciones;
  - prioridad explícita entre productos;
  - un fallback («si A se agota» → `only_if_previous_out_of_stock`);
  - o una prohibición («nunca B» → `excluded`).

  El orden de las ramas es la prioridad del usuario. Los límites por rama van en la rama y el que
  aplica a todo, en el global. **Si no está claro** qué producto es una rama, el orden, si el
  límite es por rama o global, o falta la moneda o la magnitud, el router responde `clarify`, como
  en UX11. Nunca inventa un límite.
- En un turno condicional, `intent` sigue siendo obligatorio. Alimenta la búsqueda que ve el
  comprador y es la base del intent efectivo de cada rama.

### 3.2 Resolución contra el feed, sin efectos

`resolve_branches(conditional, feed)` en `application/conditional.py` (nuevo):

1. Cada rama se resuelve a **exactamente una** variante del feed que cumple todos los criterios de
   su `match`. Se ignora la disponibilidad del feed, con el mismo truco que ya usa
   `governed_recovery.py:267`:
   `rejection_reason(candidate.model_copy(update={"available": True}), …)`.
2. Una rama con 0 coincidencias da `clarify`, con causa `branch_not_found`. Con más de una, también
   `clarify`, con causa `branch_ambiguous` y hasta tres títulos.
3. Las exclusiones marcan **todas** las variantes que coinciden. Si una exclusión no coincide con
   nada, no pasa nada.
4. También dan `clarify`:
   - dos ramas que resuelven a la misma variante (`duplicate_branch`);
   - una rama que coincide con una exclusión (`branch_excluded`);
   - una moneda distinta de COP, en `conditional` o en `intent.hard` (`currency`).
5. **Contradicción con `intent.hard`.** Con la variante resuelta, se aplica `rejection_reason` con
   el intent efectivo de la rama (§3.1), sin `max_unit_amount_minor` y otra vez con la
   disponibilidad ignorada:
   - si da un código, la rama queda `intent_mismatch`, con ese código, y su oferta no se lee
     (§3.3, paso 2);
   - aparte, si el precio unitario del feed supera `max_unit_amount_minor`, la rama queda marcada
     `feed_budget`. Ese `budget` no se decide con el feed, sino con la oferta (§3.3, pasos 4.4 y
     4.5).

   `max_unit_amount_minor` se quita de esa llamada porque `rejection_reason` devuelve el primer
   código que falla, y `budget` (`selection.py:60-64`) taparía una categoría, condición, opción o
   término que tampoco se cumpla.

Resolver no hace ninguna llamada al Merchant. Ni un `clarify` ni una lectura dejan efectos.

### 3.3 Evaluación con la oferta, en orden

`evaluate_branches(resolved, offers: OfferReader)`, donde `OfferReader` es el protocolo de
`offer_freshness.py:31-32`. En producción es `ACPClient` y en pruebas, un doble.

**Sin ramas** (solo exclusiones, como en UX07) no se evalúa nada y no se lee ninguna oferta. La
decisión es `exclusions_only`: la selección normal sigue con el feed filtrado (§3.6), y la fila de
`conditional_decisions` lleva los IDs excluidos. Desde `hp-router-v2`, esa selección lee en HP la
oferta de cada candidato para mostrarle el envío al modelo
([hp-shipping-before-choice.md](hp-shipping-before-choice.md)); son lecturas de la selección, no
de la evaluación.

Con ramas, para cada rama `i`, en orden:

1. Si la rama es `only_if_previous_out_of_stock` y alguna rama anterior no quedó `out_of_stock` ni
   `inactive`, queda `not_triggered`, sin leer su oferta.
2. Si la resolución la dejó `intent_mismatch`, queda así, sin leer su oferta, y se pasa a la
   siguiente. **No se salta:** si después alguna rama resulta elegible, la decisión no es esa
   rama, sino `clarify` con causa `intent_mismatch` en HP y una escalada en HNP (§3.9), como con
   `branch_excluded`. Así A no se salta sin un hecho comercial verificado. Si ninguna resulta
   elegible, la decisión es `none`. Una rama `intent_mismatch` después de la elegida no importa,
   porque no se llega a ella.
3. `get_offer(variant_id, LAB_DESTINATION)`. Cualquier `ACPClientError`, o cualquier otra
   excepción, deja la rama en `unknown`, con `error_code` y `status_code`, y la decisión es
   `undetermined`. **No se lee ninguna rama posterior.**
4. Con la oferta válida, el estado de la rama se decide en este orden:
   1. `product_status != "active"` → `inactive`;
   2. si no, `availability != "in_stock"` o `available_quantity < 1` → `out_of_stock`;
   3. si no, la variante aparece como no disponible en el feed del turno → `unknown`, con
      `feed_offer_mismatch` y la decisión `undetermined`. Esto es necesario porque `start` vuelve a
      rankear el feed (`purchase.py:281-296`) y la rechazaría;
   4. si no, con `max_unit_amount_minor` fijado, `pricing.items_total_minor > max_unit_amount_minor`
      → `over_limit`, por precio unitario (la cantidad es 1);
   5. si no, si la rama quedó `feed_budget` (§3.2, paso 5) → `unknown`, con `feed_offer_mismatch` y
      la decisión `undetermined`: la oferta cabe en `max_unit`, pero el feed no, y `start` la
      rechazaría;
   6. si no, `pricing.total_minor > límite efectivo` → `over_limit`. **La igualdad es elegible**;
   7. si no, `eligible`: la decisión es la rama `i`, salvo por el paso 2, y no se lee ninguna rama
      posterior.
5. Si se recorren todas sin una elegible, la decisión es `none`.

Cada rama guarda:
- su estado;
- el límite efectivo;
- de la oferta: `revision`, `product_status`, `availability`, `available_quantity`,
  `items_total_minor`, `shipping_total_minor`, `total_minor`, `expires_at` y
  `offer_snapshot_sha256` (`offer_snapshot.py:100-105`);
- en `unknown`, el código del error;
- en `intent_mismatch`, el código de `rejection_reason`;
- si quedó marcada `feed_budget`.

Las ramas que no se llegaron a evaluar quedan `not_evaluated`.

| Decisión | Etiqueta del oráculo de Maldo | Efecto |
|---|---|---|
| rama 0 | `A` | se prepara un checkout de la rama 0 |
| rama 1 | `B` | se prepara un checkout de la rama 1 |
| `none` | `none` | 0 checkouts, explicación por rama (UX05) |
| `undetermined` | `undetermined` | 0 checkouts, explicación técnica, sin pasar a B |
| `clarify` | no es una etiqueta: se registra como `capability_missing` o como fallo | 0 checkouts, pregunta. Sale de la resolución (§3.2) o de una rama `intent_mismatch` antes de la elegida (paso 2) |
| `exclusions_only` | no es una etiqueta: no hay ramas | la selección normal, sin las variantes excluidas (§3.6) |

### 3.4 Un solo checkout y verificación del total

- Para la rama elegida, `PurchaseService.start` recibe el **intent efectivo** de la rama (§3.1).
  La variante sobrevive al nuevo rankeo:
  - la resolución ya la pasó por `rejection_reason` con ese mismo intent, salvo `max_unit` (§3.2,
    paso 5);
  - la evaluación comprobó que el feed la da como disponible y dentro de `max_unit` (§3.3, pasos
    4.3 y 4.5).
- Después de `start`, el checkout ya listo se compara con la oferta de la decisión:
  `checkout.total_minor == offer.pricing.total_minor`. La comparación y la escritura de su fila
  `checkout_check` (§3.7) van en `application/conditional.py` (PR 2), y las usan HP y HNP. En HP,
  si el total difiere, aunque siga dentro del límite:
  - se cancela con la ruta existente `PurchaseService.cancel` (`purchase.py:481-499`), que ya
    funciona en el flujo gobernado (`tests/governance/test_hp_continuation.py:150`);
  - se registra `offer_changed`;
  - se le explica al comprador.

  El agente **no** prueba otra rama. HNP sigue §3.9.
- Si `start` lanza `budget_exceeded`, el checkout ya quedó cancelado. Solo pasa si la oferta cambió
  entre la lectura y la creación. En el camino condicional, `budget_exceeded` se responde como
  `offer_changed` en **todos** los modos, también en B0: no llega a REST como 409, porque si no el
  registro quedaría sin escribir.
- En el resto de la compra no cambia nada. La frescura en `present`/`approve`, la aprobación del
  comprador, la lógica de D2 y AP2 siguen igual. Solo cambia el digest del motor (D13).

### 3.5 Gobernanza: cómo interactúan los límites (leído)

En HP no hay un presupuesto global aparte del que dijo el comprador. POL-11 lee
`intent.max_total_minor` (`evaluator.py:106-116`, que viene de `provenance_sqlite.py:625`), y lo
mismo hacen POL-05 en D2 (`evaluator.py:440-452`) y el guardia de presupuesto (`purchase.py:582`).
Como el intent efectivo lleva el límite **efectivo**, los tres aplican ese límite sin cambiar la
lógica de D1 ni de D2:

- **D1** registra el intent efectivo y el feed del turno menos las exclusiones (§3.6). Su conjunto
  elegible es el rankeo de ese feed con ese intent (`enforce.py:287-291`; el replay lo recalcula
  en `d1_uow_sqlite.py:230-231`).
  - Si el `match` solo tiene `variant_id`, `HardConstraints` no lo puede expresar. El conjunto
    elegible de D1 es entonces **más amplio que la rama**: todas las variantes que pasan el resto
    del intent efectivo. POL-01 sigue pasando por pertenencia (`evaluator.py:149`).
  - No se restringe: hacerlo exige tocar los archivos medidos por D1 (§2.5). La rama la fija
    `evaluate_branches`, no D1.
- **POL-11**, precio unitario del feed ≤ límite efectivo: puede negar si el feed está desactualizado
  y su precio quedó por encima del total de la oferta. Esa negación usa el camino gobernado de hoy.
- **El límite efectivo nunca amplía** el global ni el de `intent.hard`, porque se toma el mínimo de
  los tres.
- **La lógica del motor no cambia, pero su digest sí**, por `priority` en `hnp_models.py` (§2.5,
  D13).

En HNP el global es el grant (POL-08 y el máximo del mandato). En este alcance, el límite de cada
rama es el del grant (§3.9). Las ramas HNP solo tienen `variant_id`, así que el conjunto elegible
de D1-HNP sigue siendo el de hoy: el rankeo ∩ el grant (`enforce.py:291`).

### 3.6 Exclusiones (UX07)

`excluded` se aplica **filtrando el feed del turno**: `without_variants(feed, excluded_ids)`, en
`application/conditional.py` y no en `acp/feed.py` (§2.5). El feed filtrado se usa:
- en `search_catalog` y en la lista corta que ve el modelo;
- en `recover_no_candidate`, sus alternativas y el contexto de `governed_recovery`;
- en `PurchaseService.start(feed=…)`, que ya acepta ese parámetro.

D1 registra ese mismo feed, así que **B no aparece ni en el conjunto elegible de D1**. El snapshot
de feed de D1 pasa a ser «el feed menos las exclusiones del comprador». Los IDs que se quitaron
están en la fila de `conditional_decisions` con el mismo `run_id` (§3.7). Sin exclusiones, el
snapshot es el feed de siempre.

Antes de `start` se vuelve a comprobar que la variante elegida no está excluida, como defensa en
profundidad. Si un router devuelve B de todos modos, el enum ya no la admite y se usa el camino de
`UnknownVariantSelection` y `outside_shortlist` de hoy. `search_catalog` informa
`rejections.excluded = n`, una clave nueva que el contrato ya admite
(`ui/src/lib/agent-contract.ts:111-117`).

En UX07, «prefiero A» sigue siendo una preferencia blanda: va a `preferences` y al rankeo de hoy.
Para esa parte no se usan ramas.

### 3.7 Registro

Hay una tabla nueva, `conditional_decisions`, en `SqliteAgentStore.initialize()`
(`persistence/sqlite.py`). Siempre existe, en todos los modos y en HP y HNP. Es append-only, con
triggers que impiden el UPDATE y el DELETE, como `hp_evidence`.

| Columna | Contenido |
|---|---|
| `run_id`, `sequence` | clave; el `run_id` es el del turno (`turn_run_id`) o el de la tarea HNP |
| `kind` | `decision` \| `checkout_check` |
| `outcome` | `branch` \| `none` \| `undetermined` \| `clarify` \| `exclusions_only` \| `checkout_matches` \| `offer_changed` |
| `branch_index`, `variant_id` | si hay una rama elegida |
| `body` | JSON canónico (`schema: "conditional-decision/1"`): modo, `intent_sha256`, ramas con su estado y los hechos de la oferta, IDs excluidos (también en `exclusions_only`), causa del `clarify`, `explanation_sha256` y, en `checkout_check`, el checkout y los dos totales |
| `recorded_at` | UTC |

- No guarda datos del comprador, tokens ni texto libre del modelo.
- Un turno repetido se resuelve desde el caché de `conversation_turns` y no agrega filas.
- Una tarea HNP reclamada de nuevo puede agregar otra decisión. El oráculo usa la última.
- **REST no cambia:** el oráculo lee esta tabla, como el bloque A lee `model_requests`.

### 3.8 Lo que ve el comprador

Los textos son deterministas, en español, y se arman con `format_minor_amount`.

| Caso | Texto (resumen) |
|---|---|
| Elegida la rama 0 | «Elegí {A}: es tu primera opción y el comercio confirma un total de {total} con envío, dentro de tu límite de {límite}.» |
| Elegida la rama 1 | «{A} no cumplía tu condición: {motivo}. Elegí {B}: total de {total} con envío, dentro de tu límite de {límite}.» |
| `none` | «No compré nada: ninguna de tus opciones cumple tus condiciones. {A}: {motivo}. {B}: {motivo}. No preparé ningún checkout ni compré otra alternativa.» |
| `undetermined` | «No pude verificar {A} con el comercio. Como {A} va primero, no pasé a {B} sin saber si {A} cumplía. No preparé ningún checkout. Intenta de nuevo en un momento.» |
| `offer_changed` | «El total de {X} cambió entre la verificación ({t1}) y el checkout ({t2}); cancelé ese checkout y no probé otra opción. No se hizo ningún cobro.» |
| `clarify` | «No encontré {A} en el catálogo», «Varias opciones coinciden con {A}: …; ¿cuál quieres?» o «{A} no cumple otra condición de tu pedido, así que no pasé a {B}. ¿Quieres revisar tu pedido?». Termina con «Todavía no preparé ningún checkout.» |

Los motivos son:
- «está agotado»;
- «no está a la venta»;
- «su total con envío es {total} y tu límite para esa opción es {límite}»;
- «su precio sin envío es {precio} y tu límite por unidad es {límite}»;
- «solo autorizaste {B} si {A} se agotaba»;
- «no cumple otra condición de tu pedido» (`intent_mismatch`).

La acción REST es `respond` para `none`, `undetermined` y `offer_changed`, y `clarify` para
`clarify`. UX06 dice «informar y escalar». En HP el comprador está presente, así que escalar
significa informarle y no comprar.

### 3.9 HNP: el mínimo

HNP no necesita un LLM para decidir: el comprador ya escribe su intención en un formulario tipado y
la consiente.

- **Prioridad consentida:** `ConsentScope`, `ScopeDisplay`, `ScopeView` y
  `DelegationProposalRequest` ganan `priority: tuple[Id, ...] | None`. Debe ser una permutación
  **completa** de `scope.variant_ids`, y `display.priority` debe ser igual a `priority`. Se
  **omite** de la forma canónica cuando es `None`, para que los grants guardados sigan parseando
  y su `scope_digest` conserve los bytes (§2.5; hace falta una prueba con un consentimiento
  existente). `DelegationScope` no cambia, y por lo tanto tampoco cambian `GrantFacts`, POL-08 ni
  los mandatos. **Sí cambia el digest del motor** de D1, D2 y D3, porque `hnp_models.py` está en
  `ENGINE_SOURCES` (§2.5, D13).
- **Ejecutor:** si `consent.priority` existe, `_execute` reemplaza `_shortlist` y `_select` por
  `resolve` + `evaluate_branches` (§3.3), en el orden de `priority`:
  - cada rama es un `BranchMatch` con solo `variant_id`;
  - el límite de cada rama es el `max_total_amount_minor` del intent del grant;
  - no hay `only_if_previous_out_of_stock`;
  - el lector de ofertas es el `ACPClient` que el runtime ya pasa como `merchant`, y se agrega como
    parámetro `offers` de `build_hnp_executor`.
- **Revisión prometida (D15):** `ScopeDisplay` y `ScopeView` ganan `preference_review: true`. Solo
  se incluye cuando el texto se va a revisar: hay `preference_text`, el modo es enforce y
  `AGENT_HNP_REVIEWER=llm`. En los demás casos se omite, para conservar los bytes, como
  `priority`. Lo calcula el backend en `propose`: `build_delegation_service`
  (`delegation/bootstrap.py:85-106`) le pasa al servicio si el texto se revisará. Sin
  `preference_text`, el campo no es válido. El cambio de bytes de `hnp_models.py` entra en D13.
- **Revisor (D8):**
  - con `preference_text` y `AGENT_HNP_REVIEWER=llm`, el consentimiento trae `preference_review`,
    y `_review` corre sobre la elección determinista, antes de `start`, como hoy. Recibe la lista
    corta de hoy (`rank_candidates` ∩ `variant_ids`);
  - si no la confirma, la tarea escala con `choice_not_confirmed`;
  - **si el consentimiento trae `preference_review` y el revisor no está configurado** o no está
    disponible, la tarea también termina con `choice_not_confirmed` en lugar de comprar. Sin
    revisor no hay evento `choice_reviewed`: el terminal es el registro;
  - con `priority`, manda lo consentido: sin `preference_review` no se revisa, aunque después se
    configure el revisor. Así la tarjeta sigue diciendo la verdad (§3.10);
  - sin texto o sin revisor configurado al proponer, decide solo el evaluador determinista;
  - con `none` o `undetermined` no hay elección, así que no hay revisión.

  | Resultado | Qué hace el ejecutor | Estado |
  |---|---|---|
  | rama | `_review` si el consentimiento trae `preference_review`; después `_start` con la variante y la verificación del total (§3.4) | — |
  | elección no confirmada, o revisión prometida sin revisor (D15) | `_end(run, "choice_not_confirmed", purchase=…)`, como hoy | escala en B2, `failed` en B0 |
  | `clarify` de la resolución (una variante de `priority` que no está en el feed) o `intent_mismatch` antes de la elegida (§3.3, paso 2) | `_end(run, "selection_unavailable")`, antes de cualquier creación | escala en B2, `failed` en B0 |
  | `none` | `_end(run, "no_candidate_in_scope")`, antes de cualquier efecto | escala en B2, `failed` en B0 |
  | `undetermined` | `_end(run, "selection_unavailable")`, sin `failure_kind` | escala en B2, `failed` en B0 |
  | el checkout no coincide con la oferta | `_end(run, "offer_changed", purchase=…, checkout=…)`. `_end` cancela el checkout solo si la tarea escala (`hnp_execution.py:868-869`) | escala en B2; en B0, `failed` con el checkout abierto hasta que expira |

  En B0, dejar el checkout abierto es lo que ya hace hoy `_check_freshness` (:711-719) con una
  oferta cambiada. HP sí lo cancela (§3.4).

  No hay códigos nuevos ni migración de CHECK (§2.5). La decisión va a `conditional_decisions` con
  el `run_id` de la tarea.
- **Textos:** `escalation.no_candidate_in_scope` y `escalation.selection_unavailable`
  (`delegation/contract.py:90-99`) pasan a ser neutros respecto a la causa. Por ejemplo: «Ninguna
  de las opciones que autorizaste se puede comprar ahora dentro de lo que autorizaste» y «No pude
  elegir con seguridad entre las opciones que autorizaste». El segundo cubre también el `clarify`
  y el `intent_mismatch` de la fila anterior, donde no falló ningún servicio.
- **UX07-HNP:** el ejecutor no cambia. «Nunca B» significa que B no está en `variant_ids`, y eso ya
  se hace cumplir. `_offer_delegation` (`conversation.py:671-699`) quita las variantes excluidas
  del borrador y, si las ramas se resuelven, propone `priority` en su orden. Los límites por rama
  no pasan al borrador; el total lo escribe el comprador. Un turno de delegación no evalúa ramas:
  si trae exclusiones, escribe una fila `exclusions_only` con los IDs excluidos.
- **Fuera del mínimo:** límites por rama dentro del grant, `only_if_previous_out_of_stock` en HNP y
  la prohibición escrita solo en `preference_text` (para esa sigue estando el revisor opcional).

### 3.10 UI (Storybook primero)

- **HP:** `SelectProductResult` (`ui/src/lib/agent-contract.ts:123-127`) gana un campo opcional
  `conditional?: { outcome, chosen_index, branches: [...] }`. `decision` sigue siendo
  `"select" | "clarify"`, así que no rompe nada. Un componente
  `components/agent/conditional-branches.tsx` muestra, por rama:
  - el título;
  - el estado;
  - el total con envío y el límite;
  - si es la elegida.

  Hace falta una historia por resultado: A, B, `none`, `undetermined` y `offer_changed`, con axe en
  modo error. `exclusions_only` no muestra el componente: la exclusión ya se ve en
  `rejections.excluded` (§3.6).
- **HNP:**
  - el formulario de delegación ordena las opciones marcadas, con botones para subir y bajar;
  - la tarjeta de consentimiento dice «Compra la primera opción disponible dentro del total, en este
    orden: 1. … 2. …»;
  - la frase de revisión sale **solo con `preference_review`** (D15): «Antes de comprar, el agente
    revisa tu indicación. Si no confirma que la opción elegida la respeta, no compra y te avisa.»;
  - con `priority`, indicación y sin `preference_review`, la tarjeta dice la verdad: «Tu indicación
    queda guardada, pero no cambia la elección ni se revisa antes de comprar.» Sin `priority`, la
    tarjeta queda como hoy, porque ahí el selector LLM sí puede usar la indicación;
  - la tarjeta de escalamiento usa los textos neutros;
  - `chatDraft` toma `priority` del borrador.

### 3.11 Compatibilidad con el protocolo P3 de Maldo

| Regla del protocolo | Cómo la cumple el producto |
|---|---|
| Cantidad 1, COP, montos enteros en unidades menores | `OfferPricing` es entero y COP, y el checkout es de cantidad 1 (`_validate_created_checkout`). Los límites son `int` ≥ 0 y no hay floats en ningún lado |
| Elegibilidad = condición + stock + total del Merchant ≤ límite de **esa** rama, con igualdad aceptada | §3.2, paso 5, y §3.3, pasos 2 y 4.1 a 4.7. Una rama que contradice `intent.hard` no se salta para comprar la siguiente. El límite es por rama y su efectivo nunca amplía el global ni el de `intent.hard` |
| Prioridad de A | se evalúa en orden y la primera elegible gana. Si A es elegible, B no se lee |
| `UNKNOWN` → `undetermined`, sin caer a B | §3.3, paso 3: cualquier error de lectura detiene la evaluación. Un 401, 429, timeout o JSON inválido nunca se convierte en `out_of_stock` ni en `over_limit` |
| D/UX05 = `none` + explicación, sin C | §3.3, paso 5 y §3.8. C nunca se resuelve ni se lee |
| Como máximo un checkout en el episodio, sin explorar | se decide con la oferta y se crea un solo checkout para la rama elegida. `none`, `undetermined` y `clarify` no crean ninguno |
| Operación distinguible para verificar A (DEP-11) | `POST /lab/offers/get` con `offer_id=A` queda en el proxy y en `run_events` (`offer.read`) del Merchant, que son testigos independientes del agente |
| El checkout elegido se contrasta con el estado vigente y no hay fallback automático | §3.4 |

Lo que **no** cubre: la calibración por clones (A-cal, B-cal, V, E) y el sello del oráculo son del
arnés de Maldo. El producto no los necesita para decidir.

## 4. Celdas y oráculos

### 4.1 Par, totales y límites

- **El par A/B se elige por reglas registradas antes de correr, no por IDs fijos.** Se reutilizan
  las `pair_rules` de P2 (`evaluation/scenarios/p0/merchant-provenance.json:604`, en `main` desde
  #157): A = Sony Xperia 5 II, 256 GB, nuevo; B = OnePlus 8T, 256 GB, nuevo.
- **Las reglas comparan atributos exactos:** marca, título, `storage`, condición y stock
  (`tests/integration/test_live_p0_catalog_bridge.py:82-97`). El `BranchMatch` compara términos por
  subcadena. Por eso el `BranchMatch` guionado lleva el `variant_id` que resuelvan las reglas, no
  términos.
- **El PR 6 resuelve el par** en el catálogo del episodio. En el catálogo del Merchant abf30df hay
  Xperia 5 II y 5 IV, pero no 5 III. Si una regla da 0 o más de una coincidencia, la celda se
  bloquea como `insufficient_fixture_data`, sin cambiar de par.
- **Totales:** `T_A` y `T_B` son el `pricing.total_minor` de cada oferta. Los lee el worker del
  Merchant en solo lectura (`observe_offer`, `merchant_control/core.py:211-217`) antes del turno, y
  quedan registrados con su `revision`.
- **Límites:** `T−1` es una unidad menor, sin floats. Si `T = 0`, se bloquea.
- **UX03-C** exige `T_B < T_A`. Si no se cumple, esa celda queda bloqueada; no se cambia de par.

### 4.2 HP

Todas las celdas corren en B2-HP (enforce, flujo gobernado), como `HpAgent` del bloque A
(`tests/integration/test_p0_model_failures.py:352`), y en la superficie de chat. La decisión
observada es la última fila `decision` de `conditional_decisions` para el `run_id` del turno. El
router devuelve la `ConditionalIntent` guionada: es un router estático, así que estas celdas
prueban el arnés y el producto, no la interpretación (§4.6).

| Celda | Estímulo | Oráculo |
|---|---|---|
| UX03-A | límite A = `T_A`, límite B = `T_B−1` | etiqueta `A`. Antes de la creación, 1 lectura (A). 1 POST de creación para A; compra en `awaiting_confirmation` con la variante A; B sin leer (`not_evaluated`) |
| UX03-B | límite A = `T_A−1`, límite B = `T_B` | etiqueta `B`. Lecturas A y B; A queda `over_limit` con el total `T_A`. 1 creación para B |
| UX03-C ≡ UX04-HP | límites `T_A` y `T_B`, con `T_B < T_A` | etiqueta `A` aunque B cueste menos. 1 lectura (A) y 1 creación para A. El LLM de selección no se consulta (1 sola solicitud al modelo, la del router) |
| UX03-D ≡ UX05-HP | límites `T_A−1` y `T_B−1`; C, más barato, está en el catálogo | etiqueta `none`. Lecturas A y B, las dos `over_limit`. **0 efectos**; la explicación nombra las dos causas y no menciona C; C nunca se lee |
| UX03-UNKNOWN-A | límites como en C; fallo `drop` del proxy en `/lab/offers/get` | etiqueta `undetermined`. Exactamente 1 intento de lectura, el de A: en el proxy (con `offer_id=A` en el cuerpo) y 1 `offer.read` de A en el Merchant, porque `drop` reenvía. B no se lee. **0 efectos.** Sin fallo alcanzado, la celda es inválida, no `passed` |
| UX06-HP | «si A se agota, compra B», límite global `T_B−1`; A con stock 0 (`edit_offer` `stock` antes del turno) | etiqueta `none`. A queda `out_of_stock` y B `over_limit`. **0 efectos.** El mensaje informa y no compra |
| UX06-HP-ctl | igual, con límite global `T_B` | etiqueta `B`, con 1 creación para B |
| UX06-HP-hard | igual que UX06-HP, pero sin límites en `conditional`: ni por rama ni global. `intent.hard.max_total_amount_minor` = `T_B−1` | etiqueta `none`. B queda `over_limit` con límite efectivo `T_B−1`, que sale de `intent.hard` (D14). **0 efectos** |
| UX07-HP-a | «prefiero A, nunca compres B», con B más barato | una fila `exclusions_only` con B; ninguna lectura de decisión. B no está en la lista corta que recibe el modelo (enum de la solicitud) ni en el conjunto elegible de D1. Se crea 1 checkout, que no es de B; con el selector guionado que toma el primero, es A |
| UX07-HP-b | igual, con el selector guionado devolviendo B a pesar del enum | `UnknownVariantSelection` y explicación gobernada; 0 creaciones |

Como en Maldo, UX03-D y UX05-HP comparten un episodio etiquetado con las dos identidades y un solo
denominador. Lo mismo pasa con UX03-C y UX04-HP (§8, D10).

### 4.3 HNP

Todas las celdas corren en B2-HNP. Las que no dicen otra cosa usan el `HnpAgent` de proceso
propio, que rechaza las variables `OPENAI_*` y por eso no tiene revisor: decide solo el evaluador.
Las celdas `-rev` usan `ModelHnpAgent(reviewer=True)` (`tests/integration/live_model.py:31-57`),
con selector y revisor LLM sobre `ModelServer`. Con `priority`, el selector nunca se consulta, y
eso también se comprueba.

| Celda | Estímulo | Oráculo |
|---|---|---|
| UX04-HNP | grant {A, B}, `priority` [A, B], total ≥ max(`T_A`, `T_B`) | se compra A aunque B sea más barato; 1 checkout y la orden en sandbox. La decisión queda como rama 0 en `conditional_decisions`. No hay `choice_reviewed` |
| UX04-HNP-rev | igual, con `preference_text` (el consentimiento trae `preference_review`) y el revisor guionado que confirma | se compra A; `choice_reviewed` con `confirmed`; exactamente 1 solicitud al modelo, la del revisor, y 0 del selector |
| UX04-HNP-rev-no | igual, con el revisor guionado que no confirma | `escalated` con `choice_not_confirmed`; lecturas {A: 1}; 0 creaciones; la compra que crea `_review` queda `canceled` (`hnp-intent-review.md` §6) |
| UX05-HNP | `priority` [A, B], total = min(`T_A`, `T_B`) − 1; C fuera del grant | `escalated` con `no_candidate_in_scope`; decisión `none` (A y B `over_limit`); lecturas {A: 1, B: 1}; **0 efectos**, que incluyen 0 reservas y 0 mandatos cerrados |
| UX06-HNP | `priority` [A, B], A con stock 0, total = `T_B−1` | `escalated` con `no_candidate_in_scope`; A queda `out_of_stock` y B `over_limit`; lecturas {A: 1, B: 1}; **0 efectos** |
| UX06-HNP-ctl | igual, con total `T_B` | se compra B; 1 checkout |
| UX07-HNP | turno de delegación «prefiero A, nunca compres B», con el router guionado y `excluded` = [B]. El arnés publica el borrador tal como llega | una fila `exclusions_only` con B. B no está en `delegation_draft.offers`, ni en `scope.variant_ids`, ni en ningún checkout u orden |

**UX06-HNP mide prioridad, no «solo si se agota».** Por D6, HNP no tiene
`only_if_previous_out_of_stock`: B se lee porque va segunda, no porque A se agotó. Por eso la
cobertura de UX06 en la matriz es parcial (§6).

Fuera de la matriz, pero en CI:
- `undetermined` en HNP (lectura de A con 503) da `selection_unavailable`, sin leer B y con 0
  efectos;
- con revisor y sin `preference_text`, el modelo recibe 0 solicitudes y se compra A.

**Celdas de D15**, también en CI (sin red ni gasto):

| Celda | Dónde | Oráculo |
|---|---|---|
| D15-tarjeta-rev | API de `propose` en B2 con `AGENT_HNP_REVIEWER=llm` y `preference_text`; historia de la tarjeta | `display.preference_review: true`; la tarjeta muestra la frase de revisión |
| D15-tarjeta-sin-rev | API de `propose` con `preference_text` y revisor `off`, en B2 y en B0; historia de la tarjeta | el campo se omite; la tarjeta dice «Tu indicación queda guardada, pero no cambia la elección ni se revisa antes de comprar.» |
| D15-sin-revisor | `Bench`: consentimiento con `priority` y `preference_review: true`, ejecutado sin revisor | `escalated` con `choice_not_confirmed`; 0 creaciones; ningún `choice_reviewed` |

### 4.4 «0 efectos»

Tiene dos partes, adaptadas del bloque A (`zero_effects`, `test_p0_model_failures.py:289-318`).

- **Fuera del agente:**
  - `zero_effects.merchant_no_post` cuenta hoy **todos** los POST al Merchant (:300). Leer una
    oferta también es un POST: `POST /lab/offers/get` (`acp/client.py:124-131`). El chequeo nuevo
    excluye esa ruta de forma explícita y exige 0 POST en todas las demás, de creación (normal o
    guardada), de lookup o de cualquier otra;
  - un chequeo aparte, `zero_effects.offer_reads`, exige el conteo exacto de `POST /lab/offers/get`
    por `offer_id` (tabla de abajo);
  - ninguna fila nueva en `checkout_sessions` del run;
  - `catalog_offers` con la misma revisión;
  - ninguna orden;
  - PSP: snapshot igual y 0 tokens;
  - el resto de las tablas del Merchant, iguales.

  La excepción es **`run_events`**: su diferencia solo puede tener eventos `offer.read`, y por
  `offer_id` tienen que ser los mismos de la tabla. Un evento de otro tipo falla la celda. Leer no
  es un efecto comercial, pero cuenta como evidencia de qué se verificó.

  | Celda | Lecturas por `offer_id` (POST en el proxy y `offer.read` en el Merchant) |
  |---|---|
  | UX03-D ≡ UX05-HP | {A: 1, B: 1} |
  | UX03-UNKNOWN-A | {A: 1}; `drop` reenvía, así que el Merchant registra la lectura |
  | UX06-HP y UX06-HP-hard | {A: 1, B: 1} |
  | UX05-HNP | {A: 1, B: 1} |
  | UX06-HNP | {A: 1, B: 1} |
  | UX04-HNP-rev-no | {A: 1} |

  **En UX06, el snapshot «antes» se toma después de `edit_offer`.** `edit_offer` sube la revisión
  de A en `catalog_offers` (`merchant_control/offers.py:137-140`). Si el «antes» fuera previo, ese
  cambio del estímulo contaría como efecto del agente.
- **En el ledger del agente:** ninguna fila nueva en `purchases`, `approvals`, `orders`,
  `completion_results`, `grant_reservations`, `dispatch_outbox`, `ap2_evidence`,
  `governance_evidence` (D1 no corrió) ni `hp_evidence`, y ningún mandato cerrado. Tiene que haber
  **exactamente una** fila `decision` nueva en `conditional_decisions`, con el resultado esperado.
  En HNP, la tarea queda en un estado terminal sin una compra que haya avanzado. UX04-HNP-rev-no es
  la excepción en `purchases`: `_review` crea la compra antes de revisar y la tarea la cierra en
  `canceled`, como en INF33-HNP.

En las celdas con checkout, las lecturas de decisión son las que ocurren **antes** del primer POST
de creación. Las de frescura (`present`, `close`) vienen después y no cuentan. El arnés cancela el
checkout en el teardown y lo verifica.

### 4.5 Control positivo

- **Por modo:** UX03-A, UX03-B y UX06-HP-ctl en HP, y UX04-HNP, UX04-HNP-rev y UX06-HNP-ctl en
  HNP, crean exactamente un checkout. Así se demuestra que el arnés no bloquea todo.
- **Regresión:** el pedido no condicional del bloque A (`PHONE_REQUEST`) sigue preparando su
  checkout igual que hoy. Su D1 conserva la identidad del algoritmo (`eligibility_algorithm_ref` y
  `ENGINE_VERSION`), porque los archivos medidos no cambian. El `artifact_digest` del motor sí
  cambia (§2.5, D13), y el PR 6 registra el nuevo.

### 4.6 Qué prueba el modelo guionado y qué necesita un LLM real

| Lo prueba `ModelServer`/`ScriptedModel` (determinista, sin costo) | Necesita un LLM real (corrida pagada que inicia Yair) |
|---|---|
| Dada una `ConditionalIntent`, la decisión, la etiqueta, el orden de las lecturas, que haya como máximo un checkout, que `UNKNOWN` falle cerrado, los textos, el registro y los 0 efectos | Que el router convierta la frase en la `ConditionalIntent` correcta: orden, límites por rama o globales, «si se agota» frente a «si no», prohibición frente a preferencia, magnitudes en COP |
| Que el modelo de selección no puede elegir B, porque no está en el enum ni en D1 | Que ante ambigüedad el router pida aclaración y no invente límites |
| HNP con `priority` sin revisor: no hay LLM en ese camino. Con revisor, el camino de `choice_not_confirmed` | La calidad del borrador de delegación (prioridad y exclusiones propuestas) y el juicio del revisor sobre `preference_text` |

Un modelo guionado **no** acredita comprensión: ninguna celda de §4.2 cuenta como evaluación de
interpretación. En el campo `execution` de la matriz, cada celda con router o revisor guionado se
etiqueta como tal. Como dice P3 §5: «un router estático que recibe la etiqueta esperada solo
prueba el arnés».

## 5. Cómo se prueba

1. **CI de cada PR** (sin red ni gasto), con TDD y SQLite real en `tmp_path`:
   - **unitarias:** resolución y evaluación con un `OfferReader` doble, con un caso por estado
     (incluidos `not_triggered`, `intent_mismatch`, `feed_offer_mismatch`, la igualdad `T`, `T−1`
     y cada código de `ACPClientError`), y `exclusions_only`;
   - **reglas de D14:** una rama `intent_mismatch` antes de la elegida da `clarify`, y después de
     ella no cambia nada; con `max_unit`, la oferta por encima da `over_limit` y la oferta dentro,
     con el feed por encima, da `feed_offer_mismatch`;
   - **intent efectivo:** un caso por campo de `intent.hard` que demuestra que el efectivo nunca es
     más amplio; UX06 con el límite solo en `intent.hard`; la verificación del total con totales
     iguales y distintos;
   - **de API con `ModelServer`:** REST y chat de las celdas de §4.2, contra un Merchant doble que
     sirve `/lab/offers/get` y cuenta las creaciones;
   - **HNP:** con el `Bench` de `tests/support/hnp_executor.py`, con y sin revisor, con y sin
     `preference_text` y las celdas de D15 (§4.3);
   - **serialización:** un consentimiento viejo sigue siendo canónico y conserva su
     `scope_digest`.
2. **Evidencia P0:** un episodio por celda contra el stack vivo (Merchant, PSP y PostgreSQL
   efímero) con el modelo guionado, corrido en local con `RUN_LIVE_STACK=1`.
   - Se reutiliza el arnés del bloque A: `HpAgent`, `ScriptedModel`, `recorded_case`, `p0_case`,
     `verdict` y `ledger` de `test_p0_model_failures.py`; `HnpAgent` y `ModelHnpAgent` de
     `live_model.py`; y `FaultProxy`, `edit_offer` y `observe_offer`.
   - Son 3 repeticiones por celda, como en el bloque A.
   - El archivo nuevo es `tests/integration/test_p0_conditional_intent.py`.
3. **Interpretación con un LLM real (después):**
   - es un corpus sellado de la frase natural de cada celda más dos paráfrasis, con una repetición;
   - corre sobre la infraestructura de EXP-06 (`docs/design/exp-06-semantic-hp.md`), que ya tiene
     reserva de costo;
   - lo inicia Yair a mano con un tope de gasto. Nunca corre en CI (`AGENTS.md`).

## 6. Documentación

- **Spec** (PR 1): este archivo.
- **Prerregistro** (PR 6): `evaluation/scenarios/p0-conditional-intent.json`. Se congela antes de
  correr y contiene reglas del par, celdas, guiones, oráculos y repeticiones; su hash va en el
  informe.
- **Resultados** (PR 6): `evaluation/results/p0-conditional-intent/README.md`, `summary.json` y
  JUnit, sin bases de datos. Registran el digest nuevo del motor (D13).
- **Matriz** (PR 6): en UX03 a UX07, los campos `coverage`, `implementation_status` y `execution`
  de `matrix.json`, con enlace a la evidencia.
  - UX06 queda con cobertura parcial: en HNP se mide prioridad, no «solo si se agota» (§4.3).
  - En `execution`, las celdas con router o revisor guionado se etiquetan como tales (§4.6).
- **Contrato de la UI** (PR 2): `docs/agent-ui-contract.md`, con el campo `conditional` de
  `select_product`.
- **HNP** (PR 4):
  - `docs/design/hnp-governed.md`: `priority` y `preference_review` en el consentimiento, y los
    textos neutros (:1026-1028);
  - `docs/design/hnp-intent-review.md` §6: el revisor corre según `preference_review`, y sin
    revisor una revisión prometida escala (D15).

## 7. PRs

**Los PRs 3, 4 y 5 arrancan en paralelo apenas se fusione el PR 2.** El PR 2 deja en `main` todos
los contratos compartidos: los tipos Python y TS, la tabla, `priority`, `DelegationDraftView` y la
verificación del total con su fila (§3.4). Por eso ninguno de los tres necesita al otro para
empezar. Cada archivo tiene un solo dueño:

| PR | Archivos que toca (solo este PR) |
|---|---|
| 3 HP | `src/commerce_agent/application/conversation.py`; `src/commerce_agent/runtime.py`, solo en el bloque `ConversationService(...)` (:249-264); `tests/test_conditional_hp.py` (nuevo); `tests/assistant_support.py`; `ui/src/storybook/fixtures/assistant-transport/` (los `conditional-*.sse` nuevos y el `README.md` que regenera `scripts/export_assistant_fixtures.py`) |
| 4 HNP | `src/commerce_agent/application/hnp_execution.py`; `src/commerce_agent/delegation/bootstrap.py`; `src/commerce_agent/runtime.py`, solo en el bloque `build_hnp_executor(...)` (:286-300); `src/commerce_agent/delegation/service.py` (`propose`); `src/commerce_agent/delegation/contract.py` (`HNP_MESSAGES`); `tests/test_delegation_contract.py`, que fija el texto de `escalation.selection_unavailable` (:114-117); `tests/test_delegation_api.py`, que fija el mismo texto (:737-741); `tests/delegation/test_hnp_conditional.py` (nuevo); `docs/design/hnp-governed.md` (`priority` y los textos de :1026-1028); `docs/design/hnp-intent-review.md` (§6) |
| 5 UI | `ui/src/components/agent/conditional-branches.tsx` y sus historias (nuevos); `ui/src/components/delegation/` (formulario, tarjeta de consentimiento y `delegation-copy.ts`); `ui/src/components/trace/trace-copy.ts`, con la copia neutra (:613 y :616); `ui/src/lib/delegation-draft.ts` (`chatDraft`); `ui/src/features/agent-tool-uis.tsx`, `ui/src/features/agent-results.ts` y `ui/src/features/delegation-in-chat.tsx`. Al final, y solo después del PR 3: `ui/src/storybook/assistant-transport.ts` (registra los SSE) y `ui/e2e/` |

- **Único archivo compartido:** `runtime.py`. Lo tocan los PRs 3 y 4, cada uno en su bloque, y el
  que se fusione segundo rebasa sin conflicto de contenido.
- **Fuera de su PR:** ninguno de los tres toca `domain/`, `llm/conversation_router.py`,
  `persistence/sqlite.py`, `governance/hnp_models.py`, `application/conditional.py`,
  `ui/src/lib/agent-contract.ts` ni `ui/src/lib/delegation-contract.ts`. Esos archivos son del
  PR 2. Si alguno necesita un cambio de contrato, va en un PR de contrato aparte, no dentro del 3,
  el 4 o el 5.

| # | PR | Contenido | Archivos | Tamaño | Depende de |
|---|---|---|---|---|---|
| 1 | Esta spec | — | `docs/design/p0-conditional-intent.md` | — | — |
| 2 | Contrato y evaluador puro | tipos de §3.1 y el campo `CommerceDecision.conditional` con su sección del prompt; el intent efectivo (§3.1); `resolve_branches` con `intent_mismatch`, `evaluate_branches` con `exclusions_only` y `without_variants`, puros (§3.2, §3.3, §3.6); la comparación checkout/oferta y la escritura de `checkout_check` (§3.4); la tabla `conditional_decisions` y sus métodos (§3.7); `priority` en `ConsentScope`, `ScopeDisplay`, `ScopeView`, `DelegationProposalRequest` y `DelegationDraftView`, omitido si es `None`; `preference_review` en `ScopeDisplay`, `ScopeView` y TS, omitido si no aplica y válido solo con texto (D15); el cambio del digest del motor aceptado (D13); tipos TS y fixtures JSON validadas por los dos lados | `domain/conditional.py` (nuevo), `application/conditional.py` (nuevo), `llm/conversation_router.py`, `persistence/sqlite.py`, `governance/hnp_models.py`, `delegation/service.py` (solo el request), `delegation/contract.py` (solo `ScopeView`), `ui/src/lib/agent-contract.ts`, `ui/src/lib/delegation-contract.ts`, `ui/src/lib/delegation-draft.ts` (tipo del request), `docs/agent-ui-contract.md`, fixtures y tests | M, ~1,5 h | 1 |
| 3 | HP: integración | `_after_search` y `select_product` deterministas; feed filtrado por exclusiones; intent efectivo en `start`; cancelación si el total no coincide (§3.4); arista condicional después de `prepare_checkout` en todos los modos; textos (§3.8); registro; borrador con exclusiones, prioridad y la fila `exclusions_only`; `offer_reader` en `ConversationService` y runtime; fixtures SSE (`scripts/export_assistant_fixtures.py`) | `application/conversation.py`, `runtime.py` (:249), `tests/test_conditional_hp.py`, `tests/assistant_support.py`, `ui/src/storybook/fixtures/assistant-transport/conditional-*.sse` | M-L, ~2 h | 2 |
| 4 | HNP: ejecutor | camino `priority` en `_execute`, con el revisor según `preference_review` (D8, D15), el registro, los finales reutilizados y la verificación del total (§3.9); `propose` calcula `preference_review` con lo que le pasa `build_delegation_service`; sin revisor, una revisión prometida escala (D15); `offers` en `build_hnp_executor`; textos neutros; `propose` guarda `priority`; documentos HNP de §6 | `application/hnp_execution.py`, `delegation/bootstrap.py`, `runtime.py` (:286), `delegation/service.py` (`propose`), `delegation/contract.py` (`HNP_MESSAGES`), `tests/test_delegation_contract.py`, `tests/test_delegation_api.py`, `tests/delegation/test_hnp_conditional.py`, `docs/design/hnp-governed.md`, `docs/design/hnp-intent-review.md` | M, ~1,5-2 h | 2 |
| 5 | UI y Storybook | `conditional-branches.tsx` con sus historias; formulario ordenable; tarjeta de consentimiento con el orden y la copia de revisión según `preference_review` (D15); copia neutra; `chatDraft` con prioridad; al final, E2E del recorrido condicional con el SSE del PR 3 | `ui/src/components/agent/`, `ui/src/components/delegation/`, `ui/src/features/`, `ui/e2e/` | M, ~2 h | 2; el E2E, del 3 |
| 6 | Evidencia | prerregistro; `test_p0_conditional_intent.py` con el ajuste de 0 efectos para `run_events` y `POST /lab/offers/get` (§4.4); par resuelto por las reglas (§4.1); corrida local con `RUN_LIVE_STACK=1`; informe con el digest nuevo del motor; matriz (§6) | `evaluation/scenarios/`, `evaluation/results/`, `tests/integration/` | M, ~2-3 h con la corrida | 3 y 4 |

Cada PR lleva revisor independiente, compuerta local (`docs/testing.md`) y CI en verde. Con
agentes, el camino crítico es 2 → 3/4 → 6, unas 5 a 6 horas.

## 8. Decisiones

**Registradas el 5-nov-2026, por delegación de Yair.** D1 a D11 se aceptaron tal como se
recomendaron. D12 queda abierta: se fija cuando se corra el lote pagado. **Ajustes del mismo día,
tras la revisión independiente del PR #183:** D8 se reemplaza; D2, D5, D6, D7 y D11 se precisan;
D13 y D14 son nuevas. **Segunda ronda:** D15 es nueva; D5, D8 y D14 se precisan.

| # | Decisión | Recomendación |
|---|---|---|
| D1 | ¿Capacidad en todos los modos o solo en B2? | **Todos.** Es capacidad de producto, no un control de gobernanza, así que la diferencia B0/B2 no se le atribuye a la PDP. La evidencia P0 corre en B2, como el bloque A. Consecuencia: B0 lee ofertas en los turnos condicionales |
| D2 | ¿Decidir con la oferta o con checkouts exploratorios? | **Con la oferta**, con un solo checkout. El Merchant copia el pricing de la oferta (§2.4) y la frescura ya compara `total_minor`. Las lecturas quedan en `run_events`, así que «0 efectos» las admite, contadas (§4.4). **Aceptada.** **Nota sobre P3:** «ausencia de efectos en E» (`docs/design/p3-point5-validation.md:27` en #176) se refiere a E antes del primer turno: en el validador, `compare` exige que el estado de E no cambie durante la calibración (`p3_protocol.py:201-203`). Durante el turno, P3 §5 pide una operación de verificación distinguible de A. La lectura de la oferta es esa operación: es esperada, no tolerada, y el oráculo la cuenta exacta por `offer_id` (§4.4) |
| D3 | ¿Qué cuenta como desconocido? | **Todo** `ACPClientError` (incluidos `offer_expired` y `OFFER_NOT_FOUND`, que llega como `invalid_offer`) y la discrepancia entre feed y oferta. Solo los hechos verificados hacen falsa una rama |
| D4 | ¿El total del checkout distinto del de la oferta, pero más bajo? | **Detenerse igual:** igualdad estricta. Es simple y comparable con el protocolo de Maldo |
| D5 | ¿Ambigüedad? | **Aclarar**, como en UX11: rama con 0 o más de una variante, rama excluida, o moneda o magnitud ausente. Una rama que contradice `intent.hard` no es ambigua: no es elegible, y si va antes de la elegida, se aclara (D14) |
| D6 | ¿Alcance de HNP? | **Solo prioridad**, sobre el total único del grant. Sin límites por rama ni «solo si se agota» en HNP. Las celdas UX04 a UX06 de HNP funcionan con el total del grant. Por eso UX06-HNP mide prioridad, no «solo si se agota» (§4.3) |
| D7 | ¿Códigos HNP nuevos? | **No:** `no_candidate_in_scope`, `selection_unavailable`, `offer_changed` y `choice_not_confirmed`, los dos primeros con textos neutros. Así no hay migración de CHECK |
| D8 | ¿El revisor HNP con prioridad? | **Corre si hay texto.** Con `preference_text` y `AGENT_HNP_REVIEWER=llm`, revisa la elección determinista; si no la confirma, la tarea escala con `choice_not_confirmed`. Sin texto o sin revisor configurado, decide solo el evaluador determinista. La promesa de revisión va en el consentimiento (D15). Reemplaza la recomendación anterior («se omite»): hoy el revisor revisa lo que devuelva `_select` (`hnp_execution.py:493`), y `chatDraft` llena `preference_text` con el mensaje del comprador |
| D9 | ¿`T` desde la oferta (worker) o calibrado con checkouts en clones? | **Desde la oferta** para nuestra evidencia. La corrida formal P3 usa la puerta de Maldo (#176), que el producto ya respeta |
| D10 | ¿Deduplicar UX04/UX05-HP con UX03-C/D? | **Un episodio con las dos identidades**, como propone Maldo. HNP lleva celdas propias |
| D11 | ¿Exclusión solo en la aplicación o también en D1? | **También en D1**, con el feed filtrado: B no aparece como elegible en ninguna evidencia. El snapshot de feed de D1 pasa a ser «el feed menos las exclusiones del comprador», y los IDs excluidos están en `conditional_decisions` con el mismo `run_id` (§3.6). La evidencia de los pilotos no cambia por esto, porque sin exclusiones el snapshot es el feed de siempre. Su replay con `main` sí cambia, por D13 |
| D12 | ¿Corpus del LLM real? | **Abierta:** se fija cuando se corra el lote pagado. La propuesta sigue siendo la frase de cada celda + 2 paráfrasis y 1 repetición, con un tope de costo que fija Yair, después del PR 6 |
| D13 | ¿Qué pasa con la identidad del motor? (§2.5) | **Se acepta el cambio de forma explícita.** `ENGINE_VERSION` sigue en `reference-d1-d2-d3:3` mientras la lógica del evaluador no cambie. El digest nuevo queda registrado en la evidencia del PR 6. Los pilotos se reproducen sobre su etiqueta: `hnp-pilot-v1-base` y `hnp-review-pilot-v1-base` (el HP v2, sobre `pilot-v2-base`, ya usaba otro motor). `priority` se omite cuando es `None`, para que `scope_digest` conserve los bytes de los consentimientos guardados. Se conserva la identidad del algoritmo; la del artefacto cambia |
| D14 | ¿Qué parte de `intent.hard` llega a la rama? | **Todo.** El intent efectivo es `intent.hard` endurecido con el `match` (§3.1). El límite de total es el mínimo entre la rama, el global y `intent.hard`, sin los nulos. Una rama que contradice `intent.hard` no es elegible. Si va antes de la elegida, no se salta: da `clarify` en HP y escala en HNP, como `branch_excluded`. `budget` por `max_unit` se decide con la oferta: `items_total_minor` > `max_unit` es `over_limit`; si no, `feed_offer_mismatch`. El efectivo nunca es más amplio que el original |
| D15 | ¿Cómo sabe el comprador si su indicación se revisa? | **El consentimiento lo dice.** `ScopeDisplay` lleva `preference_review: true` solo con `preference_text`, enforce y `AGENT_HNP_REVIEWER=llm`; si no, se omite. Lo calcula el backend al proponer. La tarjeta muestra la frase de revisión solo con ese campo; si no, dice que la indicación no cambia la elección ni se revisa. Si el consentimiento la promete y el revisor no está configurado o no está disponible, la tarea termina con `choice_not_confirmed`. Antes, la tarjeta prometía la revisión aunque el revisor está `off` por defecto (`config.py:73`) y prohibido fuera de enforce (`bootstrap.py:242-244`), lo que chocaba con `card_shows_the_scope` |

## 9. Fuera de alcance

- Más de 3 ramas, cantidades mayores que 1 y condiciones que no sean de stock o total (color,
  fecha de entrega o cupones; los cupones son CT17 y el Merchant los rechaza).
- Límites por rama y fallback por agotamiento dentro del grant HNP (D6).
- Exponer la decisión condicional en el panel de traza; queda en el chat y en el ledger.
- Cambios en REST, en la lógica de D1, D2 o D3, en `rank_candidates` y en los archivos medidos por
  D1. El digest del motor sí cambia (D13).
- Restringir el conjunto elegible de D1 a la rama (§3.5).
- La calibración por clones y el sello del protocolo P3, que son de Maldo.

## 10. Riesgos

- **La sección nueva del prompt del router viaja en todos los turnos.** Puede cambiar respuestas
  de otros escenarios (UX01, UX11) con un LLM real. Además:
  - cambia el esquema de Structured Outputs (`text_format=CommerceDecision`,
    `conversation_router.py:430`). `model_requests` solo guarda su nombre (`model_requests.py:231`),
    así que ese cambio no se ve en el ledger;
  - cambia el `instructions_sha256` de todos los turnos del router (`model_requests.py:241`). El
    smoke EXP-06 de `main` registró `b05c6c43…` (`evaluation/results/exp06-smoke/`).

  Hay que mantenerla corta y volver a correr EXP-06 antes del lote pagado.
- **El emparejamiento por términos es por subcadena:** «xperia 5 ii» coincidiría con un «xperia 5
  iii». En abf30df no hay 5 III, pero un término también puede coincidir con varias variantes del
  mismo producto. La resolución lo detecta como ambigüedad. La evidencia no depende de eso: el
  `BranchMatch` guionado lleva el `variant_id` (§4.1).
- **Si el router pone la identidad de una rama en `intent.hard`** (por ejemplo, el término de A),
  las demás ramas la contradicen y quedan `intent_mismatch`. Falla cerrado: una rama así nunca se
  compra, y si va antes de la elegida, la decisión es `clarify`. Lo mide el corpus de D12.
- **El revisor HNP no ve `priority`** ni por qué cayeron las ramas anteriores. Si el texto pide A y
  la prioridad lleva a B, puede no confirmar y la tarea escala. Falla cerrado.
- **Con revisor, el caso «B legítimo» puede escalar siempre.** `chatDraft` llena
  `preference_text` con el mensaje del comprador, que nombra primero a A. En UX06-HNP-ctl con
  revisor, la prioridad lleva a B y el revisor puede no confirmarlo de forma sistemática. Se mide
  en el corpus de D12.
- **Las exclusiones no se persisten entre turnos.** Dependen de que el router las vuelva a emitir
  en cada turno. El router recibe los mensajes de la conversación
  (`conversation_router.py:369`), pero nada lo obliga a repetir `excluded`. Si no lo hace, B
  vuelve a ser elegible en ese turno.
- **POL-11 usa el precio del feed:** un feed desactualizado puede negar una rama que la oferta
  admite. Falla cerrado, con el camino gobernado de hoy.
