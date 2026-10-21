"""Pure PDP observations only: these tests make no integration/effect claim."""

import hashlib
import json
from collections.abc import Callable
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
    PolicyEvaluator,
    ReferencePolicyEvaluator,
    measure_reference_engine,
    parse_input,
)
from commerce_agent.governance.models import (
    SCHEMA,
    D1PolicyArtifact,
    EvaluationMetadata,
    PolicyBinding,
    PolicyDecision,
    SelectionPolicyInput,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "d1-input.json").read_bytes())


@pytest.fixture
def data(payload: dict[str, Any]) -> SelectionPolicyInput:
    return parse_input(canonical_bytes(payload))


@pytest.fixture
def artifact() -> bytes:
    return (FIXTURES / "d1-policy.json").read_bytes()


@pytest.fixture
def metadata() -> EvaluationMetadata:
    return EvaluationMetadata(
        schema_version=SCHEMA, decided_at="2026-10-14T15:00:00.005Z", latency_ms=5
    )


def reference(
    data: SelectionPolicyInput, artifact: bytes, metadata: EvaluationMetadata
) -> ReferencePolicyEvaluator:
    return ReferencePolicyEvaluator(
        artifact_bytes=artifact,
        binding=data.expected_policy,
        metadata=metadata,
        identity=measure_reference_engine(),
    )


def boundary(
    data: SelectionPolicyInput,
    artifact: bytes | None,
    metadata: EvaluationMetadata,
    factory: Callable[[], PolicyEvaluator],
) -> D1PolicyBoundary:
    return D1PolicyBoundary(
        binding=data.expected_policy,
        artifact_bytes=artifact,
        metadata=metadata,
        engine_identity=measure_reference_engine(),
        evaluator_factory=factory,
    )


class Stub:
    def __init__(self, response: object):
        self.response = response
        self.calls = 0

    def evaluate(self, policy_input: SelectionPolicyInput) -> PolicyDecision:
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return cast(PolicyDecision, self.response)


@pytest.mark.parametrize("case", ["P01", "P02", "N01", "N02", "N01+N02", "empty"])
@pytest.mark.parametrize("purchase_mode", ["HP", "HNP"])
@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_policy_matrix(
    payload: dict[str, Any],
    artifact: bytes,
    metadata: EvaluationMetadata,
    case: str,
    purchase_mode: str,
    mode: str,
) -> None:
    payload.update(purchase_mode=purchase_mode, governance_mode=mode)
    expected: list[str] = []
    if case in ("N01", "N01+N02", "empty"):
        payload["selected_variant_id"] = "variant-absent"
        expected.append("SELECTION_NOT_ELIGIBLE")
    if case == "empty":
        payload["eligible_variant_ids"] = []
    if case in ("N02", "N01+N02"):
        payload["merchant_ref"] = "merchant-foreign"
        expected.append("ACTION_OR_MERCHANT_NOT_ALLOWED")
    data = parse_input(canonical_bytes(payload))
    evaluator = reference(data, artifact, metadata)
    decision = boundary(data, artifact, metadata, lambda: evaluator).evaluate(data)
    if purchase_mode == "HNP":
        # C02: this HP policy set never decides an HNP purchase, whatever the facts.
        assert (decision.outcome, decision.origin) == ("deny", "runtime_guard")
        assert decision.reason_codes == ("GOV_ARTIFACT_MISMATCH",)
        assert decision.engine.attempted is False
        return
    assert decision.outcome == ("deny" if expected else "allow")
    assert decision.origin == "policy"
    assert decision.reason_codes == tuple(expected or ["ALL_APPLICABLE_POLICIES_SATISFIED"])
    assert [(r.policy_id, r.result) for r in decision.rule_results] == [
        ("POL-01", "fail" if "SELECTION_NOT_ELIGIBLE" in expected else "pass"),
        ("POL-02", "fail" if "ACTION_OR_MERCHANT_NOT_ALLOWED" in expected else "pass"),
    ]
    assert decision.input_digest == input_digest(data)
    assert decision.policy_binding == data.expected_policy
    assert evaluator.evaluate(data) == evaluator.evaluate(data)


@pytest.mark.parametrize("fault", ["missing", "duplicate"])
def test_f01(payload: dict[str, Any], fault: str) -> None:
    if fault == "missing":
        del payload["eligible_variant_ids"]
    else:
        payload["eligible_variant_ids"] *= 2
    with pytest.raises(GovernanceError, match=r"^GOV_INPUT_INVALID$"):
        parse_input(canonical_bytes(payload))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("unexpected",), "sensitive-synthetic-value"),
        (("intent", "address"), "sensitive-synthetic-value"),
        (("expected_policy", "token"), "sensitive-synthetic-value"),
        (("feed_snapshot_ref", "prompt"), "sensitive-synthetic-value"),
        (("schema_version",), "commerce-policy/2.0"),
        (("intent", "schema_version"), "commerce-policy/2.0"),
        (("expected_policy", "schema_version"), "commerce-policy/2.0"),
        (("context_version",), True),
        (("context_version",), "1"),
        (("context_version",), 0),
        (("intent", "max_unit_minor"), "90000000"),
        (("intent", "max_total_minor"), -1),
        (("intent", "quantity"), True),
        (("intent", "quantity"), 1.0),
        (("intent", "currency"), "cop"),
        (("owner_ref",), "owner@example.com"),
        (("run_id",), ""),
        (("run_id",), "r" * 129),
        (("trace_id",), None),
        (("purchase_mode",), "unknown"),
        (("governance_mode",), "unknown"),
        (("phase",), "D2"),
        (("action",), "complete_purchase"),
        (("action",), "commit_dispatch"),
        (("action",), "invented-action"),
        (("authority_now",), "2026-10-14T15:00:00Z"),
        (("authority_now",), "2026-10-14T15:00:00.000+00:00"),
        (("authority_now",), "2026-02-30T15:00:00.000Z"),
        (("feed_snapshot_ref", "version"), False),
        (("feed_snapshot_ref", "digest"), "sha256:" + "A" * 64),
        (("intent", "hard_constraint_refs"), ["same", "same"]),
        (("eligible_variant_ids",), ["valid", 1]),
        (("expected_local_state_version",), None),
    ],
)
def test_c01_strict_rejection(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    # Use JSON encoding here so floats reach the production parser, not a test pre-filter.
    with pytest.raises(GovernanceError) as exc:
        parse_input(json.dumps(payload).encode())
    assert str(exc.value) == "GOV_INPUT_INVALID"
    assert "sensitive-synthetic-value" not in str(exc.value)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema_version":"commerce-policy/1.0","schema_version":"commerce-policy/1.0"}',
        b'{"intent":{"currency":"COP","currency":"COP"}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b"not-json",
        b"\xff",
        b"\xef\xbb\xbf{}",
    ],
)
def test_c01_json_invalid(payload: bytes) -> None:
    with pytest.raises(GovernanceError, match=r"^GOV_INPUT_INVALID$"):
        parse_input(payload)


def test_immutable_and_python_strict(data: SelectionPolicyInput) -> None:
    with pytest.raises(ValidationError):
        data.intent.quantity = 2
    with pytest.raises(ValidationError):
        data.merchant_ref = "other"
    with pytest.raises(ValidationError):
        SelectionPolicyInput.model_validate(data.model_dump(mode="json"))  # arrays aren't tuples
    assert isinstance(data.eligible_variant_ids, tuple)


@pytest.mark.parametrize(
    ("payload_kind", "reason"),
    [
        ("missing", "GOV_FACTS_UNAVAILABLE"),
        ("corrupt", "GOV_ARTIFACT_MISMATCH"),
        ("unknown", "GOV_ARTIFACT_MISMATCH"),
        ("version", "GOV_ARTIFACT_MISMATCH"),
        ("id", "GOV_ARTIFACT_MISMATCH"),
        ("duplicate", "GOV_ARTIFACT_MISMATCH"),
    ],
)
def test_f02_artifact(
    data: SelectionPolicyInput,
    artifact: bytes,
    metadata: EvaluationMetadata,
    payload_kind: str,
    reason: str,
) -> None:
    supplied: bytes | None = artifact
    if payload_kind == "missing":
        supplied = None
    elif payload_kind == "corrupt":
        supplied = artifact + b" "
    else:
        obj = json.loads(artifact)
        if payload_kind == "unknown":
            obj["secret"] = "synthetic-secret"
        elif payload_kind == "version":
            obj["version"] = 2
        elif payload_kind == "id":
            obj["policy_set_id"] = "other-profile"
        else:
            obj["allowed_merchant_refs"] *= 2
        supplied = canonical_bytes(obj)
    stub = Stub(None)
    result = boundary(data, supplied, metadata, lambda: stub).evaluate(data)
    assert result.reason_codes == (reason,)
    assert result.outcome == "deny" and result.origin == "runtime_guard"
    assert not result.engine.attempted and not result.rule_results and stub.calls == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_outcome",
        "unknown_reason",
        "obligations",
        "allow_fail",
        "missing_rule",
        "duplicate_rule",
        "reversed_rules",
        "deny_pass",
        "reason_mismatch",
        "decision_id",
        "phase",
        "purchase_mode",
        "mode",
        "digest",
        "off",
        "runtime_guard",
        "no_identity",
        "unknown_rule_result",
        "unknown_policy_id",
        "duplicate_reason",
        "empty_reason",
    ],
)
def test_c03_untrusted_output(
    data: SelectionPolicyInput,
    artifact: bytes,
    metadata: EvaluationMetadata,
    mutation: str,
) -> None:
    output = reference(data, artifact, metadata).evaluate(data).model_dump(mode="json")
    if mutation == "unknown_outcome":
        output["outcome"] = "maybe"
    elif mutation == "unknown_reason":
        output["reason_codes"] = ["INVENTED"]
    elif mutation == "obligations":
        output["obligations"] = ["pay"]
    elif mutation == "allow_fail":
        output["rule_results"][0]["result"] = "fail"
    elif mutation == "missing_rule":
        output["rule_results"].pop()
    elif mutation == "duplicate_rule":
        output["rule_results"] *= 2
    elif mutation == "reversed_rules":
        output["rule_results"].reverse()
    elif mutation == "deny_pass":
        output["outcome"] = "deny"
    elif mutation == "reason_mismatch":
        output["reason_codes"] = ["SELECTION_NOT_ELIGIBLE"]
    elif mutation == "decision_id":
        output["decision_id"] = "other"
    elif mutation == "phase":
        output["phase"] = "D2"
    elif mutation == "purchase_mode":
        output["purchase_mode"] = "HNP"
    elif mutation == "mode":
        output["governance_mode"] = "shadow"
    elif mutation == "digest":
        output["input_digest"] = "sha256:" + "0" * 64
    elif mutation == "off":
        output["governance_mode"] = "off"
    elif mutation == "runtime_guard":
        output.update(
            origin="runtime_guard",
            outcome="deny",
            reason_codes=["GOV_INPUT_INVALID"],
            rule_results=[],
        )
    elif mutation == "no_identity":
        output["engine"]["artifact_digest"] = None
    elif mutation == "unknown_rule_result":
        output["rule_results"][0]["result"] = "unknown"
    elif mutation == "unknown_policy_id":
        output["rule_results"][0]["policy_id"] = "POL-99"
    elif mutation == "duplicate_reason":
        output["reason_codes"] *= 2
    elif mutation == "empty_reason":
        output["reason_codes"] = []
    stub = Stub(output)
    result = boundary(data, artifact, metadata, lambda: stub).evaluate(data)
    assert result.outcome == "deny" and result.reason_codes == ("GOV_OUTPUT_INVALID",)
    assert result.engine.attempted and result.rule_results == () and stub.calls == 1


@pytest.mark.parametrize("field", ["version", "digest", "id", "engine"])
def test_output_binding_mismatch(
    data: SelectionPolicyInput,
    artifact: bytes,
    metadata: EvaluationMetadata,
    field: str,
) -> None:
    output = reference(data, artifact, metadata).evaluate(data).model_dump(mode="json")
    if field == "version":
        output["policy_binding"]["version"] = 2
    elif field == "digest":
        output["policy_binding"]["artifact_digest"] = "sha256:" + "0" * 64
    elif field == "id":
        output["policy_binding"]["policy_set_id"] = "other"
    else:
        output["engine"]["version"] = "invented-version"
    result = boundary(data, artifact, metadata, lambda: Stub(output)).evaluate(data)
    assert result.outcome == "deny" and result.reason_codes == ("GOV_ARTIFACT_MISMATCH",)


@pytest.mark.parametrize(
    "field",
    [
        "merchant_ref",
        "selected_variant_id",
        "context_version",
        "expected_local_state_version",
        "owner_ref",
        "authority_now",
        "intent",
        "policy_version",
        "policy_digest",
    ],
)
def test_c05_c08_no_transplanted_allow(
    payload: dict[str, Any],
    artifact: bytes,
    metadata: EvaluationMetadata,
    field: str,
) -> None:
    original = parse_input(canonical_bytes(payload))
    old = reference(original, artifact, metadata).evaluate(original)
    if field == "intent":
        payload["intent"]["max_total_minor"] += 1
    elif field == "policy_version":
        payload["expected_policy"]["version"] += 1
    elif field == "policy_digest":
        payload["expected_policy"]["artifact_digest"] = "sha256:" + "0" * 64
    elif field.endswith("version"):
        payload[field] += 1
    elif field == "authority_now":
        payload[field] = "2026-10-14T15:00:00.001Z"
    else:
        payload[field] = "another-id"
    changed = parse_input(canonical_bytes(payload))
    assert input_digest(changed) != input_digest(original)
    result = boundary(original, artifact, metadata, lambda: Stub(old)).evaluate(changed)
    expected = "GOV_ARTIFACT_MISMATCH" if field.startswith("policy_") else "GOV_OUTPUT_INVALID"
    assert result.outcome == "deny" and result.reason_codes == (expected,)


def test_c07_canonical_replay(payload: dict[str, Any], data: SelectionPolicyInput) -> None:
    def reverse(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: reverse(v) for k, v in reversed(list(value.items()))}
        if isinstance(value, list):
            return [reverse(v) for v in value]
        return value

    replay = parse_input(json.dumps(reverse(payload), indent=4).encode())
    assert canonical_bytes(replay) == canonical_bytes(data)
    assert input_digest(replay) == input_digest(data)
    assert (
        input_digest(data)
        == "sha256:"
        + hashlib.sha256(b"commerce-policy-input/v1\0" + canonical_bytes(data)).hexdigest()
    )
    assert input_digest(data) != sha256_digest(canonical_bytes(data))
    assert parse_input(canonical_bytes(data)) == data
    payload["eligible_variant_ids"].reverse()
    assert input_digest(parse_input(canonical_bytes(payload))) == input_digest(data)


def test_c08_sequence_order(payload: dict[str, Any], data: SelectionPolicyInput) -> None:
    payload["intent"]["hard_constraint_refs"].reverse()
    assert input_digest(parse_input(canonical_bytes(payload))) != input_digest(data)
    assert canonical_bytes({"steps": ["a", "b"]}) != canonical_bytes({"steps": ["b", "a"]})
    assert canonical_bytes({"text": "á"}) == '{"text":"á"}'.encode()
    assert canonical_bytes({"text": "é"}) != canonical_bytes({"text": "e\u0301"})


@pytest.mark.parametrize("value", [1.0, float("nan"), float("inf"), {1: "bad-key"}, {"x": b"x"}])
def test_canonical_rejects_non_json(value: Any) -> None:
    with pytest.raises(ValueError):
        canonical_bytes(value)


@pytest.mark.parametrize("failure", ["invoke", "construct"])
def test_engine_exception(
    data: SelectionPolicyInput,
    artifact: bytes,
    metadata: EvaluationMetadata,
    failure: str,
) -> None:
    def broken_factory() -> PolicyEvaluator:
        raise RuntimeError("synthetic-sensitive-error")

    stub = Stub(RuntimeError("synthetic-sensitive-error"))
    result = boundary(
        data, artifact, metadata, broken_factory if failure == "construct" else lambda: stub
    ).evaluate(data)
    assert result.outcome == "deny" and result.reason_codes == ("GOV_ENGINE_UNAVAILABLE",)
    assert result.engine.attempted == (failure == "invoke")
    assert "synthetic-sensitive-error" not in result.model_dump_json()


def test_off_no_evaluation(
    payload: dict[str, Any],
    data: SelectionPolicyInput,
    artifact: bytes,
    metadata: EvaluationMetadata,
) -> None:
    evaluator = reference(data, artifact, metadata)
    stub = Stub(evaluator.evaluate(data))
    payload["governance_mode"] = "off"
    off = parse_input(canonical_bytes(payload))
    for port in (evaluator, boundary(data, artifact, metadata, lambda: stub)):
        with pytest.raises(GovernanceError, match=r"^GOV_INPUT_INVALID$"):
            port.evaluate(off)
    assert stub.calls == 0


def test_revalidate_constructed_input(
    data: SelectionPolicyInput,
    artifact: bytes,
    metadata: EvaluationMetadata,
) -> None:
    invalid = data.model_copy(update={"merchant_ref": "invalid/merchant"})
    stub = Stub(None)
    with pytest.raises(GovernanceError, match=r"^GOV_INPUT_INVALID$"):
        boundary(data, artifact, metadata, lambda: stub).evaluate(invalid)
    assert stub.calls == 0


def test_configured_action_allowlist(
    data: SelectionPolicyInput,
    artifact: bytes,
    metadata: EvaluationMetadata,
) -> None:
    profile = json.loads(artifact)
    profile["allowed_actions"] = ["complete_purchase"]
    blocked = canonical_bytes(profile)
    binding = data.expected_policy.model_copy(update={"artifact_digest": sha256_digest(blocked)})
    payload = data.model_dump(mode="json")
    payload["expected_policy"] = binding.model_dump(mode="json")
    changed = parse_input(canonical_bytes(payload))
    decision = reference(changed, blocked, metadata).evaluate(changed)
    assert decision.reason_codes == ("ACTION_OR_MERCHANT_NOT_ALLOWED",)


def test_binding_and_artifact_strict(payload: dict[str, Any], artifact: bytes) -> None:
    binding = payload["expected_policy"]
    binding["version"] = True
    with pytest.raises(ValidationError):
        parse_model(PolicyBinding, canonical_bytes(binding))
    obj = json.loads(artifact)
    obj["allowed_actions"] *= 2
    with pytest.raises(ValidationError):
        parse_model(D1PolicyArtifact, canonical_bytes(obj))


def test_engine_identity_measured(
    data: SelectionPolicyInput,
    artifact: bytes,
    metadata: EvaluationMetadata,
) -> None:
    identity = measure_reference_engine()
    assert identity == measure_reference_engine()
    fake = identity.model_copy(update={"version": "invented"})
    with pytest.raises(GovernanceError, match=r"^GOV_ARTIFACT_MISMATCH$"):
        ReferencePolicyEvaluator(
            artifact_bytes=artifact, binding=data.expected_policy, metadata=metadata, identity=fake
        )


def test_evaluation_uses_only_prepared_data(
    data: SelectionPolicyInput,
    artifact: bytes,
    metadata: EvaluationMetadata,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = reference(data, artifact, metadata)
    port = boundary(data, artifact, metadata, lambda: evaluator)

    def forbidden_read(*args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("Evaluation must not read files")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    assert evaluator.evaluate(data) == port.evaluate(data)


@pytest.mark.parametrize("field", ["experiment_run_id", "conversation_id", "max_unit_minor"])
def test_required_nullable_fields(payload: dict[str, Any], field: str) -> None:
    target = payload["intent"] if field == "max_unit_minor" else payload
    target[field] = None
    parse_input(canonical_bytes(payload))  # explicit null, never an omitted unknown fact
    del target[field]
    with pytest.raises(GovernanceError, match=r"^GOV_INPUT_INVALID$"):
        parse_input(canonical_bytes(payload))


@pytest.mark.parametrize("failure", ["mismatch", "unavailable"])
def test_engine_identity_preflight_failure(
    data: SelectionPolicyInput,
    artifact: bytes,
    metadata: EvaluationMetadata,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    identity = measure_reference_engine()
    if failure == "mismatch":
        identity = identity.model_copy(update={"artifact_digest": "sha256:" + "0" * 64})
    else:

        def broken_measure() -> Any:
            raise OSError("synthetic-sensitive-path")

        monkeypatch.setattr(
            "commerce_agent.governance.evaluator.measure_reference_engine", broken_measure
        )
    stub = Stub(None)
    port = D1PolicyBoundary(
        binding=data.expected_policy,
        artifact_bytes=artifact,
        metadata=metadata,
        engine_identity=identity,
        evaluator_factory=lambda: stub,
    )
    result = port.evaluate(data)
    expected = "GOV_ARTIFACT_MISMATCH" if failure == "mismatch" else "GOV_ENGINE_UNAVAILABLE"
    assert result.reason_codes == (expected,) and result.outcome == "deny"
    assert not result.engine.attempted and stub.calls == 0


def test_policy_binding_is_verified_not_echoed(
    data: SelectionPolicyInput,
    artifact: bytes,
    metadata: EvaluationMetadata,
) -> None:
    # Match raw artifact digest while deliberately contradicting its embedded identity.
    binding = data.expected_policy.model_copy(update={"policy_set_id": "wrong-profile"})
    with pytest.raises(GovernanceError, match=r"^GOV_ARTIFACT_MISMATCH$"):
        ReferencePolicyEvaluator(
            artifact_bytes=artifact,
            binding=binding,
            metadata=metadata,
            identity=measure_reference_engine(),
        )
