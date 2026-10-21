# Human Not Present gobernado

Estado: **propuesta para aprobación** (HNP-00), 20-oct-2026. Fuentes: el contrato formal
[`policy-v1.md`](../b2-governance/policy-v1.md) (POL-01..10, tipos y evidencia), la
[aceptación](../b2-governance/policy-acceptance-v1.md) (HNP01–10 y C01–C12), el
[perfil AP2](../ap2-profile.md) (sección «Human Not Present», parte de este mismo PR), la ola HP
(P1 `APPLICABLE_RULES`, `rules_apply` y `b2-hp-v2`, fusionado en #73; P2 `ap2_evidence` y webhooks,
fusionados en #69 y #71, y `verify-evidence` en #74; P3 D2-HP en #72) y la especificación y el SDK
de AP2 v0.2 fijados en `e1ea56d`.

Decisiones de Yair ya tomadas: HNP se fusiona detrás de `AGENT_HNP_ENABLED=false` y la demo corre
desde la etiqueta `demo-2026-10-23` (D-3); se agrega el brazo P y se pre-registra el empate B0/B2 en
seguridad (D-2); se aceptan los valores por defecto de D-4 a D-7 y D-10; se enmienda `AGENTS.md`
(D-9), con la excepción de B0-HNP (§11).

Valores **por defecto; pendientes de confirmar con el asesor**:

- **D-1, alcance frente al documento:** el documento afirma «HNP implementado y probado en vivo»
  (Nivel 1 de §2.3 más HNP-I1). EC-HNP-03 entra solo si la corrida termina antes de congelar el
  documento.
- **D-8, contrato operativo:** policy-v1 sigue «propuesto»; `b2-hnp-v1` revisión 2 (§7) es el
  artefacto operativo de la tesis.

**Nombres que se parecen.** `HNP-00`…`HNP-11`, `HNP-M1`, `HNP-P1`, `HNP-E1`…`E3`, `HNP-V1` e
`HNP-I1` (con guion) son los PRs de esta spec. `HNP01`…`HNP10` (sin guion) son los casos de
aceptación de [policy-acceptance-v1 §5](../b2-governance/policy-acceptance-v1.md). `HN01`… son los
escenarios del piloto HNP (HNP-E1). Los experimentos offline del validador que `AGENTS.md` y
`evaluation/` llaman «HNP-01» y «HNP-02» (`hnp-matrix`) son otra cosa: en esta spec se nombran
«experimento HNP-01/HNP-02».

Esta spec es la **fuente de los nombres compartidos** por los PRs HNP-01..HNP-I1: tablas y columnas,
estados, rutas, códigos, settings, policy set, eventos, eslabones del verificador, pasos de traza y
textos. Un PR que necesite otro nombre no lo cambia por su cuenta: lo reporta al líder. Donde el plan
dejaba algo abierto, la decisión se marca **Decisión de spec** con su justificación.

## 1. Problema

Hoy el agente solo compra con el comprador presente: el clic autenticado sobre los términos exactos
es la compuerta (HP). Human Not Present (HNP) es el caso en que el comprador autoriza antes, sobre un
alcance acotado, y se va; el agente compra después sin él. Sin un diseño gobernado, HNP abre tres
riesgos que AP2 y ACP no cierran por sí solos (policy-v1 §2, threat-model): gastar fuera de lo
consentido, usar dos veces la misma autorización y seguir comprando después de revocarla. Además, la
tesis necesita evidencia verificable de que cada compra autónoma salió de un consentimiento previo y
de decisiones de política reproducibles.

## 2. Alcance

### 2.1 Qué es «HNP al 100 %»

| # | Capacidad | Cierre verificable |
| --- | --- | --- |
| C1 | Consentimiento previo en la UI (Trusted Surface) sobre un alcance tipado | Grant v1 ligado a `scope_digest`; la tarjeta no muestra texto del modelo |
| C2 | Mandatos abiertos AP2 v0.2 con `cnf`, cerrados con la Agent Key | La cadena válida verifica con el SDK; falla si se alteran total, comercio, variante, `cnf`, `sd_hash`, `checkout_hash` o si hay una restricción desconocida |
| C3 | Verificación independiente en el comercio (cadena de checkout) y en el PSP (cadena de pago) | Rechazan con recibo de error AP2 aunque el agente se salte su PDP |
| C4 | D2-HNP (POL-03..09) y D3-HNP (POL-09/10), fail-closed y reproducibles | Matriz P/N/F de POL-03..10 y C01–C11 |
| C5 | Reserva de un solo uso por CAS, con outbox en la misma transacción que D3 | Dos ejecuciones producen una orden; un timeout nunca libera la reserva |
| C6 | Revocación del dueño y kill switch del operador, re-chequeados al comprometer | Revocar antes del compromiso deja 0 efectos; después, la compra queda «en curso» |
| C7 | Escalamiento HNP→HP | Si escala antes del cierre (D1, lista corta, frescura, D2): 0 cierres y 0 presentaciones. Si escala el PSP con recibo de error: una presentación al PSP, ninguna al comercio y la reserva liberada (§8.2). Siempre con un aviso visible |
| C8 | Ejecución sin el humano | El token del comprador no se vuelve a usar después del consentimiento |
| C9 | Identidad, atribución y evidencia encadenada | H2-HNP: compras HNP con cadena completa verificada / compras HNP |
| C10 | Base HNP y evaluación pre-registrada (EC-HNP-03) | Manifiesto fusionado antes de correr (HNP-E1) |

Monitoreo mínimo: «Mis delegaciones» (estado, vigencia, alcance, última actividad y Revocar), compras
HNP en el historial y la traza, contadores HNP en `/health` y el verificador de evidencia. No hay
detección de anomalías.

### 2.2 Fuera de alcance

| Fuera | Por qué |
| --- | --- |
| Recurrencia, presupuesto acumulado (`payment.agent_recurrence`, `payment.budget`) y más de una compra por grant | El perfil v1 es de una compra (policy-v1 §1); cambiaría POL-09 |
| Subdelegación (cadenas de más de dos saltos), A2A, MCP, TAP | Ninguna interacción lo requiere |
| Credencial del usuario (OpenID4VP, passkeys) | Mismo modelo de confianza que HP: firma el proveedor del agente |
| Revocación remota instantánea en el comercio o el PSP | AP2 no la define; se mitiga con el `exp` del mandato abierto |
| Disparadores por precio, monitoreo continuo, scheduler, varias instancias | Una instancia y disparo explícito |
| Re-cotizar después de crear el checkout (D17-HNP) | Se escala, que es lo conservador |
| Que el LLM proponga la delegación en el chat | Formulario/API determinista |
| Anomalías, re-autorización periódica, push o correo | Solo actividad dentro de la app |
| Reconciliación automática de un fallo parcial entre verificadores | Queda `uncertain` con reconciliación manual |
| `shadow` para HNP | **Decisión de spec:** el arranque lo rechaza; ninguna condición del experimento lo usa y su semántica (observar sin bloquear) no está definida para D3 |
| Divulgación selectiva de los mandatos abiertos | **Decisión de spec:** se presentan completos; no contienen datos personales (comercio, variantes, montos, instrumento) |
| OPA, UCP, conformidad AP2 certificada, dinero real, 3DS, reembolsos, disputas | D11 y H01 siguen abiertos; todo es sandbox |

### 2.3 Corte si el día se acorta

1. **Nivel 1, núcleo:** HNP-00, 02, 03, 04, M1, P1, 05, 06, 08, 09a, 09b e I1.
2. **Nivel 2, evaluación:** E1, E2, V1 y la corrida.
3. **Nivel 3, UI y traza:** 01 (lado TS), 07, 10 y 11.

Se cae primero el nivel 3. Sin UI, el runner consiente por API con el JWT del comprador.

## 3. Flujo

### 3.1 Componentes

```
Comprador ──JWT──> UI (Trusted Surface: tarjeta de delegación, sin texto del modelo)
                     │ POST /delegations/proposals · POST /delegations · POST /delegations/{id}/revoke
                     ▼
┌────────────────────── Proceso del agente (una instancia, SQLite) ──────────────────────┐
│ delegation_api (ownership)   DelegationService            HnpExecutionService          │
│                              OpenMandateIssuer (clave     claim con lease · selector   │
│ /internal/hnp/* (tokens)     del proveedor)               PurchaseService.start (D1)   │
│                              ledger delegation_*          frescura · D2 · reserva      │
│ Gobernanza: D1 · D2-HNP · D3-HNP · evidencia              AgentKeyCloser · D3 · outbox │
│                                                           HnpDispatcher (PSP, comercio)│
└───────────────┬───────────────────────────────────────────────┬────────────────────────┘
                │ ACP + /ap2/checkout-jwt + /ap2/complete (cadena) │ delegate_payment (cadena de pago)
                ▼                                                   ▼
     Comercio: verifica la cadena de checkout            PSP: verifica la cadena de pago
```

El LLM solo participa en el selector (`AGENT_HNP_SELECTOR=llm`), con la elección restringida a los
ids de la lista corta (P5b). Ninguna herramienta del modelo llega al emisor ni a la Agent Key.

### 3.2 Fase 1, humano presente: propuesta y consentimiento

1. El comprador elige en la UI una o más variantes del feed, el total máximo con envío, la vigencia,
   los datos de entrega y, opcionalmente, una indicación propia.
2. `POST /delegations/proposals` valida y devuelve el alcance exacto, su `scope_digest` y el
   vencimiento de la propuesta (`AGENT_HNP_PROPOSAL_TTL_SECONDS`).
3. La UI muestra la tarjeta. El clic «Autorizar» envía `POST /delegations {proposal_id,
   scope_digest}`.
4. El servidor verifica JWT, dueño, digest y vigencia de la propuesta; firma el checkout abierto y el
   pago abierto con la clave del proveedor (`AGENT_AP2_PRIVATE_KEY_PEM`, `cnf` = Agent Key pública), y
   en una transacción crea el grant (`active`, epoch 1), los mandatos abiertos, la tarea `scheduled` y
   el evento `consented`.
5. El comprador se va. Su JWT no vuelve a usarse para esta compra.

### 3.3 Fase 2, humano no presente: ejecución

Disparo: `POST /internal/hnp/tasks/{task_id}/run` con `AGENT_HNP_SERVICE_TOKEN`. Principal
`service:hnp-executor`, `on_behalf_of` = dueño, ligado por el grant.

| Paso | Qué hace | B0-HNP | B2-HNP |
| --- | --- | --- | --- |
| 1 | Claim (CAS + lease); kill switch apagado; grant `active` y `now < expires_at` | Sí | Sí |
| 2 | Lista corta = `rank_candidates(feed, intent)` ∩ `scope.variant_ids`; selector | Sí | Sí |
| 3 | D1 (POL-01/02/11, `purchase_mode=HNP`) antes de `create_checkout` | No | Sí |
| 4 | Crea el checkout ACP con los datos de entrega del grant; guarda común de presupuesto | Sí | Sí |
| 5 | Frescura de la oferta, `stage=close` | No | Sí |
| 6 | D2-HNP (POL-03..09) con `open_proof` | No | Sí |
| 7 | **UoW-1:** evidencia D2 + reserva CAS | Reserva sí | Sí |
| 8 | `GET /ap2/checkout-jwt` y cierre con la Agent Key; **UoW-C** guarda los cerrados | Sí | Sí |
| 9 | D3-HNP (POL-09/10) con `closed_proof` | No | Sí |
| 10 | **UoW-2:** re-chequea epoch, kill switch y reserva; evidencia D3 + fila del outbox | Sí (sin D3) | Sí |
| 11 | Despacho: PSP `delegate_payment` (cadena de pago) y comercio `/ap2/complete` (cadena de checkout); recibos | Sí | Sí |
| 12 | **UoW-3:** reserva `consumed`, grant `consumed`, tarea `purchased`; compra `completed` con sus recibos (§4.6) | Sí | Sí |
| 13 | Actividad del dueño y traza | Sí | Sí |

Una revocación o un kill switch comprometidos antes del paso 10 ganan: no hay outbox ni efecto.
Después del paso 10 la compra está «en curso» y se reconcilia; no se promete cancelar.

**Intención HNP. Decisión de spec:** la intención del episodio no la interpreta el modelo. Se deriva
del alcance: `PurchaseIntent(hard=HardConstraints(currency="COP",
max_total_amount_minor=scope.total_max_minor))`, guardada en el grant. En D1,
`eligible_variant_ids` es la lista corta del paso 2 y `eligibility_algorithm_ref` identifica
`rank_candidates+grant_scope/v1`. Así POL-01 cubre una selección fuera del grant.

**Cómo lo arma HNP-05.** D1-HNP es `SqliteD1EnforceGate(..., purchase_mode="HNP")`: arma
`b2-hnp-v1` con `build_hnp_policy` y no acepta bytes de política por configuración; la lista de
comercios le llega como `merchant_refs=operator_merchant_refs(settings, configured)`, que toma las
refs que `configured_d1` ya derivó de `AGENT_D1_ALLOWED_MERCHANT_ORIGINS`. Su
`create_checkout` recibe `grant=HnpGrant(grant_id, variant_ids)`, que el executor lee del ledger, y
solo decide una compra cuyo `purchases.(purchase_mode, grant_id)` coincide; si no, `d1_unavailable`
sin evidencia. La fuente pública de elegibilidad que guarda la bóveda es la del algoritmo medido más
`eligibility_profile`, `grant_id` y `variant_ids`, así que la UoW de D1 recalcula la lista corta desde
la evidencia. `PurchaseService.start(grant=...)` crea la compra con `purchase_mode='HNP'` (un trigger
impide cambiar el modo o el grant después) y se detiene en `checkout_prepared`, con el journal
post-create en `observed`. `claim_completion`, como `pending_purchase`, solo ve compras HP.

**Cómo lo arma HNP-09a.** `HnpExecutionService.run(task_id)` (`application/hnp_execution.py`) hace
los pasos 1 a 10 y devuelve el `Step` que terminó la corrida: `applied` con UoW-2 comprometida,
`closed` con su terminal o `lease_lost`. Recibe por inyección el ledger, el store, `start`, el
gateway ACP, el `AgentKeyCloser`, la compuerta (`NullHnpPolicyGate` en `off`, sin frescura ni D1) y
el selector (`HnpSelector`; `DeterministicHnpSelector` toma la primera de la lista corta). Lee del
ledger el grant, la intención y los datos de entrega; el selector solo ve la lista corta, la
intención y `preference_text`. UoW-1 espera la `Authority` del claim; D3 y UoW-2, la que validó
UoW-1 y no la que relee UoW-C, que no compara el kill switch ni el grant (K10). Una compra que
`start` dejó `selected` termina `failed(governance_unavailable)` (K15). `payment_request_digest`
fija la petición al PSP del despacho (clave `<operation_id>:payment`, `allowance` y los digests del
checkout JWT y de los dos cerrados); 09b la reconstruye igual. Lo que §8.1 no nombra: un feed ilegible, un rechazo del comercio, una firma del
checkout o un cierre que falla terminan `checkout_failed`; un selector que falla o elige fuera de la
lista corta, `selection_not_eligible`; un grant ilegible y un re-claim con avance sin outbox,
`state_conflict` (reanudarlo es de 09b). El `outcome_code` de un deny de D1 sale de los
`reason_codes` de su evidencia (`sqlite_d1_denials`): POL-01, luego POL-02, luego POL-11.

**Arranque (HNP-09a).** Con `AGENT_HNP_ENABLED`, `build_runtime` arma el executor en un solo lugar,
`delegation/bootstrap.build_hnp_executor`, que es donde 09b enchufa el despacho. En `enforce`, la
`PurchaseService` del runtime recibe D1-HNP (`governance/bootstrap.build_hnp_d1_gate`, con
`merchant_refs=operator_merchant_refs(...)`, K11) y el executor recibe
`build_hnp_policy_gate(settings, clock, trust=hnp_trust.mandates)` y el guard de frescura del
runtime; sin ellos no arranca. Solo el executor tiene la Agent Key. `AGENT_HNP_SELECTOR=llm`
(`build_hnp_selector`) usa `CandidateChoice` de P5b restringido a la lista corta y exige
`OPENAI_API_KEY` y `OPENAI_MODEL` al arrancar. La llamada se mide como `model.hnp_select`, con sus
tokens en `stage_timings.input_tokens`/`output_tokens`; esas columnas solo existen con HNP
encendido, así que la tabla de HP no cambia.

### 3.4 Dónde corren D1, D2-HNP y D3-HNP

| Punto | Código | Políticas | Efecto protegido | Con deny |
| --- | --- | --- | --- | --- |
| D1-HNP | Compuerta D1 propia de HNP (ligada a `b2-hnp-v1`, §7), llamada por `PurchaseService.start` con `purchase_mode=HNP` | POL-01, 02, 11 | `create_checkout` | 0 checkout. POL-11: `escalated`. POL-01/02: `failed` y alerta al operador |
| D2-HNP | `HnpExecutionService`, después del checkout y de la frescura `close` en `allow` (resultado `fresh`) | POL-03..09 | Reserva CAS y cierre | 0 reserva y 0 firma. Clasificación del deny en §8.1 |
| D3-HNP | Después del cierre, antes de UoW-2 | POL-09, 10 | Fila del outbox | 0 despacho; la reserva se libera (no hay outbox) |

D1, D2 y D3 usan el evaluador de referencia en Python (D11 sigue abierto) y el policy set
`b2-hnp-v1` (§7). `b2-pilot-v1` y `b2-hp-v2` no cambian.

**Reloj.** Todo chequeo de autoridad HNP (`valid_from`/`expires_at` del grant, POL-07, vencimiento
de la propuesta, `iat`/`exp` de los mandatos, lease) usa el reloj real en UTC (inyectable en
tests), nunca `AGENT_SANDBOX_CLOCK_AT` ni el reloj comercial de D1 (`AGENT_D1_COMMERCIAL_CLOCK_ID`),
como pide HNP01. Cada UoW lee `now` una sola vez, dentro de su `BEGIN IMMEDIATE`, y lo usa para
todos sus chequeos y para `recorded_at`.

## 4. Contrato de datos

### 4.1 Identificadores

Todos cumplen `Id` de policy-v1 (`[A-Za-z0-9._:-]`, máximo 128) y se generan con `id_factory`
(`<prefijo>_<uuid4 hex>`):

| Prefijo | Objeto |
| --- | --- |
| `dpr_` | Propuesta |
| `dg_` | Grant |
| `dm_` | Mandato guardado |
| `ht_` | Tarea |
| `rsv_` | Reserva |
| `op_` | Operación (identidad durable del efecto; una por reserva) |
| `ob_` | Fila del outbox |

`run_id` de la compra HNP: se asigna en el primer claim, con el mismo formato que las compras HP.
Claves de idempotencia externas: `<operation_id>:payment` (PSP) y `<operation_id>:complete`
(comercio).

### 4.2 Alcance tipado y `scope_digest`

`DelegationScope` es el tipo de policy-v1 §4: `merchant_refs` (el `merchant_ref` que D1 usa para el
comercio configurado), `variant_ids` (1 a 5, únicos y ordenados), `currency="COP"`, `quantity_max=1`,
`total_max_minor` (entero > 0, incluye envío), `destination_binding` (`PrivateBinding`
`hmac-sha256/v1`), `instrument_ref="tesis_sandbox"` y `uses_max=1`.

**Decisión de spec, clave del destino:** `destination_binding` usa la misma clave y el mismo esquema
que `MaterialTerms.destination_binding` de D2 (hoy la clave HMAC del vault D1,
`AGENT_D1_KEY_ID`/`AGENT_D1_HMAC_KEY_HEX`). Así POL-08 compara valores del mismo espacio. Por eso
`AGENT_HNP_ENABLED=true` exige esas dos variables en cualquier modo de gobernanza.

Lo que el comprador consiente es `ConsentScope`:

```json
{
  "schema_version": "commerce-delegation/1.0",
  "proposal_id": "dpr_…",
  "owner_ref": "owner:<seudónimo>",
  "scope": { "…DelegationScope…" },
  "display": { "…ScopeView…" },
  "presented_at": "2026-10-21T15:00:00.000Z",
  "expires_at": "2026-10-22T15:00:00.000Z",
  "preference_text": null
}
```

`scope_digest = "sha256:" + hex(SHA256(b"commerce-delegation-scope/v1\0" + bytes))`, con `bytes` en
`commerce-canonical-json/1` (policy-v1 §5).

**Referencia al dueño.** El `sub` del JWT nunca se guarda en claro fuera de `owner_id`. `owner_ref`
es el mismo seudónimo que D1 y D2 usan para el dueño: `"owner:" + HMAC-SHA256` con la clave del
vault D1 sobre el dominio `owner` (`SqliteD1ProvenanceStore`, `governance/provenance.py`
`pseudonym`). Ese valor va en `ConsentScope.owner_ref`, en `on_behalf_of` y en el `actor_ref` de
usuario de `delegation_events`, y coincide con el `owner_ref` de la evidencia de gobernanza.

**Dueño del tipo.** `DelegationScope`, `ConsentScope`, su forma canónica y `scope_digest` viven en
`governance/hnp_models.py` (HNP-03). HNP-08 los usa al proponer y consentir, HNP-02 guarda los
bytes que recibe y HNP-V1 los reutiliza para recalcular el digest.

- **Decisión de spec, vencimiento fijo:** `expires_at` del grant se fija al proponer
  (`presented_at + valid_for_seconds`), porque la tarjeta debe mostrar la hora exacta que se
  consiente. La vigencia efectiva pierde a lo sumo el TTL de la propuesta.
- **Decisión de spec, indicación del comprador:** `preference_text` (opcional, 1 a 280 caracteres) es
  texto del propio comprador, no del modelo. Se muestra tal cual en la tarjeta, entra en el digest y
  solo lo lee el selector LLM. Nunca entra en `DelegationScope` ni en el PDP. Lo necesita HN10.

### 4.3 Tablas SQLite

Las crea HNP-02 en la base del agente cuando `AGENT_HNP_ENABLED=true`, salvo `purchases` (HNP-05) y
`governance_evidence` (P3 y HNP-06). Tiempos en `UtcTime` de policy-v1. Columnas marcadas
**protegida** guardan datos personales o tokens: no salen en vistas, eventos, evidencia, traza ni
logs. Es la misma protección que los mandatos HP (`ap2-profile.md`).

**`delegation_proposals`**

| Columna | Tipo y regla |
| --- | --- |
| `proposal_id` | TEXT PK |
| `owner_id` | TEXT NOT NULL |
| `scope_json` | TEXT NOT NULL, `json_valid`; `ConsentScope` canónico |
| `scope_digest` | TEXT NOT NULL |
| `checkout_details_json` | TEXT NOT NULL, **protegida**; `CheckoutDetails` (buyer y fulfillment) |
| `presented_at`, `expires_at` | TEXT NOT NULL |
| `grant_id` | TEXT NULL UNIQUE; se llena al consentir |

**`delegation_grants`**

| Columna | Tipo y regla |
| --- | --- |
| `grant_id` | TEXT PK |
| `proposal_id` | TEXT NOT NULL UNIQUE |
| `owner_id` | TEXT NOT NULL |
| `grant_version` | INTEGER NOT NULL CHECK = 1 (el alcance es inmutable en v1) |
| `scope_json`, `scope_digest` | Copia de la propuesta |
| `intent_json` | TEXT NOT NULL; la intención derivada (§3.3) |
| `checkout_details_json` | TEXT NOT NULL, **protegida** |
| `state` | TEXT CHECK IN (`active`, `consumed`, `revoked`, `expired`) |
| `revoked_reason` | TEXT NULL CHECK IN (`owner`, `operator`, `escalated`, `failed`); no nula si y solo si `state='revoked'` |
| `authority_epoch` | INTEGER NOT NULL ≥ 1; +1 en cada paso a un estado terminal |
| `state_version` | INTEGER NOT NULL ≥ 1; +1 en cada transición del grant o de su reserva |
| `valid_from` | TEXT NOT NULL (= `consented_at`) |
| `expires_at` | TEXT NOT NULL |
| `consented_at`, `updated_at` | TEXT NOT NULL |
| `closed_at` | TEXT NULL |
| `agent_key_kid` | TEXT NOT NULL; thumbprint RFC 7638 (SHA-256, base64url) de la Agent Key pública |
| `issuer_kid` | TEXT NOT NULL; thumbprint de la clave pública del proveedor |

Índice `(owner_id, state)`.

**`delegation_mandates`**

| Columna | Tipo y regla |
| --- | --- |
| `mandate_id` | TEXT PK |
| `grant_id` | TEXT NOT NULL |
| `kind` | TEXT CHECK IN (`open_checkout`, `open_payment`, `closed_checkout`, `closed_payment`) |
| `reservation_id` | TEXT NULL; no nula si y solo si `kind` empieza por `closed_` |
| `token` | TEXT NOT NULL, **protegida**; SD-JWT abierto, o la cadena `abierto~~cerrado` |
| `token_digest` | TEXT NOT NULL; `sha256:` + hex del token en ASCII |
| `link_hash` | TEXT NOT NULL; abierto: hash del JWT del emisor (`conditional_transaction_id` del pago); cerrado: `sd_hash` del último segmento (= `reference` del recibo). Ver el perfil AP2 |
| `public_jwk_json` | TEXT NOT NULL; JWK pública que verifica ese salto (emisor o Agent Key). Permite al verificador de evidencia re-verificar firmas sin conexión |
| `created_at` | TEXT NOT NULL |

UNIQUE `(grant_id, kind)`: un grant tiene a lo sumo un par abierto y un par cerrado.

**`grant_reservations`**

| Columna | Tipo y regla |
| --- | --- |
| `reservation_id` | TEXT PK |
| `grant_id`, `owner_id`, `run_id`, `checkout_id` | TEXT NOT NULL |
| `operation_id` | TEXT NOT NULL UNIQUE |
| `amount_minor` | INTEGER NOT NULL ≥ 0; total autoritativo del checkout |
| `currency` | TEXT NOT NULL CHECK = `COP` |
| `expected_epoch` | INTEGER NOT NULL; `authority_epoch` al reservar |
| `state` | TEXT CHECK IN (`reserved`, `committed`, `consumed`, `released`, `uncertain`) |
| `state_version` | INTEGER NOT NULL ≥ 1 |
| `release_basis` | TEXT NULL CHECK IN (`no_outbox`, `psp_error_receipt`) |
| `uncertain_reason` | TEXT NULL CHECK IN (`dispatch_timeout`, `partial_verifier_failure`, `receipt_mismatch`) |
| `d2_evidence_id` | TEXT NULL (nula en B0) |
| `created_at`, `updated_at` | TEXT NOT NULL |

```sql
CREATE UNIQUE INDEX one_use_per_grant ON grant_reservations(grant_id)
  WHERE state IN ('reserved','committed','uncertain','consumed');
```

**`hnp_tasks`**

| Columna | Tipo y regla |
| --- | --- |
| `task_id` | TEXT PK |
| `grant_id` | TEXT NOT NULL UNIQUE (una tarea por grant) |
| `owner_id` | TEXT NOT NULL |
| `run_id` | TEXT NULL UNIQUE |
| `state` | TEXT CHECK IN (`scheduled`, `running`, `purchased`, `escalated`, `failed`, `canceled`, `uncertain`) |
| `outcome_code` | TEXT NULL; código de §8.1, nulo en `scheduled`, `running` y `purchased` |
| `claim_count` | INTEGER NOT NULL ≥ 0 |
| `lease_id`, `lease_expires_at` | TEXT NULL; no nulos si y solo si `state='running'`. Cada claim genera un `lease_id` nuevo; cada UoW posterior lo compara y renueva `lease_expires_at` (§4.5) |
| `created_at`, `updated_at` | TEXT NOT NULL |
| `finished_at` | TEXT NULL |

**`dispatch_outbox`**

| Columna | Tipo y regla |
| --- | --- |
| `outbox_id` | TEXT PK |
| `reservation_id`, `operation_id` | TEXT NOT NULL UNIQUE |
| `grant_id`, `run_id`, `checkout_id` | TEXT NOT NULL |
| `amount_minor`, `currency`, `checkout_expires_at` | Lo necesario para reconstruir el cuerpo exacto de `delegate_payment` |
| `payment_request_digest` | TEXT NOT NULL; digest del cuerpo del primer envío (el reintento debe coincidir) |
| `authority_epoch`, `control_epoch` | INTEGER NOT NULL; observados al comprometer |
| `d3_evidence_id` | TEXT NULL (nula en B0) |
| `state` | TEXT CHECK IN (`committed`, `payment_sent`, `payment_accepted`, `complete_sent`, `completed`, `rejected`, `uncertain`) |
| `rejected_by` | TEXT NULL CHECK IN (`psp`, `merchant`) |
| `ap2_error` | TEXT NULL CHECK IN (`invalid_credential`, `invalid_mandate`, `unresolved_constraint`, `mandates_not_supported`) |
| `order_id` | TEXT NULL |
| `error_receipt_jwt` | TEXT NULL; el recibo de error verificado que llevó a `rejected` (no tiene datos personales) |
| `created_at`, `updated_at` | TEXT NOT NULL |

El token `vt_…` del PSP **no** se guarda: al reanudar se reenvía el mismo cuerpo con la misma clave
y el PSP devuelve el mismo token (su almacén lo deriva de la clave y del cuerpo). La tarjeta de prueba
sale de la configuración, nunca de la base.

**`delegation_events`**: bitácora solo de inserción, encadenada por grant como `hp_evidence`.

| Columna | Tipo y regla |
| --- | --- |
| `grant_id`, `sequence` | PK; `sequence` ≥ 1 y consecutiva |
| `event_type` | TEXT CHECK IN la lista de §9.1 |
| `actor_kind` | TEXT CHECK IN (`user`, `service`, `operator`) |
| `actor_ref` | TEXT NOT NULL; `owner_ref` del dueño (usuario, §4.2), `service:hnp-executor` u `operator` |
| `on_behalf_of` | TEXT NOT NULL; `owner_ref` del dueño |
| `task_id`, `run_id` | TEXT NULL |
| `body` | TEXT NOT NULL, `json_valid`; sin datos personales ni tokens |
| `recorded_at` | TEXT NOT NULL |
| `previous_digest` | TEXT; nulo si y solo si `sequence = 1` |
| `digest` | TEXT NOT NULL UNIQUE; `sha256:` de `b"delegation-event/v1\0"` + canónico de la fila sin `digest` |

Disparadores como los de `hp_evidence`: encadenamiento, sin `UPDATE`, sin `DELETE`.

**`hnp_controls`**: historial del kill switch, solo de inserción. **Decisión de spec:** una fila por
cambio; el estado vigente es la fila con el mayor `control_epoch`. Así el cambio queda auditado sin
otra tabla.

| Columna | Tipo y regla |
| --- | --- |
| `control_epoch` | INTEGER PK ≥ 1; la fila 1 (`kill_switch=0`, `actor_ref='bootstrap'`) se crea con la tabla |
| `kill_switch` | INTEGER CHECK IN (0, 1) |
| `actor_ref` | TEXT NOT NULL (`operator` o `bootstrap`) |
| `changed_at` | TEXT NOT NULL |

**`purchases`** (HNP-05): `purchase_mode TEXT NOT NULL DEFAULT 'HP' CHECK IN ('HP','HNP')` y
`grant_id TEXT NULL`, con `CHECK((purchase_mode = 'HNP') = (grant_id IS NOT NULL))`.
`SqliteAgentStore.pending_purchase` (y con él `pending_purchase_view` y la regla de una compra
pendiente a la vez) excluye `purchase_mode='HNP'`: una ejecución autónoma nunca aparece como compra
HP pendiente ni bloquea una compra HP del mismo dueño. Etapas y escritura de la compra HNP: §4.6.

**`governance_evidence`**: D1 ya existe y P3 agrega `phase='D2'`; HNP-06 agrega `phase='D3'`. Los
registros HNP llevan `purchase_mode='HNP'`. En D2-HNP y D3-HNP:

- `application.transition_ref` es `reservation_id` en D2 y `outbox_id` en D3.
- `expected_local_state_version` (y `application.expected_state_version`) es
  `delegation_grants.state_version` leído al armar el input, el mismo valor que
  `UsageFacts.state_version`. `resulting_state_version` es ese valor + 1 con `allow` (la reserva o
  el compromiso suben la versión del grant, §4.3) y nulo con `deny`.
- Cada decisión registra su fila en `governance_decision_scopes` (P3) con los bytes del artefacto
  `b2-hnp-v1` y del motor, como D2-HP.

### 4.4 Estados y transiciones

**Propuesta:** `proposed` (fila sin `grant_id`) → `consented` (con `grant_id`). Vencida si
`now ≥ expires_at`; no hay transición escrita.

**Grant**

| De | A | Disparador | UoW |
| --- | --- | --- | --- |
| — | `active` | Consentimiento válido | Consentimiento |
| `active` | `consumed` | Orden con ambos recibos y `reference` correcta | UoW-3 |
| `active` | `revoked(owner)` | `POST /delegations/{id}/revoke` o `revoke-all` | Revocación |
| `active` | `revoked(operator)` | Claim, UoW-1 o UoW-2 con el kill switch encendido | Ese mismo UoW |
| `active` | `revoked(escalated)` | La tarea pasa a `escalated` | El UoW que escala |
| `active` | `revoked(failed)` | La tarea pasa a `failed` | El UoW que falla |
| `active` | `expired` | Claim, UoW-1 o UoW-2 con `now ≥ expires_at` | Ese mismo UoW |

Las transiciones por kill switch y vencimiento solo ocurren mientras no hay fila en el outbox
(§4.5, re-claim). Todo paso a un estado terminal suma 1 a `authority_epoch`. **Decisión de spec:** el plan nombraba
`owner|operator|escalated`; se agrega `failed` porque con una tarea por grant un fallo deja el grant
inútil, y «Mis delegaciones» no debe mostrarlo como activo. Una tarea `uncertain` deja el grant
`active` (no se puede reusar por el índice de un solo uso). Si la orden llega después de una
revocación, la reserva pasa a `consumed` y el grant sigue `revoked`. Las vistas muestran `expired`
cuando un grant `active` ya venció, aunque la fila no se haya escrito.

**Reserva**

| De | A | Disparador |
| --- | --- | --- |
| — | `reserved` | UoW-1 |
| `reserved` | `committed` | UoW-2 (con la fila del outbox) |
| `reserved` | `released(no_outbox)` | Deny de D3, revocación, kill switch, vencimiento o cierre vencido antes de UoW-2 |
| `committed` | `released(psp_error_receipt)` | El PSP responde con recibo de error: nada llegó al comercio |
| `committed` | `consumed` | UoW-3 |
| `committed` | `uncertain` | Timeout, 5xx o 4xx sin recibo válido (incluido 409 `idempotency_conflict`) después de enviar; recibo de error del comercio después del token del PSP; recibo con `reference` distinta |
| `uncertain` | `consumed` | Conciliación manual (v1: sin código, ver abajo) |

Nunca se libera una reserva con fila en el outbox salvo con el recibo de error del PSP. Una reserva
`uncertain` no se libera automáticamente en v1.

**Tarea**

| De | A | Disparador |
| --- | --- | --- |
| — | `scheduled` | Consentimiento |
| `scheduled` | `running` | Claim exitoso |
| `scheduled` | `canceled` | Revocación antes del claim; claim con kill switch o grant vencido |
| `running` | `running` | Re-claim con el lease vencido: reanuda desde el estado durable (§4.5) |
| `running` | `purchased` / `escalated` / `failed` / `canceled` / `uncertain` | §8.1 |
| `uncertain` | `purchased` | Conciliación manual (v1: sin código) |

**`uncertain` en v1.** Ningún PR implementa `uncertain → purchased` ni `uncertain → consumed`: el
operador concilia a mano con el comercio y el PSP, fuera del sistema, y las filas quedan
`uncertain`. Una tarea `uncertain` no se puede volver a disparar (409 `hnp_task_not_runnable`). En la
evaluación, `uncertain` es un resultado terminal.

**Outbox:** `committed → payment_sent → payment_accepted → complete_sent → completed`;
`payment_sent → rejected(psp)` solo con un recibo de error válido (perfil AP2); `complete_sent →
rejected(merchant)`; `payment_sent` o `complete_sent` → `uncertain` ante timeout, 5xx o 4xx sin
recibo válido.

### 4.5 Unidades de trabajo

Todas abren `BEGIN IMMEDIATE`, leen `now` adentro (§3.4, «Reloj»), comparan versiones y escriben
evento y evidencia en la misma transacción.

| UoW | Compara | Escribe |
| --- | --- | --- |
| Consentimiento | Propuesta del dueño, sin grant, digest igual, `now < expires_at` de la propuesta | Grant, mandatos abiertos, tarea `scheduled`, `consented`, `mandates_issued` |
| Claim | Tarea `scheduled` (o `running` con lease vencido); sin outbox: kill switch y grant `active` y vigente | Tarea `running`, `lease_id` nuevo y lease de 120 s, `run_id` (solo en el primer claim), `task_claimed` (o el terminal que corresponda) |
| UoW-1 | Lease propio; grant `active`, `authority_epoch`, `state_version`, `control_epoch` y kill switch; `now + 300 s ≤ expires_at` | Reserva `reserved`, evidencia D2 `transition_committed`, `reservation_reserved` |
| UoW-C | Lease propio; reserva `reserved` y propia | Mandatos cerrados, `mandates_closed` |
| UoW-2 | Lease propio; lo de UoW-1 con `now < expires_at`, más reserva `reserved` y su `state_version` | Reserva `committed`, outbox `committed`, evidencia D3, compra `completing` (§4.6), `dispatch_committed` |
| Despacho | Lease propio; estado del outbox | El siguiente estado del outbox, o `rejected`/`uncertain` con la reserva y la tarea (§8.1) |
| UoW-3 | Lease propio; outbox `completed`, recibos verificados | Reserva y grant `consumed`, tarea `purchased`, compra `completed` (§4.6), `receipt_recorded` ×2, `consumed`, `task_finished` |
| Revocación | Grant del dueño | Grant `revoked(owner)`, epoch +1; reserva `reserved` → `released(no_outbox)`; tarea `scheduled` → `canceled`; `revoked` |
| Kill switch | — | Fila nueva en `hnp_controls` (solo si cambia el valor) |

**Decisión de spec, margen de vigencia:** UoW-1 exige `now + 300 s ≤ expires_at`; si no, la tarea
termina `canceled(grant_expired)` sin reservar ni cerrar. Así el abierto sobrevive al cierre y el
comercio y el PSP, que exigen `now < exp` sin holgura (perfil AP2), no rechazan por un vencimiento
que el agente ya podía prever.

**Re-claim con outbox.** Un re-claim cuya reserva está `committed` o `uncertain` (con fila en el
outbox) no evalúa kill switch, revocación ni vencimiento: reanuda el despacho con las mismas claves
o pasa a `uncertain`. Por esos chequeos nunca termina `canceled` ni `failed` ni libera la reserva;
lo único que libera con outbox es el recibo de error del PSP (§4.4). Los chequeos del claim solo
cierran la tarea si no hay outbox.

**Lease y fencing.** Toda UoW posterior al claim (UoW-1, UoW-C, UoW-2, despacho y UoW-3) compara
`hnp_tasks.lease_id` y `state='running'`, y renueva `lease_expires_at` (`now + 120 s`). Cada llamada
externa tiene un timeout menor que el lease. Ante un CAS perdido, el executor relee y clasifica así:

- lease ajeno → sale sin escribir terminal;
- `authority_epoch` cambió por una revocación del dueño → `canceled(revoked_by_owner)`;
- `control_epoch` distinto → `canceled(kill_switch)`, aunque el kill switch ya esté apagado;
- grant vencido → `canceled(grant_expired)`;
- cualquier otro caso → `failed(state_conflict)`, escrito también por CAS (grant `active`).

Con fila en el outbox ningún CAS perdido termina `canceled` ni `failed`: rige el re-claim con outbox
(reanudar o `uncertain`); el lease ajeno sigue saliendo sin escribir. En B2 el perdedor deja
evidencia `GOV_STATE_CONFLICT` (`application_failure`).

**Costura entre el ledger, la evidencia y la compra.** Los métodos del ledger de HNP-02 para UoW-1,
UoW-2, UoW-3 y para terminar la tarea reciben `hooks: Sequence[TransactionHook]`, con
`TransactionHook = Callable[[sqlite3.Connection], None]`. El ledger los corre en su conexión,
dentro de su `BEGIN IMMEDIATE`, después de sus propias escrituras y antes del `COMMIT`; si uno lanza,
se revierte todo. Los usan:

- HNP-06: escribe el registro D2 o D3 en `governance_evidence` y verifica la transición (como
  `_ApprovalTransition` de P3, contra `delegation_grants.state_version` y la reserva o la fila del
  outbox). `d2_evidence_id` y `d3_evidence_id` se generan antes de la transacción, como en P3, y el
  ledger los recibe como valores. Si el hook falla: `GOV_EVIDENCE_UNAVAILABLE`. La fila de
  `governance_decision_scopes` se registra al armar el input, como en P3. `SqliteHnpPolicyGate`
  (`governance/hnp_gate.py`, armado por `build_hnp_policy_gate`) devuelve en `HnpGateDecision`:
  `hooks` (el allow en UoW-1 o UoW-2, el deny en el terminal); `conflict_hooks`, que el executor pasa
  al terminal cuando UoW-1 o UoW-2 vuelve `blocked` y que guardan el allow como `blocked` más
  `GOV_STATE_CONFLICT` (el perdedor del CAS deja evidencia); y `transition_ref`, el `outbox_id` que
  nombra el allow de D3 y con el que el executor escribe la fila del outbox. Las conexiones del ledger
  de delegación activan `foreign_keys`, que la evidencia necesita.
- HNP-05: `SqliteAgentStore.transition_hnp_purchase(connection, run_id, *, expected_state_version,
  stage, failure_code=None)`, el CAS de `purchases` que usan UoW-2 y los terminales.
- HNP-09b: `SqliteAgentStore.record_hnp_completion(connection, run_id, snapshot, *,
  expected_state_version, checkout_receipt_jwt, payment_receipt_jwt)` en UoW-3.

En B0-HNP no hay hook de evidencia; los de la compra corren igual.

**Decisión de spec, UoW-C:** el plan exige que un crash después del cierre no genere otro cierre. Por
eso los cerrados se guardan apenas se firman, antes de D3. Al reanudar, si la reserva ya tiene
cerrados se reutilizan; si vencieron (cierre de 300 s) y no hay outbox, la reserva se libera y la
tarea termina `failed(close_expired_before_dispatch)`, sin firmar otro cierre.

### 4.6 La compra HNP en `purchases`

La compra HNP vive en las mismas tablas que HP, para que la traza, `/me/purchases`, los eslabones
`receipts` y `order_webhook` de P2 y el runner lean una sola fuente. `record_completion` no sirve
(exige `stage='completing'` y una fila en `approvals`), así que HNP escribe con sus propios métodos
(§4.5, costura).

| Momento | `purchases.stage` | Quién |
| --- | --- | --- |
| `PurchaseService.start` con `purchase_mode=HNP` | `received` → `selected` → `checkout_prepared`, y ahí se detiene (HP sigue a `awaiting_confirmation`) | HNP-05 |
| UoW-2 | `checkout_prepared` → `completing` | 09a, con el hook de HNP-05 |
| UoW-3 | `completing` → `completed` | 09b |
| Tarea `uncertain` | `completing` → `uncertain` | 09b |
| Deny de D1 o D1 no disponible dentro de `start` | Queda `received`: no hay checkout. El terminal de la tarea la pasa a `failed` con `failure_code` = `outcome_code`, o a `canceled` si escala (POL-11) | `start` (HNP-05); el terminal, 09a con el hook de HNP-05 |
| Checkout sin confirmar dentro de `start` (`d1_dispatch_uncertain`, `d1_post_create_uncertain`) | `uncertain`, y así queda aunque la tarea termine `failed`: el checkout pudo crearse | `start` (HNP-05) |
| Rechazo del comercio al crear o guarda de presupuesto dentro de `start` | `failed` con el código del rechazo, o `canceled`; el terminal de la tarea no la cambia | `start` (HNP-05) |
| Tarea `failed` después de `start` | → `failed`, con `failure_code` = `outcome_code` | 09a/09b, con el hook de HNP-05 |
| Tarea `escalated` o `canceled` después de `start` | → `canceled` | 09a/09b, con el hook de HNP-05 |

HNP no pasa por `awaiting_confirmation` ni `approved`, y no escribe `approvals`. Una tarea que
termina antes de llamar a `start` (claim, lista corta vacía) no tiene fila en `purchases`.

`transition_hnp_purchase` lleva `received` a `failed` o `canceled`, y `checkout_prepared` o
`completing` a su terminal. Si `start` ya dejó la compra `uncertain`, `failed` o `canceled`, un
terminal (`failed`, `canceled` o `uncertain`) solo verifica la versión y la conserva: describe lo que
pasó en el comercio, y un checkout incierto nunca se marca `failed`. Una compra que `start` dejó en
`selected` (allow de D1 sin resultado del despacho anotado) no tiene transición: necesita la
recuperación de D1.

**UoW-3 (dueño: HNP-09b).** En una sola transacción y por CAS de `purchases.state_version`:
`completion_results` (snapshot y los dos JWT de recibo), la proyección `orders` (`_project_order`),
`purchases.order_id` y `stage='completed'`, más lo del ledger (§4.5). Los eventos `receipt_recorded`
guardan `receipt_digest = "sha256:" + hex(SHA256(<JWT en ASCII>))` de esos mismos dos JWT. HNP-11 y
HNP-V1 leen `completion_results` y `orders`, como en HP.

## 5. API

Cuerpos JSON estrictos (`extra="forbid"`). Errores con la forma actual de la API: `{"code": "…",
"message": "<español>"}`. Con `AGENT_HNP_ENABLED=false` ninguna ruta de esta sección se monta: 404
genérico de FastAPI. Las rutas HP no cambian.

### 5.1 Rutas del comprador (JWT, `current_buyer`)

| Método y ruta | Cuerpo | Éxito | Errores |
| --- | --- | --- | --- |
| `POST /delegations/proposals` | `DelegationProposalRequest` | 201 `DelegationProposal` | 401; 422 `delegation_request_invalid`, `delegation_variant_unavailable`, `delegation_validity_exceeded`, `delegation_destination_unsupported` |
| `POST /delegations` | `{proposal_id, scope_digest}` | 201 `DelegationView`; 200 con el mismo grant si la propuesta ya se consintió con ese digest | 401; 404 `delegation_proposal_not_found`; 409 `delegation_scope_mismatch`, `delegation_proposal_expired`; 503 `delegation_signer_unavailable` |
| `GET /me/delegations` | — | 200 `DelegationList` (más recientes primero) | 401 |
| `GET /delegations/{grant_id}` | — | 200 `DelegationView` | 401; 404 `delegation_not_found` |
| `POST /delegations/{grant_id}/revoke` | — | 200 `RevokeReceipt`; repetir devuelve el mismo recibo; un grant ya `revoked` por otra causa (`operator`, `escalated`, `failed`) devuelve 200 con su estado actual (ver abajo) | 401; 404 `delegation_not_found`; 409 `delegation_not_revocable` (grant `consumed` o `expired`) |
| `POST /me/delegations/revoke-all` | — | 200 `RevokeAllReceipt` (solo los grants que estaban `active`) | 401 |

Un grant o una propuesta de otro dueño responden 404, igual que una inexistente.

Revocar un grant que ya está `revoked(operator|escalated|failed)` no escribe nada: responde 200 con
`RevokeReceipt{state: "revoked", revoked_reason: <la causa guardada>, revoked_at: closed_at, effect:
"no_effect", message: revoke.already_closed}`. Con `revoked(owner)` devuelve el recibo original.

`DelegationProposalRequest`: `variant_ids` (1 a 5, únicos), `total_max_minor` (entero > 0),
`currency` (`"COP"`), `valid_for_seconds` (60 ≤ v ≤ `AGENT_HNP_MAX_VALIDITY_SECONDS`), `details`
(`CheckoutDetails` de HP) y `preference_text` (opcional). Variante fuera del feed actual del comercio
configurado o en otra moneda: `delegation_variant_unavailable`. Una dirección que el comercio no
acepta: `delegation_destination_unsupported`. Un total menor que todos los precios no
se rechaza: D1 lo escalará con POL-11.

**Cómo lo arma HNP-08** (`delegation_api.py`, `delegation/service.py`, `delegation/views.py`):

- Los cuerpos se leen como JSON estricto (`parse_model`), así que un cuerpo inválido responde
  `{code, message}` con `delegation_request_invalid` (o `hnp_request_invalid` en las internas) y no
  el 422 propio de FastAPI, que las rutas HP conservan. Un `valid_for_seconds` menor que 60 es `delegation_request_invalid`; uno mayor que el
  máximo, `delegation_validity_exceeded`.
- En el alcance, `merchant_refs` es `merchant_pseudonym` de `MERCHANT_BASE_URL` y
  `destination_binding` sale de `governance/destination.py`, la misma función que usa D2 sobre los
  `fulfillment_details` del checkout (K8), con `binding_id = "destination:<proposal_id>"`. En la
  tarjeta, `merchant_name` es el vendedor del feed (o `Tesis Commerce Merchant`),
  `destination_label` es `"<city>, <state>"` de la dirección y `instrument_label` es «Tarjeta de
  prueba del sandbox».
- **El destino se normaliza como el comercio.** D2 calcula el binding sobre el eco del comercio,
  y el comercio no devuelve la dirección tal como la recibe: `AddressInfo` en
  `src/commerce_lab/contracts/commerce.py` del comercio (`abf30df`) acepta un solo destino, y
  `normalize_supported_city` reescribe la ciudad. Al proponer, antes del digest,
  `acp/destination.py` (`merchant_fulfillment`) aplica la misma regla: la ciudad sin espacios
  alrededor y en minúsculas (`casefold`) que sea `bogota` o `bogotá` pasa a `Bogota`, y el
  departamento `DC`, el país `CO` y el código postal `110111` deben llegar exactos. Los demás
  campos no cambian. Esa dirección normalizada es la que entra en `destination_binding` y en
  `destination_label`, y la que se guarda en `checkout_details_json` para que el ejecutor la
  envíe. Lo que el comercio rechazaría responde 422 `delegation_destination_unsupported`, sin
  escribir nada, para no consentir un destino que POL-08 siempre escalaría.
  `tests/fixtures/merchant_destination/cases.json` fija la equivalencia caso por caso, con el
  commit del comercio del que sale.
- Al consentir, la propuesta también se da por vencida si ya pasó el `expires_at` del grant (fijado
  al proponer): no se firma un abierto ya vencido. Antes de guardar, los abiertos se verifican con
  `verify_open_proof`; si algo falla, 503 `delegation_signer_unavailable` sin escribir nada.
- La vista de una tarea comprada toma el título de `purchases.selected_variant_id`, el total de la
  reserva y la orden del outbox. El prellenado de un escalamiento usa la variante elegida o, si la
  ejecución no llegó a elegir, la primera del alcance, con el total máximo consentido.

### 5.2 Rutas internas (`/internal/hnp/*`)

Requieren `Authorization: Bearer <token>`, se comparan en tiempo constante y nunca aceptan el JWT del
comprador. **Decisión de spec, «sin CORS»:** el `CORSMiddleware` es global, así que estas rutas
rechazan con 403 `hnp_origin_forbidden` cualquier petición con cabecera `Origin`. Un navegador no
puede dispararlas.

| Método y ruta | Token | Cuerpo | Éxito | Errores |
| --- | --- | --- | --- | --- |
| `POST /internal/hnp/tasks/{task_id}/run` | Servicio | — | 202 `TaskView` en `running` (la ejecución sigue en segundo plano); 200 `TaskView` terminal si el claim terminó la tarea (kill switch, vencida, revocada) | 401 `hnp_unauthorized`; 403 `hnp_origin_forbidden`; 404 `hnp_task_not_found`; 409 `hnp_task_not_runnable` (no `scheduled` o lease vigente) |
| `GET /internal/hnp/tasks/{task_id}` | Servicio | — | 200 `TaskView` | 401; 403; 404 |
| `POST /internal/hnp/kill-switch` | Operador | `{"enabled": bool}` | 200 `KillSwitchState {enabled, control_epoch, changed_at}`; el mismo valor no cambia el epoch | 401; 403; 422 `hnp_request_invalid` |

El token de servicio no sirve para el kill switch ni el de operador para disparar tareas.

### 5.3 Vistas (contrato de HNP-01)

Pydantic en `src/commerce_agent/delegation/contract.py` y TypeScript en
`ui/src/lib/delegation-contract.ts` (`parseDelegationProposal`, `parseDelegationView`,
`parseDelegationList`, `parseDelegationRevokeReceipt` y `parseDelegationRevokeAllReceipt`), con las
mismas fixtures `ui/src/storybook/fixtures/delegation-*.json`: `proposed`, `active`, `running`,
`purchased`, `escalated`, `revoked`, `expired`, `failed` y `uncertain`, más `revoke-receipts` (un
`RevokeAllReceipt` con los tres `effect`). Ambos lados rechazan además los pares de estados que
la spec no permite: `revoked_reason` solo en `revoked`, `outcome_code` según §8.1 (en `failed`
también los de escalamiento, por B0-HNP), `escalation` solo con la tarea `escalated` y con su mismo
código, y un `RevokeReceipt` de otra causa que `owner` solo con `no_effect` (§5.1).

| Tipo | Campos |
| --- | --- |
| `ScopeView` | `merchant_name`, `variants: [{variant_id, title}]`, `total_max: Money`, `expires_at`, `destination_label` («Ciudad, Departamento»), `instrument_label`, `uses: 1`, `preference_text: str \| null` |
| `DelegationProposal` | `proposal_id`, `scope: ScopeView`, `scope_digest`, `presented_at`, `expires_at` (de la propuesta) |
| `TaskView` | `task_id`, `state`, `outcome_code \| null`, `run_id \| null`, `order_id \| null`, `message \| null`, `updated_at` |
| `EscalationView` | `reason` (código de escalamiento), `title`, `message`, `prefill_request` |
| `DelegationView` | `grant_id`, `state`, `revoked_reason \| null`, `scope: ScopeView`, `consented_at`, `expires_at`, `task: TaskView`, `escalation: EscalationView \| null`, `last_activity_at` |
| `DelegationList` | `delegations: [DelegationView]` |
| `RevokeReceipt` | `grant_id`, `state: "revoked"`, `revoked_reason`, `revoked_at`, `effect: "no_effect" \| "released" \| "in_flight"`, `message` |
| `RevokeAllReceipt` | `receipts: [RevokeReceipt]` |

`Money` y `UtcTime` son los del contrato de traza. Los mensajes salen del backend (§8.3); la UI no
los duplica. Las vistas nunca llevan mandatos, tokens, dirección completa ni `scope_digest` salvo en
`DelegationProposal`.

### 5.4 `/health` y `/me/purchases`

`Health` agrega `hnp_enabled: bool`, `hnp_kill_switch: bool`, `hnp_active_grants: int`,
`hnp_running_tasks: int` y `hnp_uncertain_tasks: int`. **Ajuste de HNP-08:** con el flag apagado los
cinco campos se omiten (`exclude_if`) en vez de valer `false` y 0, porque las pruebas HP y los
runners de los pilotos fijan el cuerpo exacto de `/health`; ausente equivale a HNP apagado.
`hnp_active_grants` no cuenta un grant `active` que ya venció (las vistas lo muestran `expired`).
`PurchaseSummary` y `PurchaseTrace` agregan `purchase_mode: "HP" | "HNP"` (HNP-11) como campo
**opcional**: ausente equivale a `"HP"`, el backend lo omite en las compras HP (como `authorization`
en P3, `exclude_if`) y lo escribe solo con `"HNP"`. `parsePurchaseTrace` acepta su ausencia. Así las
fixtures y los bytes de HP no cambian y las pruebas HP pasan sin editarlas.

## 6. Settings

| Variable | Tipo y valor por defecto | Regla | PR |
| --- | --- | --- | --- |
| `AGENT_HNP_ENABLED` | bool, `false` | Monta las rutas, crea las tablas y arranca el executor | 08 |
| `AGENT_HNP_AGENT_KEY_PEM` | secreto | EC P-256 privada; distinta de `AGENT_AP2_PRIVATE_KEY_PEM` (el arranque compara las públicas) | 04 |
| `AGENT_HNP_SERVICE_TOKEN` | secreto | ≥ 32 caracteres | 08 |
| `AGENT_HNP_OPERATOR_TOKEN` | secreto | ≥ 32 caracteres; distinto del de servicio | 08 |
| `AGENT_HNP_MAX_VALIDITY_SECONDS` | int, `86400` | 60 a 86400 | 08 |
| `AGENT_HNP_PROPOSAL_TTL_SECONDS` | int, `600` | 60 a 1800 | 08 |
| `AGENT_HNP_SELECTOR` | `deterministic` \| `llm`, `deterministic` | `llm` exige `OPENAI_*` como el resto del agente | 09a |

Con `AGENT_HNP_ENABLED=true` el arranque exige además `AGENT_AUTH_MODE=jwt`,
`AGENT_AP2_REQUIRED=true`, `AGENT_AP2_PRIVATE_KEY_PEM`, las tres variables HNP secretas,
`AGENT_D1_KEY_ID` y `AGENT_D1_HMAC_KEY_HEX` (§4.2), y `AGENT_GOVERNANCE_MODE` en `off` o `enforce`;
con `enforce`, `AGENT_HP_FLOW=governed`. Cualquier otra combinación falla al arrancar.
**Ajuste de HNP-08:** también exige `AGENT_D1_AES_KEY_HEX`, porque los seudónimos salen de
`ProvenanceKeys` (`pseudonym`), que valida las dos claves de la bóveda juntas. Las comprobaciones
viven en `delegation/bootstrap.py`, corren antes de crear el store y fallan con `RuntimeError` sin
repetir ningún valor; el arranque construye el emisor, que compara las públicas de la Agent Key y
del proveedor.

**Decisión de spec, constantes y no settings:** cierres de 300 s, lease de 120 s, tolerancia de reloj
de los verificadores de 60 s, 5 variantes por alcance y 280 caracteres de indicación. Son parte del
perfil y de la evidencia; un ajuste por entorno los volvería una variable más del experimento. El
selector guionado (adversarial) se inyecta por código en tests y en `commerce_evaluation`, nunca por
variable de entorno.

Las variables de evaluación (`HNP_PILOT_MAX_COST_USD`) son de E2 y no entran al producto. El tope de
la corrida en vivo es **USD 2**, y la lanza el líder a mano, con permiso de Yair; ningún PR, test ni
CI la corre (`AGENTS.md`). El comercio
y el PSP no ganan variables: reutilizan su confianza AP2 y `PAYMENT_MERCHANT_ID` (`tesis_merchant`).

## 7. Policy set `b2-hnp-v1`

Policy set nuevo, revisión 2 (la 1 solo tenía la fila D1), con las filas HNP de `APPLICABLE_RULES`
(Adenda 1 de la ola HP):

| `(fase, purchase_mode)` | Reglas, en orden | Acción | Policy set | PR |
| --- | --- | --- | --- | --- |
| `(D1, HP)` | POL-01, 02, 11 | `prepare_checkout` | `b2-hp-v2` | P1 |
| `(D2, HP)` | POL-03..06 | `complete_purchase` | `b2-hp-v2` | P3 |
| `(D1, HNP)` | POL-01, 02, 11 | `prepare_checkout` | `b2-hnp-v1` | HNP-05 |
| `(D2, HNP)` | POL-03, 04, 05, 06, 07, 08, 09 | `complete_purchase` | `b2-hnp-v1` | HNP-06 |
| `(D3, HNP)` | POL-09, 10 | `commit_dispatch` | `b2-hnp-v1` | HNP-06 |

- **Selección. Decisión de spec:** una compra HNP siempre se evalúa con `b2-hnp-v1`, armado en código
  (`build_hnp_policy`, análogo a `build_d1_policy`), sin variable de entorno. D1-HNP usa una
  compuerta D1 propia, ligada a `b2-hnp-v1`; no lee `AGENT_D1_POLICY_SET_ID`, que sigue siendo el
  policy set de HP.
- **C02, modo contra policy set:** `rules_apply` no basta, porque `(D1, HP)` y `(D1, HNP)` tienen las
  mismas reglas y el perfil del piloto (`D1_PILOT_RULES`) se acepta en cualquier modo. Por eso cada
  boundary (D1, D2 y D3) deniega con `GOV_ARTIFACT_MISMATCH`, sin llamar al motor, si
  `(policy_set_id == "b2-hnp-v1") != (purchase_mode == "HNP")`. HNP-05 además restringe
  `rules_apply` para que `D1_PILOT_RULES` valga solo con `purchase_mode="HP"`.
- **Artefacto:** el mismo `D1PolicyArtifact` de P1/P3, con `policy_set_id="b2-hnp-v1"`, `version=2`,
  `allowed_merchant_refs` (la lista del operador que evalúa POL-02 de HP,
  `AGENT_D1_ALLOWED_MERCHANT_ORIGINS`; sin ella, el comercio configurado), `currency="COP"`,
  `quantity=1` y:
  - `policy_ids = APPLICABLE_RULES[("D1", "HNP")]` y `allowed_actions = ("prepare_checkout",)`;
  - `authorization` en su variante HNP, `HnpAuthorizationRules{policy_ids =
    APPLICABLE_RULES[("D2", "HNP")], allowed_actions = ("complete_purchase",), authentication_bases =
    ("service_and_grant",), profiles = ("profile:ap2-hnp-v1",)}`; el campo pasa a ser
    `AuthorizationRules | HnpAuthorizationRules | None`. El id del perfil es el `ObjectRef.id` de
    gobernanza (K6): `Id` no admite `/`, así que `ap2-hnp/v1` (`ap2_hnp.PROFILE_ID`) se nombra
    `profile:ap2-hnp-v1` (`hnp_policy.HNP_AUTHORIZATION_PROFILE_ID`);
  - `dispatch` nuevo, `DispatchRules{policy_ids = APPLICABLE_RULES[("D3", "HNP")], allowed_actions =
    ("commit_dispatch",)}`, tipado `DispatchRules | None`.

  Los campos nuevos quedan fuera de la forma canónica cuando son `None` (`exclude_if`, como
  `authorization` en P3), así que los bytes de `b2-hp-v2` y de `b2-pilot-v1` no cambian. El
  artefacto valida C02 en sus secciones: `HnpAuthorizationRules` y `dispatch` solo en `b2-hnp-v1`,
  `AuthorizationRules` nunca en él.
- **Archivos y dueños:** `governance/hnp_policy.py` (`build_hnp_policy`) lo crea HNP-05 con la fila
  D1 y lo extiende HNP-06 con `authorization` y `dispatch`. En `governance/models.py`, HNP-05 agrega
  la fila `(D1, HNP)` y HNP-06 las filas `(D2, HNP)` y `(D3, HNP)`, `"D3"` en `Phase`,
  `HnpAuthorizationRules` (la contraparte de `AuthorizationRules` de P3, con `profiles`) y
  `DispatchRules`.
- **Revisión. Decisión de spec, corregida en HNP-06:** HNP-05 crea `b2-hnp-v1` revisión 1 con la
  fila D1 y HNP-06 le agrega D2 y D3 en la revisión 2 (`HNP_POLICY_REVISION`). La bóveda de
  procedencia rechaza bytes nuevos bajo una revisión que ya registró (`d1_provenance_policies`), así
  que completar el artefacto sin subirla dejaría inservible cualquier base con un D1-HNP de la
  revisión 1. La evidencia de la revisión 1 sigue verificándose con sus propios bytes.
- **Entradas y motor:** la rama `hnp` vive en `governance/hnp_models.py`; la unión discriminada por
  `purchase_mode` es `D2PolicyInput` (`parse_d2_input`), y `models.AuthorizationPolicyInput` sigue
  nombrando la rama HP, con sus bytes. Los límites son `HnpD2PolicyBoundary` y `D3PolicyBoundary`
  (`evaluator.py`), con `ReferenceHnpAuthorizationEvaluator` y `ReferenceDispatchEvaluator`; antes
  del motor niegan C02, los prerrequisitos de `hnp_rules` y un perfil que el artefacto no admite
  (`GOV_AUTHORIZATION_PROFILE_UNRESOLVED`). El motor medido agrega `hnp_models.py` y `hnp_rules.py`
  a su manifiesto y pasa a `reference-d1-d2-d3:3`; los archivos del motor anterior (cuatro fuentes)
  siguen verificándose.
- **Reglas:** las de policy-v1 §6 con la rama `hnp` de `AuthorizationPolicyInput` (P3) y
  `DispatchPolicyInput`. Razones por id de política (`POLICY_REASONS`). Sin cortocircuito: N10 lista
  POL-09 y POL-10 cuando ambas fallan.
- **Hechos del executor:** `PrincipalFacts` con `principal_ref="service:hnp-executor"`,
  `authentication_basis="service_and_grant"`, `owns_run` por el dueño del grant, y `permitted_actions`
  del grant (las tres acciones). `GrantFacts.agent_key_ref = agent_key_kid`. `authorization_profile`
  = `{status, profile_ref: {id: "profile:ap2-hnp-v1", version: 1, digest}}`, con el digest de las
  constantes del perfil AP2 HNP (`PROFILE_DIGEST`, HNP-04).
- **POL-03 y POL-04 en HNP:** POL-03 no exige `principal_ref == owner_ref` (el principal es el
  servicio; el dueño lo liga el grant con `owns_run`). POL-04 exige el allow de D1 de este episodio,
  como en HP, y que el grant, la `reservation_intent` y la operación sean del run y del dueño
  esperados (policy-v1, POL-04); por eso un grant de otro dueño también falla POL-07 y una reserva
  de otro run también POL-09.

## 8. Escalamiento, revocación y kill switch

### 8.1 Resultados de la tarea

| Estado | `outcome_code` | Causa | Reserva | Grant | Aviso |
| --- | --- | --- | --- | --- | --- |
| `escalated` | `budget_not_satisfiable` | D1 POL-11 | — | `revoked(escalated)` | Escalamiento |
| `escalated` | `no_candidate_in_scope` | Lista corta vacía | — | `revoked(escalated)` | Escalamiento |
| `escalated` | `offer_changed` | Frescura `changed` al cerrar; se cancela el checkout propio | — | `revoked(escalated)` | Escalamiento |
| `escalated` | `terms_out_of_scope` | D2 niega solo con POL-05 y/o POL-08 | — | `revoked(escalated)` | Escalamiento |
| `escalated` | `authorization_profile_unresolved` | `GOV_AUTHORIZATION_PROFILE_UNRESOLVED`: perfil, `open_proof` o `closed_proof` en `unresolved` | — | `revoked(escalated)` | Escalamiento |
| `escalated` | `verifier_unresolved_constraint`, `verifier_mandates_not_supported` | Recibo de error del PSP con ese código | `released(psp_error_receipt)` | `revoked(escalated)` | Escalamiento |
| `canceled` | `revoked_by_owner` | Revocación del dueño sin fila en el outbox | `released(no_outbox)` | `revoked(owner)` | Revocación |
| `canceled` | `kill_switch` | Kill switch sin fila en el outbox | `released(no_outbox)` | `revoked(operator)` | Kill switch |
| `canceled` | `grant_expired` | Vencido (o sin el margen de UoW-1) sin fila en el outbox | `released(no_outbox)` | `expired` | Vencimiento |
| `failed` | `selection_not_eligible`, `merchant_not_allowed` | D1 POL-01 / POL-02; alerta al operador | — | `revoked(failed)` | Fallo |
| `failed` | `mandate_proof_invalid` | `open_proof` o `closed_proof` en `invalid` (mandatos alterados); alerta al operador | `released(no_outbox)` si había | `revoked(failed)` | Fallo |
| `failed` | `policy_denied` | D2 o D3 niega con POL-03/04/06/07/09/10, sin las causas de arriba | `released(no_outbox)` si había | `revoked(failed)` | Fallo |
| `failed` | `governance_unavailable` | `GOV_ENGINE_UNAVAILABLE`, `GOV_OUTPUT_INVALID`, `GOV_ARTIFACT_MISMATCH`, `GOV_EVIDENCE_UNAVAILABLE`, `GOV_FACTS_UNAVAILABLE`, `GOV_INPUT_INVALID`, `GOV_FRESHNESS_UNRESOLVED` | Ídem | `revoked(failed)` | Fallo |
| `failed` | `state_conflict` | Perdió un CAS sin otra causa (§4.5, «Lease y fencing») | Ídem | `revoked(failed)` | Fallo |
| `failed` | `verifier_invalid_mandate`, `verifier_invalid_credential` | Recibo de error del PSP; alerta al operador | `released(psp_error_receipt)` | `revoked(failed)` | Fallo |
| `failed` | `checkout_failed` | El comercio rechaza crear o actualizar el checkout | — | `revoked(failed)` | Fallo |
| `failed` | `budget_guard` | La guarda común cancela el checkout (total autoritativo sobre el máximo) | — | `revoked(failed)` | Fallo |
| `failed` | `close_expired_before_dispatch` | §4.5 | `released(no_outbox)` | `revoked(failed)` | Fallo |
| `uncertain` | `dispatch_timeout`, `partial_verifier_failure`, `receipt_mismatch` | Después del outbox | `uncertain` | `active` | Incierto |
| `purchased` | — | Orden con ambos recibos | `consumed` | `consumed` | Compra |

- **Clasificación de un deny de D2 o D3**, en este orden; gana la primera que aplique:
  1. El store ya tiene comprometida una revocación del dueño, un kill switch o el vencimiento →
     `canceled(revoked_by_owner | kill_switch | grant_expired)`, aunque niegue POL-07 o POL-10.
  2. `open_proof` o `closed_proof` en `invalid` → `failed(mandate_proof_invalid)` con alerta. Solo
     `unresolved` escala (runtime guard `GOV_AUTHORIZATION_PROFILE_UNRESOLVED`).
  3. Alguna de POL-03/04/06/07/09/10 → `failed(policy_denied)`.
  4. Solo POL-05 y/o POL-08 → `escalated(terms_out_of_scope)`.

  **Decisión de spec:** fail-closed (3) gana sobre escalar (4) antes que devolver la decisión.
- Con fila en el outbox, una revocación, un kill switch, un vencimiento o un CAS perdido no terminan
  la tarea (§4.5, re-claim con outbox). Los únicos terminales con outbox son `purchased`,
  `uncertain` y los del recibo de error del PSP.
- **B0-HNP nunca escala.** Sin PDP ni frescura, las causas que en B2 escalan terminan `failed` con el
  mismo `outcome_code` y sin `EscalationView` («No (falla con código)» del plan).
- Recibo de error del comercio después del token del PSP: `uncertain(partial_verifier_failure)`,
  aunque el código AP2 sea de escalamiento; el fallo parcial entre verificadores es de reconciliación
  manual (fuera de alcance).

### 8.2 Escalamiento

Equivale a `unresolved_constraint` de AP2: la decisión vuelve al humano. No se firma ni se presenta
nada (o, si el PSP rechazó, nada llegó al comercio), se libera la reserva, se cancela el checkout
propio, la tarea queda `escalated(reason)` y el grant `revoked(escalated)`. En el MVP el humano decide
con una compra HP nueva en el chat: el CTA «Comprar desde el chat» prellena `prefill_request`
(«Quiero comprar <título> por hasta <total>»). Convertir la ejecución escalada en una compra HP con un
clic queda fuera.

### 8.3 Textos canónicos

Viven en `delegation/contract.py` (`HNP_MESSAGES`, HNP-01) y llegan a la UI en `message`. Título del
escalamiento: **«Necesito tu decisión»** (clave `escalation.title`; el prellenado de §8.2 es
`escalation.prefill_request`). `<título>`, `<total>` y `<número>` son los campos `{title}`,
`{total}` y `{order_id}` de `str.format`. El `message` de un escalamiento es el texto de su código
seguido del sufijo (`escalation_message`): `suffix_rejected_by_psp` para los dos `verifier_*`, y
`suffix_before_presentation` para los demás.

| Clave | Texto |
| --- | --- |
| `escalation.budget_not_satisfiable` | El precio de catálogo de la opción disponible ya supera el total máximo que autorizaste. |
| `escalation.no_candidate_in_scope` | Ninguna de las opciones que autorizaste está disponible ahora. |
| `escalation.offer_changed` | La oferta del comercio cambió antes de que cerrara la compra. |
| `escalation.terms_out_of_scope` | El total del comercio, con envío, o algún término quedó fuera de lo que autorizaste. |
| `escalation.authorization_profile_unresolved` | No pude comprobar que la compra encaje en tu autorización. |
| `escalation.verifier_unresolved_constraint` | El proveedor de pagos no pudo comprobar una condición de tu autorización. |
| `escalation.verifier_mandates_not_supported` | El proveedor de pagos no acepta compras delegadas en este momento. |
| `escalation.suffix_before_presentation` | No firmé ni presenté ninguna autorización de pago, no se hizo ningún cobro y cerré la delegación. Si todavía quieres comprar, hazlo desde el chat y apruébala tú. |
| `escalation.suffix_rejected_by_psp` | El proveedor de pagos la rechazó, así que no se hizo ningún cobro, y cerré la delegación. Si todavía quieres comprar, hazlo desde el chat y apruébala tú. |
| `revoke.no_effect` | Revocaste la delegación. El agente ya no puede comprar con ella. |
| `revoke.released` | Revocaste la delegación antes de que el agente comprometiera el pago. No se presentó ninguna autorización ni se hizo ningún cobro. |
| `revoke.in_flight` | Revocaste la delegación, pero el agente ya había comprometido el pago. Esa compra sigue en curso y aquí verás cómo termina; no puedo prometer que se cancele. |
| `revoke.not_revocable` | Esta delegación ya terminó, así que no hay nada que revocar. |
| `revoke.already_closed` | Esta delegación ya estaba cerrada, así que el agente no puede comprar con ella. |
| `task.kill_switch` | El operador detuvo las compras delegadas. Cerré tu delegación y no se hizo ningún cobro. Si todavía quieres comprar, hazlo desde el chat. |
| `task.grant_expired` | Tu delegación venció antes de que el agente comprara. No se hizo ningún cobro. |
| `task.revoked_by_owner` | Revocaste la delegación antes de la compra. No se hizo ningún cobro. |
| `task.failed` | Detuve la compra delegada por precaución porque no pude verificar todo lo necesario. No se hizo ningún cobro y cerré la delegación. |
| `task.uncertain` | No sé todavía si la compra delegada se completó: el comercio o el proveedor de pagos no confirmó el resultado. No voy a intentarlo con otra autorización; la estoy conciliando. |
| `task.purchased` | Compré <título> por <total> con tu delegación. Orden <número>. |
| `task.running` | Estoy haciendo la compra que delegaste. |
| `task.scheduled` | Tu delegación está activa y lista para que el agente compre. |

Textos fijos de la tarjeta de consentimiento (HNP-07): título «Autorizar una compra sin ti»,
«El total máximo incluye envío», «Un solo uso», «Vence el <fecha y hora>», «Puedes revocarla antes de
que el agente comprometa el pago», botones «Autorizar» y «Cancelar».

Rótulos fijos de las demás tarjetas (HNP-07, `ui/src/components/delegation/delegation-copy.ts`):
títulos de estado «Delegación activa», «Compra en curso», «Compra completada», «Delegación
revocada», «Compra cancelada» (kill switch), «Delegación vencida», «Compra no completada» y «Compra
por confirmar» (un escalamiento usa `escalation.title`); filas «Total máximo», «Vence», «Entrega»,
«Pago», «Última actividad» y «Tu indicación»; botones «Revocar» (§2.1) y «Comprar desde el chat»
(§8.2). Manda el resultado de la tarea y, mientras no termina, el estado del grant.

Textos fijos del chat (HNP-10, mismo archivo): la tarjeta de consentimiento suma «Puedes autorizarla
hasta el <fecha y hora>», el menor entre `expires_at` de la propuesta y del alcance (el mismo límite
que aplica `POST /delegations`). Diálogo «Delegaciones» con «Nueva delegación», «Mis delegaciones»,
«Todavía no tienes delegaciones.» y «Revocar todo». Formulario «Delegar una compra»: «Opciones que
puede comprar» (las que el chat ya mostró), «Total máximo con envío (pesos)», «Vigencia» (1, 6 o 24
horas), «Entrega» con «Dirección», «Ciudad» y «Departamento», «Por ahora el comercio solo entrega en
Bogotá D.C.», «Tu indicación (opcional)» y el botón «Preparar la autorización». Los errores
`{code, message}` de §5.1 muestran el `message` del backend; solo
`delegation_destination_unsupported` lleva un texto de la UI.

### 8.4 Revocación y kill switch

- **Dueño:** el UoW de revocación es idempotente. Sin reserva: `no_effect`. Reserva `reserved` sin
  outbox: `released`. Outbox comprometido: `in_flight`, sin promesa de cancelar; la compra sigue y
  se reconcilia. Una tarea `running` sin outbox detecta el epoch nuevo en su siguiente UoW y termina
  `canceled(revoked_by_owner)`.
- **Operador:** el kill switch solo agrega una fila a `hnp_controls`. Todo claim, UoW-1 y UoW-2
  posterior sin fila en el outbox lo ve, de cualquier dueño. **Decisión de spec:** al toparse con
  él, el grant pasa a `revoked(operator)` y la tarea a `canceled(kill_switch)`; apagarlo después no
  reanuda delegaciones viejas. Una compra ya comprometida en el outbox sigue (como `in_flight`).
- **Expiración:** `exp` del mandato abierto = `expires_at` del grant (el comercio y el PSP exigen
  `now < exp` sin holgura), más el chequeo local en claim, POL-07, UoW-1 (con el margen de 300 s) y
  UoW-2. Cierres con `exp = iat + 300`.

## 9. Evidencia y verificador

### 9.1 Eventos de `delegation_events`

| `event_type` | Actor | Cuerpo (sin datos personales ni tokens) |
| --- | --- | --- |
| `consented` | `user` | `proposal_id`, `scope_digest`, `presented_at`, `consented_at`, `grant_version`, `authority_epoch`, `task_id` |
| `mandates_issued` | `service` | Por mandato abierto: `token_digest`, `link_hash`, `issuer_kid`; `cnf_kid`, `exp`, `payment_reference` |
| `task_claimed` | `service` | `run_id`, `claim_count`, `lease_expires_at`, `trigger: "service_token"` |
| `reservation_reserved` | `service` | `reservation_id`, `operation_id`, `amount_minor`, `expected_epoch`, `state_version`, `d2_evidence_id` |
| `mandates_closed` | `service` | Por cerrado: `token_digest`, `link_hash` (`sd_hash` del último salto), `checkout_hash`, `agent_key_kid` |
| `dispatch_committed` | `service` | `outbox_id`, `d3_evidence_id`, `authority_epoch`, `control_epoch` |
| `receipt_recorded` | `service` | `kind: payment \| checkout`, `receipt_digest`, `issuer`, `status`, `reference`, `error`, `payment_id` u `order_id` |
| `reservation_released` | `service`, `user` u `operator` | `reservation_id`, `release_basis` |
| `reservation_uncertain` | `service` | `reservation_id`, `uncertain_reason` |
| `consumed` | `service` | `reservation_id`, `order_id` |
| `revoked` | `user`, `operator` o `service` | `revoked_reason`, `epoch_before`, `epoch_after`, `effect` |
| `expired` | `service` | `expires_at`, `observed_at` |
| `kill_switch_blocked` | `service` | `control_epoch`, `stage: claim \| uow1 \| uow2` |
| `task_finished` | `service` | `state`, `outcome_code`, `run_id` |

Todos llevan `actor_kind`, `actor_ref`, `on_behalf_of`, `task_id` y `run_id` en la fila. Las
decisiones D1/D2/D3 viven en `governance_evidence` y la frescura `close` en `hp_evidence`, con la
atribución en su cabecera (`principal_ref`, `owner_ref`).

### 9.2 Eslabones del verificador (HNP-V1, sobre `verify-evidence` de P2)

Cada eslabón es `ok`, `missing`, `mismatch` o `not_applicable`. **Decisión de spec:** los eslabones
comunes conservan el nombre de P2 y se evalúan según `purchases.purchase_mode`; los propios de cada
modo son `not_applicable` en el otro (`approval` y `mandates` son solo HP).

| Orden | Eslabón | Modo | Qué recalcula |
| --- | --- | --- | --- |
| 1 | `intent` | Ambos | Digest de la intención (en HNP, la derivada del grant) |
| 2 | `delegation_consent` | HNP | Cadena de `delegation_events` hasta `consented`; `scope_digest` recalculado desde `scope_json` de la propuesta; `presented_at ≤ consented_at < expires_at` de la propuesta |
| 3 | `open_mandates` | HNP | `token_digest` y `link_hash` desde el token; firma con `public_jwk_json`; `cnf` = `agent_key_kid`; restricciones = alcance; `payment.reference` = `link_hash` del checkout abierto |
| 4 | `task_activation` | HNP | `task_claimed` con actor `service` y el mismo `run_id` |
| 5 | `d1_decision` | Ambos | Como P2, con `purchase_mode` igual al de la compra |
| 6 | `checkout_created` | Ambos | Como P2 |
| 7 | `freshness` | Ambos | En HNP, `close` del mismo checkout en `allow` |
| 8 | `d2_decision` | Ambos | En HNP, registro D2 `b2-hnp-v1` con `transition_ref` = `reservation_id` |
| 9 | `reservation` | HNP | Fila de la reserva y `reservation_reserved` coinciden (operación, monto, epoch) |
| 10 | `closed_mandates` | HNP | `sd_hash` del cerrado = hash del abierto presentado; firma con la Agent Key; `checkout_hash` = hash del `checkout_jwt`; `transaction_id` = `checkout_hash` |
| 11 | `d3_decision` | HNP | Registro D3 con `transition_ref` = `outbox_id` del outbox |
| 12 | `receipts` | Ambos | En HNP, los JWT de `completion_results` (§4.6): `receipt_digest` de cada `receipt_recorded` = su digest, `reference` = `link_hash` de cada cerrado y `order_id` = orden. Un rechazo del PSP usa `error_receipt_jwt` del outbox |
| 13 | `order_webhook` | Ambos | Como P2 |
| 14 | `consumption` | HNP | `consumed` y estados finales de reserva y grant |
| 15 | `revocation` | HNP, terminal | Evento `revoked` con causa `owner` u `operator`, epoch +1, y reserva `released(no_outbox)` o sin reserva |
| 16 | `kill_switch` | HNP, terminal | `kill_switch_blocked` con un `control_epoch` que existe en `hnp_controls` con `kill_switch=1` |
| 17 | `escalation` | HNP, terminal | `task_finished` en `escalated` con su `outcome_code`, grant `revoked(escalated)` y, si había, reserva liberada |

**Terminales de un bloqueo:** si la tarea terminó sin compra, la cadena se corta en uno de los
eslabones 15 a 17 (o en el registro del PDP que negó) y los eslabones posteriores al corte son
`not_applicable`; los terminales que no ocurrieron también. Una compra con orden tiene los tres en
`not_applicable`. La firma de los recibos del comercio y del PSP no se re-verifica (sus claves no se
guardan), igual que en P2.

**B0-HNP.** Como B0' en P2: sin PDP ni frescura, `intent`, `d1_decision`, `checkout_created`,
`freshness`, `d2_decision` y `d3_decision` salen `missing`, y esa es la diferencia que mide H2-HNP.
Los eslabones del ledger (2, 3, 4, 9, 10, 12, 13, 14 y los terminales) se verifican igual que en
B2-HNP.

- **H2-HNP:** compras HNP con cadena completa verificada / compras HNP con orden.
- **H2-HNP-bloqueo:** ejecuciones bloqueadas con cadena completa hasta la decisión que bloqueó /
  ejecuciones bloqueadas.

## 10. Traza (HNP-11)

UI y backend en el mismo commit: Pydantic (`trace/models.py`), `parsePurchaseTrace`, fixtures e
historias.

| `kind` | Actor | Campos nuevos | Título |
| --- | --- | --- | --- |
| `delegation_consented` | `user` | `grant_id`, `total_max: Money`, `expires_at` | «Delegación autorizada» |
| `task_activated` | `agent` | `trigger: "service"` | «Compra delegada en marcha» |
| `freshness_check` | `agent` | `stage` agrega `"close"` | «Oferta vigente al cerrar» |
| `authorization_decision` | `policy` | (el de P3) `decision: PolicyDecision` | En HNP: «Autorización delegada verificada» |
| `reservation` | `agent` | `outcome: reserved \| released \| consumed \| uncertain`, `amount: Money` | «Uso único reservado» |
| `mandates_closed` | `agent` | — | «Autorización cerrada por el agente» |
| `dispatch_decision` | `policy` | `decision: PolicyDecision` | «Despacho autorizado» |
| `revoked` | `user` u `operator` | `by: owner \| operator`, `effect` | «Delegación revocada» |
| `escalated` | `agent` | `reason` (código de §8.1) | «Necesito tu decisión» |

- **Decisión de spec:** D2-HNP reutiliza el paso `authorization_decision` que introduce P3 (si P3 se
  fusiona con otro nombre, HNP-11 usa el de `main`), y D3 tiene su propio `dispatch_decision`.
- `TraceActor` agrega `"operator"`. `PurchaseTrace` agrega `purchase_mode`, opcional y omitido en
  HP (§5.4). La traza de una compra HNP lee la compra de §4.6 (`purchases`, `completion_results`,
  `orders`), igual que HP.
- En HNP no aparecen `approval` ni `authorization_signed`; `payment_authorized`, `order_confirmed`,
  `order_event` y `failed` se reutilizan. `PolicyRef` muestra `b2-hnp-v1`, revisión 2.
- **Lo que fijó HNP-11:** tampoco hay pasos `request` ni `selection` (el campo `request` es el texto
  del ejecutor, «Compra delegada»). `canceled.by` agrega `"expiry"` (`grant_expired`). Si la tarea
  falla con otro código que la compra (#93), su fallo es un segundo `failed` con id `failed:task`.
  `escalated` va con `state: "blocked"`. `payment_authorized` HNP queda para 09b, que escribe la
  hora del envío al PSP. Fuentes y estados: `docs/design/trace-panel.md`, «Compra delegada».

## 11. Condiciones

| Mecanismo | B0-HNP (`off`) | B2-HNP (`enforce`) | P (ablación, solo evaluación) |
| --- | --- | --- | --- |
| Consentimiento, mandatos abiertos, cierre con la Agent Key, verificación en comercio y PSP | Sí | Sí | Sí |
| Claim de tarea; grant vigente y no revocado en el claim; kill switch | Sí (común, policy-v1 §9) | Sí | No |
| Reserva CAS y re-chequeo del epoch en el compromiso | Sí (común) | Sí | No |
| Lista corta restringida al grant; guarda común de presupuesto; idempotencia | Sí | Sí | Lista sin restringir; la guarda sí |
| D1, D2-HNP y D3-HNP, frescura, escalamiento, cadena de evidencia | No (falla con código) | Sí | No |

- Se pre-registra que B0-HNP y B2-HNP empatan en seguridad por construcción en casi todos los
  escenarios (D-2). B2 puede mostrar contención temprana, escalamiento, evidencia y latencia.
- El brazo P vive solo en `commerce_evaluation` (HNP-E3): compone cierre y gateways sin el ledger. El
  producto no gana ningún interruptor inseguro.
- **B0-HNP y `AGENTS.md`:** la regla de «Trust boundaries» nombra D2-HNP/D3-HNP como parte de la
  compuerta HNP y su última frase declara la única excepción: la línea base de evaluación B0-HNP
  (`AGENT_GOVERNANCE_MODE=off`, solo sandbox), que omite D2-HNP/D3-HNP pero conserva el
  consentimiento previo, la reserva de un solo uso, la revocación y el kill switch. Se le informó
  esta lectura a Yair; si la objeta, se ajusta.

## 12. Plan de PRs y criterios de aceptación

Reglas comunes: un tema por PR, spec y contrato antes que código, TDD, CI en verde, Conventional
Commits en inglés y descripción en español, menos de unas 500 líneas de producto sin tests. HNP entra
detrás de `AGENT_HNP_ENABLED=false`; la demo corre desde la etiqueta `demo-2026-10-23`.

| PR | Repo | Depende de | Nombres de esta spec que introduce | Aceptación |
| --- | --- | --- | --- | --- |
| HNP-00 · `docs(design)` | agente | — | Todos (esta spec, perfil AP2 HNP, mapeo HNP01–10, enmienda de `AGENTS.md`) | Yair aprueba la spec o acepta sus valores por defecto |
| HNP-01 · `feat(contract): delegation API and task contracts with fixtures` | agente | 00 | §5.3, `HNP_MESSAGES` | Ambos lados parsean las mismas fixtures y rechazan campos desconocidos (paso 2 de `docs/workflow.md`) |
| HNP-02 · `feat(delegation): grant ledger…` | agente | 00 | §4.3 salvo `purchases`/`governance_evidence`, §4.4, §4.5 (lease y fencing, re-claim con outbox, `TransactionHook`), §9.1 | SQLite real con hilos: de dos reservas gana una; reintento devuelve la misma; revocar antes del compromiso no deja outbox y después deja «en curso»; no se libera con outbox; timeout → `uncertain` sin liberar; kill switch bloquea claim y compromiso; re-claim con outbox no cancela ni libera; lease ajeno sale sin escribir; un hook que lanza revierte toda la UoW; sobrevive al reinicio; revocación idempotente; dueño ajeno = no encontrado; la cadena de eventos verifica. HNP03/04/05/06/09/10 a nivel del store |
| HNP-03 · `feat(governance): HNP policy facts and reference rules POL-07..POL-10` | agente | 00, P3-HP (`PrivateBinding`, `PrincipalFacts`, `MaterialTerms`, `FreshnessFacts`, `ResolvedD1`, `AuthorizationProfile`, `PolicyHeader`) | `governance/hnp_models.py` (con `DelegationScope`, `ConsentScope`, su forma canónica y `scope_digest`, §4.2), `hnp_rules.py`, `hnp_ports.py` (`HnpPolicyGate.decide_d2/decide_d3`) | P/N/F de POL-07..10; N10 lista POL-09 y POL-10; `now == expires_at` deniega; uso incoherente = `GOV_FACTS_UNAVAILABLE`; `scope_digest` de oro; modelos estrictos |
| HNP-04 · `feat(ap2): issue open mandates and close them with the agent key` | agente | 00 | `ap2_hnp.py` (`OpenMandateIssuer`, `AgentKeyCloser`, `project_acp_checkout`, `verify_open_proof`, `verify_closed_proof`), `AGENT_HNP_AGENT_KEY_PEM`, perfil `ap2-hnp/v1` | Fixtures de oro con reloj inyectado; la cadena válida verifica con `MandateClient.verify` y las cadenas del SDK; rechaza total fuera de rango, payee, variante, comercio, cierre con otra clave, `sd_hash` ajeno, `checkout_hash` distinto y restricción desconocida; ninguna herramienta del modelo llega al firmante |
| HNP-M1 · `feat(ap2): verify Human Not Present checkout mandate chains` | comercio | 00 | Verificación de la cadena de checkout y recibos de error (perfil AP2) | El camino HP no cambia; cadena válida → orden; cada alteración de HNP-04 → código AP2 correcto; CI del comercio en verde |
| HNP-P1 · `feat(ap2): verify Human Not Present payment mandate chains` | PSP | 00 | Verificación de la cadena de pago, `ap2_checkout_mandate`, recibos de error | El camino HP no cambia; cadena válida → token; alteraciones → 4xx con recibo de error; CI del PSP en verde |
| HNP-05 · `feat(governance): D1 for HNP episodes` | agente | P1-HP | `purchases.purchase_mode/grant_id`, filtro de `pending_purchase`, `transition_hnp_purchase` (§4.6), fila `(D1, HNP)`, `governance/hnp_policy.py` con `b2-hnp-v1` (D1), compuerta D1-HNP propia, C02 en el boundary D1 y `rules_apply` sin el perfil del piloto en HNP | D1 en HNP deja evidencia con `HNP`; C02 en los dos sentidos sin llamar al motor; `start` en HNP se detiene en `checkout_prepared`; bytes de la evidencia HP, de `b2-hp-v2`, de `b2-pilot-v1` y hashes históricos sin cambios |
| HNP-06 · `feat(governance): D2-HNP and D3-HNP decisions with evidence` | agente | P3-HP, 02, 03, 04, 05 | Rama `hnp`, `phase=D3`, filas `(D2, HNP)` y `(D3, HNP)`, `HnpAuthorizationRules`, `DispatchRules`, `authorization`/`dispatch` en `hnp_policy.py`, C02 en D2/D3, hook de evidencia (§4.5) con `governance_decision_scopes` | P/N/F de POL-03..10 por el boundary; motor que lanza → `GOV_ENGINE_UNAVAILABLE`; C01–C05, C09, C11; la evidencia se escribe en la transacción del ledger con `expected_local_state_version` del grant; prueba `invalid` → `mandate_proof_invalid`, `unresolved` → escala; replay reproduce |
| HNP-07 · `feat(ui): delegation consent, status and escalation cards in Storybook` | agente | 01 | Textos fijos de §8.3 | `test-storybook` verde con `play` y axe; sistema monocromo; textos en español |
| HNP-08 · `feat(api): delegation consent, revoke and kill switch routes` | agente | 01, 02, 03, 04 | §5.1, §5.2 salvo `run`, §5.4 (`/health`), §6 salvo selector | Dueño ajeno 404; digest distinto o propuesta vencida 409; revocación idempotente y de un grant ya cerrado (200 `no_effect`); kill switch sin token 401; flag apagado 404; rutas HP sin cambios; los mandatos del consentimiento verifican con HNP-04 |
| HNP-09a · `feat(hnp): autonomous executor up to the dispatch commitment` | agente | 02, 03, 04, 05 (P5b para `llm`) | Pasos 1–10, `NullHnpPolicyGate` en `off`, `AGENT_HNP_SELECTOR`, frescura `close` en `offer_freshness.py`, margen de UoW-1, clasificación de §8.1, compra `completing` en UoW-2, tokens del modelo en `stage_timings` | Gateways falsos y SQLite real: contadores de create, firma y compromiso en cada deny; conflicto CAS clasificado como en §4.5; lease ajeno; revocación antes de UoW-1 y entre UoW-1 y UoW-2; kill switch; vencimiento dentro del margen → `canceled(grant_expired)` sin cierre; frescura `changed` → `escalated` sin firma |
| HNP-E1 · `docs(evaluation): HNP pilot v1 pre-registration` | agente | 00 | `docs/design/hnp-pilot.md`, `evaluation/data/hnp-pilot-v1.json`, `hnp-scenarios.json` | Yair aprueba los oráculos antes de correr |
| HNP-09b · `feat(hnp): dispatch, receipts, escalation and the service trigger` | agente | 09a, 06, 08, M1, P1 | Paso 11–13, `POST /internal/hnp/tasks/{id}/run`, `ap2_checkout_mandate`, cadena en `/ap2/complete`, UoW-3 y `record_hnp_completion` (§4.6), `error_receipt_jwt` | Crash después de reserva, cierre, outbox o token no genera cierre nuevo; re-claim con outbox reanuda con las mismas claves aunque haya revocación, kill switch o vencimiento; timeout → `uncertain` con la reserva; 4xx sin recibo válido (incluido 409 `idempotency_conflict`) → `uncertain`; `unresolved_constraint` escala; `invalid_mandate` falla con alerta; `reference` distinta → `uncertain`; la compra queda `completed` con sus recibos en `completion_results` y `orders` |
| HNP-10 · `feat(ui): wire delegations into the chat` | agente | 07, 08 | `ui/src/features/delegations.tsx` | E2E de Playwright con API simulada: formulario → propuesta → consentimiento; «Mis delegaciones» con Revocar |
| HNP-11 · `feat(trace): Human Not Present steps in the purchase trace` | agente | 05, 06, 09a (P3-HP) | §10, `purchase_mode` opcional en traza e historial | Mismas fixtures en Pydantic y `parsePurchaseTrace`; historias por paso; las fixtures y pruebas HP pasan sin editarlas |
| HNP-E2 · `feat(evaluation): HNP pilot runner on the governance pilot harness` | agente | E1, 08, 09b (rebase sobre P4-HP) | `hnp-pilot`, `HNP_PILOT_MAX_COST_USD` | `--dry-run`; sin tope de costo se niega a correr y con más de USD 2 también; la salida del dry-run de los pilotos v1 y v2 no cambia. La corrida en vivo la lanza el líder a mano, con permiso de Yair |
| HNP-V1 · `feat(evaluation): verify HNP evidence chains` | agente | P2-HP (#74 fusionado), 02, 05, 06 | §9.2, sobre la compra de §4.6 | Fixtures sintéticas de `agent.db`: cadena completa, bloqueada y manipulada dan el estado esperado por eslabón |
| HNP-I1 · `test(integration): live HNP purchase, double use, revocation race and escalation` | agente | 09b, M1, P1 | `test_live_hnp.py`, carril HNP de `live_purchase.py` | Compra con dos recibos y cadena verificada; precio sobre el límite → `escalated`, 0 firmas; dos disparos → 1 orden; revocación con `FaultProxy` en `/ap2/checkout-jwt` → 0 órdenes y 0 tokens; cadenas alteradas directo al comercio y al PSP → rechazo; restricción desconocida, crash y timeout. Se corre a mano con `RUN_LIVE_STACK=1` |
| HNP-E3 (opcional) · `feat(evaluation): protocol-only HNP ablation arm` | agente | 09b | `commerce_evaluation/hnp/protocol_only.py` | Compone cierre y gateways sin ledger; el producto no cambia |

Orden de fusión: ola 1 (02, 03, 04, 01; M1 y P1 en sus repos; 03 solo si P3 ya está en `main`), ola
2 (05, 06, 08, 09a, 07, E1), ola 3 (09b, 11, 10, V1, E2, I1, E3). Dentro de una ola el orden va por
archivo caliente, no global. La coordinación del día está en el contrato de coordinación del líder.

## 13. Riesgos

1. **P3 no fusionado:** HNP-03, HNP-06, HNP-11 y HNP-V1 se bloquean. 09a programa contra
   `HnpPolicyGate`.
2. **P4 y E2 tocan el runner:** E2 se rebasa sobre P4 y solo importa funciones.
3. **Puente ACP→SDK (H01):** si la proyección al Checkout del SDK no encaja, el comercio evalúa
   `allowed_merchants` y `line_items` con su propio código sobre ACP y se declara.
4. **Tres repos con CI separado** y el nightly sin `MERCHANT_REPO_TOKEN`: I1 se corre a mano.
5. **Contención en SQLite** (D1, `hp_evidence` y el ledger en la misma base): transacciones cortas con
   `BEGIN IMMEDIATE`.
6. **Compra HNP como pendiente HP:** el filtro de HNP-05 es obligatorio.
7. **`parsePurchaseTrace` estricto:** HNP-11 lleva UI y backend juntos; `purchase_mode` es opcional
   y se omite en HP, así que las fixtures HP no cambian.
8. **Expansión del alcance** (re-cotizar, conversión con un clic): fuera.
9. **Código HNP visible antes de la demo:** el flag lo evita.
10. **Capacidad de revisión:** si un PR del nivel 1 necesita dos rondas, el nivel 3 se corre.
11. **B0-HNP frente a la regla de `AGENTS.md`** (§11): la excepción está escrita; si Yair la objeta, se
    ajusta.

## 14. Resumen de decisiones de spec

1. `shadow` con HNP se rechaza al arrancar.
2. Los mandatos abiertos se presentan completos, sin divulgación selectiva.
3. La intención HNP se deriva del alcance; la lista corta es `rank_candidates ∩ grant`.
4. `destination_binding` usa la clave HMAC de D2 (vault D1); HNP exige esas variables siempre.
5. `expires_at` del grant se fija al proponer.
6. `preference_text` del comprador, en el digest y solo para el selector.
7. Tabla `delegation_proposals` (el plan no listaba dónde vive la propuesta).
8. `delegation_mandates.public_jwk_json` para re-verificar firmas sin conexión.
9. `revoked_reason` agrega `failed`.
10. UoW-C guarda los cerrados antes de D3; sin cierre nuevo al reanudar.
11. El recibo de error del PSP libera la reserva; el del comercio después del token la deja `uncertain`.
12. `hnp_controls` es un historial de solo inserción.
13. Rutas internas rechazan cualquier `Origin`.
14. El kill switch revoca (`operator`) los grants que alcanza; apagarlo no los reanuda.
15. Constantes del perfil (300 s, 120 s, 60 s, 5 variantes, 280 caracteres) en código, no en settings.
16. `b2-hnp-v1` armado en código, sin variable; HNP-05 crea la revisión 1 y HNP-06 la completa en
    la revisión 2 (la bóveda no admite bytes nuevos en una revisión registrada).
17. Fail-closed gana a escalar cuando D2/D3 niega con políticas de ambos grupos.
18. Eslabones comunes con nombres de P2; los de cada modo son `not_applicable` en el otro.
19. D2-HNP reutiliza `authorization_decision` de P3; D3 usa `dispatch_decision`; `TraceActor`
    agrega `operator`.
20. Las del perfil AP2 HNP (`docs/ap2-profile.md`): hash de `payment.reference`, `reference` de los
    recibos, tolerancia de reloj solo para `iat`, proveedor de clave raíz, `payment_id` del recibo de
    error del PSP y mapeo de errores.
21. UoW-1 exige `now + 300 s ≤ expires_at` (margen de vigencia).
22. Una prueba de mandatos `invalid` es `failed(mandate_proof_invalid)` con alerta; solo `unresolved`
    escala.
23. `owner_ref` es el seudónimo D1 del dueño en el alcance, los eventos y la evidencia.
24. `purchase_mode` en traza e historial es opcional y se omite en HP.
25. `uncertain` no se concilia con código en v1.
26. Revocar un grant ya cerrado por otra causa responde 200 `no_effect`.
27. El destino se normaliza al proponer como lo hace el comercio; uno que el comercio rechazaría es
    422 `delegation_destination_unsupported`.
