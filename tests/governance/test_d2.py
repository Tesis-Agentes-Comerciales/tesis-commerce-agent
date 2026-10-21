"""D2-HP reference engine and its fail-closed boundary: pure, no store and no effects."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from commerce_agent.governance.canonical import canonical_bytes, input_digest, parse_model
from commerce_agent.governance.evaluator import (
    AuthorizationEvaluator,
    D2PolicyBoundary,
    GovernanceError,
    ReferenceAuthorizationEvaluator,
    measure_reference_engine,
    parse_authorization_input,
    verify_authorization_artifact,
)
from commerce_agent.governance.models import (
    APPLICABLE_RULES,
    D1_PILOT_RULES,
    SCHEMA,
    AuthorizationPolicyInput,
    AuthorizationRules,
    D1PolicyArtifact,
    EvaluationMetadata,
    PolicyDecision,
    rules_apply,
)
from commerce_agent.governance.provenance import build_d1_policy

FIXTURES = Path(__file__).parent / "fixtures"
POLICIES = ("POL-03", "POL-04", "POL-05", "POL-06")


@pytest.fixture
def payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "d2-input.json").read_bytes())


@pytest.fixture
def artifact() -> bytes:
    return (FIXTURES / "d2-policy.json").read_bytes()


@pytest.fixture
def metadata() -> EvaluationMetadata:
    return EvaluationMetadata(
        schema_version=SCHEMA, decided_at="2026-10-14T15:05:00.004Z", latency_ms=4
    )


def parsed(payload: dict[str, Any]) -> AuthorizationPolicyInput:
    return parse_authorization_input(canonical_bytes(payload))


def reference(
    data: AuthorizationPolicyInput, artifact: bytes, metadata: EvaluationMetadata
) -> ReferenceAuthorizationEvaluator:
    return ReferenceAuthorizationEvaluator(
        artifact_bytes=artifact,
        binding=data.expected_policy,
        metadata=metadata,
        identity=measure_reference_engine(),
    )


def boundary(
    data: AuthorizationPolicyInput,
    artifact: bytes | None,
    metadata: EvaluationMetadata,
    factory: Callable[[], AuthorizationEvaluator],
) -> D2PolicyBoundary:
    return D2PolicyBoundary(
        binding=data.expected_policy,
        artifact_bytes=artifact,
        engine_identity=measure_reference_engine(),
        metadata=metadata,
        evaluator_factory=factory,
    )


class Stub:
    def __init__(self, response: object):
        self.response = response
        self.calls = 0

    def evaluate(self, policy_input: AuthorizationPolicyInput) -> PolicyDecision:
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return cast(PolicyDecision, self.response)


def test_valid_approval_satisfies_every_rule(
    payload: dict[str, Any], artifact: bytes, metadata: EvaluationMetadata
) -> None:
    data = parsed(payload)
    result = reference(data, artifact, metadata).evaluate(data)
    assert (result.phase, result.outcome, result.origin) == ("D2", "allow", "policy")
    assert result.reason_codes == ("ALL_APPLICABLE_POLICIES_SATISFIED",)
    assert [(rule.policy_id, rule.result) for rule in result.rule_results] == [
        (policy, "pass") for policy in POLICIES
    ]
    assert result.input_digest == input_digest(data)
    assert result.engine == measure_reference_engine()


def _set(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    """A change at a dotted path; a numeric segment indexes a list."""

    def change(payload: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        target: Any = payload
        for key in parents:
            target = target[int(key)] if key.isdigit() else target[key]
        target[leaf] = value

    return change


OTHER_REF = {"schema_version": SCHEMA, "id": "other", "version": 1, "digest": "sha256:" + "c" * 64}

VIOLATIONS: dict[str, tuple[str, Callable[[dict[str, Any]], None]]] = {
    "not authenticated": ("POL-03", _set("principal.authentication_verified", False)),
    "not the owner": ("POL-03", _set("principal.owns_run", False)),
    "another principal": ("POL-03", _set("principal.principal_ref", "owner-2")),
    "action not permitted": ("POL-03", _set("principal.permitted_actions", [])),
    "basis not admitted": ("POL-03", _set("principal.authentication_basis", "service_and_grant")),
    "d1 denied": ("POL-04", _set("d1.outcome", "deny")),
    "d1 of another run": ("POL-04", _set("d1.run_id", "run-2")),
    "d1 observed in shadow": ("POL-04", _set("d1.governance_mode", "shadow")),
    "d1 for another intent": ("POL-04", _set("d1.intent_ref", OTHER_REF)),
    "d1 chose another variant": ("POL-04", _set("d1.selected_variant_id", "variant-2")),
    "checkout of another merchant": ("POL-04", _set("d1.merchant_ref", "merchant-2")),
    "attempt of another owner": ("POL-04", _set("hp.attempt_owner_ref", "owner-2")),
    "no click": ("POL-04", _set("hp.click_received", False)),
    "click before the terms": ("POL-04", _set("hp.clicked_at", "2026-10-14T15:00:00.000Z")),
    "click after now": ("POL-04", _set("hp.clicked_at", "2026-10-14T15:06:00.000Z")),
    "attempt expired": ("POL-04", _set("hp.attempt_valid_until", "2026-10-14T15:05:00.000Z")),
    "other terms than presented": ("POL-04", _set("hp.material_match", False)),
    "total over budget": ("POL-05", _set("intent.max_total_minor", 73_999)),
    "unit over limit": ("POL-05", _set("intent.max_unit_minor", 71_999)),
    "constraint not met": ("POL-05", _set("constraint_matches.0.matches", False)),
    "constraint not covered": (
        "POL-05",
        lambda payload: payload["constraint_matches"].pop(),
    ),
    "policy revision changed": ("POL-06", _set("d1.policy_binding.version", 2)),
    "context changed": ("POL-06", _set("context_version", 2)),
}


@pytest.mark.parametrize("case", sorted(VIOLATIONS))
def test_each_violation_fails_only_its_rule(
    payload: dict[str, Any], artifact: bytes, metadata: EvaluationMetadata, case: str
) -> None:
    policy, change = VIOLATIONS[case]
    change(payload)
    data = parsed(payload)
    result = reference(data, artifact, metadata).evaluate(data)
    assert result.outcome == "deny"
    assert [rule.policy_id for rule in result.rule_results if rule.result == "fail"] == [policy]
    reasons = {
        "POL-03": "PRINCIPAL_ACTION_NOT_ALLOWED",
        "POL-04": "EPISODE_BINDING_MISMATCH",
        "POL-05": "CHECKOUT_CONSTRAINT_MISMATCH",
        "POL-06": "POLICY_CONTEXT_MISMATCH",
    }
    assert result.reason_codes == (reasons[policy],)


def test_every_rule_is_evaluated_and_reasons_follow_the_catalog(
    payload: dict[str, Any], artifact: bytes, metadata: EvaluationMetadata
) -> None:
    payload["d1"]["policy_binding"]["version"] = 2
    payload["intent"]["max_total_minor"] = 1
    payload["principal"]["owns_run"] = False
    data = parsed(payload)
    result = reference(data, artifact, metadata).evaluate(data)
    assert result.reason_codes == (
        "PRINCIPAL_ACTION_NOT_ALLOWED",
        "CHECKOUT_CONSTRAINT_MISMATCH",
        "POLICY_CONTEXT_MISMATCH",
    )
    assert [rule.result for rule in result.rule_results] == ["fail", "pass", "fail", "fail"]


def test_unlimited_intent_admits_any_total(
    payload: dict[str, Any], artifact: bytes, metadata: EvaluationMetadata
) -> None:
    payload["intent"].update(max_total_minor=None, max_unit_minor=None)
    data = parsed(payload)
    assert reference(data, artifact, metadata).evaluate(data).outcome == "allow"


def test_the_input_is_strict(payload: dict[str, Any]) -> None:
    for change in (
        _set("purchase_mode", "HNP"),
        _set("phase", "D1"),
        _set("action", "prepare_checkout"),
        _set("terms.other_minor", 1),
        _set("terms.currency", "USD"),
        _set("unexpected", True),
        lambda payload: payload["constraint_matches"].append(payload["constraint_matches"][0]),
    ):
        changed = json.loads(json.dumps(payload))
        change(changed)
        with pytest.raises(GovernanceError) as error:
            parsed(changed)
        assert error.value.reason == "GOV_INPUT_INVALID"


def test_artifact_without_authorization_rules_cannot_authorize(payload: dict[str, Any]) -> None:
    data = parsed(payload)
    d1_only = (FIXTURES / "d1-policy.json").read_bytes()
    with pytest.raises(GovernanceError) as error:
        verify_authorization_artifact(d1_only, data.expected_policy)
    assert error.value.reason == "GOV_ARTIFACT_MISMATCH"


def test_d1_artifact_bytes_are_unchanged() -> None:
    """b2-pilot-v1 and every artifact without D2 rules keep their exact bytes and digest."""
    raw = (FIXTURES / "d1-policy.json").read_bytes()
    assert canonical_bytes(parse_model(D1PolicyArtifact, raw)) == raw


def test_d2_hp_is_its_own_row_of_the_table() -> None:
    """D2/HP extends the table without touching D1; no other phase admits its rules."""
    assert APPLICABLE_RULES[("D2", "HP")] == POLICIES
    assert rules_apply(POLICIES, "D2", "HP")
    assert not rules_apply(D1_PILOT_RULES, "D2", "HP")
    assert not rules_apply(POLICIES, "D1", "HP")
    assert not rules_apply(POLICIES, "D2", "HNP")
    with pytest.raises(ValidationError):
        AuthorizationRules(
            schema_version=SCHEMA,
            policy_ids=APPLICABLE_RULES[("D1", "HP")],
            allowed_actions=("complete_purchase",),
            authentication_bases=("user_jwt",),
        )


def test_only_b2_hp_v2_carries_d2_rules() -> None:
    hp, hp_binding = build_d1_policy(policy_set_id="b2-hp-v2", version=1, merchant_ref="m")
    rules = verify_authorization_artifact(hp, hp_binding)
    assert rules.policy_ids == POLICIES
    assert (rules.allowed_actions, rules.authentication_bases) == (
        ("complete_purchase",),
        ("user_jwt",),
    )
    pilot, pilot_binding = build_d1_policy(policy_set_id="b2-pilot-v1", version=1, merchant_ref="m")
    assert b"authorization" not in pilot
    with pytest.raises(GovernanceError) as error:
        verify_authorization_artifact(pilot, pilot_binding)
    assert error.value.reason == "GOV_ARTIFACT_MISMATCH"


def test_decision_reports_exactly_its_phase_rules(
    payload: dict[str, Any], artifact: bytes, metadata: EvaluationMetadata
) -> None:
    data = parsed(payload)
    raw = reference(data, artifact, metadata).evaluate(data).model_dump(mode="json")
    for rules in (
        [{"schema_version": SCHEMA, "policy_id": "POL-01", "result": "pass"}],
        raw["rule_results"][:3],
    ):
        with pytest.raises(ValidationError):
            PolicyDecision.model_validate({**raw, "rule_results": rules})


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_set("freshness.status", "stale"), "GOV_FRESHNESS_UNRESOLVED"),
        (_set("freshness.status", "unresolved"), "GOV_FRESHNESS_UNRESOLVED"),
        (
            _set("terms.commercial_expires_at", "2026-10-14T15:05:00.000Z"),
            "GOV_FRESHNESS_UNRESOLVED",
        ),
        (
            _set(
                "authorization_profile",
                {"schema_version": SCHEMA, "status": "unresolved", "profile_ref": None},
            ),
            "GOV_AUTHORIZATION_PROFILE_UNRESOLVED",
        ),
    ],
)
def test_prerequisites_deny_before_the_engine(
    payload: dict[str, Any],
    artifact: bytes,
    metadata: EvaluationMetadata,
    change: Callable[[dict[str, Any]], None],
    reason: str,
) -> None:
    change(payload)
    data = parsed(payload)
    stub = Stub(AssertionError("never invoked"))
    result = boundary(data, artifact, metadata, lambda: stub).evaluate(data)
    assert (result.outcome, result.origin, result.reason_codes) == (
        "deny",
        "runtime_guard",
        (reason,),
    )
    assert result.rule_results == () and not result.engine.attempted and stub.calls == 0


def test_boundary_returns_the_reference_result(
    payload: dict[str, Any], artifact: bytes, metadata: EvaluationMetadata
) -> None:
    data = parsed(payload)
    expected = reference(data, artifact, metadata).evaluate(data)
    result = boundary(data, artifact, metadata, lambda: reference(data, artifact, metadata))
    assert result.evaluate(data) == expected


@pytest.mark.parametrize("failure", ["factory", "invoke", "unrelated", "garbage", "artifact"])
def test_engine_failures_fail_closed(
    payload: dict[str, Any], artifact: bytes, metadata: EvaluationMetadata, failure: str
) -> None:
    data = parsed(payload)
    allowed = reference(data, artifact, metadata).evaluate(data)

    def factory() -> AuthorizationEvaluator:
        if failure == "factory":
            raise RuntimeError("engine down")
        if failure == "invoke":
            return Stub(RuntimeError("timeout"))
        if failure == "unrelated":
            return Stub(allowed.model_copy(update={"decision_id": "decision-other"}))
        return Stub({"outcome": "allow"})

    stored = None if failure == "artifact" else artifact
    result = boundary(data, stored, metadata, factory).evaluate(data)
    expected = {
        "factory": ("GOV_ENGINE_UNAVAILABLE", False),
        "invoke": ("GOV_ENGINE_UNAVAILABLE", True),
        "unrelated": ("GOV_OUTPUT_INVALID", True),
        "garbage": ("GOV_OUTPUT_INVALID", True),
        "artifact": ("GOV_FACTS_UNAVAILABLE", False),
    }[failure]
    assert (result.outcome, result.origin) == ("deny", "runtime_guard")
    assert (result.reason_codes, result.engine.attempted) == ((expected[0],), expected[1])


def test_off_never_has_a_decision(
    payload: dict[str, Any], artifact: bytes, metadata: EvaluationMetadata
) -> None:
    payload["governance_mode"] = "off"
    payload["d1"]["governance_mode"] = "off"
    data = parsed(payload)
    with pytest.raises(GovernanceError):
        boundary(data, artifact, metadata, lambda: reference(data, artifact, metadata)).evaluate(
            data
        )
