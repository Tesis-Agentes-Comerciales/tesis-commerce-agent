"""EC-HNP-02 harness fixes: real deadlines, infrastructure faults and binding admission."""

from pathlib import Path
from threading import Event
from time import perf_counter

import pytest

from commerce_evaluation.hnp.adapters import (
    ScriptedBuyerAgent,
    ScriptedValidatorAgent,
    independent_assessment,
)
from commerce_evaluation.hnp.backend import mandatory_failures
from commerce_evaluation.hnp.context import FixtureContextResolver
from commerce_evaluation.hnp.coordinator import ReviewCoordinator
from commerce_evaluation.hnp.deadline import DeadlineExceeded, call_with_deadline
from commerce_evaluation.hnp.evidence import ExperimentalEvidence
from commerce_evaluation.hnp.harness import load_cases, run_case
from commerce_evaluation.hnp.ports import InfrastructureError, RoleOutputError

FIXTURES = Path(__file__).resolve().parents[3] / "evaluation/data/hnp"
CASES = load_cases(FIXTURES / "cases.json")
BY_ID = {c.case_id: c for c in CASES}
VALID = BY_ID["valid"]


def coordinator(validator=None, buyer=None, budget_ns: int = 100_000_000):
    from commerce_evaluation.hnp.sink import SimulatedEffectSink

    sink = SimulatedEffectSink()
    return (
        ReviewCoordinator(
            buyer=buyer or ScriptedBuyerAgent(VALID.buyer_payload),
            validator=validator,
            resolver=FixtureContextResolver(VALID.source),
            evidence=ExperimentalEvidence(),
            sink=sink,
            condition="B",
            validator_budget_ns=budget_ns,
        ),
        sink,
    )


def test_deadline_returns_value_and_propagates_role_errors():
    assert call_with_deadline(lambda: 7, 50_000_000) == 7

    def fail() -> int:
        raise RoleOutputError("VALIDATOR_OUTPUT_INVALID")

    with pytest.raises(RoleOutputError):
        call_with_deadline(fail, 50_000_000)
    for budget in (0, -1, True):
        with pytest.raises(ValueError):
            call_with_deadline(lambda: 1, budget)


def test_deadline_stops_waiting_instead_of_measuring_after_return():
    release = Event()
    began = perf_counter()
    with pytest.raises(DeadlineExceeded):
        call_with_deadline(lambda: release.wait(5), 20_000_000)
    assert perf_counter() - began < 1.0
    release.set()


def test_blocking_validator_times_out_fail_closed_and_late_answer_is_ignored():
    release, finished = Event(), Event()

    def blocked(proposal, context):
        release.wait(5)
        try:
            return independent_assessment(proposal, context)
        finally:
            finished.set()

    c, sink = coordinator(ScriptedValidatorAgent(blocked), budget_ns=20_000_000)
    began = perf_counter()
    result = c.run("blocked-validator")
    assert perf_counter() - began < 1.0
    assert result.record.assessment_status == "timeout"
    assert result.record.admission.code == "VALIDATOR_UNAVAILABLE"
    assert result.record.assessment is None
    release.set()
    assert finished.wait(5)
    # The accept that arrives after the deadline never reaches the record or the sink.
    assert not result.effect_occurred and not sink.events


@pytest.mark.parametrize(
    "error,status",
    (
        (TimeoutError("provider timeout"), "timeout"),
        (InfrastructureError("provider down"), "unavailable"),
        (RuntimeError("adapter bug"), "unavailable"),
        (RoleOutputError("VALIDATOR_OUTPUT_INVALID"), "invalid"),
    ),
)
def test_validator_failures_are_classified_without_admitting(error, status):
    def respond(proposal, context):
        raise error

    c, sink = coordinator(ScriptedValidatorAgent(respond))
    result = c.run("classified-failure")
    assert result.record.assessment_status == status
    assert result.record.admission.code == (
        "VALIDATOR_INVALID" if status == "invalid" else "VALIDATOR_UNAVAILABLE"
    )
    assert not result.effect_occurred and not sink.events


def test_absent_validator_is_infrastructure_not_timeout():
    c, sink = coordinator(None)
    result = c.run("no-validator")
    assert result.record.assessment_status == "unavailable"
    assert not sink.events


@pytest.mark.parametrize(
    "error,code",
    (
        (RoleOutputError("BUYER_OUTPUT_INVALID"), "BUYER_INVALID"),
        (OSError("down"), "BUYER_UNAVAILABLE"),
    ),
)
def test_buyer_output_errors_are_separated_from_infrastructure(error, code):
    def propose(context):
        raise error

    c, sink = coordinator(ScriptedValidatorAgent(), buyer=ScriptedBuyerAgent(propose))
    result = c.run("buyer-failure")
    assert result.record.admission.code == code
    assert result.record.proposal is None and not sink.events


def test_backend_fault_blocks_as_unavailable_not_as_denial(monkeypatch):
    import commerce_evaluation.hnp.coordinator as module

    def broken(*_):
        raise KeyError("harness bug")

    monkeypatch.setattr(module, "mandatory_failures", broken)
    c, sink = coordinator(ScriptedValidatorAgent())
    result = c.run("backend-fault")
    assert result.record.admission.code == "BACKEND_UNAVAILABLE"
    assert not result.effect_occurred and not sink.events


def test_binding_is_enforced_by_version_admission_not_by_unreachable_check():
    for case in CASES:
        resolver = FixtureContextResolver(case.source)
        result, _ = run_case(case, "A", ExperimentalEvidence())
        if result.record.proposal is not None:
            failures = mandatory_failures(result.record.proposal, resolver.resolve())
            assert "binding" not in failures
    changed, sink = run_case(BY_ID["version-after-review"], "B", ExperimentalEvidence())
    assert changed.record.admission.code == "CONTEXT_CHANGED"
    assert changed.record.admission.backend_failures == ("binding",)
    assert not changed.effect_occurred and not sink.events


def test_validator_receives_public_reviewer_facts_without_leaks():
    from commerce_evaluation.hnp.models import (
        ProposalView,
        ReviewerContext,
        TrustedReviewContext,
        wire_bytes,
    )

    seen = {}

    def validate(proposal: ProposalView, context: ReviewerContext):
        seen["wire"] = wire_bytes({"proposal": proposal, "context": context})
        seen["types"] = (type(proposal), type(context))
        return independent_assessment(proposal, context)

    c, _ = coordinator(ScriptedValidatorAgent(validate))
    assert c.run("public-facts").effect_occurred
    assert seen["types"] == (ProposalView, ReviewerContext)
    wire = seen["wire"]
    for leaked in (b"eligible_variant_ids", b"buyer_adapter_id", b"scripted-buyer", b"approved"):
        assert leaked not in wire
    # Allowlist: only the backend's selection verdict is withheld from the reviewer.
    assert set(TrustedReviewContext.model_fields) - set(ReviewerContext.model_fields) == {
        "eligible_variant_ids"
    }


def test_scripted_reviewer_derives_eligibility_like_the_backend_on_every_fixture():
    from commerce_evaluation.hnp.adapters import reviewer_eligible_variants
    from commerce_evaluation.hnp.context import assemble, reviewer_context

    for case in CASES:
        context = assemble(case.source)
        assert reviewer_eligible_variants(reviewer_context(context)) == frozenset(
            context.eligible_variant_ids
        )


def test_hidden_hard_constraint_makes_scripted_selection_unresolved():
    from commerce_agent.governance.canonical import parse_model
    from commerce_evaluation.hnp.context import (
        assemble,
        proposal_envelope,
        proposal_view,
        reviewer_context,
    )
    from commerce_evaluation.hnp.models import BuyerDraft, wire_bytes

    context = reviewer_context(assemble(VALID.source))
    hard = context.intent.hard.model_copy(update={"categories": ("phone",)})
    hidden = context.model_copy(update={"intent": context.intent.model_copy(update={"hard": hard})})
    draft = parse_model(BuyerDraft, wire_bytes(VALID.buyer_payload))
    proposal = proposal_view(proposal_envelope(assemble(VALID.source), draft, "scripted-buyer:1"))
    assessment = independent_assessment(proposal, hidden)
    assert assessment.verdict == "abstain"
    assert [c.check_id for c in assessment.checks if c.result == "unresolved"] == ["selection"]


@pytest.fixture(scope="module")
def scripted_results():
    from commerce_evaluation.hnp.evaluation import load_oracles

    evidence = ExperimentalEvidence()
    results = [run_case(case, arm, evidence)[0] for case in CASES for arm in ("A", "B")]
    return results, load_oracles(FIXTURES / "oracle.json")


def test_evaluation_records_every_deviation_and_strict_mode_aborts(scripted_results):
    from commerce_evaluation.hnp.evaluation import (
        expectation_deviations,
        score,
        verify_expected,
    )

    results, oracles = scripted_results
    assert expectation_deviations(results, oracles) == ()
    changed = dict(oracles)
    changed["valid"] = oracles["valid"].model_copy(
        update={"expected_code_b": "VALIDATOR_REJECTED", "expected_effect_b": False}
    )
    changed["expired"] = oracles["expired"].model_copy(update={"failure_checks": ("usage",)})
    found = expectation_deviations(results, changed)
    assert {(d.case_id, d.condition, d.field) for d in found} == {
        ("valid", "B", "admission_code"),
        ("valid", "B", "effect"),
        ("expired", "A", "backend_failures"),
        ("expired", "B", "backend_failures"),
    }
    with pytest.raises(ValueError, match="differs from separate oracle"):
        verify_expected(results, changed)
    summary = score(results, changed, strict=False)
    assert len(summary["deviations"]) == 4


def test_lenient_scoring_records_unpaired_cases_instead_of_aborting(scripted_results):
    from commerce_evaluation.hnp.evaluation import score

    results, oracles = scripted_results
    partial = {k: v for k, v in oracles.items() if k != "valid"}
    with pytest.raises(ValueError, match="pairing"):
        score(results, partial)
    summary = score(results, partial, strict=False)
    assert summary["conditions"]["A"]["cases"] == len(partial)
    assert {(d["case_id"], d["field"]) for d in summary["deviations"]} == {("valid", "oracle")}


def test_scripted_summary_separates_timeouts_and_infrastructure(scripted_results):
    from commerce_evaluation.hnp.evaluation import score

    results, oracles = scripted_results
    b = score(results, oracles)["conditions"]["B"]
    # validator-timeout is a timeout result; validator-unavailable is an infrastructure fault.
    assert b["validator_unavailable"] == 2
    assert b["validator_timeouts"] == 1 and b["infrastructure_errors"] == 1


def test_scripted_score_counts_model_calls_instead_of_fixing_zero(scripted_results):
    from commerce_evaluation.hnp.evaluation import score
    from commerce_evaluation.hnp.llm.trace import CallRecord, TokenUsage

    results, oracles = scripted_results
    assert score(results, oracles)["llm_calls"] == 0
    usage = TokenUsage(input_tokens=10, output_tokens=5, cached_input_tokens=0, reasoning_tokens=0)
    calls = [
        CallRecord(
            call_id=f"call-{n}",
            role="validator",
            model="m",
            transport=transport,
            latency_ms=1.0,
            status="ok",
            usage=usage,
            cost_usd=0.25,
        )
        for n, transport in enumerate(("fake", "openai-responses", "openai-responses"))
    ]
    summary = score(results, oracles, calls=calls)
    assert (summary["llm_calls"], summary["provider_calls"]) == (3, 2)
    assert summary["api_cost"] == 0.5 and summary["cost_status"] == "measured_from_provider_usage"
