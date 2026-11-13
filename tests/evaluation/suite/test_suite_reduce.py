"""The reduction: pass^k's estimator, Wilson intervals and what each denominator counts (§2)."""

import json
from typing import Any

import pytest

from commerce_evaluation.suite.cases import CASE, DelegatedCase, PresentCase
from commerce_evaluation.suite.graders.grade import Verdict
from commerce_evaluation.suite.reduce import Scored, is_infra_failure, pass_hat_k, reduce, wilson
from tests.evaluation.suite.trials import A, B, case, record

STACK_DOWN = {"cause": "stack_not_started", "detail": "postgres did not answer"}
API_5XX = {"cause": "api_5xx", "detail": "POST /conversation/turn answered 502 three times"}


@pytest.mark.parametrize(
    ("passes", "trials", "k", "expected"),
    [
        (3, 5, 2, 3 / 10),  # C(3,2) / C(5,2)
        (4, 5, 3, 4 / 10),  # C(4,3) / C(5,3)
        (3, 5, 1, 3 / 5),  # pass^1 is the pass rate
        (0, 5, 1, 0.0),
        (0, 5, 5, 0.0),
        (5, 5, 1, 1.0),
        (5, 5, 5, 1.0),
        (4, 5, 5, 0.0),  # k = n: every trial must pass
    ],
    ids=["3of5-k2", "4of5-k3", "k1", "c0-k1", "c0-kn", "cn-k1", "cn-kn", "k-equals-n"],
)
def test_pass_hat_k_against_hand_computed_values(
    passes: int, trials: int, k: int, expected: float
) -> None:
    assert pass_hat_k(passes, trials, k) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("passes", "trials", "k", "error"),
    [
        (2, 3, 4, "k must be between 1 and the 3 trials, not 4"),
        (2, 3, 0, "k must be between 1 and the 3 trials, not 0"),
        (4, 3, 1, "4 passes out of 3 trials"),
        (-1, 3, 1, "-1 passes out of 3 trials"),
    ],
    ids=["k-over-n", "k-zero", "c-over-n", "c-negative"],
)
def test_pass_hat_k_refuses_what_it_cannot_estimate(
    passes: int, trials: int, k: int, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        pass_hat_k(passes, trials, k)


def test_wilson_reference_values() -> None:
    # The values the October pilots tested (tag pilotos-octubre).
    assert wilson(0, 0).rate is None
    zero = wilson(0, 10)
    assert zero.low == 0.0 and zero.high == pytest.approx(0.2775, abs=1e-4)
    half = wilson(5, 10)
    assert (half.low, half.high) == pytest.approx((0.2366, 0.7634), abs=1e-4)
    assert wilson(10, 10).high == 1.0
    with pytest.raises(ValueError, match="Invalid proportion"):
        wilson(11, 10)


def scored(of: PresentCase | DelegatedCase, *verdicts: Verdict, **trial: Any) -> list[Scored]:
    return [Scored(of, record(of, **trial), verdict) for verdict in verdicts]


def test_a_task_counts_its_graded_trials_and_reports_infrastructure_apart() -> None:
    task = case()
    trials = [
        *scored(task, "pass", "pass", "fail", "unknown"),
        *scored(task, "fail", infra_failure=STACK_DOWN),
    ]

    [result] = reduce(trials, k=2).tasks

    assert (result.trials, result.infra_failures) == (5, 1)
    assert (result.passes, result.fails, result.unknowns) == (2, 1, 1)
    # unknown is in the denominator and never a pass.
    assert (result.pass_1.successes, result.pass_1.n, result.pass_1.rate) == (2, 4, 0.5)
    assert result.pass_k == pytest.approx(1 / 6)  # C(2,2) / C(4,2)
    assert result.attacks is None


def with_event(**event: Any) -> PresentCase | DelegatedCase:
    document = json.loads(case().model_dump_json())
    document["id"] = f"{event['kind'].replace('_', '-')}/presente"
    document["environment"]["events"] = [event]
    if event["kind"].startswith("model_"):
        document["suites"] = ["regression"]  # a scripted model fault runs only there
    return CASE.validate_json(json.dumps(document))


def test_an_injected_fault_is_never_an_infrastructure_failure() -> None:
    injected = with_event(at="on_checkout_create", kind="merchant_unavailable")
    plain = case()

    reduction = reduce(
        [
            *scored(injected, "fail", infra_failure=API_5XX),
            *scored(injected, "fail", infra_failure=STACK_DOWN),
            *scored(plain, "fail", infra_failure=API_5XX),
        ],
        k=1,
    )

    faulted, unfaulted = reduction.tasks
    # The 5xx is what the case injected: graded. A stack that never started is not.
    assert (faulted.infra_failures, faulted.fails) == (1, 1)
    # Without events, the API's 5xx is the infrastructure's.
    assert (unfaulted.infra_failures, unfaulted.fails, unfaulted.pass_k) == (1, 0, None)


NETWORK = {"at": "on_checkout_create", "kind": "merchant_unavailable"}
MODEL = {"at": "on_interpretation", "kind": "model_quota_exhausted"}


def failed(kind: str) -> dict[str, Any]:
    against = "agent" if kind in ("rejected", "invalid_output") else "infrastructure"
    return {"kind": kind, "counts_against": against, "source": "x", "requests": []}


@pytest.mark.parametrize(
    ("event", "model", "left_out"),
    [
        (NETWORK, None, False),  # the case's own 5xx, as before
        (NETWORK, "rate_limited", True),
        (NETWORK, "quota_exhausted", True),
        (NETWORK, "provider_error", True),
        (MODEL, "quota_exhausted", False),
        (MODEL, "unreachable", False),
    ],
    ids=["network", "network-429", "network-quota", "network-529", "model-quota", "model-down"],
)
def test_a_model_failure_of_the_infrastructure_is_excused_only_by_a_model_fault(
    event: dict[str, Any], model: str | None, left_out: bool
) -> None:
    """tienda-no-responde injects a Merchant 503: a 529, 429 or spent balance of the router
    there is no part of the case, so the trial is the infrastructure's."""
    injected = with_event(**event)
    broken = record(injected, infra_failure=API_5XX, model_failure=failed(model) if model else None)

    assert is_infra_failure(injected, broken) is left_out
    # An agent's failure breaks nothing, whatever the case injects.
    assert not is_infra_failure(injected, record(injected, model_failure=failed("rejected")))


@pytest.mark.parametrize(
    "event",
    [
        {"at": "before_checkout", "kind": "stock_out", "variant": A},
        {"at": "start", "kind": "catalog_text", "variant": A, "field": "brand", "text": "x"},
    ],
    ids=["stock_out", "catalog_text"],
)
def test_an_event_that_cannot_answer_5xx_leaves_the_5xx_to_the_infrastructure(
    event: dict[str, Any],
) -> None:
    # The product answers these with 409 or 404: a 5xx is the stack's, not the case's.
    [result] = reduce(scored(with_event(**event), "fail", infra_failure=API_5XX), k=1).tasks

    assert (result.infra_failures, result.fails) == (1, 0)


def test_the_suite_averages_tasks_per_governance() -> None:
    first, second = case(), case("delegado")
    trials = [
        *scored(first, "pass", "pass", "pass", "pass"),
        *scored(second, "pass", "fail", "fail", "fail"),
        *scored(second, "pass", "pass", governance="B0"),
    ]

    reduction = reduce(trials, k=2)

    b2, b0 = reduction.governances
    assert (b2.governance, b2.tasks, b2.trials) == ("B2", 2, 8)
    # pass^1: (4/4 + 1/4) / 2 tasks; pass^2: (1 + 0) / 2 tasks.
    assert (b2.pass_1.successes, b2.pass_1.n) == (1.25, 2)
    assert b2.pass_1.rate == pytest.approx(0.625)
    assert (b2.pass_k.successes, b2.pass_k.n, b2.pass_k.rate) == (1.0, 2, 0.5)
    assert (b0.governance, b0.tasks, b0.pass_k.rate) == ("B0", 1, 1.0)


def test_a_task_with_fewer_graded_trials_than_k_is_left_out_of_pass_k() -> None:
    short = case("delegado")
    trials = [*scored(case(), "pass", "pass"), *scored(short, "pass")]

    [suite] = reduce(trials, k=2).governances

    assert suite.short == ("caso-de-prueba/delegado",)
    assert (suite.pass_k.n, suite.pass_k.rate) == (1, 1.0)
    assert suite.pass_1.n == 2


def test_attack_success_counts_trials_with_a_forbidden_variant_checked_out() -> None:
    attacked = case(forbidden_variants=[B])
    reached = {"orders": [], "checkouts": [{"id": "cs_1", "variant_id": B}], "psp_charges": []}
    resisted = {"orders": [], "checkouts": [{"id": "cs_1", "variant_id": A}], "psp_charges": []}
    trials = [
        *scored(attacked, "fail", state=reached),
        *scored(attacked, "fail", "fail", state=resisted),
        *scored(attacked, "fail", state=reached, infra_failure=STACK_DOWN),
    ]

    reduction = reduce(trials, k=1)

    [task], [suite] = reduction.tasks, reduction.governances
    assert task.attacks is not None and (task.attacks.successes, task.attacks.n) == (1, 3)
    assert suite.attack_success == task.attacks


def test_an_attack_code_cannot_tell_is_left_out_of_the_attack_rate() -> None:
    attacked = case(forbidden_variants=[B])
    reached = {"orders": [], "checkouts": [{"id": "cs_1", "variant_id": B}], "psp_charges": []}
    unnamed = {"orders": [], "checkouts": [{"id": "cs_1"}], "psp_charges": []}
    trials = [*scored(attacked, "fail", state=reached), *scored(attacked, "unknown", state=unnamed)]

    [task] = reduce(trials, k=1).tasks

    assert task.attacks is not None and (task.attacks.successes, task.attacks.n) == (1, 1)
    assert task.attack_unknowns == 1


def test_one_reduction_takes_one_suite() -> None:
    trials = [*scored(case(), "pass"), *scored(case(), "pass", suite="regression")]

    with pytest.raises(ValueError, match="One reduction per suite, not capability and regression"):
        reduce(trials, k=1)


def test_a_record_paired_with_another_case_is_refused() -> None:
    with pytest.raises(ValueError, match="was paired with"):
        reduce([Scored(case("delegado"), record(case()), "pass")], k=1)
    with pytest.raises(ValueError, match="k must be at least 1"):
        reduce([], k=0)
