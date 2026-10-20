"""EC-HNP-02 harness fixes: real deadlines, infrastructure faults and binding admission."""

from pathlib import Path
from threading import Event
from time import perf_counter

import pytest

from commerce_agent.hnp_experiment.adapters import (
    ScriptedBuyerAgent,
    ScriptedValidatorAgent,
    independent_assessment,
)
from commerce_agent.hnp_experiment.backend import mandatory_failures
from commerce_agent.hnp_experiment.context import FixtureContextResolver
from commerce_agent.hnp_experiment.coordinator import ReviewCoordinator
from commerce_agent.hnp_experiment.deadline import DeadlineExceeded, call_with_deadline
from commerce_agent.hnp_experiment.evidence import ExperimentalEvidence
from commerce_agent.hnp_experiment.harness import load_cases, run_case
from commerce_agent.hnp_experiment.ports import InfrastructureError, RoleOutputError

FIXTURES = Path(__file__).parent / "fixtures"
CASES = load_cases(FIXTURES / "cases.json")
BY_ID = {c.case_id: c for c in CASES}
VALID = BY_ID["valid"]


def coordinator(validator=None, buyer=None, budget_ns: int = 100_000_000):
    from commerce_agent.hnp_experiment.sink import SimulatedEffectSink

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
    import commerce_agent.hnp_experiment.coordinator as module

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
