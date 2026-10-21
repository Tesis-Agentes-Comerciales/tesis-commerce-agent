"""D1 for Human Not Present: the `(D1, HNP)` row, `b2-hnp-v1` and C02 at the D1 boundary.

docs/design/hnp-governed.md §7. HP keeps its bytes: `b2-hp-v2`, `b2-pilot-v1` and the HP evidence
are pinned with the values computed on `main` before HNP-05.
"""

import json
from typing import Any

import pytest

from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.evaluator import (
    D1PolicyBoundary,
    GovernanceError,
    ReferencePolicyEvaluator,
    measure_reference_engine,
    parse_input,
    verify_artifact,
)
from commerce_agent.governance.evidence import DecisionRecord, EvidenceApplication
from commerce_agent.governance.hnp_policy import (
    GRANT_SCOPE_ELIGIBILITY,
    HNP_POLICY_REVISION,
    HnpGrant,
    build_hnp_policy,
    grant_scope_eligibility,
    hnp_d1_configuration,
    registered_grant,
)
from commerce_agent.governance.models import (
    APPLICABLE_RULES,
    D1_PILOT_RULES,
    HNP_POLICY_SET_ID,
    SCHEMA,
    D1PolicyArtifact,
    EngineIdentity,
    HnpAuthorizationRules,
    ObjectRef,
    PolicyDecision,
    SelectionPolicyInput,
    policy_set_matches_mode,
    rules_apply,
)
from commerce_agent.governance.provenance import (
    ClockProfile,
    D1EnforceConfiguration,
    build_d1_policy,
)
from tests.governance.test_d1_budget import MERCHANT_REF, METADATA, evaluate, policy_input

GRANT = HnpGrant(grant_id="dg_0123456789abcdef", variant_ids=("variant-2", "variant-1"))


def hnp_input(
    *, artifact: bytes | None = None, eligible: tuple[str, ...] = ("variant-1",), **changes: Any
) -> tuple[SelectionPolicyInput, bytes]:
    """The S02 fixture input as an HNP episode bound to `b2-hnp-v1` (or to `artifact`)."""
    data, _ = policy_input()
    payload, binding = build_hnp_policy(merchant_ref=MERCHANT_REF)
    if artifact is not None:
        payload = artifact
        parsed = json.loads(artifact)
        binding = binding.model_copy(
            update={
                "policy_set_id": parsed["policy_set_id"],
                "artifact_digest": sha256_digest(artifact),
            }
        )
    changed = json.loads(canonical_bytes(data)) | {
        "purchase_mode": "HNP",
        "expected_policy": binding.model_dump(mode="json"),
        "eligible_variant_ids": list(eligible),
        **changes,
    }
    return parse_input(canonical_bytes(changed)), payload


class Untouchable:
    """An engine factory the boundary must never reach."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> ReferencePolicyEvaluator:
        self.calls += 1
        raise AssertionError("The boundary called the engine")


def boundary(data: SelectionPolicyInput, artifact: bytes, factory: Any) -> PolicyDecision:
    return D1PolicyBoundary(
        binding=data.expected_policy,
        artifact_bytes=artifact,
        metadata=METADATA,
        engine_identity=measure_reference_engine(),
        evaluator_factory=factory,
    ).evaluate(data)


def test_hnp_adds_its_own_d1_row_with_the_same_rules() -> None:
    assert dict(APPLICABLE_RULES) == {
        ("D1", "HP"): ("POL-01", "POL-02", "POL-11"),
        ("D2", "HP"): ("POL-03", "POL-04", "POL-05", "POL-06"),
        ("D1", "HNP"): ("POL-01", "POL-02", "POL-11"),
        ("D2", "HNP"): ("POL-03", "POL-04", "POL-05", "POL-06", "POL-07", "POL-08", "POL-09"),
        ("D3", "HNP"): ("POL-09", "POL-10"),
    }


def test_the_pilot_profile_applies_only_to_human_present() -> None:
    assert rules_apply(D1_PILOT_RULES, "D1", "HP")
    assert not rules_apply(D1_PILOT_RULES, "D1", "HNP")
    assert rules_apply(APPLICABLE_RULES[("D1", "HNP")], "D1", "HNP")


@pytest.mark.parametrize(
    ("policy_set_id", "purchase_mode", "matches"),
    [
        (HNP_POLICY_SET_ID, "HNP", True),
        (HNP_POLICY_SET_ID, "HP", False),
        ("b2-hp-v2", "HNP", False),
        ("b2-pilot-v1", "HNP", False),
        ("b2-hp-v2", "HP", True),
        ("b2-pilot-v1", "HP", True),
    ],
)
def test_b2_hnp_v1_governs_exactly_the_hnp_purchases(
    policy_set_id: str, purchase_mode: Any, matches: bool
) -> None:
    assert policy_set_matches_mode(policy_set_id, purchase_mode) is matches


def test_b2_hnp_v1_is_built_in_code_with_its_d1_d2_and_d3_rows() -> None:
    """Revision 2: the D1 row of revision 1 plus `authorization` (D2-HNP) and `dispatch` (D3)."""
    payload, binding = build_hnp_policy(merchant_ref=MERCHANT_REF)

    assert payload == (
        b'{"allowed_actions":["prepare_checkout"],"allowed_merchant_refs":["merchant-1"],'
        b'"authorization":{"allowed_actions":["complete_purchase"],'
        b'"authentication_bases":["service_and_grant"],'
        b'"policy_ids":["POL-03","POL-04","POL-05","POL-06","POL-07","POL-08","POL-09"],'
        b'"profiles":["profile:ap2-hnp-v1"],"schema_version":"commerce-policy/1.0"},'
        b'"currency":"COP","dispatch":{"allowed_actions":["commit_dispatch"],'
        b'"policy_ids":["POL-09","POL-10"],"schema_version":"commerce-policy/1.0"},'
        b'"policy_ids":["POL-01","POL-02","POL-11"],'
        b'"policy_set_id":"b2-hnp-v1","quantity":1,"schema_version":"commerce-policy/1.0",'
        b'"version":2}'
    )
    artifact = verify_artifact(payload, binding)
    assert (binding.policy_set_id, binding.version) == (HNP_POLICY_SET_ID, HNP_POLICY_REVISION)
    assert artifact.policy_ids == APPLICABLE_RULES[("D1", "HNP")]
    assert isinstance(artifact.authorization, HnpAuthorizationRules)
    assert artifact.authorization.policy_ids == APPLICABLE_RULES[("D2", "HNP")]
    assert artifact.dispatch is not None
    assert artifact.dispatch.policy_ids == APPLICABLE_RULES[("D3", "HNP")]


def test_the_hp_builder_never_builds_b2_hnp_v1() -> None:
    """AGENT_D1_POLICY_SET_ID names the HP set; b2-hnp-v1 has one builder."""
    with pytest.raises(GovernanceError) as refused:
        build_d1_policy(policy_set_id=HNP_POLICY_SET_ID, version=1, merchant_ref=MERCHANT_REF)

    assert refused.value.reason == "GOV_ARTIFACT_MISMATCH"


def test_d1_hnp_ignores_the_hp_policy_set_of_the_configuration() -> None:
    hp = D1EnforceConfiguration(
        governance_mode="enforce",
        merchant_base_url="http://127.0.0.1:4120",
        policy_set_id="b2-hp-v2",
        policy_revision=7,
        currency="COP",
        quantity=1,
        clock=ClockProfile(
            authority_source="system_utc",
            commercial_source="system_utc",
            authority_provider=None,
            commercial_provider=None,
            fixed_commercial_at=None,
        ),
    )

    hnp = hnp_d1_configuration(hp)

    assert (hnp.policy_set_id, hnp.policy_revision) == (HNP_POLICY_SET_ID, HNP_POLICY_REVISION)
    assert hnp.model_dump(exclude={"policy_set_id", "policy_revision"}) == hp.model_dump(
        exclude={"policy_set_id", "policy_revision"}
    )


@pytest.mark.parametrize(
    ("eligible", "unit", "rules", "reasons"),
    [
        pytest.param(
            ("variant-1",),
            90_000_000,
            [("POL-01", "pass"), ("POL-02", "pass"), ("POL-11", "pass")],
            ("ALL_APPLICABLE_POLICIES_SATISFIED",),
            id="allow",
        ),
        pytest.param(
            ("variant-2",),
            90_000_000,
            [("POL-01", "fail"), ("POL-02", "pass"), ("POL-11", "pass")],
            ("SELECTION_NOT_ELIGIBLE",),
            id="selection-outside-the-grant",
        ),
        pytest.param(
            ("variant-1",),
            154_000_000,
            [("POL-01", "pass"), ("POL-02", "pass"), ("POL-11", "fail")],
            ("BUDGET_NOT_SATISFIABLE",),
            id="price-over-the-authorized-total",
        ),
    ],
)
def test_d1_hnp_evaluates_pol01_pol02_and_pol11(
    eligible: tuple[str, ...], unit: int, rules: list[tuple[str, str]], reasons: tuple[str, ...]
) -> None:
    data, artifact = hnp_input(eligible=eligible, selected_unit_minor=unit)

    decision = evaluate(data, artifact)

    assert (decision.origin, decision.phase, decision.purchase_mode) == ("policy", "D1", "HNP")
    assert decision.policy_binding.policy_set_id == HNP_POLICY_SET_ID
    assert [(rule.policy_id, rule.result) for rule in decision.rule_results] == rules
    assert decision.reason_codes == reasons


@pytest.mark.parametrize(
    "direction",
    ["hp-episode-with-the-hnp-policy", "hnp-episode-with-b2-hp-v2", "hnp-episode-with-pilot"],
)
def test_c02_denies_a_mode_against_the_policy_set_without_calling_the_engine(
    direction: str,
) -> None:
    if direction == "hp-episode-with-the-hnp-policy":
        hnp, artifact = hnp_input()
        data = parse_input(
            canonical_bytes(json.loads(canonical_bytes(hnp)) | {"purchase_mode": "HP"})
        )
    else:
        set_id = "b2-hp-v2" if direction == "hnp-episode-with-b2-hp-v2" else "b2-pilot-v1"
        hp, artifact = policy_input(set_id)
        data = parse_input(
            canonical_bytes(json.loads(canonical_bytes(hp)) | {"purchase_mode": "HNP"})
        )
    factory = Untouchable()

    decision = boundary(data, artifact, factory)

    assert factory.calls == 0
    assert (decision.outcome, decision.origin) == ("deny", "runtime_guard")
    assert decision.reason_codes == ("GOV_ARTIFACT_MISMATCH",)
    assert decision.engine.attempted is False
    assert decision.purchase_mode == data.purchase_mode


def test_c02_also_holds_in_the_reference_engine() -> None:
    hnp, artifact = hnp_input()
    hp = parse_input(canonical_bytes(json.loads(canonical_bytes(hnp)) | {"purchase_mode": "HP"}))
    engine = ReferencePolicyEvaluator(
        artifact_bytes=artifact,
        binding=hp.expected_policy,
        metadata=METADATA,
        identity=measure_reference_engine(),
    )

    with pytest.raises(GovernanceError) as refused:
        engine.evaluate(hp)

    assert refused.value.reason == "GOV_ARTIFACT_MISMATCH"


def test_a_b2_hnp_v1_artifact_with_the_pilot_rules_never_evaluates() -> None:
    """C02 holds (b2-hnp-v1 with HNP), but `rules_apply` refuses the pilot profile in HNP."""
    payload, _ = build_hnp_policy(merchant_ref=MERCHANT_REF)
    pilot = canonical_bytes(
        D1PolicyArtifact.model_validate_json(payload).model_copy(
            update={"policy_ids": D1_PILOT_RULES}
        )
    )
    data, artifact = hnp_input(artifact=pilot)
    factory = Untouchable()

    decision = boundary(data, artifact, factory)

    assert factory.calls == 0
    assert decision.reason_codes == ("GOV_ARTIFACT_MISMATCH",)


def test_an_hnp_decision_with_the_pilot_rules_is_not_a_valid_decision() -> None:
    data, artifact = hnp_input(selected_unit_minor=90_000_000)
    allowed = evaluate(data, artifact)
    assert allowed.outcome == "allow"
    payload = json.loads(canonical_bytes(allowed))
    # Consistent pilot results (both pass, so the reasons and the outcome still hold).
    payload["rule_results"] = payload["rule_results"][:2]

    with pytest.raises(ValueError):
        PolicyDecision.model_validate_json(canonical_bytes(payload))


def test_the_grant_scope_eligibility_names_its_profile_and_the_grant() -> None:
    measured = canonical_bytes({"role": "eligibility", "sources": {"selection.py": "code"}})

    source = grant_scope_eligibility(measured, GRANT)

    body = json.loads(source)
    assert (
        body["eligibility_profile"] == GRANT_SCOPE_ELIGIBILITY == "rank_candidates+grant_scope/v1"
    )
    assert (body["grant_id"], body["variant_ids"]) == (GRANT.grant_id, ["variant-1", "variant-2"])
    assert body["sources"] == {"selection.py": "code"}
    assert registered_grant(source) == GRANT
    with pytest.raises(GovernanceError):
        registered_grant(measured)


# Values computed on org/main (fa2b4b3) before HNP-05, with the engine identity normalized: the
# engine digest changes with every change to models.py or evaluator.py, the bytes around it do not.
HP_ARTIFACT_DIGESTS = {
    "b2-hp-v2": "sha256:e78d7e004760af6d2ad54a6c6ee2855550e1647567cd6b3c72a4ecd16178cd63",
    "b2-pilot-v1": "sha256:d83269d7f9d4f1e18de16af348f0fac2392bea24cffb7b91d5a6374859fce03f",
}
HP_EVIDENCE_DIGESTS = {
    "b2-hp-v2": "sha256:0e5bdebc57846e64411b9c890f5bf5bb762adfdc68c109c136e6225bad805755",
    "b2-pilot-v1": "sha256:f08aad3aa173401a9d71fb9ce7ee1bd6a6d9184db4e400d699ef88e3e6585506",
}
NORMALIZED_ENGINE = EngineIdentity(
    schema_version=SCHEMA,
    kind="python",
    version="reference-d1-d2:2",
    artifact_digest="sha256:" + "0" * 64,
    attempted=True,
)


def hp_record(policy_set_id: str) -> bytes:
    """A D1 HP evidence record of the S02 fixture, through the same boundary as the runtime."""
    data, artifact = policy_input(policy_set_id)
    decision = evaluate(data, artifact).model_copy(update={"engine": NORMALIZED_ENGINE})
    record = DecisionRecord(
        schema_version=SCHEMA,
        evidence_id="evidence-1",
        sequence=1,
        recorded_at=decision.decided_at,
        record_kind="decision_record",
        canonicalization_profile="commerce-canonical-json/1",
        canonical_input_bytes=canonical_bytes(data).decode(),
        decision=decision,
        policy_artifact_ref=ObjectRef(
            schema_version=SCHEMA,
            id="policy-1",
            version=1,
            digest=data.expected_policy.artifact_digest,
        ),
        engine_artifact_ref=ObjectRef(
            schema_version=SCHEMA,
            id="engine-1",
            version=1,
            digest=NORMALIZED_ENGINE.artifact_digest or "",
        ),
        source_refs=(
            data.intent.intent_ref,
            data.feed_snapshot_ref,
            data.eligibility_algorithm_ref,
            data.clock_profile_ref,
        ),
        application=EvidenceApplication(
            schema_version=SCHEMA,
            disposition="blocked",
            transition_ref=None,
            expected_state_version=1,
            resulting_state_version=None,
        ),
        replayability="full",
    )
    return canonical_bytes(DecisionRecord.model_validate(record))


@pytest.mark.parametrize("policy_set_id", ["b2-hp-v2", "b2-pilot-v1"])
def test_hp_policies_and_hp_evidence_keep_their_bytes(policy_set_id: str) -> None:
    payload, binding = build_d1_policy(
        policy_set_id=policy_set_id, version=1, merchant_ref=MERCHANT_REF
    )

    assert binding.artifact_digest == sha256_digest(payload) == HP_ARTIFACT_DIGESTS[policy_set_id]
    assert sha256_digest(hp_record(policy_set_id)) == HP_EVIDENCE_DIGESTS[policy_set_id]
