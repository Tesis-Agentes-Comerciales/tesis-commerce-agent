"""Strict, immutable wire models of the policy contract (policy-v1): D1, D2 and D3."""

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

SCHEMA = "commerce-policy/1.0"
Id = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Minor = Annotated[int, Field(ge=0)]
Version = Annotated[int, Field(ge=1)]
One = Annotated[int, Field(ge=1, le=1)]
Action = Literal["prepare_checkout", "complete_purchase", "commit_dispatch"]
PurchaseMode = Literal["HP", "HNP"]
GovernanceMode = Literal["off", "shadow", "enforce"]
PolicyId = Literal[
    "POL-01",
    "POL-02",
    "POL-03",
    "POL-04",
    "POL-05",
    "POL-06",
    "POL-07",
    "POL-08",
    "POL-09",
    "POL-10",
    "POL-11",
]
POLICY_REASONS: dict[PolicyId, str] = {
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
Phase = Literal["D1", "D2", "D3"]
Rules = tuple[PolicyId, ...]
# The rules each (phase, purchase mode) evaluates, in order, from b2-hp-v2 on. A new phase or
# purchase mode adds its own row and leaves the others alone: a row is part of the bytes of the
# policy artifacts built from it (governance/provenance.py).
APPLICABLE_RULES: Mapping[tuple[Phase, PurchaseMode], Rules] = MappingProxyType(
    {
        ("D1", "HP"): ("POL-01", "POL-02", "POL-11"),
        ("D2", "HP"): ("POL-03", "POL-04", "POL-05", "POL-06"),
        ("D1", "HNP"): ("POL-01", "POL-02", "POL-11"),
        ("D2", "HNP"): ("POL-03", "POL-04", "POL-05", "POL-06", "POL-07", "POL-08", "POL-09"),
        ("D3", "HNP"): ("POL-09", "POL-10"),
    }
)
# b2-pilot-v1 predates the table: its published artifact evaluates POL-01 and POL-02 in D1.
D1_PILOT_RULES: Rules = ("POL-01", "POL-02")
D1_PROFILES: tuple[Rules, ...] = (
    D1_PILOT_RULES,
    *(rules for (phase, _), rules in APPLICABLE_RULES.items() if phase == "D1"),
)
# The policy set of every HNP decision, built in code (governance/hnp_policy.py).
HNP_POLICY_SET_ID = "b2-hnp-v1"


def rules_apply(rules: Rules, phase: Phase, purchase_mode: PurchaseMode) -> bool:
    """Whether a policy may evaluate these rules for this phase and purchase mode.

    The pilot's profile is Human Present only: an HNP purchase never runs without POL-11.
    """
    pilot = (phase, purchase_mode) == ("D1", "HP") and rules == D1_PILOT_RULES
    return pilot or rules == APPLICABLE_RULES.get((phase, purchase_mode))


def policy_set_matches_mode(policy_set_id: str, purchase_mode: PurchaseMode) -> bool:
    """C02: b2-hnp-v1 governs exactly the HNP purchases. `(D1, HP)` and `(D1, HNP)` list the
    same rules, so `rules_apply` alone cannot tell an HP policy from the HNP one."""
    return (policy_set_id == HNP_POLICY_SET_ID) == (purchase_mode == "HNP")


GuardReason = Literal[
    "GOV_INPUT_INVALID",
    "GOV_FACTS_UNAVAILABLE",
    "GOV_ENGINE_UNAVAILABLE",
    "GOV_OUTPUT_INVALID",
    "GOV_ARTIFACT_MISMATCH",
    "GOV_EVIDENCE_UNAVAILABLE",
    "GOV_STATE_CONFLICT",
    "GOV_FRESHNESS_UNRESOLVED",
    "GOV_AUTHORIZATION_PROFILE_UNRESOLVED",
]
ReasonCode = (
    Literal[
        "ALL_APPLICABLE_POLICIES_SATISFIED",
        "SELECTION_NOT_ELIGIBLE",
        "ACTION_OR_MERCHANT_NOT_ALLOWED",
        "PRINCIPAL_ACTION_NOT_ALLOWED",
        "EPISODE_BINDING_MISMATCH",
        "CHECKOUT_CONSTRAINT_MISMATCH",
        "POLICY_CONTEXT_MISMATCH",
        "DELEGATION_NOT_ACTIVE",
        "DELEGATION_SCOPE_EXCEEDED",
        "DELEGATION_USE_UNAVAILABLE",
        "DISPATCH_AUTHORITY_CHANGED",
        "BUDGET_NOT_SATISFIABLE",
    ]
    | GuardReason
)


def _utc(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    return value


UtcTime = Annotated[
    str,
    Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"),
    AfterValidator(_utc),
]


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ValueError("Duplicate collection member")
    return values


Ids = Annotated[tuple[Id, ...], AfterValidator(_unique)]
Actions = Annotated[tuple[Action, ...], AfterValidator(_unique)]


class ContractModel(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, revalidate_instances="always"
    )
    schema_version: Literal["commerce-policy/1.0"]


class ObjectRef(ContractModel):
    id: Id
    version: Version
    digest: Digest


class PolicyBinding(ContractModel):
    policy_set_id: Id
    version: Version
    artifact_digest: Digest


class PolicyHeader(ContractModel):
    """Common header of every policy input; each phase fixes `phase`, `action` and its modes."""

    decision_id: Id
    run_id: Id
    trace_id: Id
    owner_ref: Id
    governance_mode: GovernanceMode
    context_version: Version
    expected_local_state_version: Version
    expected_policy: PolicyBinding
    authority_now: UtcTime
    commercial_now: UtcTime
    clock_profile_ref: ObjectRef
    experiment_run_id: Id | None
    conversation_id: Id | None


class InputHeader(PolicyHeader):
    purchase_mode: PurchaseMode
    phase: Literal["D1"]
    action: Literal["prepare_checkout"]


class IntentFacts(ContractModel):
    intent_ref: ObjectRef
    currency: Literal["COP"]
    max_unit_minor: Minor | None
    max_total_minor: Minor | None
    quantity: One
    hard_constraint_refs: Ids
    normalization_version: Id


class PrincipalFacts(ContractModel):
    principal_ref: Id
    authentication_basis: Literal["user_jwt", "service_and_grant"]
    authentication_verified: bool
    owns_run: bool
    permitted_actions: Actions


class SelectionPolicyInput(InputHeader):
    intent: IntentFacts
    feed_snapshot_ref: ObjectRef
    eligibility_algorithm_ref: ObjectRef
    eligible_variant_ids: Ids
    selected_variant_id: Id
    merchant_ref: Id
    # POL-11: catalog unit price of the selected variant in the registered feed snapshot, in COP
    # minor units. Absent when not observed in COP, and in inputs recorded before POL-11.
    selected_unit_minor: Minor | None = None
    # D1-HNP only: the executor acting under the grant, for attribution (hnp-governed.md §7, §9.1).
    # No D1 rule reads it. Absent in HP and in D1-HNP inputs recorded before it.
    principal: PrincipalFacts | None = None

    @field_validator("eligible_variant_ids")
    @classmethod
    def ordered_eligible_set(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(value))

    @model_validator(mode="after")
    def principal_only_in_hnp(self) -> Self:
        if self.principal is not None and self.purchase_mode != "HNP":
            raise ValueError("Only a D1-HNP input names its principal")
        return self

    @model_serializer(mode="wrap")
    def canonical_without_absent_facts(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        # Evidence stored before POL-11 has no price, and HP has no principal: omitting an absent
        # one keeps their digests.
        data = handler(self)
        for name in ("selected_unit_minor", "principal"):
            if data.get(name) is None:
                data.pop(name, None)
        return data


class PrivateBinding(ContractModel):
    """Keyed HMAC of a private value (a destination), never the value itself."""

    binding_id: Id
    scheme: Literal["hmac-sha256/v1"]
    key_id: Id
    value: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class MaterialTerms(ContractModel):
    """Typed projection of the authoritative checkout, without buyer data."""

    snapshot_ref: ObjectRef
    merchant_ref: Id
    checkout_id: Id
    variant_id: Id
    checkout_status: Literal["ready_for_payment"]
    quantity: One
    unit_minor: Minor
    shipping_minor: Minor
    other_minor: Minor
    total_minor: Minor
    currency: Literal["COP"]
    fulfillment_service_ref: Id
    destination_binding: PrivateBinding
    commercial_expires_at: UtcTime
    material_digest: Digest
    material_contract_ref: ObjectRef

    @model_validator(mode="after")
    def explained_total(self) -> Self:
        if self.total_minor != self.unit_minor * self.quantity + self.shipping_minor + (
            self.other_minor
        ):
            raise ValueError("The total must be explained by its breakdown")
        return self


class FreshnessFacts(ContractModel):
    status: Literal["current", "stale", "unresolved"]
    observed_at: UtcTime
    valid_until: UtcTime
    contract_ref: ObjectRef
    source_offer_ref: ObjectRef


class ConstraintMatch(ContractModel):
    constraint_ref: Id
    kind: Literal["category", "condition", "option", "term"]
    matches: bool
    source_snapshot_ref: ObjectRef


class ResolvedD1(ContractModel):
    """The committed D1 decision of the run, resolved from the ledger, never from an ID alone."""

    decision_id: Id
    evidence_id: Id
    evidence_digest: Digest
    outcome: Literal["allow", "deny"]
    run_id: Id
    owner_ref: Id
    purchase_mode: PurchaseMode
    governance_mode: GovernanceMode
    intent_ref: ObjectRef
    feed_snapshot_ref: ObjectRef
    selected_variant_id: Id
    merchant_ref: Id
    policy_binding: PolicyBinding
    context_version: Version
    input_digest: Digest


class AuthorizationProfile(ContractModel):
    status: Literal["validated", "unresolved"]
    profile_ref: ObjectRef | None

    @model_validator(mode="after")
    def referenced(self) -> Self:
        if self.status == "validated" and self.profile_ref is None:
            raise ValueError("A validated profile needs its reference")
        return self


class HpAuthorizationFacts(ContractModel):
    """Presentation and click resolved by the server; the UI only echoes a fingerprint."""

    approval_attempt_id: Id
    attempt_version: Version
    attempt_owner_ref: Id
    presented_snapshot_ref: ObjectRef
    presented_at: UtcTime
    attempt_valid_until: UtcTime
    click_received: bool
    clicked_at: UtcTime
    material_match: bool
    consent_binding_ref: ObjectRef


def constraint_set(values: tuple["ConstraintMatch", ...]) -> tuple["ConstraintMatch", ...]:
    """`constraint_matches` of D2 (both branches): one match per constraint, sorted by ref."""
    refs = tuple(match.constraint_ref for match in values)
    if len(set(refs)) != len(refs):
        raise ValueError("Duplicate constraint match")
    return tuple(sorted(values, key=lambda match: match.constraint_ref))


class HpAuthorizationPolicyInput(PolicyHeader):
    """HP branch of the D2 input, selected by `purchase_mode`."""

    purchase_mode: Literal["HP"]
    phase: Literal["D2"]
    action: Literal["complete_purchase"]
    principal: PrincipalFacts
    intent: IntentFacts
    selected_variant_id: Id
    terms: MaterialTerms
    freshness: FreshnessFacts
    constraint_matches: Annotated[tuple[ConstraintMatch, ...], AfterValidator(constraint_set)]
    d1: ResolvedD1
    authorization_id: Id
    operation_id: Id
    authorization_profile: AuthorizationProfile
    hp: HpAuthorizationFacts


# The HP branch under the name the D2-HP gate and its tests use. The D2 input is the union
# discriminated by `purchase_mode`, `hnp_models.D2PolicyInput`: its HNP branch lives outside this
# module, so HP evidence keeps its bytes.
AuthorizationPolicyInput = HpAuthorizationPolicyInput


class RuleResult(ContractModel):
    policy_id: PolicyId
    result: Literal["pass", "fail"]


class EngineIdentity(ContractModel):
    kind: Literal["python", "opa", "test"]
    version: Id | None
    artifact_digest: Digest | None
    attempted: bool


class EvaluationMetadata(ContractModel):
    """Backend-provided observation metadata; never read a clock inside evaluate."""

    decided_at: UtcTime
    latency_ms: Minor


class PolicyDecision(ContractModel):
    decision_id: Id
    phase: Phase
    purchase_mode: PurchaseMode
    governance_mode: GovernanceMode
    outcome: Literal["allow", "deny"]
    origin: Literal["policy", "runtime_guard"]
    reason_codes: tuple[ReasonCode, ...]
    rule_results: tuple[RuleResult, ...]
    input_digest: Digest
    policy_binding: PolicyBinding
    engine: EngineIdentity
    engine_decision_id: Id | None
    decided_at: UtcTime
    latency_ms: Minor

    @model_validator(mode="after")
    def consistent(self) -> Self:
        _unique(self.reason_codes)
        if self.governance_mode == "off":
            raise ValueError("off has no evaluated decision")
        if self.origin == "runtime_guard":
            if (
                self.outcome != "deny"
                or self.rule_results
                or not self.reason_codes
                or any(not code.startswith("GOV_") for code in self.reason_codes)
            ):
                raise ValueError("Invalid runtime guard result")
            return self
        if (
            not self.engine.attempted
            or self.engine.version is None
            or self.engine.artifact_digest is None
        ):
            raise ValueError("Missing measured engine identity")
        ids: Rules = tuple(rule.policy_id for rule in self.rule_results)
        if not rules_apply(ids, self.phase, self.purchase_mode):
            raise ValueError("Rules other than the ones this phase and purchase mode apply")
        failed = tuple(
            POLICY_REASONS[rule.policy_id] for rule in self.rule_results if rule.result == "fail"
        )
        expected = failed or ("ALL_APPLICABLE_POLICIES_SATISFIED",)
        if self.reason_codes != expected or self.outcome != ("deny" if failed else "allow"):
            raise ValueError("Inconsistent rule results, reasons or outcome")
        return self


class AuthorizationRules(ContractModel):
    """D2-HP section of a policy artifact: what POL-03..06 admit when approving."""

    policy_ids: Rules
    allowed_actions: tuple[Literal["complete_purchase"]]
    authentication_bases: tuple[Literal["user_jwt"]]

    @field_validator("policy_ids")
    @classmethod
    def d2_hp_row(cls, value: Rules) -> Rules:
        if value != APPLICABLE_RULES[("D2", "HP")]:
            raise ValueError("The D2-HP section lists the D2/HP row of the table")
        return value


class HnpAuthorizationRules(ContractModel):
    """D2-HNP section of `b2-hnp-v1`: what POL-03..09 admit for an autonomous purchase.

    `profiles` lists the authorization profiles whose proofs D2 and D3 accept, by the `id` of
    their `ObjectRef` (`profile:ap2-hnp-v1`).
    """

    policy_ids: Rules
    allowed_actions: tuple[Literal["complete_purchase"]]
    authentication_bases: tuple[Literal["service_and_grant"]]
    profiles: Annotated[Ids, Field(min_length=1)]

    @field_validator("policy_ids")
    @classmethod
    def d2_hnp_row(cls, value: Rules) -> Rules:
        if value != APPLICABLE_RULES[("D2", "HNP")]:
            raise ValueError("The D2-HNP section lists the D2/HNP row of the table")
        return value


class DispatchRules(ContractModel):
    """D3-HNP section of `b2-hnp-v1`: POL-09 and POL-10 before the dispatch is committed."""

    policy_ids: Rules
    allowed_actions: tuple[Literal["commit_dispatch"]]

    @field_validator("policy_ids")
    @classmethod
    def d3_hnp_row(cls, value: Rules) -> Rules:
        if value != APPLICABLE_RULES[("D3", "HNP")]:
            raise ValueError("The D3-HNP section lists the D3/HNP row of the table")
        return value


class D1PolicyArtifact(ContractModel):
    """Offline reference profile, supplied through trusted configuration, not input.

    `authorization` holds the D2 rules of its purchase mode (`AuthorizationRules` for HP,
    `HnpAuthorizationRules` only in `b2-hnp-v1`) and `dispatch` the D3 rules, only in
    `b2-hnp-v1`. An artifact without them is serialized exactly as before (b2-pilot-v1 keeps
    its digest), and its purchases never run D2 or D3.
    """

    policy_set_id: Id
    version: Version
    # Pilot bytes stay ["POL-01","POL-02"]; b2-hp-v2 and b2-hnp-v1 list their D1 row.
    policy_ids: Rules
    allowed_actions: Actions
    allowed_merchant_refs: Ids
    currency: Literal["COP"]
    quantity: One
    authorization: AuthorizationRules | HnpAuthorizationRules | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    dispatch: DispatchRules | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("policy_ids")
    @classmethod
    def known_d1_profile(cls, value: Rules) -> Rules:
        if value not in D1_PROFILES:
            raise ValueError("A D1 artifact lists the pilot's rules or a D1 row of the table")
        return value

    @model_validator(mode="after")
    def sections_of_its_mode(self) -> Self:
        """C02 in the artifact: HNP sections only in b2-hnp-v1, HP ones never in it."""
        hnp = self.policy_set_id == HNP_POLICY_SET_ID
        if self.authorization is not None and hnp != isinstance(
            self.authorization, HnpAuthorizationRules
        ):
            raise ValueError("The D2 section belongs to the other purchase mode")
        if self.dispatch is not None and not isinstance(self.authorization, HnpAuthorizationRules):
            raise ValueError("D3 only follows a D2-HNP section")
        return self
