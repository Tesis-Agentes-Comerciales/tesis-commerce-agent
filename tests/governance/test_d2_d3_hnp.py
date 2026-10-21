"""D2-HNP and D3-HNP through their boundaries (policy-acceptance-v1 §2 and §3).

P/N/F of POL-03..10 by the boundary over the reference engine and the real `b2-hnp-v1`
artifact, C01-C05 at the boundary and C02 in the artifact, and the evidence of both phases in a
real SQLite ledger: replay reproduces the decision and a second body under the same evidence ID
is a conflict (C09). The fixtures are those of HNP-03 (§1 of the acceptance: 89.000.000 plus
1.000.000 of shipping against a 100.000.000 grant).
"""

import copy
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from commerce_agent.governance.canonical import canonical_bytes, input_digest, parse_model
from commerce_agent.governance.evaluator import (
    ENGINE_SOURCES,
    D2PolicyBoundary,
    D3PolicyBoundary,
    DispatchEvaluator,
    GovernanceError,
    HnpAuthorizationEvaluator,
    HnpD2PolicyBoundary,
    ReferenceDispatchEvaluator,
    ReferenceHnpAuthorizationEvaluator,
    measure_reference_engine,
    parse_authorization_input,
    parse_dispatch_input,
    parse_hnp_authorization_input,
    verify_authorization_artifact,
    verify_dispatch_artifact,
    verify_hnp_authorization_artifact,
)
from commerce_agent.governance.evidence import (
    DecisionRecord,
    EvidenceApplication,
    EvidenceError,
    EvidenceScope,
    required_refs,
)
from commerce_agent.governance.evidence_artifacts import (
    LEGACY_SOURCE_NAMES,
    ReferenceSourceArchive,
    capture_reference_sources,
)
from commerce_agent.governance.hnp_models import (
    DispatchPolicyInput,
    HnpAuthorizationPolicyInput,
    parse_d2_input,
)
from commerce_agent.governance.hnp_policy import HNP_AUTHORIZATION_PROFILE_ID, build_hnp_policy
from commerce_agent.governance.models import (
    APPLICABLE_RULES,
    HNP_POLICY_SET_ID,
    POLICY_REASONS,
    SCHEMA,
    AuthorizationRules,
    D1PolicyArtifact,
    DispatchRules,
    EvaluationMetadata,
    HnpAuthorizationRules,
    ObjectRef,
    PolicyBinding,
    PolicyDecision,
    PolicyId,
)
from commerce_agent.governance.provenance import build_d1_policy
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore

FIXTURES = Path(__file__).parent / "fixtures"
ARTIFACT, BINDING = build_hnp_policy(merchant_ref="merchant-1")
HP_ARTIFACT, HP_BINDING = build_d1_policy(
    policy_set_id="b2-hp-v2", version=1, merchant_ref="merchant-1"
)
METADATA = EvaluationMetadata(
    schema_version=SCHEMA, decided_at="2026-10-21T16:00:00.004Z", latency_ms=4
)
D2_RULES = APPLICABLE_RULES[("D2", "HNP")]
D3_RULES = APPLICABLE_RULES[("D3", "HNP")]
OTHER_REF = {"schema_version": SCHEMA, "id": "other", "version": 1, "digest": "sha256:" + "c" * 64}
Change = Callable[[dict[str, Any]], None]


def _set(path: str, value: Any) -> Change:
    """A change at a dotted path; a numeric segment indexes a list."""

    def change(payload: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        target: Any = payload
        for key in parents:
            target = target[int(key)] if key.isdigit() else target[key]
        target[leaf] = copy.deepcopy(value)

    return change


def _both(*changes: Change) -> Change:
    def change(payload: dict[str, Any]) -> None:
        for each in changes:
            each(payload)

    return change


def _bound(payload: dict[str, Any], *paths: str) -> dict[str, Any]:
    """The fixture under the real `b2-hnp-v1` binding, as the gate assembles it."""
    binding = BINDING.model_dump(mode="json")
    payload["expected_policy"] = binding
    for path in paths:
        _set(path, binding)(payload)
    return payload


def d2_payload() -> dict[str, Any]:
    return _bound(json.loads((FIXTURES / "d2-hnp-input.json").read_bytes()), "d1.policy_binding")


def d3_payload() -> dict[str, Any]:
    return _bound(json.loads((FIXTURES / "d3-hnp-input.json").read_bytes()), "d2.policy_binding")


def d2(change: Change | None = None) -> HnpAuthorizationPolicyInput:
    payload = d2_payload()
    if change:
        change(payload)
    return parse_hnp_authorization_input(canonical_bytes(payload))


def d3(change: Change | None = None) -> DispatchPolicyInput:
    payload = d3_payload()
    if change:
        change(payload)
    return parse_dispatch_input(canonical_bytes(payload))


def reference_d2(binding: PolicyBinding = BINDING) -> ReferenceHnpAuthorizationEvaluator:
    return ReferenceHnpAuthorizationEvaluator(
        artifact_bytes=ARTIFACT,
        binding=binding,
        metadata=METADATA,
        identity=measure_reference_engine(),
    )


def reference_d3() -> ReferenceDispatchEvaluator:
    return ReferenceDispatchEvaluator(
        artifact_bytes=ARTIFACT,
        binding=BINDING,
        metadata=METADATA,
        identity=measure_reference_engine(),
    )


def boundary_d2(
    factory: Callable[[], HnpAuthorizationEvaluator] = reference_d2,
    *,
    artifact: bytes | None = ARTIFACT,
    binding: PolicyBinding = BINDING,
) -> HnpD2PolicyBoundary:
    return HnpD2PolicyBoundary(
        binding=binding,
        artifact_bytes=artifact,
        engine_identity=measure_reference_engine(),
        metadata=METADATA,
        evaluator_factory=factory,
    )


def boundary_d3(
    factory: Callable[[], DispatchEvaluator] = reference_d3,
    *,
    artifact: bytes | None = ARTIFACT,
    binding: PolicyBinding = BINDING,
) -> D3PolicyBoundary:
    return D3PolicyBoundary(
        binding=binding,
        artifact_bytes=artifact,
        engine_identity=measure_reference_engine(),
        metadata=METADATA,
        evaluator_factory=factory,
    )


class Stub:
    """An engine that answers what it is told, for D2 and D3 alike."""

    def __init__(self, response: object):
        self.response = response
        self.calls = 0

    def evaluate(self, policy_input: Any) -> PolicyDecision:
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return cast(PolicyDecision, self.response)


def failed(decision: PolicyDecision) -> list[PolicyId]:
    return [rule.policy_id for rule in decision.rule_results if rule.result == "fail"]


def runtime_guard(decision: PolicyDecision) -> tuple[Any, ...]:
    assert (decision.outcome, decision.origin, decision.rule_results) == (
        "deny",
        "runtime_guard",
        (),
    )
    return decision.reason_codes, decision.engine.attempted


# P: the valid fixtures allow through the boundary, with every rule of their row.


def test_p_d2_hnp_allows_with_pol03_to_pol09() -> None:
    data = d2()
    result = boundary_d2().evaluate(data)

    assert (result.phase, result.purchase_mode, result.outcome, result.origin) == (
        "D2",
        "HNP",
        "allow",
        "policy",
    )
    assert result.reason_codes == ("ALL_APPLICABLE_POLICIES_SATISFIED",)
    assert [(rule.policy_id, rule.result) for rule in result.rule_results] == [
        (policy, "pass") for policy in D2_RULES
    ]
    assert (result.input_digest, result.policy_binding) == (input_digest(data), BINDING)
    assert result.engine == measure_reference_engine()


def test_p_d3_hnp_allows_with_pol09_and_pol10() -> None:
    data = d3()
    result = boundary_d3().evaluate(data)

    assert (result.phase, result.outcome, result.reason_codes) == (
        "D3",
        "allow",
        ("ALL_APPLICABLE_POLICIES_SATISFIED",),
    )
    assert [(rule.policy_id, rule.result) for rule in result.rule_results] == [
        ("POL-09", "pass"),
        ("POL-10", "pass"),
    ]


def test_p09_a_retry_with_its_own_identical_reservation_allows() -> None:
    def own(payload: dict[str, Any]) -> None:
        intent = payload["hnp"]["reservation_intent"]
        payload["hnp"]["usage"]["same_operation_reservation"] = {
            **intent,
            "state": "reserved",
            "state_version": 1,
        }

    assert boundary_d2().evaluate(d2(own)).outcome == "allow"


# N: each change fails exactly the rules listed, every rule is evaluated (no short-circuit).

D2_VIOLATIONS: dict[str, tuple[tuple[PolicyId, ...], Change]] = {
    "N03 not authenticated": (("POL-03",), _set("principal.authentication_verified", False)),
    "N03 not the owner's run": (("POL-03",), _set("principal.owns_run", False)),
    "N03 action not permitted": (
        ("POL-03",),
        _set("principal.permitted_actions", ["prepare_checkout"]),
    ),
    "N03 basis of a human": (("POL-03",), _set("principal.authentication_basis", "user_jwt")),
    "N04 d1 denied": (("POL-04",), _set("d1.outcome", "deny")),
    "N04 d1 of another run": (("POL-04",), _set("d1.run_id", "run-2")),
    "N04 d1 for another intent": (("POL-04",), _set("d1.intent_ref", OTHER_REF)),
    "N04 d1 observed in shadow": (("POL-04",), _set("d1.governance_mode", "shadow")),
    "N04 d1 of another merchant": (("POL-04",), _set("d1.merchant_ref", "merchant-2")),
    # The grant and the reservation belong to the run's owner (POL-04), which POL-07 and
    # POL-09 also check from their side.
    "N04 grant of another owner": (
        ("POL-04", "POL-07"),
        _set("hnp.grant.owner_ref", "owner:" + "6" * 64),
    ),
    "N04 reservation of another run": (
        ("POL-04", "POL-09"),
        _set("hnp.reservation_intent.run_id", "run-2"),
    ),
    "N05 total over the intent": (("POL-05",), _set("intent.max_total_minor", 89_999_999)),
    "N05 unit over the intent": (("POL-05",), _set("intent.max_unit_minor", 88_999_999)),
    "N06 d1 under another revision": (("POL-06",), _set("d1.policy_binding.version", 9)),
    "N06 another context": (("POL-06",), _set("context_version", 2)),
    "N07 grant revoked": (("POL-07",), _set("hnp.grant.state", "revoked")),
    "N07 now at expires_at": (
        ("POL-07",),
        _set("hnp.grant.expires_at", "2026-10-21T16:00:00.000Z"),
    ),
    "N07 before valid_from": (
        ("POL-07",),
        _set("hnp.grant.valid_from", "2026-10-21T16:00:00.001Z"),
    ),
    "N07 open proof invalid": (("POL-07",), _set("hnp.open_proof.status", "invalid")),
    "N07 proof of another agent key": (
        ("POL-07",),
        _set("hnp.open_proof.verified_agent_key_ref", "agent-key-2"),
    ),
    "N08 another destination": (
        ("POL-08",),
        _set("hnp.grant.scope.destination_binding.value", "4" * 64),
    ),
    "N08 another merchant": (("POL-08",), _set("hnp.grant.scope.merchant_refs", ["merchant-2"])),
    "N08 another variant": (("POL-08",), _set("hnp.grant.scope.variant_ids", ["variant-2"])),
    "N08 total over the grant": (
        ("POL-08",),
        _both(
            _set("hnp.grant.scope.total_max_minor", 89_999_999),
            _set("intent.max_total_minor", 100_000_000),
        ),
    ),
    "N08 another instrument": (("POL-08",), _set("hnp.requested_instrument_ref", "card")),
    "N09 use consumed": (
        ("POL-09",),
        _both(
            _set("hnp.usage.uses_consumed", 1),
            _set("hnp.usage.amount_consumed_minor", 90_000_000),
        ),
    ),
    "N09 reserved by another operation": (
        ("POL-09",),
        _both(
            _set("hnp.usage.uses_reserved_other", 1),
            _set("hnp.usage.amount_reserved_other_minor", 90_000_000),
        ),
    ),
    "N09 own amount differs": (("POL-09",), _set("hnp.reservation_intent.amount_minor", 1)),
    "N09 previous epoch": (("POL-09",), _set("hnp.reservation_intent.expected_epoch", 2)),
}


@pytest.mark.parametrize("case", sorted(D2_VIOLATIONS))
def test_n_d2_hnp_each_change_fails_exactly_its_rules(case: str) -> None:
    policies, change = D2_VIOLATIONS[case]
    stub_free = boundary_d2().evaluate(d2(change))

    assert (stub_free.outcome, stub_free.origin) == ("deny", "policy")
    assert tuple(failed(stub_free)) == policies
    assert stub_free.reason_codes == tuple(POLICY_REASONS[policy] for policy in policies)
    assert [rule.policy_id for rule in stub_free.rule_results] == list(D2_RULES)


D3_VIOLATIONS: dict[str, tuple[tuple[PolicyId, ...], Change]] = {
    "N09 reservation of another amount": (
        ("POL-09",),
        _both(
            _set("reservation.amount_minor", 1),
            _set("usage.same_operation_reservation.amount_minor", 1),
        ),
    ),
    "N09 another use consumed": (
        ("POL-09",),
        _both(
            _set("usage.uses_consumed", 1),
            _set("usage.amount_consumed_minor", 90_000_000),
            _set("usage.same_operation_reservation.state", "released"),
            _set("reservation.state", "released"),
        ),
    ),
    "N10 d2 denied": (("POL-10",), _set("d2.outcome", "deny")),
    "N10 closed proof invalid": (("POL-10",), _set("closed_proof.status", "invalid")),
    "N10 proof of another authorization": (
        ("POL-10",),
        _set("closed_proof.authorization_id", "authorization-2"),
    ),
    "N10 terms other than d2's": (
        ("POL-10",),
        _set("d2.material_digest", "sha256:" + "3" * 64),
    ),
    "N10 grant revoked": (("POL-10",), _set("grant.state", "revoked")),
    "N10 epoch changed, reservation lost": (
        ("POL-09", "POL-10"),
        _set("grant.authority_epoch", 2),
    ),
}


@pytest.mark.parametrize("case", sorted(D3_VIOLATIONS))
def test_n_d3_hnp_each_change_fails_exactly_its_rules(case: str) -> None:
    policies, change = D3_VIOLATIONS[case]
    result = boundary_d3().evaluate(d3(change))

    assert (result.outcome, result.origin) == ("deny", "policy")
    assert tuple(failed(result)) == policies
    assert result.reason_codes == tuple(POLICY_REASONS[policy] for policy in policies)


# F: guards before the engine. The engine is never called and no rule is reported.

D2_GUARDS: dict[str, tuple[str, Change]] = {
    "F07 usage of another grant": (
        "GOV_FACTS_UNAVAILABLE",
        _set("hnp.usage.grant_ref", OTHER_REF),
    ),
    "F09 usage at another version than expected": (
        "GOV_FACTS_UNAVAILABLE",
        _set("hnp.usage.state_version", 2),
    ),
    "terms stale": ("GOV_FRESHNESS_UNRESOLVED", _set("freshness.status", "stale")),
    "terms expired": (
        "GOV_FRESHNESS_UNRESOLVED",
        _set("terms.commercial_expires_at", "2026-10-21T16:00:00.000Z"),
    ),
    "F07 profile unresolved": (
        "GOV_AUTHORIZATION_PROFILE_UNRESOLVED",
        _set(
            "authorization_profile",
            {"schema_version": SCHEMA, "status": "unresolved", "profile_ref": None},
        ),
    ),
    "F07 open proof unresolved": (
        "GOV_AUTHORIZATION_PROFILE_UNRESOLVED",
        _set("hnp.open_proof.status", "unresolved"),
    ),
    "F08 profile the artifact does not admit": (
        "GOV_AUTHORIZATION_PROFILE_UNRESOLVED",
        _both(
            _set("authorization_profile.profile_ref.id", "profile:other"),
            _set("hnp.open_proof.profile_ref.id", "profile:other"),
        ),
    ),
    "F08 proof checked against another profile": (
        "GOV_AUTHORIZATION_PROFILE_UNRESOLVED",
        _set("hnp.open_proof.profile_ref.version", 2),
    ),
}


@pytest.mark.parametrize("case", sorted(D2_GUARDS))
def test_f_d2_hnp_guards_deny_before_the_engine(case: str) -> None:
    reason, change = D2_GUARDS[case]
    stub = Stub(AssertionError("never invoked"))

    result = boundary_d2(lambda: stub).evaluate(d2(change))

    assert runtime_guard(result) == ((reason,), False)
    assert stub.calls == 0


D3_GUARDS: dict[str, tuple[str, Change]] = {
    "usage without this reservation": (
        "GOV_FACTS_UNAVAILABLE",
        _set("usage.same_operation_reservation", None),
    ),
    "terms stale": ("GOV_FRESHNESS_UNRESOLVED", _set("freshness.status", "unresolved")),
    "closed proof unresolved": (
        "GOV_AUTHORIZATION_PROFILE_UNRESOLVED",
        _set("closed_proof.status", "unresolved"),
    ),
    "closed proof under a profile not admitted": (
        "GOV_AUTHORIZATION_PROFILE_UNRESOLVED",
        _set("closed_proof.profile_ref.id", "profile:other"),
    ),
}


@pytest.mark.parametrize("case", sorted(D3_GUARDS))
def test_f_d3_hnp_guards_deny_before_the_engine(case: str) -> None:
    reason, change = D3_GUARDS[case]
    stub = Stub(AssertionError("never invoked"))

    result = boundary_d3(lambda: stub).evaluate(d3(change))

    assert runtime_guard(result) == ((reason,), False)
    assert stub.calls == 0


def test_f_an_invalid_proof_is_a_policy_deny_not_a_guard() -> None:
    """`invalid` reaches the rules (POL-07 / POL-10); only `unresolved` stops before them."""
    d2_result = boundary_d2().evaluate(d2(_set("hnp.open_proof.status", "invalid")))
    d3_result = boundary_d3().evaluate(d3(_set("closed_proof.status", "invalid")))

    assert (d2_result.origin, failed(d2_result)) == ("policy", ["POL-07"])
    assert (d3_result.origin, failed(d3_result)) == ("policy", ["POL-10"])


@pytest.mark.parametrize("phase", ["D2", "D3"])
def test_f02_an_unrecoverable_or_foreign_artifact_never_reaches_the_engine(phase: str) -> None:
    stub = Stub(AssertionError("never invoked"))
    other = BINDING.model_copy(update={"artifact_digest": "sha256:" + "0" * 64})
    if phase == "D2":
        missing = boundary_d2(lambda: stub, artifact=None).evaluate(d2())
        foreign = boundary_d2(lambda: stub, binding=other).evaluate(d2())
    else:
        missing = boundary_d3(lambda: stub, artifact=None).evaluate(d3())
        foreign = boundary_d3(lambda: stub, binding=other).evaluate(d3())

    assert runtime_guard(missing) == (("GOV_FACTS_UNAVAILABLE",), False)
    assert runtime_guard(foreign) == (("GOV_ARTIFACT_MISMATCH",), False)
    assert stub.calls == 0


# C01: strict input, rejected before any engine.


@pytest.mark.parametrize(
    "change",
    [
        _set("unexpected", True),
        _set("hnp.grant.state", "paused"),
        _set("schema_version", "commerce-policy/2.0"),
        _set("hnp.open_proof.status", "maybe"),
        _set("terms.total_minor", "90000000"),
        _set("hnp.grant.scope.variant_ids", ["variant-2", "variant-1"]),
        _set("operation_id", None),
        _set("hnp.reservation_intent.amount_minor", True),
    ],
)
def test_c01_the_d2_hnp_input_is_strict(change: Change) -> None:
    payload = d2_payload()
    change(payload)

    with pytest.raises(GovernanceError) as rejected:
        parse_hnp_authorization_input(json.dumps(payload).encode())

    assert rejected.value.reason == "GOV_INPUT_INVALID"


def test_c01_duplicate_keys_and_unknown_d3_fields_are_rejected() -> None:
    duplicate = canonical_bytes(d3_payload())[:-1] + b',"operation_id":"op_2"}'
    unknown = d3_payload() | {"human_approved": True}

    for payload in (duplicate, canonical_bytes(unknown)):
        with pytest.raises(GovernanceError) as rejected:
            parse_dispatch_input(payload)
        assert rejected.value.reason == "GOV_INPUT_INVALID"


# C02: one branch per purchase mode and one policy set per mode.


def test_c02_the_d2_union_never_falls_back_to_the_other_branch() -> None:
    hnp = d2_payload()
    hp = json.loads((FIXTURES / "d2-input.json").read_bytes())
    assert isinstance(parse_d2_input(canonical_bytes(hnp)), HnpAuthorizationPolicyInput)

    hnp_with_hp = hnp | {"hp": hp["hp"]}
    hp_with_hnp = hp | {"hnp": hnp["hnp"]}
    hp_as_hnp = hp | {"purchase_mode": "HNP"}
    for payload in (hnp_with_hp, hp_with_hnp, hp_as_hnp, hnp | {"human_approved": True}):
        with pytest.raises(ValueError):
            parse_d2_input(canonical_bytes(payload))
        with pytest.raises(GovernanceError):
            parse_hnp_authorization_input(canonical_bytes(payload))
    with pytest.raises(GovernanceError):
        parse_authorization_input(canonical_bytes(hnp))


def test_c02_an_hnp_decision_under_an_hp_policy_never_reaches_the_engine() -> None:
    stub = Stub(AssertionError("never invoked"))
    hp_policy = _set("expected_policy", HP_BINDING.model_dump(mode="json"))

    d2_result = HnpD2PolicyBoundary(
        binding=HP_BINDING,
        artifact_bytes=HP_ARTIFACT,
        engine_identity=measure_reference_engine(),
        metadata=METADATA,
        evaluator_factory=lambda: stub,
    ).evaluate(d2(hp_policy))
    d3_result = D3PolicyBoundary(
        binding=HP_BINDING,
        artifact_bytes=HP_ARTIFACT,
        engine_identity=measure_reference_engine(),
        metadata=METADATA,
        evaluator_factory=lambda: stub,
    ).evaluate(d3(hp_policy))

    assert runtime_guard(d2_result) == (("GOV_ARTIFACT_MISMATCH",), False)
    assert runtime_guard(d3_result) == (("GOV_ARTIFACT_MISMATCH",), False)
    assert stub.calls == 0


def test_c02_an_hp_approval_under_b2_hnp_v1_never_reaches_the_engine() -> None:
    payload = json.loads((FIXTURES / "d2-input.json").read_bytes())
    payload["expected_policy"] = BINDING.model_dump(mode="json")
    data = parse_authorization_input(canonical_bytes(payload))
    stub = Stub(AssertionError("never invoked"))

    result = D2PolicyBoundary(
        binding=BINDING,
        artifact_bytes=ARTIFACT,
        engine_identity=measure_reference_engine(),
        metadata=METADATA,
        evaluator_factory=lambda: stub,
    ).evaluate(data)

    assert runtime_guard(result) == (("GOV_ARTIFACT_MISMATCH",), False)
    assert stub.calls == 0


def test_c02_each_artifact_carries_only_the_sections_of_its_mode() -> None:
    artifact = json.loads(ARTIFACT)
    hp = json.loads(HP_ARTIFACT)
    hp_rules = copy.deepcopy(hp["authorization"])
    hnp_rules = copy.deepcopy(artifact["authorization"])
    for invalid in (
        artifact | {"authorization": hp_rules},
        hp | {"authorization": hnp_rules},
        hp | {"dispatch": artifact["dispatch"]},
        artifact | {"authorization": None},
        artifact | {"authorization": hnp_rules | {"profiles": []}},
        artifact | {"dispatch": artifact["dispatch"] | {"policy_ids": ["POL-10"]}},
    ):
        with pytest.raises(ValidationError):
            D1PolicyArtifact.model_validate_json(json.dumps(invalid))

    assert isinstance(verify_hnp_authorization_artifact(ARTIFACT, BINDING), HnpAuthorizationRules)
    assert verify_hnp_authorization_artifact(ARTIFACT, BINDING).profiles == (
        HNP_AUTHORIZATION_PROFILE_ID,
    )
    assert isinstance(verify_dispatch_artifact(ARTIFACT, BINDING), DispatchRules)
    assert isinstance(verify_authorization_artifact(HP_ARTIFACT, HP_BINDING), AuthorizationRules)
    for verify, payload, binding in (
        (verify_authorization_artifact, ARTIFACT, BINDING),
        (verify_hnp_authorization_artifact, HP_ARTIFACT, HP_BINDING),
        (verify_dispatch_artifact, HP_ARTIFACT, HP_BINDING),
    ):
        with pytest.raises(GovernanceError) as refused:
            verify(payload, binding)
        assert refused.value.reason == "GOV_ARTIFACT_MISMATCH"


def test_the_hnp_rows_are_the_rows_of_the_rules_module() -> None:
    from commerce_agent.governance.hnp_rules import DELEGATION_RULES, DISPATCH_RULES

    assert (*APPLICABLE_RULES[("D2", "HP")], *DELEGATION_RULES) == D2_RULES
    assert D3_RULES == DISPATCH_RULES
    assert BINDING.policy_set_id == HNP_POLICY_SET_ID


# C03 and C04: an engine that fails or answers something else never allows.


def _allowed(phase: str) -> PolicyDecision:
    return reference_d2().evaluate(d2()) if phase == "D2" else reference_d3().evaluate(d3())


def _evaluate(phase: str, stub: Stub) -> PolicyDecision:
    if phase == "D2":
        return boundary_d2(lambda: stub).evaluate(d2())
    return boundary_d3(lambda: stub).evaluate(d3())


@pytest.mark.parametrize("phase", ["D2", "D3"])
@pytest.mark.parametrize(
    "answer",
    ["unknown reason", "allow with a failed rule", "missing rule", "duplicated rule", "garbage"],
)
def test_c03_an_invalid_engine_output_is_gov_output_invalid(phase: str, answer: str) -> None:
    raw = _allowed(phase).model_dump(mode="json")
    rules = raw["rule_results"]
    response = {
        "unknown reason": raw | {"reason_codes": ["SOMETHING_ELSE"]},
        "allow with a failed rule": raw
        | {"rule_results": [rules[0] | {"result": "fail"}, *rules[1:]]},
        "missing rule": raw | {"rule_results": rules[:-1]},
        "duplicated rule": raw | {"rule_results": [*rules, rules[-1]]},
        "garbage": {"outcome": "allow"},
    }[answer]

    result = _evaluate(phase, Stub(response))

    assert runtime_guard(result) == (("GOV_OUTPUT_INVALID",), True)


@pytest.mark.parametrize("phase", ["D2", "D3"])
def test_c04_an_engine_that_raises_is_gov_engine_unavailable(phase: str) -> None:
    raising = _evaluate(phase, Stub(RuntimeError("engine down")))
    timing_out = _evaluate(phase, Stub(TimeoutError("timeout")))

    def broken_factory() -> Stub:
        raise ConnectionError("unreachable")

    if phase == "D2":
        unreachable = boundary_d2(broken_factory).evaluate(d2())
    else:
        unreachable = boundary_d3(broken_factory).evaluate(d3())

    assert runtime_guard(raising) == (("GOV_ENGINE_UNAVAILABLE",), True)
    assert runtime_guard(timing_out) == (("GOV_ENGINE_UNAVAILABLE",), True)
    assert runtime_guard(unreachable) == (("GOV_ENGINE_UNAVAILABLE",), False)


# C05: an allow of another input, policy or engine is never reused.


@pytest.mark.parametrize("phase", ["D2", "D3"])
def test_c05_an_allow_for_another_input_is_not_reused(phase: str) -> None:
    allowed = _allowed(phase)
    other_input = allowed.model_copy(update={"input_digest": "sha256:" + "1" * 64})
    other_decision = allowed.model_copy(update={"decision_id": "decision-other"})

    assert runtime_guard(_evaluate(phase, Stub(other_input))) == (("GOV_OUTPUT_INVALID",), True)
    assert runtime_guard(_evaluate(phase, Stub(other_decision))) == (("GOV_OUTPUT_INVALID",), True)


def test_c05_an_altered_fact_changes_the_digest_and_its_allow_does_not_apply() -> None:
    allowed = _allowed("D2")
    altered = d2(_set("terms.fulfillment_service_ref", "shipping-2"))
    assert input_digest(altered) != allowed.input_digest

    result = boundary_d2(lambda: Stub(allowed)).evaluate(altered)

    assert runtime_guard(result) == (("GOV_OUTPUT_INVALID",), True)


def test_c05_an_engine_of_another_identity_is_an_artifact_mismatch() -> None:
    allowed = _allowed("D3")
    foreign = allowed.model_copy(
        update={"engine": allowed.engine.model_copy(update={"version": "other-engine:1"})}
    )

    assert runtime_guard(_evaluate("D3", Stub(foreign))) == (("GOV_ARTIFACT_MISMATCH",), True)


def test_off_never_has_a_d2_or_d3_decision() -> None:
    off = _both(_set("governance_mode", "off"))
    with pytest.raises(GovernanceError):
        boundary_d2().evaluate(d2(_both(off, _set("d1.governance_mode", "off"))))
    with pytest.raises(GovernanceError):
        boundary_d3().evaluate(d3(_both(off, _set("d2.governance_mode", "off"))))


# The engine identity covers the HNP rules (K9), and archives before HNP still verify.


def test_the_measured_engine_includes_the_hnp_models_and_rules() -> None:
    assert ENGINE_SOURCES[-2:] == ("hnp_models.py", "hnp_rules.py")
    archive = parse_model(ReferenceSourceArchive, capture_reference_sources())
    assert tuple(source.name for source in archive.sources) == ENGINE_SOURCES
    assert archive.engine_digest() == measure_reference_engine().artifact_digest


def test_an_archive_of_the_engine_before_hnp_stays_verifiable() -> None:
    archive = json.loads(capture_reference_sources())
    archive["sources"] = [
        source for source in archive["sources"] if source["name"] in LEGACY_SOURCE_NAMES
    ]
    legacy = parse_model(ReferenceSourceArchive, canonical_bytes(archive))
    assert tuple(source.name for source in legacy.sources) == LEGACY_SOURCE_NAMES

    archive["sources"] = archive["sources"][:3]
    with pytest.raises(ValidationError):
        parse_model(ReferenceSourceArchive, canonical_bytes(archive))


# Evidence of D2-HNP and D3 in a real ledger: scoped, replayable and append-only (C05, C09).


class Applied:
    """Stands in for the transition the gate verifies on the ledger's connection."""

    def verify(self, connection: sqlite3.Connection, run_id: str, application: Any) -> bool:
        return True


def _ref(name: str, digest: str | None) -> ObjectRef:
    assert digest is not None
    return ObjectRef(schema_version=SCHEMA, id=name, version=1, digest=digest)


def _scope(data: HnpAuthorizationPolicyInput | DispatchPolicyInput) -> EvidenceScope:
    return EvidenceScope(
        schema_version=SCHEMA,
        run_id=data.run_id,
        trace_id=data.trace_id,
        owner_ref=data.owner_ref,
        purchase_mode="HNP",
        governance_mode="enforce",
        phase=data.phase,
        context_version=data.context_version,
        policy_binding=data.expected_policy,
        source_refs=tuple(dict.fromkeys(required_refs(data))),
        policy_artifact_ref=_ref("policy:b2-hnp-v1", BINDING.artifact_digest),
        engine_artifact_ref=_ref("engine", measure_reference_engine().artifact_digest),
    )


def _record(
    data: HnpAuthorizationPolicyInput | DispatchPolicyInput,
    decision: PolicyDecision,
    sequence: int,
    transition_ref: str,
) -> DecisionRecord:
    scope = _scope(data)
    expected = data.expected_local_state_version
    return DecisionRecord(
        schema_version=SCHEMA,
        record_kind="decision_record",
        evidence_id=f"evidence-{data.phase.lower()}",
        sequence=sequence,
        recorded_at=decision.decided_at,
        canonicalization_profile="commerce-canonical-json/1",
        canonical_input_bytes=canonical_bytes(data).decode(),
        decision=decision,
        policy_artifact_ref=scope.policy_artifact_ref,
        engine_artifact_ref=scope.engine_artifact_ref,
        source_refs=scope.source_refs,
        application=EvidenceApplication(
            schema_version=SCHEMA,
            disposition="transition_committed",
            transition_ref=transition_ref,
            expected_state_version=expected,
            resulting_state_version=expected + 1,
        ),
        replayability="full",
    )


def _ledger(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _register(connection: sqlite3.Connection, *inputs: Any) -> SqliteGovernanceEvidenceStore:
    ledger = SqliteGovernanceEvidenceStore(connection)
    episode = _scope(inputs[0]).model_copy(
        update={
            "phase": "D1",
            "source_refs": (),
            "policy_artifact_ref": None,
            "engine_artifact_ref": None,
        }
    )
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        ledger.initialize()
        ledger.initialize_decision_scopes()
        ledger.register_episode(episode, policy_bytes=None, engine_bytes=None)
        for data in inputs:
            ledger.register_decision_scope(
                data.decision_id,
                _scope(data),
                policy_bytes=ARTIFACT,
                engine_bytes=capture_reference_sources(),
            )
    return ledger


def _append(
    connection: sqlite3.Connection, ledger: SqliteGovernanceEvidenceStore, record: DecisionRecord
) -> None:
    ledger.prepare_decision(record)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        ledger.record_decision(
            record,
            expected_state_version=record.application.expected_state_version,
            transition_verifier=Applied(),
        )


def test_d2_and_d3_evidence_replays_from_the_ledger(tmp_path: Path) -> None:
    d2_input, d3_input = d2(), d3()
    d2_record = _record(d2_input, boundary_d2().evaluate(d2_input), 1, "rsv_1")
    d3_record = _record(d3_input, boundary_d3().evaluate(d3_input), 2, "ob_1")

    with closing(_ledger(tmp_path / "ledger.db")) as connection:
        ledger = _register(connection, d2_input, d3_input)
        assert ledger.admits_phase_d3()
        _append(connection, ledger, d2_record)
        _append(connection, ledger, d3_record)
        phases = connection.execute(
            "SELECT phase, expected_state_version FROM governance_evidence ORDER BY sequence"
        ).fetchall()
        replayed = [ledger.replay(record.evidence_id) for record in (d2_record, d3_record)]
        stored = ledger.for_run("run-1")

    assert phases == [("D2", 1), ("D3", 2)]
    assert stored == (d2_record, d3_record)
    for replay, record in zip(replayed, (d2_record, d3_record), strict=True):
        assert (replay.outcome, replay.reason_codes, replay.rule_results) == (
            record.decision.outcome,
            record.decision.reason_codes,
            record.decision.rule_results,
        )
    assert isinstance(stored[1], DecisionRecord)
    assert stored[1].any_input() == d3_input


def test_c05_evidence_of_another_state_version_is_a_conflict(tmp_path: Path) -> None:
    data = d2()
    record = _record(data, boundary_d2().evaluate(data), 1, "rsv_1")

    with closing(_ledger(tmp_path / "ledger.db")) as connection:
        ledger = _register(connection, data)
        ledger.prepare_decision(record)
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(EvidenceError) as conflict:
            ledger.record_decision(record, expected_state_version=2, transition_verifier=Applied())
        connection.rollback()

    assert conflict.value.code == "GOV_STATE_CONFLICT"


def test_c09_one_evidence_id_never_gets_a_second_body(tmp_path: Path) -> None:
    data = d3()
    record = _record(data, boundary_d3().evaluate(data), 1, "ob_1")
    other = record.model_copy(
        update={"application": record.application.model_copy(update={"transition_ref": "ob_2"})}
    )

    with closing(_ledger(tmp_path / "ledger.db")) as connection:
        ledger = _register(connection, data)
        _append(connection, ledger, record)
        _append(connection, ledger, record)  # The same body again is the same evidence.
        with pytest.raises(EvidenceError) as conflict:
            _append(connection, ledger, other)
        count = connection.execute("SELECT COUNT(*) FROM governance_evidence").fetchone()[0]
        stored = ledger.get(record.evidence_id)

    assert conflict.value.code == "GOV_STATE_CONFLICT"
    assert (count, stored) == (1, record)


def test_c09_a_concurrent_sequence_is_a_conflict(tmp_path: Path) -> None:
    d2_input, d3_input = d2(), d3()
    d2_record = _record(d2_input, boundary_d2().evaluate(d2_input), 1, "rsv_1")
    racing = _record(d3_input, boundary_d3().evaluate(d3_input), 1, "ob_1")

    with closing(_ledger(tmp_path / "ledger.db")) as connection:
        ledger = _register(connection, d2_input, d3_input)
        _append(connection, ledger, d2_record)
        with pytest.raises(EvidenceError) as conflict:
            _append(connection, ledger, racing)

    assert conflict.value.code == "GOV_STATE_CONFLICT"


def test_a_ledger_created_before_d3_is_detected(tmp_path: Path) -> None:
    with closing(_ledger(tmp_path / "legacy.db")) as connection:
        ledger = SqliteGovernanceEvidenceStore(connection)
        assert ledger.admits_phase_d3()
        connection.execute(
            "CREATE TABLE governance_evidence (evidence_id TEXT PRIMARY KEY, "
            "phase TEXT NOT NULL CHECK(phase IN ('D1','D2')))"
        )
        assert ledger.admits_phase_d2()
        assert not ledger.admits_phase_d3()
