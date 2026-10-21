# D2-HP: autorización verificada al aprobar

Estado: implementado. Backend en el PR A; traza y UI en el PR B. Ola «HP al 100 %» (P3), 21-oct-2026. Contrato formal:
[`policy-v1.md`](../b2-governance/policy-v1.md), secciones 2, 4 (`AuthorizationPolicyInput`), 5 y 6
(POL-03..06). Coordinación: decisión compartida 3.

## Problema

Hoy D1 (POL-01, POL-02) se evalúa antes de crear el checkout. Al aprobar solo corren guardas sueltas
de la aplicación: la propiedad de la compra en `api.py:owned_purchase`, el presupuesto en
`PurchaseService._enforce_authoritative_budget` (después de crear el checkout) y la frescura de la
oferta. Ninguna produce una decisión de política ni evidencia propia, así que la tesis no puede
mostrar que la compra que se firmó es la misma que la política revisó, con los mismos términos y la
misma versión de la política.

## Alcance

- **D2-HP:** al aprobar, el motor de políticas evalúa POL-03..06 sobre hechos que ensambla el
  backend desde su store. Nunca desde el modelo ni desde el cuerpo de la petición.
- Evidencia `decision_record` con punto de decisión `D2` en `governance_evidence`, enlazada a la
  decisión D1 del mismo run, a la frescura `approve` y a la aprobación.
- Un deny no firma, no registra la aprobación y no completa.
- Traza: paso nuevo «Autorización verificada» entre «Tu aprobación» y «Firmar la autorización».
- UI del deny en el chat.

**Fuera:** la rama HNP de `AuthorizationPolicyInput` (POL-07..09) y D3; el *challenge* de
presentación de `governance/human_consent.py`; D2 en `shadow`; migrar bases de datos creadas antes
de este cambio (ver Riesgos).

### Código parcial existente

`governance/human_consent.py`, `persistence/human_consent_sqlite.py`, `persistence/d2_d17_source.py`
y `d2_consent_api.py` son el experimento EC-GOV-07C: una app separada, nunca montada, que registra el
consentimiento a términos de un checkout `not_ready_for_payment` sobre el flujo D1 «guarded»
(`SqliteGuardedD1`, `PreselectionD1Request`) y siempre responde `continuation_allowed: false`. No
evalúa POL-03..06, no produce `PolicyDecision` y no conoce el flujo HP gobernado
(`SqliteD1EnforceGate`, `ApprovalService`). No se reutiliza: D2-HP toma sus hechos del store del flujo
gobernado. Se reutiliza el resto de la infraestructura D1: modelos estrictos, canonicalización,
`measure_reference_engine`, el vault de procedencia, el ledger `governance_evidence` con replay y el
patrón de *boundary* fail-closed.

## Orden en el flujo de aprobación

`POST /purchases/{run_id}/approval`:

1. **API:** JWT verificado (`current_buyer`) y propiedad (`owned_purchase`). Un fallo responde 401 o
   404 antes del PDP, sin evidencia y sin revelar si la compra existe.
2. **Continuación gobernada** (`_block_d1_continuation`), etapa `awaiting_confirmation` y, con AP2,
   verificación del `checkout_jwt`.
3. **`ApprovalService.approve`:** checkout aprobable (`ready_for_payment`, sin vencer) y huella igual
   a la que vio el comprador.
4. **Frescura `approve`** (`OfferFreshnessGuard`): relee la oferta y deja `freshness_check` en
   `hp_evidence`. Con cambios, 409 y no sigue.
5. **D2:** ensambla `AuthorizationPolicyInput`, evalúa POL-03..06 y prepara la evidencia (replay del
   motor de referencia, fuera de la transacción). Con deny registra la evidencia `blocked` y
   responde 409: `d2_denied` si la política negó y `d2_unavailable` si fue una guarda operativa
   (motor caído, salida inválida, hechos que faltan). No hay firma.
6. **Firma** de los mandatos AP2, en memoria.
7. **Una sola transacción SQLite:** CAS sobre `purchases.state_version`, fila de `approvals` (con el
   `evidence_id` de D2), etapa `approved` y evidencia D2 `transition_committed`. Si algo falla, se
   revierte todo, los mandatos se descartan y se responde 409 `d2_allow_unapplied`.
8. **Completar** (`/complete`) sigue igual: exige la aprobación, que solo existe con un allow D2
   comprometido.

## Hechos y fuente autoritativa

La entrada sigue `AuthorizationPolicyInput` (rama HP) del contrato formal. En código es una unión
discriminada por `purchase_mode` con una sola rama, `HpAuthorizationPolicyInput`
(`purchase_mode: "HP"`); la rama HNP se agrega como segundo miembro sin cambiar los bytes de la
evidencia HP. Todos los hechos salen del backend; el cuerpo de la petición solo aporta la huella
que se compara.

| Hecho | Fuente autoritativa |
| --- | --- |
| Cabecera: `run_id`, `trace_id`, `owner_ref`, `conversation_id`, `clock_profile_ref` | Contexto D1 del vault de procedencia, re-registrado con la configuración actual |
| `context_version`, `expected_policy` | Ese mismo registro: con la configuración sin cambios, el mismo contexto y la misma política |
| `expected_local_state_version` | `purchases.state_version` al evaluar |
| `authority_now`, `commercial_now` | `ClockReader` del gate (el mismo perfil de reloj que D1) |
| `principal` | `actor_id` del JWT verificado; `owns_run` y `principal_ref` se recalculan contra `purchases.owner_id`. Acciones del perfil de comprador humano (`complete_purchase`) |
| `intent` | `IntentFacts` de la entrada canónica de la decisión D1 comprometida (salió del vault) |
| `d1` | La decisión D1 `transition_committed` del run en `governance_evidence`, con su `evidence_id` y el digest de su registro |
| `terms` (`MaterialTerms`) | GET autoritativo del checkout en el comercio. El destino va como HMAC (`PrivateBinding`), nunca en claro |
| `freshness` | Último `freshness_check` de `hp_evidence`: etapa `approve`, mismo checkout |
| `constraint_matches` | Cada restricción dura de la intención (vault), recalculada con `rejection_reason` sobre la variante del feed congelado de D1 |
| `hp` | Últimos términos que presentó el servidor (`approval_terms`: el chat y `GET /me/pending-purchase` los guardan, y ahora también `GET /purchases/{run_id}/approval` cuando su huella es otra; releer los mismos términos conserva la hora en que se mostraron), huella del clic frente a la presentada y a la del checkout actual |
| `authorization_profile` | Perfil de aprobación configurado: AP2 (`hp-ap2/v1`) o HMAC (`hp-hmac/v1`) |
| `authorization_id` / `operation_id` | Derivados del run y del intento / la operación de la decisión D1 |

### Reglas

| Regla | Permite si | Deniega con |
| --- | --- | --- |
| POL-03 | Autenticado, dueño del run (`principal_ref = owner_ref`), `complete_purchase` en las acciones del principal y del artefacto, base de autenticación admitida por el artefacto | `PRINCIPAL_ACTION_NOT_ALLOWED` |
| POL-04 | D1 es allow del mismo run, dueño, modos e intención; misma variante en D1, selección y checkout; mismo comercio; intento del mismo dueño, clic recibido, `presented_at ≤ clicked_at ≤ authority_now`, `commercial_now < attempt_valid_until` y `material_match` | `EPISODE_BINDING_MISMATCH` |
| POL-05 | Moneda, cantidad 1 y variante coinciden; precio unitario y **total autoritativo del checkout** dentro de los límites no nulos de la intención; cobertura exacta de las restricciones duras y todas cumplen | `CHECKOUT_CONSTRAINT_MISMATCH` |
| POL-06 | El `policy_binding` y el `context_version` de D1 son los esperados ahora | `POLICY_CONTEXT_MISMATCH` |

Las cuatro se evalúan siempre, sin cortocircuito. La tabla `APPLICABLE_RULES` de P1 gana la fila
`("D2", "HP")` → POL-03..06 sin tocar la de D1; `rules_apply` solo admite el perfil piloto
(POL-01, POL-02) en D1, y el límite D2 responde `GOV_ARTIFACT_MISMATCH` si el artefacto no trae la
fila de la fase y el modo de la entrada. El artefacto `b2-hp-v2` revisión 1 (decisión compartida 1;
aún sin evidencia publicada) lleva, además de su `policy_ids` D1, una sección `authorization`:

```json
{"policy_ids": ["POL-03", "POL-04", "POL-05", "POL-06"],
 "allowed_actions": ["complete_purchase"], "authentication_bases": ["user_jwt"]}
```

`build_d1_policy` la agrega solo a `b2-hp-v2` (`D2_RULES_BY_POLICY_SET`). D2 corre solo si el
artefacto configurado la trae; `b2-pilot-v1` no la trae, no serializa la clave y conserva sus bytes.

## Fail-closed

`D2PolicyBoundary` envuelve al motor como `D1PolicyBoundary`:

- Prerrequisitos de aplicación, antes de invocar el motor: artefacto verificado con sección
  `authorization` y binding igual al esperado (`GOV_ARTIFACT_MISMATCH`), frescura `current` y
  `commercial_now < terms.commercial_expires_at` (`GOV_FRESHNESS_UNRESOLVED`), perfil `validated`
  (`GOV_AUTHORIZATION_PROFILE_UNRESOLVED`).
- Una excepción del motor da `GOV_ENGINE_UNAVAILABLE`; una salida inválida, `GOV_OUTPUT_INVALID`.
- Esas denegaciones son `origin: runtime_guard`, sin `rule_results`.
- Si los hechos no se pueden ensamblar (falta la decisión D1, términos no representables), no hay
  entrada válida: se registra `input_rejected` (`GOV_FACTS_UNAVAILABLE`) si el episodio existe.
- En todos los casos la respuesta es 409 y no se firma. Una base de evidencia caída también detiene
  la aprobación.

## Evidencia y cadena

Cada evaluación es un `decision_record` nuevo en `governance_evidence`, con `phase = 'D2'` y la
secuencia siguiente del run (después de D1). Su ámbito se registra por decisión en
`governance_decision_scopes` (tabla aditiva, solo inserción), con los bytes del artefacto y del motor
que la evaluaron, así el replay no depende de la configuración del episodio D1.

| Enlace | Dónde está |
| --- | --- |
| D2 → D1 | `d1.decision_id`, `d1.evidence_id`, `d1.evidence_digest` y `d1.input_digest` en la entrada canónica |
| D2 → frescura `approve` | `freshness.source_offer_ref.digest` = `digest` de la fila de `hp_evidence` |
| D2 → términos presentados | `hp.presented_snapshot_ref` y `hp.consent_binding_ref` |
| Aprobación → D2 | `approvals.authorization_evidence_id` (columna nueva, nula en compras sin D2), cubierta por la firma HMAC de la aprobación |

Los hechos que no son del ledger quedan en el vault de procedencia de D1 como fuentes de tipo
`authorization`, una por rol y run (una versión nueva si cambian): `checkout_terms` (GET del
checkout), `presented_terms` y `consent_binding` van cifrados porque llevan datos del comprador;
`material_contract`, `freshness_contract` y `authorization_profile` son contratos públicos. Sus
`ObjectRef` son los que nombra la entrada y el ámbito de la decisión. La entrada canónica no lleva
correo, dirección ni la huella del clic.

Con allow, `application.disposition = transition_committed`, `transition_ref = authorization_id` y
`resulting_state_version = expected + 1`, escritos en la misma transacción que la aprobación. Con
deny, `blocked`. El ledger verifica la transición en la misma conexión antes de escribirla.

## Qué ve el usuario con un deny

- **API:** 409 con el texto del chat, sin nombres internos:
  - `d2_denied`: «La política de compra no autorizó esta compra al aprobarla. No se firmó ninguna
    autorización ni se hizo ningún cobro.»
  - `d2_unavailable`: «No pude verificar la autorización de la compra, así que la detuve por
    precaución. No se firmó ninguna autorización ni se hizo ningún cobro.»
  - `d2_allow_unapplied`: «La política autorizó la compra, pero no pude registrar tu aprobación, así
    que la detuve por precaución. No se firmó ninguna autorización ni se hizo ningún cobro.»
- **Chat:** la tarjeta de aprobación se reemplaza por una tarjeta del mismo patrón que
  `StaleTermsCard`/`PolicyBlockedCard`: ámbar, sin aviso rojo, que dice que no se firmó nada, con las
  reglas que no se cumplieron cuando la traza las trae y un solo botón, «Cancelar compra». La compra
  sigue en `awaiting_confirmation`; al recargar, `GET /me/pending-purchase` la devuelve con
  `terms_status: "authorization_denied"`.
- **Traza:** el paso «Autorización verificada» queda `blocked` con sus reglas, «Tu aprobación» queda
  «No se ejecutó» y la compra sale como «Bloqueada por la política».

## Modos

| Condición | D2 |
| --- | --- |
| B0' (`off`) | No corre: no hay gate, ni evidencia, ni paso en la traza |
| `shadow` | No corre (fuera de alcance) |
| `enforce` + `blocked` | No llega: la compra se detiene antes del checkout |
| `enforce` + `governed` + `b2-pilot-v1` | No corre: el artefacto no trae reglas D2. El piloto v1 se reproduce igual |
| `enforce` + `governed` + `b2-hp-v2` | Corre en cada aprobación |

## Traza

Paso nuevo en `PurchaseTrace` (Pydantic, `trace-contract.ts`, `parsePurchaseTrace`, fixtures e
historias):

```ts
| { kind: "authorization_decision"; decision: PolicyDecision }  // actor "policy"
```

Reutiliza `PolicyDecision` (mismas reglas, efecto y referencias). Va después de «Tu aprobación» y
antes de «Autorización firmada». No es un control del recorrido base: con la política apagada o con
`b2-pilot-v1` no aparece como «No se ejecutó». Títulos: «Autorización verificada», «La política no
autorizó la compra» (una regla falló), «No se pudo verificar la autorización» (guarda, sin reglas
evaluadas) y «La política autorizó la compra; se detuvo por precaución» (allow no aplicado).

Cada regla revisa varios hechos, así que solo trae los valores que explican su resultado: POL-04,
la variante aprobada y la que revisó D1 si difieren (si falla por la presentación o el clic, sin
valores); POL-05, el total autoritativo del checkout frente al presupuesto total si se cumplió o si
el total lo supera (no si falló por una restricción); POL-06, la política de D1 y la vigente si
difieren. POL-03 nunca muestra valores (sería la identidad del dueño).

Con la última decisión D2 en `blocked` y sin aprobación, la traza marca «Tu aprobación» como
`skipped` y la compra como `blocked`; el historial toma la última decisión, D1 o D2.

## Chat

Aprobar con 409 `d2_denied`, `d2_unavailable` o `d2_allow_unapplied` reemplaza la tarjeta de
aprobación por la de compra que ya no se puede aprobar (`StaleTermsCard`), en ámbar y sin aviso rojo:
«La política de compra no autorizó esta compra» o «No pude verificar la autorización de la compra»,
«No se firmó ninguna autorización ni se hizo ningún cobro.», la regla que falló cuando llega la
traza y solo «Cancelar compra». Aprobar otra vez se rechazaría igual. `GET /me/pending-purchase`
restaura ese estado con `terms_status` `authorization_denied` o `authorization_unverified` (lee la
última fila D2 del run; `SqliteD2AuthorizationGate.refusal`).

## Criterios de aceptación

1. En `enforce` + `governed` con reglas D2, aprobar una compra válida deja un `decision_record` D2
   `allow`/`transition_committed` y la aprobación con su `evidence_id`, en una transacción.
2. Un total autoritativo sobre el límite, una variante distinta a la de D1 o un cambio de política
   entre D1 y D2 dan deny con la razón del catálogo; no hay fila en `approvals`, ni mandatos, ni
   llamada al PSP o al comercio.
3. Un motor que falla o hechos que faltan dan 409, sin firma (fail-closed).
4. Con `off` o `b2-pilot-v1` el flujo es idéntico al actual (las pruebas existentes no cambian).
5. El replay de la evidencia D2 reproduce outcome, razones y reglas.
6. La traza muestra el paso nuevo con las mismas fixtures en Pydantic y en `parsePurchaseTrace`.

## Plan de PRs

| PR | Título | Contenido |
| --- | --- | --- |
| A | `feat(governance): authorize HP approvals with POL-03..06 (D2)` | Spec, modelos, motor, ledger, ensamblado, integración en la aprobación, mensajes de la API |
| B | `feat(trace): show the D2 authorization step and its denial in the chat` | Paso de traza, `parsePurchaseTrace`, fixtures, tarjeta del deny, historias, E2E |

B se apila sobre A. A va rebasado sobre P1 (`feat/d1-budget-rule`): la fila D2 de la tabla y la
sección `authorization` de `b2-hp-v2` son parte de A.

## Riesgos y desviaciones del contrato formal

- **Bases antiguas.** `governance_evidence` tenía `CHECK(phase = 'D1')`. Las tablas nuevas admiten
  `D2`; una base creada antes no se migra. Con reglas D2 configuradas, el agente se niega a arrancar
  sobre esa base (como cualquier ajuste inválido de `enforce`). El piloto y `demo-stack` crean una
  base por corrida.
- **Ventana del intento.** El contrato pide `authority_now < attempt_valid_until`. La vigencia del
  intento es la del checkout, que está en el reloj comercial (sandbox en el piloto), así que se
  compara con `commercial_now`, como ya hace `require_approvable`. No se inventa un TTL.
- **Clic.** El clic es la propia petición de aprobación: `clicked_at` es `authority_now`. No hay
  *challenge* de presentación (fuera de alcance).
- **`d1.merchant_ref` y `d1.evidence_*`.** Se agregan a los hechos resueltos de D1 para POL-04 y para
  la cadena de evidencia.
- **Base de autenticación.** El gate solo corre después de `current_buyer`; con `AGENT_AUTH_MODE=jwt`
  es un JWT verificado y en el modo local del laboratorio es el principal configurado. En los dos
  casos el hecho es `user_jwt`, que es la base que admite `b2-hp-v2`.
- **Piloto.** `governance_pilot.read_governed_evidence` lee todo `governance_evidence`: con
  `b2-hp-v2` las filas D2 aparecen en `d1_decisions`. Quien las cuente debe separar por
  `decision.phase` (P2/P4).
- **Restricciones duras.** Se recalculan sobre los atributos de la variante en el feed congelado de
  D1: el checkout ACP no trae categoría, condición ni opciones.
- **Desglose.** `other_minor = total − unitario × cantidad − envío`; un resto negativo no es
  representable y detiene la aprobación.
- **Identidad del motor.** El código de D2 vive en `models.py` y `evaluator.py`, que ya mide
  `measure_reference_engine`: cambia el digest del motor de referencia y su versión pasa de
  `reference-d1:1` a `reference-d1-d2:2`. La evidencia histórica sigue verificándose con su archivo;
  su replay con el código nuevo ya no aplica, como antes.
