"""Strict, immutable wire models for the first (D1 only) policy increment."""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

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
]
POLICY_REASONS = (
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
)
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


class InputHeader(ContractModel):
    decision_id: Id
    run_id: Id
    trace_id: Id
    owner_ref: Id
    purchase_mode: PurchaseMode
    governance_mode: GovernanceMode
    phase: Literal["D1"]
    action: Literal["prepare_checkout"]
    context_version: Version
    expected_local_state_version: Version
    expected_policy: PolicyBinding
    authority_now: UtcTime
    commercial_now: UtcTime
    clock_profile_ref: ObjectRef
    experiment_run_id: Id | None
    conversation_id: Id | None


class IntentFacts(ContractModel):
    intent_ref: ObjectRef
    currency: Literal["COP"]
    max_unit_minor: Minor | None
    max_total_minor: Minor | None
    quantity: One
    hard_constraint_refs: Ids
    normalization_version: Id


class SelectionPolicyInput(InputHeader):
    intent: IntentFacts
    feed_snapshot_ref: ObjectRef
    eligibility_algorithm_ref: ObjectRef
    eligible_variant_ids: Ids
    selected_variant_id: Id
    merchant_ref: Id

    @field_validator("eligible_variant_ids")
    @classmethod
    def ordered_eligible_set(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(value))


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
    phase: Literal["D1"]
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
        if tuple(rule.policy_id for rule in self.rule_results) != ("POL-01", "POL-02"):
            raise ValueError("D1 requires exactly POL-01, POL-02 in order")
        failed = tuple(
            POLICY_REASONS[index]
            for index, rule in enumerate(self.rule_results)
            if rule.result == "fail"
        )
        expected = failed or ("ALL_APPLICABLE_POLICIES_SATISFIED",)
        if self.reason_codes != expected or self.outcome != ("deny" if failed else "allow"):
            raise ValueError("Inconsistent rule results, reasons or outcome")
        return self


class D1PolicyArtifact(ContractModel):
    """Offline reference profile, supplied through trusted configuration, not input."""

    policy_set_id: Id
    version: Version
    policy_ids: tuple[Literal["POL-01"], Literal["POL-02"]]
    allowed_actions: Actions
    allowed_merchant_refs: Ids
    currency: Literal["COP"]
    quantity: One
