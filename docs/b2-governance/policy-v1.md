# Contrato formal de políticas B2 v1

Estado: **PROPUESTO PARA REVISIÓN HUMANA**, EC-GOV-01, 2026-10-14.
Base inspeccionada: `da89fec0a29409b4f9d532e2a5625fd3573626a7`.
Este documento especifica comportamiento futuro; no implementa ni valida B2/HNP.
Ningún propietario humano ni decisión técnica se da por aprobado. La entrega académica
indicada para esta tarea es prototipo v2 al cierre de Sprint 2, semana 11; semanas 12–14
para evaluación. Es una restricción de planificación, no evidencia de capacidad o avance.

## 1. Fuentes, alcance y precedencia

Se leyeron en orden el [roadmap HP/HNP](system-design-v1/roadmap.md), el
[registro de decisiones](system-design-v1/decisions.json), la
[revisión D1/D2](design-review.md), el [contrato anterior](policy-contract.md),
las [amenazas](threat-model.md), el [plan de evaluación](evaluation-plan.md),
el [cierre de diseño](design-closure-2026-10-11.md) y el
[manifiesto anterior](../../evaluation/data/b2-design.json).
El roadmap del 11 de octubre prima como propuesta reciente. Este contrato concreta
D10/D12/D13/D15/D16/D30/D31; no cambia el estado del registro ni cierra D38.
Complementos: [aceptación](policy-acceptance-v1.md) y [ADR del motor](adr-policy-engine-v1.md).

Perfil propuesto: HP y HNP, B2C, un merchant configurado, una unidad, COP y pago
sintético; HNP de compra única, sin recurrencia ni subdelegación. B2B no se incorpora.
Solo se formalizan POL-01–POL-10 del roadmap. Validación de esquemas, cripto,
frescura, identidad, CAS y evidencia son controles de aplicación, no nuevas políticas.

### Contradicciones conservadas, sin sobrescribir antecedentes

| Fuente anterior | Diferencia con esta propuesta | Tratamiento para revisión |
| --- | --- | --- |
| policy-contract.md y b2-design.json: P1–P5/GOV-001–010 | El roadmap usa POL-01–10 y D1/D2 por modo/D3-HNP | No hay equivalencia numérica GOV→POL; conservar el manifiesto histórico y elaborar uno nuevo antes de evaluar |
| policy-contract.md: `ask_for_authority`, obligaciones dinámicas y puerto async | Aquí solo `allow/deny`, pasos fijos y puerto síncrono | Separar aclaración conversacional de consentimiento; no ejecutar instrucciones del motor |
| design-review.md: no tercer PDP en B2C HP | HNP añade revocación y uso dinámico aunque sea B2C | D3 es exclusivo de HNP; no añadir D3-HP por analogía |
| evaluation-plan.md: S03 pide autoridad; S02 es deny del PDP | S03 solicita información; S02 puede terminar antes de D1 | Conservar escenarios y resultados; documentar capa/oráculo nuevo sin inventar decisiones que no ocurrieron |
| policy-contract.md: 100 ms y OPA/bundle concretos | D11/D32 exigen presupuesto y elección con evidencia | 100 ms no es SLO aprobado ni medición; ver ADR |
| threat-model.md: todos los fallos añadidos bloquean | Shadow del roadmap no bloquea por fallo del observador añadido | Los controles de autoridad comunes nunca se apagan; fallo añadido sí tiene semántica por modo |
| b2-design.json: constantes HP y OPA declarado | El diseño incluye nueva base HNP y motor aún candidato | No reutilizar ese manifiesto como configuración aprobada de una ejecución HNP |

## 2. Semántica de evaluación y efecto

| Punto | Políticas aplicables | Ubicación y siguiente efecto protegido |
| --- | --- | --- |
| D1, HP/HNP | POL-01, POL-02 | `PurchaseService.start`, antes de `create_checkout` |
| D2-HP | POL-03–POL-06 | Tras clic, ownership y frescura; antes de `ApprovalService.approve`/firma |
| D2-HNP | POL-03–POL-09 | Tras resolver delegación; antes de reserva CAS y cierre de mandatos |
| D3-HNP | POL-09, POL-10 | Tras D2/reserva/cierre; antes del compromiso durable de despacho |

Evaluación pura y determinista sobre hechos resueltos y artefacto de políticas fijado.
`PolicyEvaluator.evaluate(PolicyInput) -> PolicyDecision` es un puerto síncrono
propuesto, sin red comercial, consultas al store, firma ni herramientas del LLM.
La implementación concreta puede ser un adaptador OPA o Python; el llamador impone timeout.
Se evalúan todas las políticas aplicables, sin cortocircuitar la evidencia de razones.
`allow` exige que todas pasen; cualquier incumplimiento da `deny`.
Las no aplicables no se reportan como aprobadas ni autorizan otra fase.

El resultado no es una capability portable. En `enforce`, antes del siguiente efecto
se exige además: validaciones comunes válidas, decisión ligada a esa entrada/versión,
evidencia durable y transición/CAS correcto. El LLM, un ID, un HTTP 200 o un booleano
aportado por el cliente no satisfacen esas condiciones.
Los pasos de aplicación se fijan por fase; el motor no devuelve un programa de obligaciones.

## 3. Tipos comunes, autoridades y privacidad

Todo modelo nuevo tiene `schema_version="commerce-policy/1.0"`, campos extra prohibidos,
inmutabilidad tras construcción y validación estricta recursiva. No coercionar bool→int,
strings→números, fechas sin zona ni tipos desconocidos. Campos requeridos no admiten null;
solo las uniones/optionals enumeradas abajo lo admiten. Un dato desconocido requerido
no se transforma en cero, lista vacía, coincidencia o permiso.

| Tipo propuesto | Definición |
| --- | --- |
| `Id` | String opaco no vacío, máximo 128, alfabeto `[A-Za-z0-9._:-]`; emitido/resuelto por backend, sin PII |
| `Digest` | `sha256:` + 64 hex minúsculas; hash de objeto no sensible, nunca de dirección/correo en claro |
| `UtcTime` | RFC3339 UTC `Z`, precisión de milisegundos; parser rechaza fecha ambigua |
| `Minor` / `Version` | Entero estricto ≥0 / entero estricto ≥1; sin float, NaN o bool |
| `Currency` | Código ISO alfabético mayúsculo; perfil v1 admite solo `COP`; ACP `cop` se transforma explícitamente |
| `Action` | Enum cerrado `prepare_checkout`, `complete_purchase`, `commit_dispatch` |
| `PurchaseMode` / `GovernanceMode` | `HP/HNP` y `off/shadow/enforce`, respectivamente; campos distintos |
| `ObjectRef` | `{id:Id, version:Version, digest:Digest}`; dato ya resuelto, no URL arbitraria |
| `PolicyBinding` | `{policy_set_id:Id, version:Version, artifact_digest:Digest, schema_version:string}`; schema exacto v1 |
| `PrivateBinding` | `{binding_id:Id, scheme:"hmac-sha256/v1", key_id:Id, value:64hex}`; HMAC resuelto por backend con propósito/ámbito fijados |

Límites de longitud/cardinalidad para colecciones y tamaño total se fijarán en D32 antes
de entrada externa/enforcement. El primer incremento solo usa fixtures pequeñas de confianza;
no promete resistencia a agotamiento de recursos con límites todavía sin medir.

### Cabecera común de las tres entradas

Campos obligatorios: `schema_version`, `decision_id:Id`, `run_id:Id`, `trace_id:Id`,
`owner_ref:Id`, `purchase_mode:PurchaseMode`, `governance_mode:GovernanceMode`,
`phase:"D1"|"D2"|"D3"`, `action:Action`, `context_version:Version`,
`expected_local_state_version:Version`,
`expected_policy:PolicyBinding`, `authority_now:UtcTime`, `commercial_now:UtcTime`,
`clock_profile_ref:ObjectRef`. `experiment_run_id:Id|null` y `conversation_id:Id|null`
se incluyen explícitamente; null significa no aplicable, no autoridad desconocida.
El reloj real de autoridad controla grants y expiración de pruebas de identidad;
el reloj comercial puede ser sintético solo si el episodio lo fija. AP2 se verifica
con su reloj criptográfico definido, nunca con el tiempo aportado por el usuario.

| Hechos / emisor autoritativo | Uso y conservación |
| --- | --- |
| Cabecera, modos, versión esperada / runtime y store | Modo fijado por episodio; `context_version` identifica contexto normativo/configuración del episodio; `expected_local_state_version` es la versión CAS actual y puede cambiar entre fases; no leerlos del body ni del modelo |
| `run_id`, owner, operation/attempt/grant/reservation refs / store de aplicación | IDs correlacionan; verificar ámbito y relaciones al resolver y al escribir |
| Intención / `PurchaseIntent` validada tras conversación | Su interpretación es propuesta, no autoridad; conservar snapshot operativo y versión; intención ambigua no llega a D1 |
| Elegibles / `rank_candidates`, intención y feed congelado | Feed no autoriza precio final; conservar IDs, referencias y versión del algoritmo |
| Términos / merchant a través de `CheckoutSession` validada y guard de frescura | Proyección tipada sin PII; no copiar `extra="allow"`, diccionarios ACP libres ni descripciones al PDP |
| Principal y ownership / API JWT verificado; HNP servicio autenticado y owner del grant | `PrincipalFacts` se ensambla en servidor; el evaluador no autentica |
| Estado/epoch/uso/reserva / store transaccional de delegaciones | Snapshot para política; la transacción vuelve a comprobar versiones, no confía en snapshot para exclusividad |
| Perfil y resultados criptográficos / verificadores AP2 y trust configurado | Solo refs y estado verificado; el motor no verifica firmas |

`IntentFacts` = `{intent_ref:ObjectRef, currency:Currency, max_unit_minor:Minor|null,
max_total_minor:Minor|null, quantity:1, hard_constraint_refs:tuple[Id],
normalization_version:Id}`. Null en un límite significa ausencia explícita de ese
límite en intención HP, nunca presupuesto ilimitado en HNP. Se reutilizan
`PurchaseIntent.hard`/`HardConstraints`; cantidad 1 procede del perfil, pues no es
un campo actual de `PurchaseIntent`. Las preferencias blandas no autorizan ni deniegan.
La intención completa queda en store protegido, sin copiar prompt ni términos libres al PDP.

`ConstraintMatch` = `{constraint_ref:Id, kind:category|condition|option|term,
matches:bool, source_snapshot_ref:ObjectRef}`. El assembler exige una fila por
restricción de intención, sin duplicados ni omisiones, y calcula los resultados
desde candidato/snapshot validados. Permite reutilizar filtros existentes sin dar
a Rego texto libre para interpretar. Replay del PDP reproduce estos hechos;
recalcular interpretación, filtros o firmas requiere snapshots operativos protegidos.

`PrincipalFacts` = `{principal_ref:Id, authentication_basis:user_jwt|service_and_grant,
authentication_verified:bool, owns_run:bool, permitted_actions:tuple[Action]}`.
Acciones proceden de perfil/autorización backend, no de claims sin verificar.
El owner resuelto debe coincidir con cabecera; para HNP, principal de servicio y
owner humano son distintos y se vinculan explícitamente mediante el grant.

`MaterialTerms` = `{snapshot_ref:ObjectRef, merchant_ref:Id, checkout_id:Id,
variant_id:Id, checkout_status:"ready_for_payment", quantity:1, unit_minor:Minor, shipping_minor:Minor,
other_minor:Minor, total_minor:Minor, currency:Currency,
fulfillment_service_ref:Id, destination_binding:PrivateBinding,
commercial_expires_at:UtcTime, material_digest:Digest, material_contract_ref:ObjectRef}`.
`total_minor=unit_minor*quantity+shipping_minor+other_minor`; la proyección debe
explicar cada total ACP sin inventar desglose ni descartar cargos. Un campo no
representable bloquea el perfil hasta resolver D17. No reutilizar fingerprint HP
actual como digest material: incluye campos técnicos y contenido privado.

`FreshnessFacts` = `{status:current|stale|unresolved, observed_at:UtcTime,
valid_until:UtcTime, contract_ref:ObjectRef, source_offer_ref:ObjectRef}`.
Solo `current`, observación dentro del intervalo autorizado por D17 y
`commercial_now < commercial_expires_at` permiten avanzar. GET almacenado no
prueba current. TTL y contrato deben existir en configuración revisada antes de enforce;
no se inventa una vigencia numérica en este documento.

### Identidades y relaciones

`run_id` es compra, no episodio experimental. `trace_id` correlaciona, no autentica.
`decision_id` identifica una evaluación; una reevaluación genera otro ID.
Intento HP tiene un `approval_attempt_id` estable para mismo cuerpo/retry;
nuevos términos/presentación crean otro intento. `operation_id` es identidad durable
de efecto lógico; no es la clave idempotente externa ni se regenera al reintentar.
Grant/version identifica alcance consentido; epoch identifica autoridad operativa
actual. Todas las relaciones mantienen run/owner/merchant y versiones explícitas.

## 4. Entradas D1, D2 y D3

### SelectionPolicyInput

Cabecera con `phase=D1`, `action=prepare_checkout`, más:

| Campo | Tipo / condición |
| --- | --- |
| `intent` | `IntentFacts` |
| `feed_snapshot_ref` / `eligibility_algorithm_ref` | `ObjectRef` / `ObjectRef` |
| `eligible_variant_ids` | Tupla de `Id`, única y ordenada; solo resultados del filtro |
| `selected_variant_id` | `Id`; pertenece al conjunto para permitir |
| `merchant_ref` | `Id` configurado y resuelto |

El artefacto de políticas conserva allowlist de acciones/merchant y perfil comercial.
No se envía un `action_allowed` inventado por el modelo. Sin candidatos o con
ambigüedad, ConversationService responde antes de D1; si el servicio recibe una
selección inválida directamente, su guarda común bloquea y el evaluador offline
debe denegar el fixture equivalente. No crear un allow/deny ficticio para cubrir S02.

### AuthorizationPolicyInput: unión discriminada por purchase_mode

Cabecera con `phase=D2`, `action=complete_purchase`, más campos comunes:

| Campo | Tipo / condición |
| --- | --- |
| `principal` / `intent` | `PrincipalFacts` / `IntentFacts` |
| `selected_variant_id` / `terms` / `freshness` | `Id` / `MaterialTerms` / `FreshnessFacts` |
| `constraint_matches` | Tupla de `ConstraintMatch`, cobertura exacta de `intent.hard_constraint_refs` |
| `d1` | Hechos resueltos de decisión: `decision_id, outcome, run_id, owner_ref, purchase_mode, governance_mode, intent_ref, feed_snapshot_ref, selected_variant_id, policy_binding, context_version, input_digest`; tipos homónimos de cabecera/Decision y ObjectRef para las referencias |
| `authorization_id` / `operation_id` | `Id` preasignados por backend y durables |
| `authorization_profile` | `{status:validated\|unresolved, profile_ref:ObjectRef\|null}`; ref obligatoria si validated |

El store debe resolver D1 completo y su evidencia, no aceptar solo un ID/outcome.
Una decisión shadow puede observarse/reproducirse, pero no reutilizarse como permiso
en enforce; la cabecera del registro resuelto incluye ese modo y se verifica también.

Rama **HP** agrega únicamente `hp`:
`{approval_attempt_id:Id, attempt_version:Version, attempt_owner_ref:Id,
presented_snapshot_ref:ObjectRef, presented_at:UtcTime, attempt_valid_until:UtcTime,
click_received:bool, clicked_at:UtcTime,
material_match:bool, consent_binding_ref:ObjectRef}`.
El servidor resuelve presentación/clic verificados; el body solo devuelve referencia
y fingerprint según API futura. `material_match` se calcula con D17, nunca lo declara la UI.
No se admiten campos `hnp` ni un grant como sustituto del clic.

Prerrequisitos de aplicación D2-HP: `click_received=true`,
`presented_at ≤ clicked_at ≤ authority_now < attempt_valid_until`, ownership,
comparación material válida y freshness current. La ventana del intento deberá
fijarse/revisarse en D17/D19/D28; no inventar TTL ni aceptar consentimiento de
un mensaje del chat. Para todo D2/D3, perfil de autorización validated y términos
vigentes son prerrequisitos; un allow de POL no permite saltarlos.

Rama **HNP** agrega únicamente `hnp`:
`{grant:GrantFacts, usage:UsageFacts, reservation_intent:ReservationIntent, requested_instrument_ref:Id,
open_proof:OpenProofFacts}`. No tiene `hp`, clic fabricado ni `human_approved`.

| Tipo HNP | Campos estrictos |
| --- | --- |
| `GrantFacts` | `grant_ref:ObjectRef, owner_ref:Id, agent_key_ref:Id, state:active\|revoked\|expired\|consumed, authority_epoch:Version, valid_from:UtcTime, expires_at:UtcTime, permitted_actions:tuple[Action], scope:DelegationScope` |
| `DelegationScope` | `merchant_refs:tuple[Id], variant_ids:tuple[Id], currency:Currency, quantity_max:1, total_max_minor:Minor, destination_binding:PrivateBinding, instrument_ref:Id, uses_max:1`; todos explícitos y no vacíos cuando son colecciones |
| `UsageFacts` | `grant_ref:ObjectRef, state_version:Version, uses_consumed:Minor, uses_reserved_other:Minor, amount_consumed_minor:Minor, amount_reserved_other_minor:Minor, same_operation_reservation:ReservationFacts\|null` |
| `ReservationIntent` | `reservation_id:Id, operation_id:Id, run_id:Id, owner_ref:Id, grant_ref:ObjectRef, expected_epoch:Version, amount_minor:Minor, currency:Currency` |
| `ReservationFacts` | Campos de ReservationIntent + `state:reserved\|committed\|consumed\|released\|uncertain, state_version:Version` |
| `OpenProofFacts` | `status:valid\|invalid\|unresolved, profile_ref:ObjectRef\|null, open_checkout_ref:ObjectRef, open_payment_ref:ObjectRef, consent_ref:ObjectRef, verified_agent_key_ref:Id` |

No constraints libres ni excepciones desconocidas: el assembler rechaza perfiles
no representables. Los refs a mandatos y pruebas son opacos y protegidos.
`uses_reserved_other` excluye la reserva de la misma operación: el retry no cuenta dos veces.
Los contadores deben ser coherentes con las reservas/consumos resueltos; en el
perfil single-use no puede haber más de un uso entre consumido, ajeno y propio.
Un uso cero no puede ocultar un importe consumido/reservado no cero; incoherencia
de proyección es GOV_FACTS_UNAVAILABLE, no capacidad libre.
Un `allow` D2 con capacidad observada aún necesita crear/revalidar reserva por CAS
antes de firmar. No afirmar exclusividad por una lectura o por POL-09 aislada.

### DispatchPolicyInput

Solo HNP: cabecera `phase=D3`, `action=commit_dispatch`, más:
`principal:PrincipalFacts`, `intent:IntentFacts`, `terms:MaterialTerms`,
`freshness:FreshnessFacts`, `grant:GrantFacts`, `usage:UsageFacts`,
`reservation:ReservationFacts`, `operation_id:Id`,
`d2:ResolvedD2`, `closed_proof:ClosedProofFacts`.
`ResolvedD2` conserva la cabecera D2, outcome/input_digest/policy_binding,
authorization_id, intent_ref, selected_variant_id, snapshot_ref/material_digest,
grant_ref, authority_epoch, reservation_id, operation_id, requested_instrument_ref
y application_transition_ref;
todos resueltos del ledger/store y con tipos homónimos ya definidos. La transición
referenciada acredita que la reserva D2 fue aplicada, no solo observada.
`ClosedProofFacts` tiene `status:valid|invalid|unresolved`, `profile_ref:ObjectRef|null`,
`checkout_chain_ref:ObjectRef`, `payment_chain_ref:ObjectRef`, `original_checkout_jwt_digest:Digest`
y `verified_agent_key_ref:Id`, más los vínculos verificados `authorization_id:Id`,
`operation_id:Id`, `grant_ref:ObjectRef`, `snapshot_ref:ObjectRef`,
`material_digest:Digest`, `requested_instrument_ref:Id`. Deben coincidir con D2 y
el grant actuales antes de aplicar D3. No contiene JWT ni mandatos en claro.

D3 solo prepara compromiso si D2 sigue aplicable a esos términos, grant, reserva,
operación y versión de políticas. Cambio material requiere nuevo D2 y cierre;
si ya hubo presentación remota incierta, no producir otro cierre: reconciliar primero.

## 5. PolicyDecision, razones y evidencia

`PolicyDecision` tiene campos obligatorios:
`schema_version`, `decision_id:Id`, `phase:D1|D2|D3`, `purchase_mode:HP|HNP`,
`governance_mode:off|shadow|enforce`, `outcome:allow|deny`, `origin:policy|runtime_guard`,
`reason_codes:tuple[ReasonCode]`, `rule_results:tuple[{policy_id:POL-01..POL-10,
result:pass|fail}]`, `input_digest:Digest`, `policy_binding:PolicyBinding`,
`engine:{kind:python|opa|test, version:Id|null, artifact_digest:Digest|null, attempted:bool}`,
`engine_decision_id:Id|null`, `decided_at:UtcTime`, `latency_ms:Minor`.
Modo off no invoca evaluador ni fabrica una PolicyDecision; el enum se conserva
para correlación/validación y una respuesta con off es inválida para aplicación.
Con `origin=policy`, versión/digest de motor son obligatorios y verificados;
con runtime_guard pueden ser null si no pudieron comprobarse. No registrar la
versión configurada como si fuese identidad efectivamente medida del motor.

Razones de política estables:

| Política | Razón de rechazo |
| --- | --- |
| POL-01 | `SELECTION_NOT_ELIGIBLE` |
| POL-02 | `ACTION_OR_MERCHANT_NOT_ALLOWED` |
| POL-03 | `PRINCIPAL_ACTION_NOT_ALLOWED` |
| POL-04 | `EPISODE_BINDING_MISMATCH` |
| POL-05 | `CHECKOUT_CONSTRAINT_MISMATCH` |
| POL-06 | `POLICY_CONTEXT_MISMATCH` |
| POL-07 | `DELEGATION_NOT_ACTIVE` |
| POL-08 | `DELEGATION_SCOPE_EXCEEDED` |
| POL-09 | `DELEGATION_USE_UNAVAILABLE` |
| POL-10 | `DISPATCH_AUTHORITY_CHANGED` |

Allow usa solo `ALL_APPLICABLE_POLICIES_SATISFIED`. Deny enumera razones de reglas
fallidas, sin duplicados y en orden POL-01→10. `rule_results` cubre exactamente
las aplicables según fase/modo. No hay mezcla allow+fallos ni razón arbitraria.

Errores del llamador se distinguen de rechazo normativo: `GOV_INPUT_INVALID`,
`GOV_FACTS_UNAVAILABLE`, `GOV_ENGINE_UNAVAILABLE`, `GOV_OUTPUT_INVALID`,
`GOV_ARTIFACT_MISMATCH`, `GOV_EVIDENCE_UNAVAILABLE`, `GOV_STATE_CONFLICT`,
`GOV_FRESHNESS_UNRESOLVED`, `GOV_AUTHORIZATION_PROFILE_UNRESOLVED`.
Son controles derivados de D13/D17/D20/D23/D30, no POL-11 en adelante.
El wrapper confiado produce `deny, origin=runtime_guard`, razones de ese catálogo
y `rule_results=[]` cuando no hubo evaluación válida; `attempted=false` distingue
un rechazo previo a invocar el motor, sin inventar una ejecución. Si había una decisión válida
y después falla evidencia/CAS, no reescribirla: bloquear la aplicación y registrar
el fallo por separado si es posible. Una salida desconocida nunca se acepta ni se
reinterpreta como allow; un fallo operativo no se presenta como falta de consentimiento.

### Digest y replay

Perfil de serialización propuesto `commerce-canonical-json/1`: JSON UTF-8 sin BOM,
claves de objeto ordenadas, separadores `,`/`:`, sin espacios, escapes JSON estándar,
Unicode conservado sin normalización adicional, solo enteros, bool, null, strings,
arrays y objetos tipados. Rechazar claves JSON duplicadas antes de parsear; ordenar
conjuntos declarados y mantener orden de listas semánticas. Tiempos ya normalizados
y `ensure_ascii=false`. No es una declaración de conformidad JCS.
`input_digest=SHA256(b"commerce-policy-input/v1\0" + canonical_input_bytes)`.
La entrada no contiene su propio digest ni la salida: no hay autorreferencia.
Cada ejecución conserva exactamente bytes, perfil y artefacto de políticas; distintos
IDs/relojes pueden dar digest distinto aunque el resultado lógico sea igual.
El runtime contrasta la identidad/digest desplegados con configuración confiable;
no basta que el motor haga eco de `expected_policy`.

`DecisionEvidence` es una unión estricta versionada con tres variantes:

| Variante | Campos y reglas |
| --- | --- |
| `record_kind=decision_record` | `evidence_id:Id, sequence:Version, recorded_at:UtcTime, canonicalization_profile:Id, canonical_input_bytes:utf8-string, decision:PolicyDecision, policy_artifact_ref:ObjectRef\|null, engine_artifact_ref:ObjectRef\|null, source_refs:tuple[ObjectRef], application:{disposition:observed_shadow\|blocked\|transition_committed, transition_ref:Id\|null, expected_state_version:Version, resulting_state_version:Version\|null}, replayability:full\|policy_not_evaluated` |
| `record_kind=input_rejected` | `evidence_id, sequence, recorded_at, schema_version, run_id, trace_id, decision_id, reason_code:GOV_INPUT_INVALID\|GOV_FACTS_UNAVAILABLE, safe_field_paths:tuple[Id], replayability:not_evaluable`; no raw input, supuesto digest válido ni resultado ficticio del motor |
| `record_kind=application_failure` | `evidence_id:Id, sequence:Version, recorded_at:UtcTime, schema_version, run_id:Id, trace_id:Id, decision_id:Id, reason_code:GOV_STATE_CONFLICT\|GOV_EVIDENCE_UNAVAILABLE, expected_state_version:Version, observed_state_version:Version\|null, replayability:not_evaluable`; correlaciona fallo de aplicación sin reescribir el resultado de política |

La primera variante conserva la entrada tipada reconstruible, no solo su hash.
Con origin=policy requiere ambos artefactos recuperables y replayability=full;
con runtime_guard admite refs null si faltan y usa policy_not_evaluated. No promete
reproducir un timeout real mediante replay lógico de reglas. Si el store está caído,
tampoco puede prometer persistir application_failure: no avanzar y contabilizar
la ausencia de evidencia en la observación operativa disponible, sin inventar un registro.
`transition_committed` exige referencia/versión resultante y escritura en la misma
transacción; observación shadow tiene ambas null. Identidades run/owner/trace/fase
proceden de la entrada y deben coincidir con proyecciones del ledger, si existen.
Append-only: no sobrescribir decisiones previas; invalidación/reevaluación es otro
registro relacionado. Doble escritura del mismo evidence_id con bytes diferentes
entra en conflicto; secuencia es monotónica por run bajo store transaccional.
Reproducir input+artefacto debe conservar outcome, razones y rule_results; latencia,
timestamp de replay e ID interno del motor no son parte de equivalencia lógica.

Excluir de evaluador y ledger: bearer/JWT de identidad o checkout, mandatos/recibos
completos, claves, tokens/instrumentos de pago, claves idempotentes externas,
correo/teléfono/nombre/dirección, prompts/historial/razonamiento, texto libre de
catálogo y URLs arbitrarias. Usar refs opacas, HMAC para vínculos privados y digests
de objetos no sensibles; los originales necesarios para recuperación viven aparte
en almacenamiento operativo protegido. La política de acceso/retención sigue en D30.
Append-only de aplicación no implica resistencia a un administrador comprometido.

## 6. Catálogo POL-01–POL-10

Para todas las fichas: fuente normativa propuesta = catálogo del roadmap, D12.
Propietario humano: **POR CONFIRMAR** en cada caso; no se asigna trabajo a Yair,
Sebastián ni Darío por inferencia. Evidencia común = DecisionEvidence completa y
artefacto fijado. Los casos P/N/F referidos están en la matriz de aceptación.

### POL-01 — Selección elegible

- Requisito/fallo: impedir selección fuera de intención o feed del mismo episodio.
- Momento: D1. Hechos: `intent.intent_ref`, `feed_snapshot_ref`,
  `eligibility_algorithm_ref`, `eligible_variant_ids`, `selected_variant_id`.
- Autoridad: intención versionada en store + `rank_candidates` sobre feed validado;
  no el listado que devuelva el LLM.
- Permite si `selected_variant_id ∈ eligible_variant_ids` y referencias resueltas
  corresponden a la misma intención/feed usados para ese conjunto.
- Deniega `SELECTION_NOT_ELIGIBLE`; no crear checkout. Conserva la guarda Python B0.
- Código responsable: `domain/selection.py` y `PurchaseService.start`; evaluador
  propuesto verifica pertenencia. Humano: por confirmar.
- Evidencia específica: conjunto completo, elegido, algoritmo e intención/feed refs.
- Casos: P01/N01/F01. Dependencias: D10/D15/D16; independiente de H01.

### POL-02 — Acción y merchant admitidos

- Requisito/fallo: impedir destino/acción fuera del perfil, incluso si variante es elegible.
- Momento: D1. Hechos: `action`, `merchant_ref`, `expected_policy`, perfil fijado.
- Autoridad: runtime y datos del artefacto revisado; discovery/catálogo no otorgan confianza.
- Permite si acción es `prepare_checkout`, merchant pertenece a allowlist y perfil
  resuelto coincide con configuración. No aceptar alias/origen aportado por el modelo.
- Deniega `ACTION_OR_MERCHANT_NOT_ALLOWED`; no invocar merchant.
- Código: assembler/evaluador propuestos y `PurchaseService.start`. Humano: por confirmar.
- Evidencia: merchant/action, perfil completo recuperable por digest y configuración.
- Casos: P02/N02/F02. Dependencias: D04/D09/D14; IDs de perfil definibles sin H01.

### POL-03 — Principal habilitado para autorizar

- Requisito/fallo: impedir autorización de compra ajena o acción fuera de su contexto.
- Momento: D2-HP/HNP. Hechos: `principal`, `owner_ref`, `action`, perfil y modo.
- Autoridad: API con JWT/ownership; HNP servicio autenticado y vínculo owner/grant
  resuelto por backend. El motor consume hechos, no verifica JWT.
- Permite si autenticación y ownership son verdaderos, acción `complete_purchase`
  está en permitted_actions y perfil admite la base de autenticación para ese modo.
- Deniega `PRINCIPAL_ACTION_NOT_ALLOWED`; no firmar. Fallos de auth se rechazan
  antes del PDP, sin revelar existencia de una compra ajena.
- Código: `api.py:current_buyer/owned_purchase`, `PurchaseService.approve`;
  autorización HNP propuesta. Humano: por confirmar.
- Evidencia: refs pseudónimas, base de autenticación, decisión y perfil, nunca JWT.
- Casos: P03/N03/F03. Dependencias: D19/D27; cripto HNP depende de H01/H03.

### POL-04 — Continuidad del episodio

- Requisito/fallo: impedir reutilizar un allow de otra intención, compra o intento.
- Momento: D2-HP/HNP. Hechos: `d1`, cabecera, intención, selección, terms,
  authorization_id, operación y rama hp/hnp.
- Autoridad: ledger/store; backend resuelve D1, presentación o grant y relaciones.
- Permite si D1 es allow del mismo run/owner/modo/intención/selección; terms contiene
  esa variante y merchant; HP intento/presentación/clic pertenecen a ese run/owner
  y `material_match=true`; HNP grant/reservation_intent/operación pertenecen al run
  y owner esperado. Un input shadow no habilita una transición enforce.
- Deniega `EPISODE_BINDING_MISMATCH`; no firmar ni trasplantar consentimiento.
- Código: assembler/evidence store propuestos + `PurchaseService.approve`.
  Humano: por confirmar.
- Evidencia: relaciones resueltas y versiones, presented/current snapshots HP o
  grant/operación HNP. Casos: P04/N04/F04. Dependencias: D16/D18/D19/D23/D17.

### POL-05 — Términos compatibles con la intención

- Requisito/fallo: impedir total autoritativo, moneda, cantidad o atributos incompatibles.
- Momento: D2-HP/HNP. Hechos: `intent`, `terms`, `constraint_matches`, selección.
- Autoridad: límites de intención validada; precio/envío/stock y snapshot del merchant;
  matches recalculados por assembler. El precio del feed es solo observado.
- Permite si currency coincide, quantity=1, variante coincide, unit/total no exceden
  cada límite no null y toda restricción hard está cubierta exactamente y coincide.
  Presupuesto total incluye envío y demás cargos, no preferencias blandas.
- Deniega `CHECKOUT_CONSTRAINT_MISMATCH`; no firmar; recuperación no amplía intención.
- Código: `PurchaseService._enforce_authoritative_budget` y validación de checkout
  existentes más assembler/evaluador propuestos. Humano: por confirmar.
- Evidencia: límites, desglose material y cobertura de restricciones con referencias.
- Casos: P05/N05/F05. Dependencias: D15/D17; conserva guardas comunes B0.

### POL-06 — Versión de política y contexto compatible

- Requisito/fallo: impedir usar decisión válida bajo políticas o contexto incompatibles.
- Momento: D2-HP/HNP. Hechos: `d1.policy_binding`, `expected_policy`,
  `context_version`, schemas y artefacto activo verificado.
- Autoridad: runtime/readiness y ledger/store, nunca eco de versión del motor.
- Permite si schema, contexto relevante y versión/digest de D1 coinciden con los
  esperados. V1 exige igualdad; cambiar versión requiere reevaluar D1 explícitamente.
- Deniega `POLICY_CONTEXT_MISMATCH`; no fallback ni migración silenciosa de allow.
- Código: runtime + assembler/evaluador propuestos. Humano: por confirmar.
- Evidencia: bindings reales/configurados y refs de decisiones reemplazadas.
- Casos: P06/N06/F06. Dependencias: D11/D14/D20/D31; política no elegida aún.

### POL-07 — Delegación vigente y vinculada

- Requisito/fallo: impedir cierre con grant revocado, expirado, consumido o ajeno.
- Momento: D2-HNP. Hechos: `hnp.grant`, owner/servicio, Agent Key verificada,
  action y authority_now, `open_proof`.
- Autoridad: store de delegación + verificador de consentimiento/mandatos abiertos;
  claims suministrados sin verificar nunca son hechos de autoridad.
- Permite si grant.state=active, valid_from ≤ authority_now < expires_at,
  owner y Agent Key coinciden con vínculos verificados y acción está en el grant.
  `open_proof.status=valid` y perfil validated son prerrequisitos de aplicación.
- Deniega `DELEGATION_NOT_ACTIVE`; no reservar ni cerrar mandatos.
- Código: servicio/store de delegaciones y autorización HNP propuestos.
  Humano: por confirmar.
- Evidencia: versión/epoch/intervalo del grant, vínculos de owner/clave y proof refs.
- Casos: P07/N07/F07. Dependencias: H01/H02/H03/D28; no confundir HNP con HP sin clic.

### POL-08 — Términos dentro del alcance delegado

- Requisito/fallo: impedir expandir merchant, producto, destino, gasto o instrumento.
- Momento: D2-HNP. Hechos: grant.scope, terms, instrument_ref resuelto y open_proof.
- Autoridad: alcance consentido versionado y merchant actual; verificadores AP2
  comprueban independientemente las restricciones que les correspondan.
- Permite si merchant/variant están en allowlists, moneda coincide, quantity ≤1,
  total ≤total_max_minor, destino ligado coincide e instrumento es el consentido.
  Proyección de constraints debe estar completa; desconocido no se ignora.
- Deniega `DELEGATION_SCOPE_EXCEEDED`; detener o solicitar nueva delegación, no
  convertir el deny en autorización conversacional.
- Código: autorización HNP/assembler/evaluador propuestos, verificadores externos.
  Humano: por confirmar.
- Evidencia: scope completo tipado, términos, bindings y perfil de adaptación.
- Casos: P08/N08/F08. Dependencias: H01/H02/H03/D17; destino/lifecycle son controles propios.

### POL-09 — Uso disponible y reserva de la operación

- Requisito/fallo: impedir doble uso del mismo grant aunque cambie checkout o clave remota.
- Momento: D2-HNP y D3-HNP. Hechos: usage, grant.scope/epoch, reservation_intent
  o reservation, operation/run/owner, monto/moneda actuales.
- Autoridad: store transaccional de delegaciones, no contador del grafo o modelo.
- D2 permite con grant activo y ningún uso consumido ni reserva ajena, si puede
  reservar una compra/total dentro del scope; o con reserva propia todavía reserved
  exactamente coincidente. D3 exige reserva propia reserved, mismo grant/epoch,
  run/owner/operación/monto/moneda, sin consumo ni reserva ajena.
- Deniega `DELEGATION_USE_UNAVAILABLE`. Tras allow D2, CAS debe reservar antes de
  cerrar; un CAS perdedor bloquea con GOV_STATE_CONFLICT aunque el PDP haya permitido.
  Timeout o resultado remoto incierto nunca libera cupo automáticamente.
- Código: store/reserva/UoW propuestos; evaluador solo decide sobre snapshot.
  Humano: por confirmar.
- Evidencia: contadores/versiones antes de CAS, reserva y resultado de transición.
- Casos: P09/N09/F09. Dependencias: H04/D18/D22/D23/D24; exclusividad necesita prueba.

### POL-10 — Autoridad vigente al comprometer despacho

- Requisito/fallo: impedir despachar tras perder autoridad o reutilizar D2 obsoleto.
- Momento: D3-HNP. Hechos: D2 resuelto, grant/epoch/uso/reserva actuales,
  términos/frescura, closed_proof, expected_local_state_version y operación.
- Autoridad: store único para revocación/compromiso, merchant para términos,
  verificadores para cadena y runtime para versión permitida.
- Permite si principal verificado y owner siguen vinculados, `commit_dispatch` está
  permitido por el perfil, D2 allow enlaza exactamente intención/términos/grant/reserva/operación,
  grant sigue activo y no expirado, epoch coincide, reserva propia es válida,
  perfil/políticas no cambiaron y términos siguen vigentes. La Agent Key y los
  vínculos de closed_proof coinciden con grant y D2. Proof valid es
  prerrequisito. La UoW vuelve a comprobar versión/epoch/reserva al comprometer outbox.
- Deniega `DISPATCH_AUTHORITY_CHANGED`; no despachar. Reserva solo se libera con
  prueba de ausencia de presentación o resultado autoritativo reconciliado según perfil.
- Código: autorización/ejecutor HNP y UoW/outbox propuestos. Humano: por confirmar.
- Evidencia: D2/D3, versiones esperadas/observadas, orden revocación/compromiso,
  reserva, operación y luego recibos referenciados. Casos: P10/N10/F10.
- Dependencias: D17/D23/D24/H01/H04/H05/H06; allow no congela autoridad distribuida.

## 7. Matriz de responsabilidades y puntos reales

| Componente | Verifica / aplica | No sustituye | Estado/ruta |
| --- | --- | --- | --- |
| ConversationService | Clarificación, cero candidatos y presentación; no interpreta deny como aprobación | Consentimiento, política, cripto | Existe: [conversation.py](../../src/commerce_agent/application/conversation.py); `_recover_no_candidate` aún usa router |
| PurchaseService | Elegibilidad común, presupuesto y D1 cerca de create; D2 antes de approve | Autoridad comercial ni atomicidad remota | Existe: [purchase.py](../../src/commerce_agent/application/purchase.py) |
| PolicyEvaluator | POL aplicables, resultado puro versionado | JWT, AP2, frescura, reserva CAS | Puerto y adaptadores nuevos; [ADR](adr-policy-engine-v1.md) |
| ApprovalService | Aprobación HP exacta, expiración y firma después de D2 | Delegación HNP ni PDP | Existe: [approval.py](../../src/commerce_agent/approval.py) |
| OfferFreshnessGuard | Obtener términos actuales, clasificar materialidad y vincular presentación | Recotización autoritativa ni CAS de merchant | Nuevo: `application/offer_freshness.py`; D17 abierto |
| CompletionService | Evidencia de autorización, claim, idempotencia, recibos/reconciliación | Commit HNP mediante solo claim HP | Existe: [completion.py](../../src/commerce_agent/completion.py); pago delegado ocurre antes de claim y debe revisarse para HNP |
| GovernanceEvidenceStore | Historial, replay y enlace transaccional con transición | Logs OPA ni checkpoints como autoridad | Nuevo; adaptar [sqlite.py](../../src/commerce_agent/persistence/sqlite.py) con UoW |
| Merchant | Precio/stock/términos y revisión actual, orden e idempotencia; verificación propia AP2 | Decisión del agente ni PSP | Servicio externo; frontera local [ACPClient](../../src/commerce_agent/acp/client.py) |
| Payment Provider | Alcance/uso del token sintético, confirmación/recibo, mandato de pago | Orden/stock ni delegación HNP por existir HP | Servicio externo; frontera [delegated_payment.py](../../src/commerce_agent/delegated_payment.py) |
| Verificadores AP2 | Firmas, trusts, vigencia, hashes, vínculos y constraints perfilados | Readiness OPA ni estado de revocación local | HP existe: [ap2.py](../../src/commerce_agent/ap2.py); HNP pendiente H01/H03 |
| Store transaccional de delegaciones | Consentimiento versionado, epoch, reserva, uso y compromiso/CAS | Un allow de política ni transacción global | Nuevo; único dueño local de autoridad HNP |

No se propone otro repositorio ni acceso del agente a la base de merchant/proveedor.
Ninguno de los módulos nombrados como nuevos existe por aparecer en esta matriz.

## 8. D11, D17, H01 y D23: qué queda abierto

| Decisión | Se puede acordar ahora, sujeto a revisión humana | Evidencia que falta |
| --- | --- | --- |
| D11 | Puerto síncrono, entradas/razones y equivalencia lógica de motores | [ADR](adr-policy-engine-v1.md): benchmark y fallos; versión/budgets; OPA sigue candidato recomendado, no elegido |
| D17 | Cambio material requiere nuevo clic HP; cambio técnico no amplía derechos; HNP reevalúa scope | Contrato de refresh/TTL, digest material, preservar JWT original frente a reemplazo y prueba end-to-end sin bucle |
| H01 | Owner/grant/epoch/uso/operación, scopes semánticos, fail-closed ante unknown | Perfil de claims, cnf, chains, hash JWT original, constraints estándar/propios y verificadores interoperables |
| D23 | Evaluación fuera de SQL; evidencia+transición/CAS juntas; no transacción global | Esquema/migraciones/UoW, reserva single-use, carreras/crash/recovery y outbox antes del efecto |

H01 no bloquea diseñar un ID de grant, un límite o una razón. Sí bloquea declarar
`OpenProofFacts/ClosedProofFacts` validados o habilitar HNP. No rellenar refs/perfiles
desconocidos con valores de ejemplo en producción.

Propuesta D17 preferida por roadmap: refresh inocuo preserva revisión, contenido y
bytes firmados/consentidos; `observed_at` separado. Alternativa: nueva firma y vínculo
material explícito al clic. Ambas necesitan prueba; el contrato no presupone resuelto
ese vínculo. Cambio de precio, envío, destino, variante o vigencia es material; un
timestamp técnico aislado no lo es. Aun con términos iguales, expiración/stock impiden
avanzar. Una recotización técnicamente inocua no debe crear otro intento HP.

Propuesta D23: D2-HNP evalúa fuera de SQL; UoW valida versiones, guarda evidencia y
reserva. Solo después se materializa la cadena recuperable. D3 reevalúa fuera de SQL;
UoW compara epoch/reserva/versión, guarda D3 y compromete outbox con la misma operación.
Antes de commit, revocación gana e impide despacho; después, la operación queda en
curso, conserva identidad y se reconcilia. Incluir el primer envío al PSP que pueda
consumir autoridad dentro del alcance del compromiso, no solo `merchant.complete`.
Una garantía de rechazo remoto inmediato tras revocar exigiría otro contrato online;
no se promete. Fallos de firma/persistencia/reinicio no permiten generar cierres nuevos
si hay una presentación previa incierta. HP también necesita intento y recuperación
de firma/aprobación/estado; las escrituras actuales separadas no acreditan D23.

## 9. Modos y mapa incremental propuesto

| Mecanismo | off | shadow | enforce |
| --- | --- | --- | --- |
| PDP/evidencia añadidos | No invocados | Evaluar/observar; deny/fallo no cambia conducta base | Deny/fallo bloquea siguiente efecto |
| Identidad, elegibilidad, HP/AP2, presupuesto, idempotencia | Se conservan | Se conservan | Se conservan |
| Refresh con efecto sobre merchant | Base B0 intacta | No mutar; usar observación/replay o condición experimental separada | Solo contrato D17 revisado, con mecanismo identificado |
| HNP autoridad/uso/revocación/cripto | Exige nueva base HNP equivalente | Nunca se deshabilitan | Obligatorios además del PDP |

Modo fijado por episodio; enforce nunca degrada automáticamente a shadow/off.
Shadow con evidencia perdida contabiliza pérdida, no inventa un registro exitoso.
Timeouts/errores del observador añadido deben estar acotados sin alterar efectos
comerciales de B0; su sobrecosto temporal se mide por separado.

Mapa para tickets posteriores, no autorización de código en EC-GOV-01:

1. **Primer incremento sin H01:** modelos/puerto/razones puros D1, canonicalización
   y evaluador de referencia de prueba POL-01/02. Rutas nuevas propuestas
   `governance/{models,ports,reasons,canonical}.py` y pruebas nuevas offline.
   Ninguna importación desde runtime ni cambio de compra, DB o B0. Fuente reutilizada:
   [intención](../../src/commerce_agent/domain/intent.py) y
   [selección](../../src/commerce_agent/domain/selection.py). La referencia Python
   es oráculo de contrato, no elección de motor para producción experimental.
2. Tras revisión del contrato, D11/D18/D23: ledger/UoW y ensamblado D1; composición
   en [runtime.py](../../src/commerce_agent/runtime.py) e inserción en start;
   empezar offline/shadow con preservación de base. No afirmar que D38 esté cerrado.
3. D17/D19 y D23 HP: intentos, frescura y D2 antes de approve; luego completion
   con evidencia correlacionada. No esperar H01 para trabajos HP independientes,
   pero no omitir sus propias dependencias.
4. H01/H02/H03/H04/H05: contratos HNP y verificadores; luego D2/D3, UoW/outbox y
   worker. Recepción HNP y frescura en servicios externos requieren tickets propios.

El primer incremento es aislado y reversible, no una integración B2 ni excepción
general al gate D38. Su aceptación y pruebas futuras están en la
[matriz](policy-acceptance-v1.md). El calendario académico no cambia los gates.
