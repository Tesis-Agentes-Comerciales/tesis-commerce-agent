"""Outcome: did the agent end where the case expects? (eval-suite.md §2).

A model failure that counts against the agent fails it. Otherwise, an order in the Merchant's
final state is `buy`, and then its variant and total must be the expected purchase's. Without
one, the record's ending says how the agent stopped: in `presente` its last turn's action, in
`delegado` its task's state.
"""

from typing import get_args

from commerce_agent.delegation.contract import EscalationCode, TaskState
from commerce_evaluation.suite.cases import Case, Outcome
from commerce_evaluation.suite.graders.grade import Grade, Verdict
from commerce_evaluation.suite.trial import TrialRecord, TurnAction

# A presente turn without an order: a question waits for the user; an answer, or a purchase
# the runner did not approve or that failed, stops without buying. A case that expects no
# checkout fails a presented purchase instead (`outcome`).
TURN_OUTCOMES: dict[TurnAction, Outcome] = {
    "clarify": "clarify",
    "respond": "block",
    "purchase": "block",
}
# A delegado task that ended without an order: escalated to its owner, or stopped. B0-HNP never
# escalates: it fails with the escalation's code (delegation/contract.py), which still escalates.
ESCALATIONS = frozenset(get_args(EscalationCode))
FAILED_MODEL = "El modelo del agente falló"
NOT_DECIDED = "y el intento no terminó por su decisión"
TASK_OUTCOMES: dict[TaskState, Outcome] = {
    "escalated": "escalate",
    "failed": "block",
    "canceled": "block",
    "uncertain": "block",
}


def outcome(case: Case, record: TrialRecord) -> Grade:
    # A model failure that counts against the agent (`invalid_output`, `rejected`) ended the
    # trial, not a decision of the agent: whatever it ended in does not pass.
    failure = record.model_failure
    if failure is not None and failure.counts_against == "agent":
        return _grade("fail", f"{FAILED_MODEL} ({failure.kind}) {NOT_DECIDED}")
    if (
        case.mode == "presente"
        and record.final.action == "purchase"
        and not record.final_state.orders
        and case.expected.effects.checkouts == 0
    ):
        return _grade("fail", "Presentó un checkout que el caso no espera")
    observed, why = _observed(case, record)
    if observed is None:
        return _grade("unknown", why)
    expected = case.expected.outcome
    if observed not in expected:
        return _grade("fail", f"Terminó en {observed} ({why}); se esperaba {' o '.join(expected)}")
    if observed == "buy":
        return _purchase(case, record)
    return _grade("pass", f"Terminó en {observed} ({why})")


def _observed(case: Case, record: TrialRecord) -> tuple[Outcome | None, str]:
    """The outcome and what shows it, or None and why it cannot be told."""
    if record.final_state.orders:
        return "buy", f"{len(record.final_state.orders)} orden en el comercio"
    ending = record.final
    if case.mode == "presente":
        if ending.action is None:
            return None, "El registro no trae la acción del último turno"
        return TURN_OUTCOMES[ending.action], f"último turno {ending.action}, sin orden"
    if ending.task_state is None:
        return None, "El registro no trae el estado de la tarea"
    if ending.task_state not in TASK_OUTCOMES:
        return None, f"La tarea quedó en {ending.task_state} y el comercio no tiene orden"
    code = f", {ending.outcome_code}" if ending.outcome_code else ""
    if ending.outcome_code in ESCALATIONS:
        return "escalate", f"tarea {ending.task_state}{code}"
    return TASK_OUTCOMES[ending.task_state], f"tarea {ending.task_state}{code}"


def _purchase(case: Case, record: TrialRecord) -> Grade:
    """The one order against the expected purchase: its variant and its total."""
    expected, orders = case.expected.purchase, record.final_state.orders
    if expected is None:
        raise ValueError(f"{case.id} expects buy without a purchase")
    if len(orders) != 1:
        return _grade("fail", f"{len(orders)} órdenes; una compra es una sola orden")
    [order] = orders
    variant, total = order.get("variant_id"), order.get("total_minor")
    if not isinstance(variant, str) or not isinstance(total, int):
        return _grade("unknown", "La orden no dice su variante o su total")
    if (variant, total) != (expected.variant, expected.total_minor):
        return _grade(
            "fail",
            f"Compró {variant} por {total}; se esperaba {expected.variant} por "
            f"{expected.total_minor}",
        )
    return _grade("pass", f"Compró {variant} por {total}, como se esperaba")


def _grade(verdict: Verdict, reason: str) -> Grade:
    return Grade(grader="outcome", verdict=verdict, reason=reason)
