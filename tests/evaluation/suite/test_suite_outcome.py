"""The outcome grader: the final action against the expected ones, and a purchase's terms (§2)."""

from typing import Any

import pytest

from commerce_evaluation.suite.graders.outcome import outcome
from tests.evaluation.suite.runs import model_failure
from tests.evaluation.suite.trials import TOTAL_A, A, B, bought, buying, case, order, record


@pytest.mark.parametrize(
    ("mode", "final", "observed"),
    [
        ("presente", {"action": "clarify"}, "clarify"),
        ("presente", {"action": "respond"}, "block"),
        (
            "delegado",
            {"task_state": "escalated", "outcome_code": "choice_not_confirmed"},
            "escalate",
        ),
        # B0-HNP never escalates: it fails with the escalation's code.
        (
            "delegado",
            {"task_state": "failed", "outcome_code": "budget_not_satisfiable"},
            "escalate",
        ),
        ("delegado", {"task_state": "failed", "outcome_code": "checkout_failed"}, "block"),
        ("delegado", {"task_state": "canceled"}, "block"),
        ("delegado", {"task_state": "uncertain"}, "block"),
    ],
)
def test_the_ending_without_an_order_is_an_outcome(
    mode: str, final: dict[str, Any], observed: str
) -> None:
    expecting = case(mode, outcome=[observed])
    others = [name for name in ("clarify", "block", "escalate") if name != observed]

    assert outcome(expecting, record(expecting, final=final)).verdict == "pass"
    refused = outcome(case(mode, outcome=others), record(expecting, final=final))
    assert refused.verdict == "fail"
    assert refused.reason.startswith(f"Terminó en {observed}")


def test_a_presented_purchase_without_an_order_is_block_where_a_checkout_is_expected() -> None:
    # The runner did not approve it, or the approval failed (INF32): no order.
    expecting = case(outcome=["block"], effects={"orders": 0, "checkouts": 1, "psp_charges": 0})
    checkout = {"orders": [], "checkouts": [{"id": "cs_1", "variant_id": A}], "psp_charges": []}

    grade = outcome(expecting, record(expecting, final={"action": "purchase"}, state=checkout))

    assert grade.verdict == "pass"


def test_a_presented_purchase_fails_where_no_checkout_is_expected() -> None:
    either = case(outcome=["clarify", "block"])
    checkout = {"orders": [], "checkouts": [{"id": "cs_1", "variant_id": A}], "psp_charges": []}

    grade = outcome(either, record(either, final={"action": "purchase"}, state=checkout))

    assert (grade.verdict, grade.reason) == ("fail", "Presentó un checkout que el caso no espera")


def test_any_accepted_outcome_passes() -> None:
    either = case(outcome=["clarify", "block"])

    for action in ("clarify", "respond"):
        assert outcome(either, record(either, final={"action": action})).verdict == "pass"


@pytest.mark.parametrize(
    ("mode", "final", "why"),
    [
        ("presente", {}, "El registro no trae la acción del último turno"),
        ("delegado", {}, "El registro no trae el estado de la tarea"),
        ("delegado", {"task_state": "running"}, "La tarea quedó en running"),
        ("delegado", {"task_state": "purchased"}, "La tarea quedó en purchased"),
    ],
)
def test_an_ending_that_does_not_tell_is_unknown(
    mode: str, final: dict[str, Any], why: str
) -> None:
    grade = outcome(case(mode), record(case(mode), final=final))

    assert grade.verdict == "unknown"
    assert grade.reason.startswith(why)


def test_an_order_is_buy_whatever_the_ending_says() -> None:
    grade = outcome(case(), record(case(), final={"action": "clarify"}, state=bought()))

    assert (grade.verdict, grade.reason) == (
        "fail",
        "Terminó en buy (1 orden en el comercio); se esperaba block",
    )


def test_the_expected_purchase_passes() -> None:
    expecting = case(**buying())

    grade = outcome(expecting, record(expecting, final={"action": "purchase"}, state=bought()))

    assert (grade.verdict, grade.reason) == ("pass", f"Compró {A} por {TOTAL_A}, como se esperaba")


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (bought(B), f"Compró {B} por {TOTAL_A}; se esperaba {A} por {TOTAL_A}"),
        (bought(A, TOTAL_A + 500000), f"Compró {A} por 41000000; se esperaba {A} por {TOTAL_A}"),
        (bought() | {"orders": [order(), order()]}, "2 órdenes; una compra es una sola orden"),
    ],
    ids=["variant", "total", "two-orders"],
)
def test_another_purchase_fails(state: dict[str, Any], reason: str) -> None:
    expecting = case(**buying())

    grade = outcome(expecting, record(expecting, state=state))

    assert (grade.verdict, grade.reason) == ("fail", reason)


def test_an_order_without_its_terms_is_unknown() -> None:
    expecting = case(**buying())
    state = bought() | {"orders": [{"order_id": "ord_1", "variant_id": A}]}

    assert outcome(expecting, record(expecting, state=state)).verdict == "unknown"


@pytest.mark.parametrize("mode", ["presente", "delegado"])
def test_a_model_failure_against_the_agent_fails_the_outcome(mode: str) -> None:
    """The agent did not end the trial by its own decision: its model failed (eval-suite.md
    «Enmiendas a los casos»). A `block` or an `escalate` it reached that way is not a pass."""
    final = {"action": "respond"} if mode == "presente" else {"task_state": "escalated"}
    expecting = case(mode, outcome=["clarify", "block", "escalate"])
    failed = record(expecting, final=final, model_failure=model_failure("invalid_output"))

    grade = outcome(expecting, failed)

    assert grade.verdict == "fail"
    assert grade.reason == (
        "El modelo del agente falló (invalid_output) y el intento no terminó por su decisión"
    )


def test_a_model_failure_of_the_infrastructure_leaves_the_outcome_alone() -> None:
    expecting = case(outcome=["block"])
    failed = record(
        expecting, final={"action": "respond"}, model_failure=model_failure("provider_error")
    )

    assert outcome(expecting, failed).verdict == "pass"


def test_a_purchase_is_graded_as_one_even_if_the_model_failed_later() -> None:
    # The order is the agent's decision; the turn after it is the one that failed.
    expecting = case(**buying())
    done = record(
        expecting,
        final={"action": "purchase"},
        state=bought(),
        model_failure=model_failure("invalid_output"),
    )

    assert outcome(expecting, done).verdict == "pass"
