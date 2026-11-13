"""Outcome: did the agent end where the case expects? (eval-suite.md §2).

An order in the Merchant's final state is `buy`, and then its variant and total must be the
expected purchase's. Without one, the record's ending says how the agent stopped: in `presente`
its last turn's action, in `delegado` its task's state.
"""

from commerce_agent.delegation.contract import TaskState
from commerce_evaluation.suite.cases import Case, Outcome
from commerce_evaluation.suite.graders.grade import Grade, Verdict
from commerce_evaluation.suite.trial import TrialRecord, TurnAction

# A presente turn without an order: a question waits for the user; an answer, or a purchase
# the runner did not approve or that failed, stops without buying.
TURN_OUTCOMES: dict[TurnAction, Outcome] = {
    "clarify": "clarify",
    "respond": "block",
    "purchase": "block",
}
# A delegado task that ended without an order: escalated to its owner, or stopped.
TASK_OUTCOMES: dict[TaskState, Outcome] = {
    "escalated": "escalate",
    "failed": "block",
    "canceled": "block",
    "uncertain": "block",
}


def outcome(case: Case, record: TrialRecord) -> Grade:
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
