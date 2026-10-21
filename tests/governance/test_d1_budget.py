"""POL-11 (reachable total budget) in the pure D1 evaluator, its artifact and its input fact.

Only `b2-hp-v2` evaluates POL-11; `b2-pilot-v1` keeps its bytes, its digest and its two rules so
the published pilot stays reproducible (docs/design/hp-p1-budget-d1.md).
"""

import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from commerce_agent.governance.canonical import (
    canonical_bytes,
    input_digest,
    parse_model,
    sha256_digest,
)
from commerce_agent.governance.evaluator import (
    D1PolicyBoundary,
    GovernanceError,
    ReferencePolicyEvaluator,
    measure_reference_engine,
    parse_input,
    verify_artifact,
)
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.models import (
    APPLICABLE_RULES,
    POLICY_REASONS,
    SCHEMA,
    D1PolicyArtifact,
    EvaluationMetadata,
    PolicyDecision,
    SelectionPolicyInput,
)
from commerce_agent.governance.provenance import build_d1_policy

FIXTURES = Path(__file__).parent / "fixtures"
MERCHANT_REF = "merchant-1"
# S02: the Pixel 8 costs 1.540.000 COP in the catalog and the buyer can spend 1.000.000 in total.
PIXEL_8_MINOR = 154_000_000
BUDGET_MINOR = 100_000_000
METADATA = EvaluationMetadata(
    schema_version=SCHEMA, decided_at="2026-10-21T15:00:00.005Z", latency_ms=3
)


def fixture_payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "d1-input.json").read_bytes())


def policy_input(
    policy_set_id: str = "b2-hp-v2",
    *,
    unit: int | None = PIXEL_8_MINOR,
    max_total: int | None = BUDGET_MINOR,
    merchant_ref: str = MERCHANT_REF,
) -> tuple[SelectionPolicyInput, bytes]:
    """The fixture input bound to the named policy set, with the S02 facts."""
    artifact, binding = build_d1_policy(
        policy_set_id=policy_set_id, version=1, merchant_ref=MERCHANT_REF
    )
    payload = fixture_payload()
    payload["expected_policy"] = binding.model_dump(mode="json")
    payload["intent"]["max_unit_minor"] = None
    payload["intent"]["max_total_minor"] = max_total
    payload["merchant_ref"] = merchant_ref
    if unit is not None:
        payload["selected_unit_minor"] = unit
    return parse_input(canonical_bytes(payload)), artifact


def evaluate(data: SelectionPolicyInput, artifact: bytes) -> PolicyDecision:
    evaluator = ReferencePolicyEvaluator(
        artifact_bytes=artifact,
        binding=data.expected_policy,
        metadata=METADATA,
        identity=measure_reference_engine(),
    )
    return D1PolicyBoundary(
        binding=data.expected_policy,
        artifact_bytes=artifact,
        metadata=METADATA,
        engine_identity=measure_reference_engine(),
        evaluator_factory=lambda: evaluator,
    ).evaluate(data)


def rules(decision: PolicyDecision) -> list[tuple[str, str]]:
    return [(rule.policy_id, rule.result) for rule in decision.rule_results]


def test_every_policy_keeps_its_own_reason_code() -> None:
    """Codes follow the policy id, never a rule's position, so adding a rule moves none."""
    assert POLICY_REASONS == {
        "POL-01": "SELECTION_NOT_ELIGIBLE",
        "POL-02": "ACTION_OR_MERCHANT_NOT_ALLOWED",
        "POL-03": "PRINCIPAL_ACTION_NOT_ALLOWED",
        "POL-04": "EPISODE_BINDING_MISMATCH",
        "POL-05": "CHECKOUT_CONSTRAINT_MISMATCH",
        "POL-06": "POLICY_CONTEXT_MISMATCH",
        "POL-07": "DELEGATION_NOT_ACTIVE",
        "POL-08": "DELEGATION_SCOPE_EXCEEDED",
        "POL-09": "DELEGATION_USE_UNAVAILABLE",
        "POL-10": "DISPATCH_AUTHORITY_CHANGED",
        "POL-11": "BUDGET_NOT_SATISFIABLE",
    }


def test_the_rules_that_apply_depend_on_the_phase_and_the_purchase_mode() -> None:
    """New phases and purchase modes add rows; the D1/HP row is part of b2-hp-v2's bytes."""
    assert dict(APPLICABLE_RULES) == {("D1", "HP"): ("POL-01", "POL-02", "POL-11")}
    payload, binding = build_d1_policy(
        policy_set_id="b2-hp-v2", version=1, merchant_ref=MERCHANT_REF
    )
    assert verify_artifact(payload, binding).policy_ids == APPLICABLE_RULES[("D1", "HP")]


def test_a_purchase_mode_without_its_own_row_never_gets_the_hp_rules() -> None:
    data, artifact = policy_input()
    hnp = parse_input(canonical_bytes(json.loads(canonical_bytes(data)) | {"purchase_mode": "HNP"}))

    decision = evaluate(hnp, artifact)

    assert (decision.outcome, decision.origin) == ("deny", "runtime_guard")
    assert decision.reason_codes == ("GOV_ARTIFACT_MISMATCH",)
    assert decision.engine.attempted is False


def test_the_pilot_v1_artifact_keeps_its_bytes_and_two_rules() -> None:
    payload, binding = build_d1_policy(
        policy_set_id="b2-pilot-v1", version=1, merchant_ref=MERCHANT_REF
    )

    assert payload == (
        b'{"allowed_actions":["prepare_checkout"],"allowed_merchant_refs":["merchant-1"],'
        b'"currency":"COP","policy_ids":["POL-01","POL-02"],"policy_set_id":"b2-pilot-v1",'
        b'"quantity":1,"schema_version":"commerce-policy/1.0","version":1}'
    )
    assert binding.artifact_digest == sha256_digest(payload)
    assert verify_artifact(payload, binding).policy_ids == ("POL-01", "POL-02")


def test_b2_hp_v2_lists_pol11_after_the_pilot_rules() -> None:
    payload, binding = build_d1_policy(
        policy_set_id="b2-hp-v2", version=1, merchant_ref=MERCHANT_REF
    )

    artifact = verify_artifact(payload, binding)
    assert artifact.policy_ids == ("POL-01", "POL-02", "POL-11")
    assert (artifact.policy_set_id, artifact.version) == ("b2-hp-v2", 1)


@pytest.mark.parametrize(
    "policy_ids",
    [
        ["POL-01"],
        ["POL-01", "POL-02", "POL-03"],
        ["POL-01", "POL-11", "POL-02"],
        ["POL-11"],
        ["POL-01", "POL-02", "POL-11", "POL-11"],
    ],
)
def test_an_artifact_only_declares_a_known_d1_profile(policy_ids: list[str]) -> None:
    payload, _ = build_d1_policy(policy_set_id="b2-hp-v2", version=1, merchant_ref=MERCHANT_REF)
    changed = json.loads(payload) | {"policy_ids": policy_ids}

    with pytest.raises(ValidationError):
        parse_model(D1PolicyArtifact, canonical_bytes(changed))


@pytest.mark.parametrize(
    ("unit", "max_total", "result"),
    [
        pytest.param(PIXEL_8_MINOR, BUDGET_MINOR, "fail", id="s02-price-over-total"),
        pytest.param(PIXEL_8_MINOR, PIXEL_8_MINOR, "pass", id="price-equal-to-total"),
        pytest.param(PIXEL_8_MINOR, 160_000_000, "pass", id="price-within-total"),
        pytest.param(PIXEL_8_MINOR, None, "pass", id="no-total-budget"),
        pytest.param(None, None, "pass", id="no-price-no-total"),
        pytest.param(None, BUDGET_MINOR, "fail", id="no-price-fails-closed"),
        pytest.param(0, 0, "pass", id="zero-price-zero-total"),
    ],
)
def test_pol11_compares_the_catalog_price_with_the_total_budget(
    unit: int | None, max_total: int | None, result: str
) -> None:
    data, artifact = policy_input(unit=unit, max_total=max_total)

    decision = evaluate(data, artifact)

    assert decision.origin == "policy"
    assert rules(decision) == [("POL-01", "pass"), ("POL-02", "pass"), ("POL-11", result)]
    if result == "fail":
        assert (decision.outcome, decision.reason_codes) == ("deny", ("BUDGET_NOT_SATISFIABLE",))
    else:
        assert decision.outcome == "allow"
        assert decision.reason_codes == ("ALL_APPLICABLE_POLICIES_SATISFIED",)
    assert decision.input_digest == input_digest(data)


def test_failed_rules_keep_their_own_codes_in_rule_order() -> None:
    data, artifact = policy_input(merchant_ref="merchant-foreign")

    decision = evaluate(data, artifact)

    assert rules(decision) == [("POL-01", "pass"), ("POL-02", "fail"), ("POL-11", "fail")]
    assert decision.reason_codes == ("ACTION_OR_MERCHANT_NOT_ALLOWED", "BUDGET_NOT_SATISFIABLE")


def test_the_pilot_v1_policy_never_evaluates_pol11() -> None:
    data, artifact = policy_input("b2-pilot-v1")

    decision = evaluate(data, artifact)

    assert decision.outcome == "allow"
    assert rules(decision) == [("POL-01", "pass"), ("POL-02", "pass")]


class Answer:
    """An engine that answers a fixed decision for whatever input it gets."""

    def __init__(self, decision: PolicyDecision):
        self.decision = decision

    def evaluate(self, policy_input: SelectionPolicyInput) -> PolicyDecision:
        return self.decision


@pytest.mark.parametrize(
    ("answered_by", "bound_to"),
    [
        pytest.param("b2-pilot-v1", "b2-hp-v2", id="skips-pol11"),
        pytest.param("b2-hp-v2", "b2-pilot-v1", id="adds-pol11"),
    ],
)
def test_the_boundary_refuses_rules_other_than_the_artifacts(
    answered_by: str, bound_to: str
) -> None:
    data, artifact = policy_input(bound_to)
    other, other_artifact = policy_input(answered_by)
    answer = evaluate(other, other_artifact).model_copy(
        update={"input_digest": input_digest(data), "policy_binding": data.expected_policy}
    )

    decision = D1PolicyBoundary(
        binding=data.expected_policy,
        artifact_bytes=artifact,
        metadata=METADATA,
        engine_identity=measure_reference_engine(),
        evaluator_factory=lambda: Answer(answer),
    ).evaluate(data)

    assert (decision.outcome, decision.origin) == ("deny", "runtime_guard")
    assert decision.reason_codes == ("GOV_OUTPUT_INVALID",)


@pytest.mark.parametrize(
    "order",
    [
        ["POL-11", "POL-01", "POL-02"],
        ["POL-01", "POL-11"],
        ["POL-01", "POL-02", "POL-11", "POL-11"],
    ],
)
def test_a_decision_only_carries_a_known_d1_profile(order: list[str]) -> None:
    data, artifact = policy_input()
    decision = json.loads(canonical_bytes(evaluate(data, artifact)))
    decision["rule_results"] = [
        {"schema_version": SCHEMA, "policy_id": policy_id, "result": "pass"} for policy_id in order
    ]
    decision["reason_codes"] = ["ALL_APPLICABLE_POLICIES_SATISFIED"]
    decision["outcome"] = "allow"

    with pytest.raises(ValidationError):
        parse_model(PolicyDecision, canonical_bytes(decision))


def test_a_failed_pol11_needs_its_own_reason_code() -> None:
    data, artifact = policy_input()
    decision = json.loads(canonical_bytes(evaluate(data, artifact)))
    decision["reason_codes"] = ["PRINCIPAL_ACTION_NOT_ALLOWED"]

    with pytest.raises(ValidationError):
        parse_model(PolicyDecision, canonical_bytes(decision))


@pytest.mark.parametrize("value", [-1, True, "154000000", 1.5])
def test_the_price_fact_is_a_strict_amount(value: object) -> None:
    payload = fixture_payload() | {"selected_unit_minor": value}

    with pytest.raises(GovernanceError, match=r"^GOV_INPUT_INVALID$"):
        parse_input(json.dumps(payload).encode())


def test_an_input_recorded_before_pol11_keeps_its_canonical_bytes() -> None:
    # Evidence stored before the fact existed must still verify with the same digest.
    payload = fixture_payload()

    data = parse_input(canonical_bytes(payload))

    assert data.selected_unit_minor is None
    assert canonical_bytes(data) == canonical_bytes(payload)
    assert b"selected_unit_minor" not in canonical_bytes(data)


def test_an_observed_price_enters_the_canonical_input() -> None:
    data, _ = policy_input()

    assert json.loads(canonical_bytes(data))["selected_unit_minor"] == PIXEL_8_MINOR
    assert parse_input(canonical_bytes(data)) == data


def test_a_stored_decision_from_before_pol11_still_parses() -> None:
    payload = fixture_payload()
    artifact = (FIXTURES / "d1-policy.json").read_bytes()
    data = parse_input(canonical_bytes(payload))
    decision = evaluate(data, artifact)
    ref = {"schema_version": SCHEMA, "id": "x", "version": 1}
    record = {
        "schema_version": SCHEMA,
        "evidence_id": "evidence-1",
        "sequence": 1,
        "recorded_at": "2026-10-14T15:00:00.010Z",
        "record_kind": "decision_record",
        "canonicalization_profile": "commerce-canonical-json/1",
        "canonical_input_bytes": canonical_bytes(payload).decode(),
        "decision": json.loads(canonical_bytes(decision)),
        "policy_artifact_ref": ref | {"digest": decision.policy_binding.artifact_digest},
        "engine_artifact_ref": ref | {"digest": cast(str, decision.engine.artifact_digest)},
        "source_refs": [
            json.loads(canonical_bytes(item))
            for item in (
                data.intent.intent_ref,
                data.feed_snapshot_ref,
                data.eligibility_algorithm_ref,
                data.clock_profile_ref,
            )
        ],
        "application": {
            "schema_version": SCHEMA,
            "disposition": "blocked",
            "transition_ref": None,
            "expected_state_version": 1,
            "resulting_state_version": None,
        },
        "replayability": "full",
    }

    parsed = parse_model(DecisionRecord, canonical_bytes(record))

    assert parsed.policy_input() == data
