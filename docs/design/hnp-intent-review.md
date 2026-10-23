# Revisor de intención en compras delegadas (HNP)

Plan aprobado por Yair el 22-oct-2026 («Sí, plan acotado»): un segundo agente revisa la elección
del selector, detrás de un interruptor apagado por defecto. Extiende `hnp-governed.md` §3.3, §8.1 y
§10, y `run-traceability.md` §4.4. El experimento que lo mide es `docs/design/hnp-review-pilot.md`
(PR R2).

## 1. Problema

D1, D2-HNP y D3-HNP cuidan los **límites** del grant: variantes, comercio, monto, destino y
vigencia. Ninguno lee la **intención** que el comprador escribió en `preference_text`. Si el grant
permite el Pixel 8 negro y el OnePlus 11, y el comprador dice «Quiero el Google Pixel 8 negro. No
compres otro modelo.», un selector que elige el OnePlus pasa todas las reglas y compra. HNP-02
(20-oct, offline) mostró que un validador LLM que repite las reglas no agrega capturas; el hueco que
queda es justo ese: una elección dentro del grant que contradice lo pedido.

## 2. Qué cubre y qué no

- **Cubre:** si la variante que eligió el selector honra lo que pidió el comprador en
  `preference_text`, dentro de la lista corta.
- **No cubre:** los límites. El revisor no decide ni reemplaza a D1/D2/D3, al presupuesto ni al
  cierre; una elección fuera de la lista corta sigue siendo de K12 (POL-01 en B2-HNP), y el revisor
  ni siquiera se llama. Tampoco elige otra variante ni reintenta: solo confirma o detiene.
- **Solo B2-HNP:** `AGENT_HNP_REVIEWER=llm` exige `enforce`. B0-HNP no revisa (no tiene escalamiento).

## 3. Entradas e independencia

`HnpIntentReviewer.review(shortlist, intent, preference_text, chosen_variant_id) -> IntentReview`
(`application/hnp_intent_review.py`). Ve lo mismo que el selector, más el id elegido:

- la lista corta como observaciones estructuradas del catálogo (`_candidate_payload` del canal
  cerrado: título, variante, categorías, condiciones, opciones y precio observado, con
  `preference_score` y `matched_preferences`), nunca descripciones libres del feed;
- la intención derivada del grant y `preference_text`.

**No** recibe el razonamiento del selector (su `assistant_message`): el selector solo devuelve un id,
y así el juicio es independiente. El modelo es el mismo `OPENAI_MODEL`, con otro prompt.

## 4. Contrato

```text
IntentReview(decision: "confirmed" | "not_confirmed", reason: ReviewReason)
ReviewReason = "matches_request" | "contradicts_preference" | "outside_request" | "unclear"
             | "review_unavailable"
```

`confirmed` va solo con `matches_request`, y `not_confirmed` con cualquiera de los otros cuatro.
`review_unavailable` nunca sale del modelo: lo pone el ejecutor.

`ModelHnpIntentReviewer` (`llm/intent_review.py`), prompt `hnp-intent-review-v1`: salida
estructurada con `decision` y `reason` (sin `review_unavailable`), `extra="forbid"`, `strict`. Un par
incoherente, una respuesta sin salida, un error del SDK o un timeout levantan
`IntentInterpretationError`.

## 5. Falla cerrado

Una excepción del revisor, un timeout o un resultado que no es un `IntentReview` válido dan
`not_confirmed` / `review_unavailable`. Nunca se compra sin una confirmación explícita.

## 6. Dónde corre y desenlace

En `HnpExecutionService._execute`, después del selector y del `_hold` del lease, y antes de `start`
(D1, el checkout y cualquier POST):

1. Solo si hay revisor y el id elegido está en la lista corta.
2. Crea la fila de la compra HNP en `received` (la misma que crearía `start`, que es idempotente),
   para que la traza exista también cuando el revisor detiene la corrida.
3. Llama al revisor dentro de `model_request_scope(run_id=...)`.
4. Anota `choice_reviewed` en el ledger con una unidad de trabajo cercada por el lease, que además lo
   renueva; con el lease perdido, la corrida para sin escribir nada más.
5. `confirmed`: sigue a `start` como siempre. `not_confirmed`: termina
   `escalated(choice_not_confirmed)` y cierra la compra en `canceled`. 0 checkouts, 0 POST al
   comercio, 0 firmas; el grant queda `revoked(escalated)` y el comprador ve el aviso de
   escalamiento.

`choice_not_confirmed` entra en `EscalationCode` y en `_ESCALATIONS`. Texto (§8.3):
`escalation.choice_not_confirmed` = «No pude confirmar que la opción elegida sea la que pediste.», con
el sufijo `suffix_before_presentation`.

Un re-claim después de crear la compra y antes de `start` termina `failed(state_conflict)`, como un
corte dentro de `start` (§4.5): nunca compra.

## 7. Evidencia

Cada revisión, confirme o no, es un evento `choice_reviewed` de `delegation_events` (actor `service`)
con `{decision, reason, prompt_id, model}`. La llamada al modelo se mide como `model.hnp_review` en
`stage_timings` (con tokens) y queda en `model_requests` bajo el `run_id` de la tarea, con su entrada
y su salida estructurada.

Con el revisor apagado no hay eventos `choice_reviewed`, ni filas `model.hnp_review`, ni compra
creada antes de `start`: el flujo es el de main byte a byte.

Una base creada antes de este cambio no admite el evento ni el código en sus `CHECK`; con
`AGENT_HNP_REVIEWER=llm` el arranque la rechaza en vez de fallar a mitad de una corrida.

## 8. Traza

Paso nuevo `choice_reviewed`, actor `agent`, con `decision` y `reason`; `state` es `done` si
confirma y `blocked` si no. Va después de `task_activated` (HNP no tiene paso `selection`) y antes de
la decisión D1; en un empate de milisegundo, antes de `policy_decision` y `checkout_created`.

- Título «Revisión de la elección»; detalle «Confirmada: …» o «No confirmada: …» con el motivo en
  español.
- No es un control del recorrido normal (`DELEGATED_CONTROL_ORDER`): solo existe con el revisor
  encendido, y una fila «No se ejecutó» suya mentiría con él apagado, como la frescura o D2.
- `escalated` acepta `choice_not_confirmed` y tiene su causa en la UI.
- Una traza sin decisiones de política y con `choice_reviewed` informa `policy_mode: "enforce"`: el
  revisor solo corre en B2-HNP.
- Fixture `ui/src/storybook/trace-fixtures/hnp/enforce-review-not-confirmed.json` con su historia;
  `tests/test_hnp_trace_contract.py` la valida con Pydantic.

## 9. Configuración

| Variable | Valores | Regla |
| --- | --- | --- |
| `AGENT_HNP_REVIEWER` | `off` \| `llm`, `off` | `llm` exige `AGENT_HNP_ENABLED=true`, `AGENT_GOVERNANCE_MODE=enforce`, `OPENAI_API_KEY` y `OPENAI_MODEL` |

`/health` agrega `hnp_reviewer: "off" | "llm"` junto a los demás campos `hnp_*` (solo con HNP
encendido). `uv run demo-stack --mode b2-hnp --hnp-reviewer {off,llm}` (por defecto `off`; otro modo
lo rechaza) se lo pasa solo al agente, comprueba que `/health` reporte el mismo valor antes de abrir
la UI, y el resumen dice «revisor llm».

## 10. Criterios de aceptación

- Apagado: las pruebas HNP existentes pasan igual y no hay `choice_reviewed`.
- Un revisor que confirma: la compra sigue, `choice_reviewed(confirmed)` y el paso en su lugar.
- Uno que no confirma: `escalated(choice_not_confirmed)`, 0 checkouts y 0 POST, con evento y paso.
- Excepción o resultado inválido: `not_confirmed` / `review_unavailable`.
- El revisor no recibe el razonamiento del selector ni descripciones libres del catálogo.
- Arranque y `/health`, `ModelHnpIntentReviewer` con transporte falso, `demo-stack` y el contrato
  UI/backend con la fixture nueva.

## 11. Riesgos

- **Bloqueos falsos:** un revisor demasiado estricto escala compras correctas. Lo mide H-utility del
  piloto (HR01 y HR03).
- **Latencia y costo:** una llamada más al modelo por compra delegada; se reporta p50/p95 de
  `model.hnp_review`.
- **Costo medido:** `flow_trace.py` lee solo `model.hnp_select`; el piloto (PR R2) debe sumar
  `model.hnp_review` al costo y a la latencia de cada episodio.
- **Evidencia:** el verificador HNP-V1 (`verify_evidence_hnp.py`) toma la fila de la compra como señal
  de que D1 corrió. Una corrida detenida por el revisor tiene compra sin D1, así que el verificador
  debe tratar `choice_not_confirmed` como un terminal anterior a D1 (PR R2).
