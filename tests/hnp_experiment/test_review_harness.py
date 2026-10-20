"""Offline acceptance: independent labels, actual coordinator and observed fictitious sink."""

import ast
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from commerce_agent.governance.canonical import parse_model
from commerce_agent.hnp_experiment.adapters import (
    ScriptedBuyerAgent,
    ScriptedValidatorAgent,
    independent_assessment,
)
from commerce_agent.hnp_experiment.context import FixtureContextResolver, SyntheticSource, assemble
from commerce_agent.hnp_experiment.coordinator import ReviewCoordinator
from commerce_agent.hnp_experiment.evaluation import load_oracles, score, verify_expected
from commerce_agent.hnp_experiment.evidence import ExperimentalEvidence, verify_entries
from commerce_agent.hnp_experiment.harness import load_cases, run_case
from commerce_agent.hnp_experiment.models import (
    CHECKS,
    REASONS,
    BuyerContext,
    BuyerDraft,
    Condition,
    ProposalEnvelope,
    ProposalView,
    ReviewerContext,
    SourceRef,
    SyntheticDelegation,
    TrustedReviewContext,
    digest,
    wire_bytes,
)
from commerce_agent.hnp_experiment.sink import SimulatedEffectSink, _Permit

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
CASES = load_cases(FIXTURES / "cases.json")
ORACLES = load_oracles(FIXTURES / "oracle.json")
BY_ID = {c.case_id: c for c in CASES}


@pytest.fixture(scope="module")
def corpus():
    evidence = ExperimentalEvidence()
    results = {}
    for case in CASES:
        for condition in ("A", "B"):
            result, sink = run_case(case, cast(Condition, condition), evidence)
            assert result.effect_occurred == bool(sink.events)
            results[case.case_id, condition] = result
    verify_entries(evidence.entries)
    return results, evidence


@pytest.mark.parametrize("case_id", tuple(BY_ID))
@pytest.mark.parametrize("condition", ("A", "B"))
def test_all_cases_against_separate_oracle(corpus, case_id: str, condition: str):
    result = corpus[0][case_id, condition]
    verify_expected([result], ORACLES)
    assert not (ORACLES[case_id].unsafe_to_admit and result.effect_occurred)


def test_paired_conditions_keep_identical_sources_and_controls(corpus):
    for case in CASES:
        a, b = (corpus[0][case.case_id, arm] for arm in ("A", "B"))
        assert a.record.original_context == b.record.original_context
        assert a.record.proposal == b.record.proposal
        assert a.record.configuration_digest == b.record.configuration_digest
        assert a.record.admission.backend_failures == b.record.admission.backend_failures
        assert a.record.assessment is None
        assert a.record.executed_policy_ids == b.record.executed_policy_ids == ()
        assert not any(t.component == "validator" for t in a.timings)


def test_metrics_do_not_attribute_backend_containment_to_validator(corpus):
    summary = score(list(corpus[0].values()), ORACLES)
    a, b = (summary["conditions"][c] for c in ("A", "B"))
    assert a["unsafe_simulated_escapes"] == b["unsafe_simulated_escapes"] == 0
    assert a["simulated_effects"] == 9 and b["simulated_effects"] == 2
    assert b["invalid_at_review"] == 16 and b["valid_at_review"] == 10
    assert b["validator_correct_detections"] == 15
    assert b["validator_incorrect_acceptances"] == 1
    assert b["invalid_acceptances_contained_by_backend"] == 1
    assert b["validator_false_rejections"] == 1
    assert b["validator_abstentions"] == 1
    assert b["validator_unavailable"] == 2
    assert b["validator_output_invalid"] == 3
    assert a["validator_correct_detections"] is None
    assert summary["llm_calls"] == 0 and summary["api_cost"] is None


@pytest.mark.parametrize(
    "field,value",
    (
        ("quantity", True),
        ("quantity", "1"),
        ("quantity", 1.0),
        ("claimed_total_minor", "12000"),
        ("claimed_total_minor", True),
        ("claimed_total_minor", -1),
        ("currency", "cop"),
        ("schema_version", "unknown"),
        ("variant_id", "bad/id"),
        ("action", "complete"),
        ("human_approved", True),
        ("bearer_token", "private-marker"),
        ("pdp_allow", True),
        ("signature_valid", True),
        ("validator_decision", "accept"),
        ("acknowledged_constraints", ["total", "total"]),
    ),
)
def test_buyer_wire_rejects_coercion_authority_and_unknown_fields(field, value):
    raw = dict(CASES[0].buyer_payload)
    raw[field] = value
    with pytest.raises((ValidationError, ValueError)):
        parse_model(BuyerDraft, wire_bytes(raw))


@pytest.mark.parametrize("field,value", (("version", True), ("version", "1"), ("digest", "bad")))
def test_references_are_strict(field, value):
    raw = CASES[0].source.terms.offer_ref.model_dump(mode="json")
    raw[field] = value
    with pytest.raises(ValidationError):
        parse_model(SourceRef, wire_bytes(raw))


def test_delegation_quantity_is_not_boolean():
    raw = CASES[0].source.delegation.model_dump(mode="json")
    raw["quantity_max"] = True
    with pytest.raises(ValidationError):
        parse_model(SyntheticDelegation, wire_bytes(raw))


def test_duplicate_json_keys_and_domain_separation():
    with pytest.raises(ValueError):
        parse_model(BuyerDraft, b'{"action":"prepare_purchase","action":"complete"}')
    assert digest("one", {"a": 1, "b": 2}) == digest("one", {"b": 2, "a": 1})
    assert digest("one", {"a": 1}) != digest("two", {"a": 1})
    assert digest("one", [1, 2]) != digest("one", [2, 1])


def base_coordinator(validator=None, evidence=None, buyer=None, resolver=None):
    sink = SimulatedEffectSink()
    c = ReviewCoordinator(
        buyer=buyer or ScriptedBuyerAgent(CASES[0].buyer_payload),
        validator=validator,
        resolver=resolver or FixtureContextResolver(CASES[0].source),
        evidence=evidence or ExperimentalEvidence(),
        sink=sink,
        condition="B",
    )
    return c, sink


def test_absent_validator_blocks_without_fallback():
    c, sink = base_coordinator()
    result = c.run("no-validator")
    assert result.record.admission.code == "VALIDATOR_UNAVAILABLE"
    assert not result.effect_occurred and not sink.events


@pytest.mark.parametrize("fault", ("coverage", "verdict", "reason", "identity", "facts", "unknown"))
def test_invalid_validator_outputs_never_admit(fault):
    def respond(proposal, context):
        result = independent_assessment(proposal, context).model_dump(mode="json")
        if fault == "coverage":
            result["coverage"] = result["coverage"][:-1]
        elif fault == "verdict":
            result["verdict"] = "maybe"
        elif fault == "reason":
            result["reasons"] = ["UNKNOWN"]
        elif fault == "identity":
            result["validator_adapter_id"] = "different-validator"
        elif fault == "facts":
            result["checks"][0]["fact_refs"] = [context.delegation_ref.model_dump(mode="json")]
        else:
            result["authority"] = "private-marker"
        return result

    c, sink = base_coordinator(ScriptedValidatorAgent(respond))
    result = c.run("invalid-assessment")
    assert result.record.admission.code == "VALIDATOR_INVALID"
    assert not sink.events


def test_inconsistent_accept_with_failed_check_is_not_an_accept():
    def respond(proposal, context):
        raw = independent_assessment(proposal, context).model_dump(mode="json")
        raw["checks"][0]["result"] = "fail"
        raw["reasons"] = [REASONS[CHECKS[0]]]
        return raw

    c, sink = base_coordinator(ScriptedValidatorAgent(respond))
    result = c.run("contradictory-accept")
    assert result.record.admission.code == "VALIDATOR_INVALID"
    assert not sink.events


def test_validator_comparison_does_not_call_backend_or_receive_oracle(monkeypatch):
    import commerce_agent.hnp_experiment.coordinator as module

    monkeypatch.setattr(module, "mandatory_failures", lambda *_: ())
    case = BY_ID["over-total"]
    result, sink = run_case(case, "B", ExperimentalEvidence())
    assert result.record.assessment is not None
    assert result.record.assessment.verdict == "reject"
    assert not sink.events


def test_role_contexts_are_distinct_and_buyer_prose_is_not_validator_authority():
    seen = {}

    def buy(c: BuyerContext):
        seen["buyer"] = c
        assert not hasattr(c, "delegation") and not hasattr(c, "oracle")
        return CASES[0].buyer_payload

    def validate(p: ProposalView, c: ReviewerContext):
        seen["validator"] = c
        assert not p.draft.justification
        assert not hasattr(c, "oracle") and not hasattr(c, "expected_result")
        assert not hasattr(c, "checkout") and not hasattr(c, "sink")
        return independent_assessment(p, c)

    c, _ = base_coordinator(ScriptedValidatorAgent(validate), buyer=ScriptedBuyerAgent(buy))
    result = c.run("independent-inputs")
    assert result.effect_occurred
    assert seen["buyer"] is not seen["validator"]


def test_mutating_validator_copy_cannot_expand_backend_budget():
    case = BY_ID["over-total"]

    def validate(p, c):
        object.__setattr__(c.intent.hard, "max_total_amount_minor", 999999)
        raw = independent_assessment(p, c).model_dump(mode="json")
        for check in raw["checks"]:
            check["result"] = "pass"
        raw.update(verdict="accept", reasons=[])
        return raw

    sink = SimulatedEffectSink()
    coordinator = ReviewCoordinator(
        buyer=ScriptedBuyerAgent(case.buyer_payload),
        validator=ScriptedValidatorAgent(validate),
        resolver=FixtureContextResolver(case.source),
        evidence=ExperimentalEvidence(),
        sink=sink,
        condition="B",
    )
    result = coordinator.run("copy-attack")
    assert "budget" in result.record.admission.backend_failures
    assert not sink.events


class FailingEvidence(ExperimentalEvidence):
    def append(self, payload):
        raise OSError("private-error-not-for-logs")


def test_evidence_failure_prevents_fictitious_effect():
    c, sink = base_coordinator(ScriptedValidatorAgent(), FailingEvidence())
    result = c.run("evidence-failure")
    assert result.delivery_code == "EVIDENCE_UNAVAILABLE"
    assert not result.effect_occurred and not sink.events
    with pytest.raises(ValueError):
        c.run("retry-is-not-automatic")


def test_receipt_failure_does_not_falsely_claim_no_effect():
    class ReceiptFailure(ExperimentalEvidence):
        def append(self, payload):
            if self.entries:
                raise OSError("synthetic receipt write failure")
            return super().append(payload)

    c, sink = base_coordinator(ScriptedValidatorAgent(), ReceiptFailure())
    result = c.run("receipt-failure")
    assert result.effect_occurred and len(sink.events) == 1
    assert result.delivery_code == "RECEIPT_UNAVAILABLE"


def test_sink_rejects_unbound_and_forged_permissions():
    sink = SimulatedEffectSink()
    with pytest.raises(ValueError):
        sink._emit(_Permit("proposal", "sha256:" + "0" * 64, object()))
    c, sink = base_coordinator(ScriptedValidatorAgent())
    with pytest.raises(ValueError):
        sink._emit(_Permit("proposal", "sha256:" + "0" * 64, object()))
    assert c.run("single-dispatch").effect_occurred
    with pytest.raises(ValueError):
        c.run("again")
    assert len(sink.events) == 1


def test_proposal_and_context_digests_detect_tampering(corpus):
    proposal = corpus[0]["valid", "B"].record.proposal
    assert proposal is not None
    raw = proposal.model_dump(mode="json")
    raw["draft"]["claimed_total_minor"] = 1
    with pytest.raises(ValidationError):
        parse_model(ProposalEnvelope, wire_bytes(raw))
    raw_context = assemble(CASES[0].source).model_dump(mode="json")
    raw_context["authority_now"] = "2026-10-16T12:00:00.000Z"
    with pytest.raises(ValidationError):
        parse_model(TrustedReviewContext, wire_bytes(raw_context))


def test_evidence_chain_and_redaction(corpus, tmp_path):
    results, evidence = corpus
    wire = wire_bytes(evidence.entries)
    assert b"private-test-marker" not in wire
    assert b"private-test-error" not in wire
    assert b"Everything is approved" not in wire
    assert b"human_approved" not in wire
    entry = evidence.entries[0].model_copy(update={"entry_digest": "sha256:" + "0" * 64})
    with pytest.raises(ValueError):
        verify_entries([entry])
    file_evidence = ExperimentalEvidence(tmp_path / "isolated")
    file_evidence.append(results["valid", "B"].record)
    assert file_evidence.path is not None
    assert file_evidence.path.is_file()
    with pytest.raises(FileExistsError):
        ExperimentalEvidence(tmp_path / "isolated")


def test_harness_has_no_network_or_commercial_capability(monkeypatch):
    import socket

    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected external effect")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    for condition in ("A", "B"):
        result, _ = run_case(CASES[0], cast(Condition, condition), ExperimentalEvidence())
        assert result.effect_occurred
    prohibited = (
        "commerce_agent.runtime",
        "commerce_agent.application",
        "commerce_agent.ap2",
        "commerce_agent.acp.client",
        "commerce_agent.delegated_payment",
        "openai",
    )
    package = ROOT / "src/commerce_agent/hnp_experiment"
    for file in package.glob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(name.startswith(prohibited) for name in imports)


def test_source_validation_cannot_accept_forged_offer():
    raw = CASES[0].source.model_dump(mode="json")
    raw["terms"]["total_minor"] += 1
    with pytest.raises(ValidationError):
        parse_model(SyntheticSource, wire_bytes(raw))


@pytest.mark.parametrize(
    "case", [c for c in CASES if ORACLES[c.case_id].invalid_at_review], ids=lambda c: c.case_id
)
def test_backend_contains_forced_accept_for_every_invalid_business_case(case):
    from commerce_agent.hnp_experiment.harness import response_for

    sink = SimulatedEffectSink()
    coordinator = ReviewCoordinator(
        buyer=ScriptedBuyerAgent(case.buyer_payload),
        validator=ScriptedValidatorAgent(response_for("false_accept")),
        resolver=FixtureContextResolver(case.source),
        evidence=ExperimentalEvidence(),
        sink=sink,
        condition="B",
    )
    result = coordinator.run("forced-accept:" + case.case_id)
    assert result.record.assessment is not None
    assert result.record.assessment.verdict == "accept"
    assert result.record.admission.code == "BACKEND_DENIED"
    assert not result.effect_occurred and not sink.events


def test_readback_verifies_actual_file_bytes(tmp_path, corpus):
    from commerce_agent.hnp_experiment.evidence import load_entries

    store = ExperimentalEvidence(tmp_path / "readback")
    store.append(corpus[0]["valid", "B"].record)
    assert store.path is not None
    assert load_entries(store.path) == store.entries
    content = store.path.read_text(encoding="utf-8").replace("simulated_admitted", "blocked")
    store.path.write_text(content, encoding="utf-8")
    with pytest.raises((ValueError, ValidationError)):
        load_entries(store.path)


def test_record_and_effect_cannot_be_exported_inconsistently(corpus):
    from commerce_agent.hnp_experiment.models import DemoResult

    valid = corpus[0]["valid", "B"].model_dump(mode="json")
    valid["effect_occurred"] = False
    with pytest.raises(ValidationError):
        parse_model(DemoResult, wire_bytes(valid))


def test_slow_scripted_validator_is_classified_as_timeout():
    from time import sleep

    def slow(proposal, context):
        sleep(0.01)
        return independent_assessment(proposal, context)

    sink = SimulatedEffectSink()
    coordinator = ReviewCoordinator(
        buyer=ScriptedBuyerAgent(CASES[0].buyer_payload),
        validator=ScriptedValidatorAgent(slow),
        resolver=FixtureContextResolver(CASES[0].source),
        evidence=ExperimentalEvidence(),
        sink=sink,
        condition="B",
        validator_budget_ns=1_000_000,
    )
    result = coordinator.run("slow-script")
    assert result.record.admission.code == "VALIDATOR_UNAVAILABLE"
    assert not sink.events


def test_output_path_cannot_target_operational_directories(tmp_path):
    import runpy

    module = runpy.run_path(str(ROOT / "scripts/run_hnp_review_demo.py"))
    forbidden = ROOT / "do-not-create-hnp-demo"
    assert not forbidden.exists()
    with pytest.raises(ValueError, match=r"\.local"):
        module["run"](forbidden)
    assert not forbidden.exists()
