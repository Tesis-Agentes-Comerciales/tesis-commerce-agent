"""D1 and D2 evidence wire contract. Backend scope is separate from client input."""

import json
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .canonical import canonical_bytes, input_digest, parse_model
from .evaluator import parse_authorization_input, parse_input
from .models import (
    AuthorizationPolicyInput,
    AuthorizationProfile,
    ContractModel,
    FreshnessFacts,
    GuardReason,
    HpAuthorizationFacts,
    Id,
    Ids,
    IntentFacts,
    MaterialTerms,
    ObjectRef,
    PolicyBinding,
    PolicyDecision,
    PrincipalFacts,
    PurchaseMode,
    ResolvedD1,
    SelectionPolicyInput,
    UtcTime,
    Version,
)

PolicyInput = SelectionPolicyInput | AuthorizationPolicyInput


def required_refs(data: PolicyInput) -> tuple[ObjectRef, ...]:
    """Sources an evidence record must name so its input can be resolved again."""
    if isinstance(data, SelectionPolicyInput):
        return (
            data.intent.intent_ref,
            data.feed_snapshot_ref,
            data.eligibility_algorithm_ref,
            data.clock_profile_ref,
        )
    return (
        data.intent.intent_ref,
        data.d1.feed_snapshot_ref,
        data.clock_profile_ref,
        data.terms.snapshot_ref,
        data.freshness.source_offer_ref,
        data.hp.presented_snapshot_ref,
    )


class EvidenceError(ValueError):
    """Safe code only; never expose SQLite errors or rejected payload values."""

    def __init__(self, code: GuardReason):
        self.code: GuardReason = code
        super().__init__(code)


class EvidenceApplication(ContractModel):
    disposition: Literal["observed_shadow", "blocked", "transition_committed"]
    transition_ref: Id | None
    expected_state_version: Version
    resulting_state_version: Version | None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.disposition == "transition_committed":
            if (
                self.transition_ref is None
                or self.resulting_state_version is None
                or self.resulting_state_version <= self.expected_state_version
            ):
                raise ValueError("Transition requires a local CAS successor and reference")
        elif self.transition_ref is not None or self.resulting_state_version is not None:
            raise ValueError("No transition may be claimed")
        return self


class EvidenceHeader(ContractModel):
    evidence_id: Id
    sequence: Version
    recorded_at: UtcTime


class DecisionRecord(EvidenceHeader):
    record_kind: Literal["decision_record"]
    canonicalization_profile: Literal["commerce-canonical-json/1"]
    canonical_input_bytes: str
    decision: PolicyDecision
    policy_artifact_ref: ObjectRef | None
    engine_artifact_ref: ObjectRef | None
    source_refs: tuple[ObjectRef, ...]
    application: EvidenceApplication
    replayability: Literal["full", "policy_not_evaluated"]

    def policy_input(self) -> SelectionPolicyInput:
        """The D1 input; a D2 record has no selection input (GOV_INPUT_INVALID)."""
        return parse_input(self.canonical_input_bytes.encode("utf-8"))

    def authorization_input(self) -> AuthorizationPolicyInput:
        return parse_authorization_input(self.canonical_input_bytes.encode("utf-8"))

    def any_input(self) -> PolicyInput:
        return self.authorization_input() if self.decision.phase == "D2" else self.policy_input()

    @model_validator(mode="after")
    def consistent(self) -> Self:
        data = self.any_input()
        if canonical_bytes(data) != self.canonical_input_bytes.encode("utf-8"):
            raise ValueError("Input is not canonical")
        result = self.decision
        if (
            result.input_digest != input_digest(data)
            or result.decision_id != data.decision_id
            or result.phase != data.phase
            or result.purchase_mode != data.purchase_mode
            or result.governance_mode != data.governance_mode
            or result.policy_binding != data.expected_policy
        ):
            raise ValueError("Decision is unrelated to input")
        app = self.application
        if app.expected_state_version != data.expected_local_state_version:
            raise ValueError("State version mismatch")
        if app.disposition == "observed_shadow" and data.governance_mode != "shadow":
            raise ValueError("Only shadow can claim observation")
        if app.disposition == "transition_committed" and (
            data.governance_mode != "enforce"
            or result.outcome != "allow"
            or result.origin != "policy"
        ):
            raise ValueError("Invalid transition claim")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("Duplicate source reference")
        if any(ref not in self.source_refs for ref in required_refs(data)):
            raise ValueError("Missing input source reference")
        if result.origin == "policy":
            if (
                self.replayability != "full"
                or self.policy_artifact_ref is None
                or self.engine_artifact_ref is None
            ):
                raise ValueError("Evaluated policy requires both recoverable artifacts")
        elif self.replayability != "policy_not_evaluated":
            raise ValueError("Operational denial is not a policy replay")
        if (
            self.policy_artifact_ref is not None
            and self.policy_artifact_ref.digest != result.policy_binding.artifact_digest
        ):
            raise ValueError("Policy artifact digest mismatch")
        if (
            self.engine_artifact_ref is not None
            and self.engine_artifact_ref.digest != result.engine.artifact_digest
        ):
            raise ValueError("Engine artifact digest mismatch")
        return self


class InputRejected(EvidenceHeader):
    record_kind: Literal["input_rejected"]
    run_id: Id
    trace_id: Id
    decision_id: Id
    reason_code: Literal["GOV_INPUT_INVALID", "GOV_FACTS_UNAVAILABLE"]
    safe_field_paths: Ids
    replayability: Literal["not_evaluable"]

    @model_validator(mode="after")
    def safe_paths(self) -> Self:
        # Closed model-derived path catalog: never persist attacker-chosen key names.
        if any(path not in SAFE_INPUT_PATHS for path in self.safe_field_paths):
            raise ValueError("Unknown diagnostic path")
        return self


class ApplicationFailure(EvidenceHeader):
    record_kind: Literal["application_failure"]
    run_id: Id
    trace_id: Id
    decision_id: Id
    reason_code: Literal["GOV_STATE_CONFLICT", "GOV_EVIDENCE_UNAVAILABLE"]
    expected_state_version: Version
    observed_state_version: Version | None
    replayability: Literal["not_evaluable"]


DecisionEvidence = Annotated[
    DecisionRecord | InputRejected | ApplicationFailure, Field(discriminator="record_kind")
]


def validate_evidence(value: DecisionEvidence) -> DecisionEvidence:
    try:
        # Validate BEFORE serialization: Pydantic's int serializer can turn a
        # constructed bool into 1 and destroy the evidence of an invalid type.
        if isinstance(value, DecisionRecord):
            checked = DecisionRecord.model_validate(value)
        elif isinstance(value, InputRejected):
            checked = InputRejected.model_validate(value)
        elif isinstance(value, ApplicationFailure):
            checked = ApplicationFailure.model_validate(value)
        else:
            raise ValueError("Evidence requires a typed contract")
        return parse_evidence(canonical_bytes(checked))
    except Exception:
        raise EvidenceError("GOV_INPUT_INVALID") from None


class EvidenceScope(ContractModel):
    """Trusted backend registration, never a body/LLM authority assertion.

    References here must have been resolved for this episode by the caller. The
    ledger checks equality and binding; it cannot authenticate opaque references.
    """

    run_id: Id
    trace_id: Id
    owner_ref: Id
    purchase_mode: PurchaseMode
    governance_mode: Literal["shadow", "enforce"]
    phase: Literal["D1", "D2"]
    context_version: Version
    policy_binding: PolicyBinding
    source_refs: tuple[ObjectRef, ...]
    policy_artifact_ref: ObjectRef | None
    engine_artifact_ref: ObjectRef | None

    @model_validator(mode="after")
    def unique_refs(self) -> Self:
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("Duplicate source reference")
        return self


def _paths(model: type[ContractModel], prefix: str = "") -> set[str]:
    return {prefix + name for name in model.model_fields}


SAFE_INPUT_PATHS = frozenset(
    _paths(SelectionPolicyInput)
    | _paths(IntentFacts, "intent.")
    | _paths(ObjectRef, "intent.intent_ref.")
    | _paths(PolicyBinding, "expected_policy.")
    | _paths(ObjectRef, "feed_snapshot_ref.")
    | _paths(ObjectRef, "clock_profile_ref.")
    | _paths(ObjectRef, "eligibility_algorithm_ref.")
    | _paths(AuthorizationPolicyInput)
    | _paths(PrincipalFacts, "principal.")
    | _paths(MaterialTerms, "terms.")
    | _paths(FreshnessFacts, "freshness.")
    | _paths(ResolvedD1, "d1.")
    | _paths(AuthorizationProfile, "authorization_profile.")
    | _paths(HpAuthorizationFacts, "hp.")
)


def scope_input(scope: EvidenceScope, data: PolicyInput) -> None:
    if (
        data.run_id != scope.run_id
        or data.trace_id != scope.trace_id
        or data.owner_ref != scope.owner_ref
        or data.phase != scope.phase
        or data.purchase_mode != scope.purchase_mode
        or data.governance_mode != scope.governance_mode
        or data.context_version != scope.context_version
        or data.expected_policy != scope.policy_binding
    ):
        raise EvidenceError("GOV_INPUT_INVALID")
    if any(ref not in scope.source_refs for ref in required_refs(data)):
        raise EvidenceError("GOV_INPUT_INVALID")


def parse_evidence(payload: bytes) -> DecisionEvidence:
    """Internal import only; clients cannot submit evidence through any endpoint."""
    try:
        # This first read only selects the model. parse_model validates the
        # original bytes including duplicate keys, floats and unknown fields.
        value = json.loads(payload.decode("utf-8"))
        kind = value["record_kind"]
        model = {
            "decision_record": DecisionRecord,
            "input_rejected": InputRejected,
            "application_failure": ApplicationFailure,
        }[kind]
        return parse_model(model, payload)
    except Exception:
        raise EvidenceError("GOV_INPUT_INVALID") from None
