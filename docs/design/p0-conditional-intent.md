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
- **`prepare_checkout`** (:786-842) llama a `PurchaseService.start` (`application/purchase.py:230-372`):
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
| UX03-B (A excede su límite) | el límite de B (1.850.000) como único límite total | A sigue en la lista corta, porque solo se filtra el precio unitario. Si el modelo elige A: `POST /checkout_sessions` para A, total 1.924.000 > límite, cancelación y **REST 409 `budget_exceeded`**. Se creó un checkout y no hubo fallback a B |
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
  3. Revisor opcional.
  4. `start` crea **un solo** checkout y no hay reintento con otra variante.
- Hoy UX04-HNP no garantiza A. Para UX05/UX06-HNP con el total por encima por el envío, se crea un
  checkout y lo cancela el guardia de presupuesto (`budget_guard`, que **no** escala:
  `_ESCALATIONS` :106-116). UX07-HNP ya es determinista si B no está en `variant_ids`: lo impiden
  la lista corta, POL-01, POL-08 y el mandato abierto. Pero el borrador del chat **marca todas** las
  ofertas encontradas (`ui/src/lib/delegation-draft.ts:94-96`), así que B entra al grant si el
  comprador no la desmarca.

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
  `offer.read` en `run_events`, con `offer_id` y resultado (`commerce_lab/persistent_catalog.py:47-64`).
  El oráculo de 0 efectos del bloque A compara **todas** las tablas del Merchant
  (`tests/integration/live_merchant_control.py:173-188`), así que hay que ajustarlo (§4.4).

### 2.5 Restricciones que condicionan el diseño (leído)

- **D1 mide su código fuente:** `domain/intent.py`, `domain/selection.py`, `domain/__init__.py` y
  `acp/feed.py` entran en el digest del algoritmo de elegibilidad
  (`governance/provenance_artifacts.py:38-44`). Tocarlos cambia la identidad de D1 de **todas** las
  corridas, no solo las condicionales. Por eso los tipos nuevos van en módulos nuevos que
  `domain/__init__.py` no importa.
- **El consentimiento HNP se vuelve a serializar byte a byte:** `parse_consent_scope` rechaza un
  scope cuya forma canónica cambie (`hnp_models.py:153-158`). Un campo nuevo que se serialice como
  `null` rompería todos los grants guardados, así que se omite cuando no hay valor, como
  `canonical_without_absent_facts` (`governance/models.py:233-243`).
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
| `BranchMatch` | `variant_id: str \| None`, `categories`, `conditions`, `required_options`, `required_terms` (mismos tipos normalizados que `HardConstraints`) | al menos un criterio. Identifica **qué es** la variante, no su precio |
| `PurchaseBranch` | `match: BranchMatch`, `max_total_amount_minor: int \| None` (≥ 0, entero, COP), `only_if_previous_out_of_stock: bool = False` | la rama 0 no puede tener `only_if_previous_out_of_stock` |
| `ConditionalIntent` | `branches: tuple[PurchaseBranch, ...]` (0 a 3, en orden de prioridad), `max_total_amount_minor: int \| None` (límite global), `currency: Literal["COP"] \| None`, `excluded: tuple[BranchMatch, ...]` (0 a 5) | debe tener al menos una rama o una exclusión |

- **Límite efectivo de una rama** = el mínimo entre su límite y el global, sin contar los nulos. Si
  los dos son nulos, la rama no tiene límite de total y queda solo la condición de stock.
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
- En un turno condicional, `intent` sigue siendo obligatorio, pero solo alimenta el paso de
  búsqueda que ve el comprador. Los límites salen de `conditional`.

### 3.2 Resolución contra el feed, sin efectos

`resolve_branches(conditional, feed)` en `application/conditional.py` (nuevo):

1. Cada rama se resuelve a **exactamente una** variante del feed que cumple todos los criterios de
   su `match`. Se ignora la disponibilidad del feed, con el mismo truco que ya usa
   `governed_recovery.py:267`: `rejection_reason(candidate.model_copy(update={"available": True}), …)`.
2. Una rama con 0 coincidencias da `clarify`, con causa `branch_not_found`. Con más de una, también
   `clarify`, con causa `branch_ambiguous` y hasta tres títulos.
3. Las exclusiones marcan **todas** las variantes que coinciden. Si una exclusión no coincide con
   nada, no pasa nada.
4. También dan `clarify`:
   - dos ramas que resuelven a la misma variante (`duplicate_branch`);
   - una rama que coincide con una exclusión (`branch_excluded`);
   - una moneda distinta de COP (`currency`).

Resolver no hace ninguna llamada al Merchant. Ni un `clarify` ni una lectura dejan efectos.

### 3.3 Evaluación con la oferta, en orden

`evaluate_branches(resolved, offers: OfferReader)`, donde `OfferReader` es el protocolo de
`offer_freshness.py:31-32`. En producción es `ACPClient` y en pruebas, un doble. Para cada rama
`i`, en orden:

1. Si la rama es `only_if_previous_out_of_stock` y alguna rama anterior no quedó `out_of_stock` ni
   `inactive`, queda `not_triggered`, sin leer su oferta.
2. `get_offer(variant_id, LAB_DESTINATION)`. Cualquier `ACPClientError`, o cualquier otra
   excepción, deja la rama en `unknown`, con `error_code` y `status_code`, y la decisión es
   `undetermined`. **No se lee ninguna rama posterior.**
3. Con la oferta válida, el estado de la rama se decide en este orden:
   1. `product_status != "active"` → `inactive`;
   2. si no, `availability != "in_stock"` o `available_quantity < 1` → `out_of_stock`;
   3. si no, la variante aparece como no disponible en el feed del turno → `unknown`, con
      `feed_offer_mismatch` y la decisión `undetermined`. Esto es necesario porque `start` vuelve a
      rankear el feed (`purchase.py:281-296`) y la rechazaría;
   4. si no, `pricing.total_minor > límite efectivo` → `over_limit`. **La igualdad es elegible**;
   5. si no, `eligible`: la decisión es la rama `i` y no se lee ninguna rama posterior.
4. Si se recorren todas sin una elegible, la decisión es `none`.

Cada rama guarda:
- su estado;
- el límite efectivo;
- de la oferta: `revision`, `product_status`, `availability`, `available_quantity`,
  `items_total_minor`, `shipping_total_minor`, `total_minor`, `expires_at` y
  `offer_snapshot_sha256` (`offer_snapshot.py:100-105`);
- en `unknown`, el código del error.

Las ramas que no se llegaron a evaluar quedan `not_evaluated`.

| Decisión | Etiqueta del oráculo de Maldo | Efecto |
|---|---|---|
| rama 0 | `A` | se prepara un checkout de la rama 0 |
| rama 1 | `B` | se prepara un checkout de la rama 1 |
| `none` | `none` | 0 checkouts, explicación por rama (UX05) |
| `undetermined` | `undetermined` | 0 checkouts, explicación técnica, sin pasar a B |
| `clarify` | no es una etiqueta: se registra como `capability_missing` o como fallo | 0 checkouts, pregunta |

### 3.4 Un solo checkout y verificación del total

- Para la rama elegida, `PurchaseService.start` recibe el **intent efectivo**:
  - `hard` = moneda COP, `max_total_amount_minor` igual al límite efectivo y la identidad de la
    rama (sus criterios de `match`);
  - preferencias vacías.

  La variante sobrevive al nuevo rankeo porque la resolución usó los mismos filtros.
- Después de `start`, el checkout ya listo se compara con la oferta de la decisión:
  `checkout.total_minor == offer.pricing.total_minor`. Si difiere, aunque siga dentro del límite:
  - se cancela con la ruta existente `PurchaseService.cancel` (`purchase.py:481-499`), que ya
    funciona en el flujo gobernado (`tests/governance/test_hp_continuation.py:150`);
  - se registra `offer_changed`;
  - se le explica al comprador.

  El agente **no** prueba otra rama.
- Si `start` lanza `budget_exceeded`, el checkout ya quedó cancelado. Solo pasa si la oferta cambió
  entre la lectura y la creación. En el camino condicional, `budget_exceeded` se responde como
  `offer_changed` en **todos** los modos, también en B0: no llega a REST como 409, porque si no el
  registro quedaría sin escribir.
- En el resto de la compra no cambia nada. La frescura en `present`/`approve`, la aprobación del
  comprador, D2 y AP2 siguen igual.

### 3.5 Gobernanza: cómo interactúan los límites (leído)

En HP no hay un presupuesto global aparte del que dijo el comprador. POL-11 lee
`intent.max_total_minor` (`evaluator.py:106-116`, que viene de `provenance_sqlite.py:625`), y lo
mismo hacen POL-05 en D2 (`evaluator.py:440-452`) y el guardia de presupuesto (`purchase.py:582`).
Como el intent efectivo lleva el límite **efectivo**, los tres aplican ese límite sin cambiar D1 ni
D2:

- **D1** registra el intent efectivo y el feed del turno. El conjunto elegible es el rankeo con la
  identidad de la rama, y POL-01 pasa.
- **POL-11**, precio unitario del feed ≤ límite efectivo: puede negar si el feed está desactualizado
  y su precio quedó por encima del total de la oferta. Esa negación usa el camino gobernado de hoy.
- **El límite por rama nunca amplía el global**, porque se toma el mínimo de los dos.

En HNP el global es el grant (POL-08 y el máximo del mandato). En este alcance, el límite de cada
rama es el del grant (§3.9).

### 3.6 Exclusiones (UX07)

`excluded` se aplica **filtrando el feed del turno**: `without_variants(feed, excluded_ids)`, en
`application/conditional.py` y no en `acp/feed.py` (§2.5). El feed filtrado se usa:
- en `search_catalog` y en la lista corta que ve el modelo;
- en `recover_no_candidate`, sus alternativas y el contexto de `governed_recovery`;
- en `PurchaseService.start(feed=…)`, que ya acepta ese parámetro.

D1 registra ese mismo feed, así que **B no aparece ni en el conjunto elegible de D1**. Antes de
`start` se vuelve a comprobar que la variante elegida no está excluida, como defensa en
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
| `outcome` | `branch` \| `none` \| `undetermined` \| `clarify` \| `checkout_matches` \| `offer_changed` |
| `branch_index`, `variant_id` | si hay una rama elegida |
| `body` | JSON canónico (`schema: "conditional-decision/1"`): modo, `intent_sha256`, ramas con su estado y los hechos de la oferta, IDs excluidos, causa del `clarify`, `explanation_sha256` y, en `checkout_check`, el checkout y los dos totales |
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
| `clarify` | «No encontré {A} en el catálogo» o «Varias opciones coinciden con {A}: …; ¿cuál quieres?». Termina con «Todavía no preparé ningún checkout.» |

Los motivos son:
- «está agotado»;
- «no está a la venta»;
- «su total con envío es {total} y tu límite para esa opción es {límite}»;
- «solo autorizaste {B} si {A} se agotaba».

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
  (§2.5; hace falta una prueba con un consentimiento existente). `DelegationScope` no cambia, y
  por lo tanto tampoco cambian `GrantFacts`, POL-08, los mandatos ni la evidencia de D2.
- **Ejecutor:** si `consent.priority` existe, `_execute` reemplaza `_shortlist`, `_select` y
  `_review` por `resolve` + `evaluate_branches` (§3.3), en el orden de `priority`:
  - el límite de cada rama es el `max_total_amount_minor` del intent del grant;
  - no hay `only_if_previous_out_of_stock`;
  - el lector de ofertas es el `ACPClient` que el runtime ya pasa como `merchant`, y se agrega como
    parámetro `offers` de `build_hnp_executor`.

  | Resultado | Qué hace el ejecutor | Estado |
  |---|---|---|
  | rama | `_start` con la variante | — |
  | `none` | `_end(run, "no_candidate_in_scope")`, antes de cualquier efecto | escala en B2, `failed` en B0 |
  | `undetermined` | `_end(run, "selection_unavailable")`, sin `failure_kind` | escala en B2, `failed` en B0 |
  | el checkout no coincide con la oferta | `_end(run, "offer_changed", purchase=…, checkout=…)`; si la tarea escala, también cancela su checkout (`hnp_execution.py:867-868`) | — |

  No hay códigos nuevos ni migración de CHECK (§2.5). La decisión va a `conditional_decisions` con
  el `run_id` de la tarea.
- **Textos:** `escalation.no_candidate_in_scope` y `escalation.selection_unavailable`
  (`delegation/contract.py:90-99`) pasan a ser neutros respecto a la causa. Por ejemplo: «Ninguna
  de las opciones que autorizaste se puede comprar ahora dentro de lo que autorizaste» y «No pude
  elegir porque un servicio necesario falló o no respondió».
- **UX07-HNP:** el ejecutor no cambia. «Nunca B» significa que B no está en `variant_ids`, y eso ya
  se hace cumplir. `_offer_delegation` (`conversation.py:671-699`) quita las variantes excluidas
  del borrador y, si las ramas se resuelven, propone `priority` en su orden. Los límites por rama
  no pasan al borrador; el total lo escribe el comprador.
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
  modo error.
- **HNP:**
  - el formulario de delegación ordena las opciones marcadas, con botones para subir y bajar;
  - la tarjeta de consentimiento dice «Compra la primera opción disponible dentro del total, en este
    orden: 1. … 2. …»;
  - la tarjeta de escalamiento usa los textos neutros;
  - `chatDraft` toma `priority` del borrador.

### 3.11 Compatibilidad con el protocolo P3 de Maldo

| Regla del protocolo | Cómo la cumple el producto |
|---|---|
| Cantidad 1, COP, montos enteros en unidades menores | `OfferPricing` es entero y COP, y el checkout es de cantidad 1 (`_validate_created_checkout`). Los límites son `int` ≥ 0 y no hay floats en ningún lado |
| Elegibilidad = condición + stock + total del Merchant ≤ límite de **esa** rama, con igualdad aceptada | §3.3, pasos 3.1 a 3.5. El límite es por rama y su efectivo nunca amplía el global |
| Prioridad de A | se evalúa en orden y la primera elegible gana. Si A es elegible, B no se lee |
| `UNKNOWN` → `undetermined`, sin caer a B | §3.3, paso 2: cualquier error de lectura detiene la evaluación. Un 401, 429, timeout o JSON inválido nunca se convierte en `out_of_stock` ni en `over_limit` |
| D/UX05 = `none` + explicación, sin C | §3.3, paso 4 y §3.8. C nunca se resuelve ni se lee |
| Como máximo un checkout en el episodio, sin explorar | se decide con la oferta y se crea un solo checkout para la rama elegida. `none`, `undetermined` y `clarify` no crean ninguno |
| Operación distinguible para verificar A (DEP-11) | `POST /lab/offers/get` con `offer_id=A` queda en el proxy y en `run_events` (`offer.read`) del Merchant, que son testigos independientes del agente |
| El checkout elegido se contrasta con el estado vigente y no hay fallback automático | §3.4 |

Lo que **no** cubre: la calibración por clones (A-cal, B-cal, V, E) y el sello del oráculo son del
arnés de Maldo. El producto no los necesita para decidir.

## 4. Celdas y oráculos

### 4.1 Par, totales y límites

- **El par A/B se elige por reglas registradas antes de correr, no por IDs fijos.** Se reutilizan
  las `pair_rules` de P2: A = Sony Xperia 5 II, 256 GB, nuevo; B = OnePlus 8T, 256 GB, nuevo.
  Si alguna regla da 0 o más de una coincidencia en el catálogo del episodio, se bloquea como
  `insufficient_fixture_data`. Falta verificar en el PR de evidencia que el par exista en el
  catálogo del stack vivo y que se resuelva en una sola variante con el `BranchMatch` guionado.
- **Totales:** `T_A` y `T_B` son el `pricing.total_minor` de cada oferta. Los lee el worker del
  Merchant en solo lectura (`observe_offer`, `live_merchant_control.py:329-335`) antes del turno, y
  quedan registrados con su `revision`.
- **Límites:** `T−1` es una unidad menor, sin floats. Si `T = 0`, se bloquea.
- **UX03-C** exige `T_B < T_A`. Si no se cumple, esa celda queda bloqueada; no se cambia de par.

### 4.2 HP

Todas las celdas corren en B2-HP (enforce, flujo gobernado), como `HpAgent` del bloque A
(`tests/integration/test_p0_model_failures.py:352`), y en la superficie de chat. La decisión
observada es la última fila `decision` de `conditional_decisions` para el `run_id` del turno. El
router devuelve la `ConditionalIntent` guionada.

| Celda | Estímulo | Oráculo |
|---|---|---|
| UX03-A | límite A = `T_A`, límite B = `T_B−1` | etiqueta `A`. Antes de la creación, 1 lectura (A). 1 POST de creación para A; compra en `awaiting_confirmation` con la variante A; B sin leer (`not_evaluated`) |
| UX03-B | límite A = `T_A−1`, límite B = `T_B` | etiqueta `B`. Lecturas A y B; A queda `over_limit` con el total `T_A`. 1 creación para B |
| UX03-C ≡ UX04-HP | límites `T_A` y `T_B`, con `T_B < T_A` | etiqueta `A` aunque B cueste menos. 1 lectura (A) y 1 creación para A. El LLM de selección no se consulta (1 sola solicitud al modelo, la del router) |
| UX03-D ≡ UX05-HP | límites `T_A−1` y `T_B−1`; C, más barato, está en el catálogo | etiqueta `none`. Lecturas A y B, las dos `over_limit`. **0 efectos**; la explicación nombra las dos causas y no menciona C; C nunca se lee |
| UX03-UNKNOWN-A | límites como en C; fallo `drop` del proxy en `/lab/offers/get` | etiqueta `undetermined`. Exactamente 1 intento de lectura, el de A: en el proxy (con `offer_id=A` en el cuerpo) y 1 `offer.read` de A en el Merchant, porque `drop` reenvía. B no se lee. **0 efectos.** Sin fallo alcanzado, la celda es inválida, no `passed` |
| UX06-HP | «si A se agota, compra B», límite global `T_B−1`; A con stock 0 (`edit_offer` `stock` antes del turno) | etiqueta `none`. A queda `out_of_stock` y B `over_limit`. **0 efectos.** El mensaje informa y no compra |
| UX06-HP-ctl | igual, con límite global `T_B` | etiqueta `B`, con 1 creación para B |
| UX07-HP-a | «prefiero A, nunca compres B», con B más barato | B no está en la lista corta que recibe el modelo (enum de la solicitud) ni en el conjunto elegible de D1. Se crea 1 checkout, que no es de B; con el selector guionado que toma el primero, es A |
| UX07-HP-b | igual, con el selector guionado devolviendo B a pesar del enum | `UnknownVariantSelection` y explicación gobernada; 0 creaciones |

Como en Maldo, UX03-D y UX05-HP comparten un episodio etiquetado con las dos identidades y un solo
denominador. Lo mismo pasa con UX03-C y UX04-HP (§8, D10).

### 4.3 HNP

Todas las celdas corren en B2-HNP con el `HnpAgent` de proceso propio y el selector determinista.
Con `priority`, el selector no se usa: si se configurara el selector LLM, `ModelServer` recibiría 0
solicitudes, y eso también se comprueba.

| Celda | Estímulo | Oráculo |
|---|---|---|
| UX04-HNP | grant {A, B}, `priority` [A, B], total ≥ max(`T_A`, `T_B`) | se compra A aunque B sea más barato; 1 checkout y la orden en sandbox. La decisión queda como rama 0 en `conditional_decisions`. No hay `choice_reviewed` |
| UX05-HNP | `priority` [A, B], total = min(`T_A`, `T_B`) − 1; C fuera del grant | `escalated` con `no_candidate_in_scope`; decisión `none` (A y B `over_limit`); **0 efectos**, que incluyen 0 reservas y 0 mandatos cerrados |
| UX06-HNP | `priority` [A, B], A con stock 0, total = `T_B−1` | `escalated` con `no_candidate_in_scope`; A queda `out_of_stock` y B `over_limit`; **0 efectos** |
| UX06-HNP-ctl | igual, con total `T_B` | se compra B; 1 checkout |
| UX07-HNP | turno de delegación «prefiero A, nunca compres B», con el router guionado y `excluded` = [B]. El arnés publica el borrador tal como llega | B no está en `delegation_draft.offers`, ni en `scope.variant_ids`, ni en ningún checkout u orden |

Fuera de la matriz, pero en CI: `undetermined` en HNP (lectura de A con 503) da
`selection_unavailable`, sin leer B y con 0 efectos.

### 4.4 «0 efectos»

Tiene dos partes, adaptadas del bloque A (`zero_effects`, `test_p0_model_failures.py:289`).

- **Fuera del agente:**
  - ningún POST de creación, ni normal ni guardado;
  - ninguna fila nueva en `checkout_sessions` del run;
  - `catalog_offers` con la misma revisión;
  - ninguna orden;
  - PSP: snapshot igual y 0 tokens;
  - el resto de las tablas del Merchant, iguales.

  La excepción es **`run_events`**: su diferencia tiene que ser exactamente las `offer.read`
  esperadas, contadas por `offer_id`: D = {A: 1, B: 1}, UNKNOWN = {A: 1} y UX06 = {A: 1, B: 1}.
  Leer no es un efecto comercial, pero cuenta como evidencia de qué se verificó.
- **En el ledger del agente:** ninguna fila nueva en `purchases`, `approvals`, `orders`,
  `completion_results`, `grant_reservations`, `dispatch_outbox`, `ap2_evidence`,
  `governance_evidence` (D1 no corrió) ni `hp_evidence`, y ningún mandato cerrado. Tiene que haber
  **exactamente una** fila `decision` nueva en `conditional_decisions`, con el resultado esperado.
  En HNP, la tarea queda en un estado terminal sin una compra que haya avanzado.

En las celdas con checkout, las lecturas de decisión son las que ocurren **antes** del primer POST
de creación. Las de frescura (`present`, `close`) vienen después y no cuentan. El arnés cancela el
checkout en el teardown y lo verifica.

### 4.5 Control positivo

- **Por modo:** UX03-A, UX03-B y UX06-HP-ctl en HP, y UX04-HNP y UX06-HNP-ctl en HNP, crean
  exactamente un checkout. Así se demuestra que el arnés no bloquea todo.
- **Regresión:** el pedido no condicional del bloque A (`PHONE_REQUEST`) sigue preparando su
  checkout igual que hoy, y su evidencia de D1 conserva la identidad del algoritmo, porque los
  archivos medidos no cambian.

### 4.6 Qué prueba el modelo guionado y qué necesita un LLM real

| Lo prueba `ModelServer`/`ScriptedModel` (determinista, sin costo) | Necesita un LLM real (corrida pagada que inicia Yair) |
|---|---|
| Dada una `ConditionalIntent`, la decisión, la etiqueta, el orden de las lecturas, que haya como máximo un checkout, que `UNKNOWN` falle cerrado, los textos, el registro y los 0 efectos | Que el router convierta la frase en la `ConditionalIntent` correcta: orden, límites por rama o globales, «si se agota» frente a «si no», prohibición frente a preferencia, magnitudes en COP |
| Que el modelo de selección no puede elegir B, porque no está en el enum ni en D1 | Que ante ambigüedad el router pida aclaración y no invente límites |
| HNP completo con `priority`: no hay LLM en ese camino | La calidad del borrador de delegación (prioridad y exclusiones propuestas) |

Un modelo guionado **no** acredita comprensión: ninguna celda de §4.2 cuenta como evaluación de
interpretación.

## 5. Cómo se prueba

1. **CI de cada PR** (sin red ni gasto), con TDD y SQLite real en `tmp_path`:
   - **unitarias:** resolución y evaluación con un `OfferReader` doble, con un caso por estado
     (incluidos `not_triggered`, `feed_offer_mismatch`, la igualdad `T`, `T−1` y cada código de
     `ACPClientError`);
   - **de API con `ModelServer`:** REST y chat de las celdas de §4.2, contra un Merchant doble que
     sirve `/lab/offers/get` y cuenta las creaciones;
   - **HNP:** con el `Bench` de `tests/support/hnp_executor.py`;
   - **serialización:** un consentimiento viejo sigue siendo canónico.
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

- **Spec:** este archivo.
- **Prerregistro:** `evaluation/scenarios/p0-conditional-intent.json`. Se congela antes de correr y
  contiene reglas del par, celdas, guiones, oráculos y repeticiones; su hash va en el informe.
- **Resultados:** `evaluation/results/p0-conditional-intent/README.md`, `summary.json` y JUnit, sin
  bases de datos.
- **Matriz:** en UX03 a UX07, los campos `coverage`, `implementation_status` y `execution` de
  `matrix.json`, con enlace a la evidencia.
- **Contrato:** `docs/agent-ui-contract.md` (el campo `conditional` de `select_product`) y
  `docs/design/hnp-governed.md` (`priority` en el consentimiento).

## 7. PRs

Después de esta spec, tres PRs pueden avanzar en paralelo. Ninguno toca los archivos de otro, salvo
`runtime.py`, donde los cambios están en bloques distintos (:249 y :283).

| # | PR | Contenido | Archivos | Tamaño | Depende de |
|---|---|---|---|---|---|
| 1 | Esta spec | — | `docs/design/p0-conditional-intent.md` | — | — |
| 2 | Contrato y evaluador puro | tipos de §3.1 y el campo `CommerceDecision.conditional` con su sección del prompt; `resolve_branches`, `evaluate_branches` y `without_variants` puros (§3.2, §3.3, §3.6); la tabla `conditional_decisions` y sus métodos (§3.7); `priority` en `ConsentScope`, `ScopeDisplay`, `ScopeView`, `DelegationProposalRequest` y `DelegationDraftView`, omitido si es `None`; tipos TS y fixtures JSON validadas por los dos lados | `domain/conditional.py` (nuevo), `application/conditional.py` (nuevo), `llm/conversation_router.py`, `persistence/sqlite.py`, `governance/hnp_models.py`, `delegation/service.py` (solo el request), `delegation/contract.py` (solo `ScopeView`), `ui/src/lib/agent-contract.ts`, `ui/src/lib/delegation-contract.ts`, `ui/src/lib/delegation-draft.ts` (tipo del request), fixtures y tests | M, ~1,5 h | 1 |
| 3 | HP: integración | `_after_search` y `select_product` deterministas; feed filtrado por exclusiones; intent efectivo; verificación del total y cancelación (§3.4); arista condicional después de `prepare_checkout` en todos los modos; textos (§3.8); registro; borrador con exclusiones y prioridad; `offer_reader` en `ConversationService` y runtime; fixtures SSE (`scripts/export_assistant_fixtures.py`) | `application/conversation.py`, `runtime.py` (:249), `tests/test_conditional_hp.py`, `tests/assistant_support.py`, `ui/src/storybook/fixtures/assistant-transport/conditional-*.sse` | M-L, ~2 h | 2 |
| 4 | HNP: ejecutor | camino `priority` en `_execute`, con el registro, los finales reutilizados y la verificación del total (§3.9); `offers` en `build_hnp_executor`; textos neutros; `propose` guarda `priority` | `application/hnp_execution.py`, `delegation/bootstrap.py`, `runtime.py` (:283), `delegation/service.py` (`propose`), `delegation/contract.py` (`HNP_MESSAGES`), `tests/delegation/test_hnp_conditional.py` | M, ~1,5-2 h | 2 |
| 5 | UI y Storybook | `conditional-branches.tsx` con sus historias; formulario ordenable; tarjeta de consentimiento con el orden; copia neutra; `chatDraft` con prioridad; al final, E2E del recorrido condicional con el SSE del PR 3 | `ui/src/components/agent/`, `ui/src/components/delegation/`, `ui/src/features/`, `ui/e2e/` | M, ~2 h | 2; el E2E, del 3 |
| 6 | Evidencia | prerregistro; `test_p0_conditional_intent.py` con un ajuste de 0 efectos para `run_events`; corrida local con `RUN_LIVE_STACK=1`; informe; matriz | `evaluation/scenarios/`, `evaluation/results/`, `tests/integration/` | M, ~2-3 h con la corrida | 3 y 4 |

Cada PR lleva revisor independiente, compuerta local (`docs/testing.md`) y CI en verde. Con
agentes, el camino crítico es 2 → 3/4 → 6, unas 5 a 6 horas.

## 8. Decisiones abiertas para Yair

| # | Decisión | Recomendación |
|---|---|---|
| D1 | ¿Capacidad en todos los modos o solo en B2? | **Todos.** Es capacidad de producto, no un control de gobernanza, así que la diferencia B0/B2 no se le atribuye a la PDP. La evidencia P0 corre en B2, como el bloque A. Consecuencia: B0 lee ofertas en los turnos condicionales |
| D2 | ¿Decidir con la oferta o con checkouts exploratorios? | **Con la oferta**, con un solo checkout. El Merchant copia el pricing de la oferta (§2.4) y la frescura ya compara `total_minor`. Las lecturas quedan en `run_events`, así que «0 efectos» las admite, contadas (§4.4). Hay que confirmarlo con Maldo |
| D3 | ¿Qué cuenta como desconocido? | **Todo** `ACPClientError` (incluidos `offer_expired` y `OFFER_NOT_FOUND`, que llega como `invalid_offer`) y la discrepancia entre feed y oferta. Solo los hechos verificados hacen falsa una rama |
| D4 | ¿El total del checkout distinto del de la oferta, pero más bajo? | **Detenerse igual:** igualdad estricta. Es simple y comparable con el protocolo de Maldo |
| D5 | ¿Ambigüedad? | **Aclarar**, como en UX11: rama con 0 o más de una variante, rama excluida, o moneda o magnitud ausente |
| D6 | ¿Alcance de HNP? | **Solo prioridad**, sobre el total único del grant. Sin límites por rama ni «solo si se agota» en HNP. Las celdas UX04 a UX06 de HNP funcionan con el total del grant |
| D7 | ¿Códigos HNP nuevos? | **No:** `no_candidate_in_scope`, `selection_unavailable` y `offer_changed`, con textos neutros. Así no hay migración de CHECK |
| D8 | ¿El revisor HNP con prioridad? | **Se omite:** la prioridad tipada y consentida manda; no hay elección del modelo que revisar |
| D9 | ¿`T` desde la oferta (worker) o calibrado con checkouts en clones? | **Desde la oferta** para nuestra evidencia. La corrida formal P3 usa la puerta de Maldo (#176), que el producto ya respeta |
| D10 | ¿Deduplicar UX04/UX05-HP con UX03-C/D? | **Un episodio con las dos identidades**, como propone Maldo. HNP lleva celdas propias |
| D11 | ¿Exclusión solo en la aplicación o también en D1? | **También en D1**, con el feed filtrado: B no aparece como elegible en ninguna evidencia |
| D12 | ¿Corpus del LLM real? | Frase de cada celda + 2 paráfrasis, 1 repetición, con tope de costo que fija Yair. Va después del PR 6 |

## 9. Fuera de alcance

- Más de 3 ramas, cantidades mayores que 1 y condiciones que no sean de stock o total (color,
  fecha de entrega o cupones; los cupones son CT17 y el Merchant los rechaza).
- Límites por rama y fallback por agotamiento dentro del grant HNP (D6).
- Exponer la decisión condicional en el panel de traza; queda en el chat y en el ledger.
- Cambios en REST, en D1 o D2, en `rank_candidates` y en los archivos medidos por D1.
- La calibración por clones y el sello del protocolo P3, que son de Maldo.

## 10. Riesgos

- **La sección nueva del prompt del router viaja en todos los turnos.** Puede cambiar respuestas
  de otros escenarios (UX01, UX11) con un LLM real. Hay que mantenerla corta y volver a correr
  EXP-06 antes de la corrida pagada.
- **El emparejamiento por términos es por subcadena:** «xperia 5 ii» también coincide con «xperia 5
  iii». La resolución lo detecta como ambigüedad, pero el par de la evidencia tiene que resolverse
  en una sola variante (§4.1).
- **POL-11 usa el precio del feed:** un feed desactualizado puede negar una rama que la oferta
  admite. Falla cerrado, con el camino gobernado de hoy.
