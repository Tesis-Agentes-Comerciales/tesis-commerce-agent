# EXP-01 — Inventario del resto (propuesto, no ejecutado)

Lectura de matrix.json y referencias de la base 65183d5; la cobertura histórica es code_reading_only, no ejecución nueva.
El primer lote contiene MA24 HNP e INF30/31 HP. Sus otros modos/UX siguen pendientes; el inventario siguiente cubre los otros 30 escenarios (34 unidades mínimas por las cinco celdas UX03).
Prioridad cualitativa según reutilización observada y dependencias, no estimación de tiempo validada.

## A — siguiente lote recomendado; sin pagos ni LLM

| Escenario | Dependencia y distinción obligatoria | Referencia existente declarada |
| --- | --- | --- |
| CT15 | Reutilizar reprice_offer y control de presupuesto; fijar mutación entre feed y creación, ambos lados del límite. | `tests/test_purchase_service.py::test_authoritative_total_above_hard_budget_cancels`; `tests/integration/test_live_hnp.py::test_hnp_price_over_the_limit_escalates_without_signing` |
| CT14 | alter_offer_fixture después de preparar HP; observar revalidación antes de aprobación; no completar. | `tests/test_approval.py::test_changed_total_invalidates_approval`; `tests/integration/test_live_b2_governed_purchase.py::test_b2_merchant_refusal_fails_the_purchase_and_frees_the_buyer` |
| CT13 | HnpAgent + mutación de stock después del consentimiento; fallback únicamente si existe autoridad explícita. | `tests/integration/test_live_b2_governed_purchase.py::test_b2_merchant_refusal_fails_the_purchase_and_frees_the_buyer`; `tests/integration/test_live_hnp.py::test_hnp_price_over_the_limit_escalates_without_signing` |
| CT16 | edit_offer(shipping) y guardas de frescura; distinguir cambio de oferta del total ya preparado. | `tests/integration/test_live_b2_d2_authorization.py::test_s04_freshness_refuses_before_d2_and_the_next_purchase_is_authorized` |
| UX10 | Consentimiento HNP de una unidad y entrada tipada de dos; falta comprobar estímulo exacto y límite. | `tests/test_purchase_service.py::test_checkout_cannot_add_another_item_after_delivery_update` |

## B — reutilización parcial; fijar fase/contrato antes de ejecutar

| Escenario | Dependencia y distinción obligatoria | Referencia existente declarada |
| --- | --- | --- |
| MA22 | No existe edición in-place: acordar revocar y consentir un presupuesto nuevo sin reutilizar autoridad vieja. | `tests/delegation/test_delegation_execution.py::test_revoking_before_the_commitment_leaves_no_outbox` |
| MA23 | Mismo límite de contrato: revocar y crear alcance nuevo que excluya la variante. | `tests/delegation/test_delegation_execution.py::test_revoking_before_the_commitment_leaves_no_outbox` |
| UX08 | Nueva propuesta A→B y consentimiento; falta recorrido exacto y comprobar que aprobación A no sirve para B. | `tests/test_api.py::test_pending_purchase_can_be_canceled_through_api`; `tests/test_approval.py::test_changed_total_invalidates_approval` |
| UX09 | Barrera de búsqueda y cancelación antes de nuevos efectos; falta entrada de cancelación concurrente. | `tests/test_api.py::test_pending_purchase_can_be_canceled_through_api` |
| CT18 | Dos actores y última unidad; el consumo por segundo comprador puede requerir compra completa adicional autorizada. | `tests/integration/test_live_hnp.py::test_hnp_two_triggers_at_once_make_one_order` |
| CT19 | Mutación controlada de identidad/desaparición; no confundir stock cero con variante inexistente. | `tests/test_purchase_service.py::test_model_can_only_select_a_deterministically_eligible_candidate` |
| INF33 | Revisor existente con fallo/JSON inválido en frontera del modelo; no usar un modelo pagado ni confundirlo con revisión semántica. | `tests/delegation/test_hnp_model_reviewer.py::test_a_failed_or_invalid_answer_is_an_interpretation_error` |

## C — dependen de frontera LLM; no autorizados como comprensión semántica

| Escenario | Dependencia y distinción obligatoria | Referencia existente declarada |
| --- | --- | --- |
| INF27 | Inyectar 429 en transporte existente y medir cotas; no requiere proveedor pagado, pero falta cableado al flujo completo. | `tests/test_model_requests.py::test_the_delegated_choice_counts_the_sdk_retry` |
| INF28 | Inyectar insufficient_quota separado de 429 transitorio; falta clasificación completa y efecto cero. | Sin referencia suficiente en la matriz. |
| INF29 | Fallo de red antes de selector mediante transporte existente; falta episodio completo de abstención. | `tests/delegation/test_hnp_model_selector.py::test_a_failed_call_is_an_interpretation_error` |
| UX01 | Aclaración de solicitud vaga: sin LLM real solo se puede evaluar el contrato/control, no comprensión. | `tests/test_conversation_router.py::test_broad_category_requires_candidate_clarification` |
| UX02 | Referente entre variantes: exige evaluar interpretación real; un selected_variant_id preparado no es evidencia. | `tests/test_conversation_router.py::test_router_turns_ungrounded_purchase_into_clarification` |
| UX07 | Prohibición frente a preferencia: falta representación/interpretación determinista de exclusión. | `tests/evaluation/test_hnp_review_episode.py::test_a_choice_not_confirmed_is_hr02_s_registered_escalation` |
| UX11 | Moneda/magnitud ambiguas: no inventar intención tipada para simular aclaración del Agent. | Sin referencia suficiente en la matriz. |
| UX12 | No hay fecha fiable Merchant: evaluar abstención/explicación sin inventar promesa de entrega. | Sin referencia suficiente en la matriz. |
| CT21 | Probar primero que el campo tercero llega al prompt; una fixture de prompt no prueba resistencia del modelo. | `tests/test_purchase_service.py::test_model_can_only_select_a_deterministically_eligible_candidate` |

## D — dependencia UX03/P3 pendiente; no implementar en EXP-01

| Escenario | Dependencia y distinción obligatoria | Referencia existente declarada |
| --- | --- | --- |
| UX03 | Cinco celdas: falta representación de ramas/límites y protocolo Merchant aceptado; oráculo actual sintético. | `tests/test_selection.py::test_preferences_rank_matches_before_lower_observed_price`; `tests/test_selection.py::test_budget_uses_observed_feed_amount_as_a_filter_only` |
| UX04 | Prioridad entre ramas condicionales; ranking ordinario no acredita esa capacidad. | `tests/test_selection.py::test_preferences_rank_matches_before_lower_observed_price` |
| UX05 | Dos ramas incumplidas y prohibición de C; no sustituir por catálogo vacío. | `tests/test_api.py::test_no_candidate_returns_catalog_alternatives_instead_of_an_error` |
| UX06 | Fallback autorizado con límite independiente; no atribuir rechazo global de presupuesto a este estímulo. | `tests/test_purchase_service.py::test_authoritative_total_above_hard_budget_cancels`; `tests/integration/test_live_hnp.py::test_hnp_price_over_the_limit_escalates_without_signing` |
| CT20 | Decisión fallback B persistida y retorno de A; depende del flujo condicional. | Sin referencia suficiente en la matriz. |

## E — capacidad/alcance de efectos sin resolver

| Escenario | Dependencia y distinción obligatoria | Referencia existente declarada |
| --- | --- | --- |
| CT17 | Cupones/descuentos no soportados por update ACP; no simular su expiración como integración real. | Sin referencia suficiente en la matriz. |
| INF32 | PSP modela approved/declined, no fondos insuficientes ni saldo; no renombrar declined. Requiere decisión de contrato. | `tests/integration/test_live_purchase.py::test_b0_declined_payment_leaves_the_checkout_open_and_no_order` |
| MA25 | Existe carrera de revocación posterior al compromiso; ambos órdenes exactos requieren barreras y autorización si se completa compra nueva. | `tests/delegation/test_delegation_execution.py::test_revoking_before_the_commitment_leaves_no_outbox`; `tests/integration/test_live_hnp.py::test_hnp_revocation_after_the_commit_is_in_flight_and_the_order_stands` |
| MA26 | Reutiliza revocación post-compromiso, pero nuevo pago completo fuera del primer lote requiere autorización; no hay reembolso demostrado. | `tests/integration/test_live_hnp.py::test_hnp_revocation_after_the_commit_is_in_flight_and_the_order_stands` |

## Recomendación

Primero cerrar los resultados EXP-01. Después proponer CT15 y CT14: mutación de precio antes de creación y stock después de checkout HP, sin aprobar/pagar ni LLM. CT13 es el siguiente candidato HNP con abstención si se agota, sin asumir fallback.
No iniciar ninguno de estos casos en esta unidad. No ampliar arquitectura ni modificar producto para obtener passed.
