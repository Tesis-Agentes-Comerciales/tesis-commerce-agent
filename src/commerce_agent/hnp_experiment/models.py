"""Strict experimental contracts, deliberately separate from policy decisions."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from commerce_agent.domain import PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.models import Digest, Id, Ids, Minor, PolicyId, UtcTime, Version

SCHEMA = "hnp-review/0.1"
CheckId = Literal[
    "selection",
    "merchant",
    "budget",
    "offer",
    "consent",
    "validity",
    "revocation",
    "usage",
    "constraints",
    "scope",
    "binding",
]
CHECKS: tuple[CheckId, ...] = (
    "selection",
    "merchant",
    "budget",
    "offer",
    "consent",
    "validity",
    "revocation",
    "usage",
    "constraints",
    "scope",
    "binding",
)
RELATED: dict[CheckId, tuple[PolicyId, ...]] = {
    "selection": ("POL-01",),
    "merchant": ("POL-02", "POL-08"),
    "budget": ("POL-05", "POL-08"),
    "offer": ("POL-04", "POL-10"),
    "consent": ("POL-07",),
    "validity": ("POL-07",),
    "revocation": ("POL-07", "POL-10"),
    "usage": ("POL-09",),
    "constraints": ("POL-05", "POL-08"),
    "scope": ("POL-08",),
    "binding": ("POL-04", "POL-06", "POL-10"),
}
Reason = Literal[
    "VAL_SELECTION",
    "VAL_MERCHANT",
    "VAL_BUDGET",
    "VAL_OFFER",
    "VAL_CONSENT",
    "VAL_VALIDITY",
    "VAL_REVOCATION",
    "VAL_USAGE",
    "VAL_CONSTRAINTS",
    "VAL_SCOPE",
    "VAL_BINDING",
]
REASONS: dict[CheckId, Reason] = dict(
    zip(
        CHECKS,
        (
            "VAL_SELECTION",
            "VAL_MERCHANT",
            "VAL_BUDGET",
            "VAL_OFFER",
            "VAL_CONSENT",
            "VAL_VALIDITY",
            "VAL_REVOCATION",
            "VAL_USAGE",
            "VAL_CONSTRAINTS",
            "VAL_SCOPE",
            "VAL_BINDING",
        ),
        strict=True,
    )
)
Condition = Literal["A", "B"]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


def wire_bytes(value: object) -> bytes:
    def project(item):
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        if isinstance(item, dict):
            return {key: project(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [project(child) for child in item]
        return item

    return canonical_bytes(project(value))


def digest(domain: str, value: object) -> str:
    return sha256_digest(domain.encode("ascii") + b"\0" + wire_bytes(value))


class ExperimentalModel(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, revalidate_instances="always"
    )
    schema_version: Literal["hnp-review/0.1"] = SCHEMA


class SourceRef(ExperimentalModel):
    id: Id
    version: Version
    digest: Digest


class Candidate(ExperimentalModel):
    product_id: Id
    variant_id: Id
    conditions: Ids
    observed_unit_minor: Minor
    currency: Currency


class SyntheticTerms(ExperimentalModel):
    product_id: Id
    variant_id: Id
    merchant_ref: Id
    quantity: Annotated[int, Field(ge=1, le=10)]
    currency: Currency
    unit_minor: Minor
    shipping_minor: Minor
    total_minor: Minor
    destination_ref: Id
    available: bool
    offer_ref: SourceRef

    @model_validator(mode="after")
    def sum_matches(self) -> Self:
        if self.unit_minor * self.quantity + self.shipping_minor != self.total_minor:
            raise ValueError("Incoherent synthetic amounts")
        return self


class SyntheticDelegation(ExperimentalModel):
    authority_basis: Literal["synthetic_fixture"] = "synthetic_fixture"
    human_consent: bool
    state: Literal["active", "expired", "revoked", "consumed"]
    valid_from: UtcTime
    expires_at: UtcTime
    merchant_ref: Id
    variant_ids: Ids
    destination_ref: Id
    currency: Currency
    quantity_max: Annotated[int, Field(ge=1, le=1)]
    total_max_minor: Minor
    uses_consumed: Minor
    uses_reserved_other: Minor
    authority_epoch: Version

    @model_validator(mode="after")
    def interval(self) -> Self:
        if self.valid_from >= self.expires_at or not self.variant_ids:
            raise ValueError("Invalid synthetic delegation")
        return self


class TrustedReviewContext(ExperimentalModel):
    episode_ref: SourceRef
    intent_ref: SourceRef
    feed_ref: SourceRef
    delegation_ref: SourceRef
    context_ref: SourceRef
    intent: PurchaseIntent
    required_constraint_ids: Ids
    candidates: tuple[Candidate, ...]
    eligible_variant_ids: Ids
    authorized_merchant_ref: Id
    terms: SyntheticTerms
    delegation: SyntheticDelegation
    authority_now: UtcTime
    protocol_verification: Literal["not_executed"] = "not_executed"

    @model_validator(mode="after")
    def content_bound(self) -> Self:
        if not self.required_constraint_ids:
            raise ValueError("Missing original restrictions")
        if len({c.variant_id for c in self.candidates}) != len(self.candidates):
            raise ValueError("Duplicate candidate")
        expected = digest("hnp-context/v1", self.model_dump(mode="json", exclude={"context_ref"}))
        if self.context_ref.digest != expected:
            raise ValueError("Context digest mismatch")
        return self


class BuyerContext(ExperimentalModel):
    intent: PurchaseIntent
    intent_ref: SourceRef
    candidates: tuple[Candidate, ...]
    terms: SyntheticTerms
    constraint_ids: Ids


class BuyerDraft(ExperimentalModel):
    action: Literal["prepare_purchase"]
    product_id: Id
    variant_id: Id
    merchant_ref: Id
    quantity: Annotated[int, Field(ge=1, le=10)]
    currency: Currency
    claimed_unit_minor: Minor | None
    claimed_total_minor: Minor | None
    offer_ref: SourceRef
    acknowledged_constraints: Ids
    # Non-authoritative prose is not sent to the validator or retained in evidence/digests.
    justification: str = Field(default="", max_length=240, exclude=True, repr=False)


class ProposalEnvelope(ExperimentalModel):
    proposal_id: Id
    revision: Version
    episode_ref: SourceRef
    intent_ref: SourceRef
    buyer_adapter_id: Id
    draft: BuyerDraft
    proposal_digest: Digest

    @model_validator(mode="after")
    def content_bound(self) -> Self:
        expected = digest(
            "hnp-proposal/v1", self.model_dump(mode="json", exclude={"proposal_digest"})
        )
        if self.proposal_digest != expected:
            raise ValueError("Proposal digest mismatch")
        return self


class CheckResult(ExperimentalModel):
    check_id: CheckId
    result: Literal["pass", "fail", "unresolved"]
    related_policy_ids: tuple[PolicyId, ...]
    fact_refs: tuple[SourceRef, ...]

    @model_validator(mode="after")
    def provenance(self) -> Self:
        if self.related_policy_ids != RELATED[self.check_id] or not self.fact_refs:
            raise ValueError("Invalid check references")
        if len(set(self.fact_refs)) != len(self.fact_refs):
            raise ValueError("Duplicate reference")
        return self


class ValidatorAssessment(ExperimentalModel):
    proposal_digest: Digest
    context_ref: SourceRef
    validator_adapter_id: Id
    verdict: Literal["accept", "reject", "abstain"]
    checks: tuple[CheckResult, ...]
    coverage: tuple[CheckId, ...]
    reasons: tuple[Reason, ...]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        ids = tuple(c.check_id for c in self.checks)
        if ids != self.coverage or len(set(ids)) != len(ids):
            raise ValueError("Invalid coverage")
        expected_reasons = tuple(REASONS[c.check_id] for c in self.checks if c.result != "pass")
        if self.reasons != expected_reasons:
            raise ValueError("Invalid reasons")
        expected = (
            "reject"
            if any(c.result == "fail" for c in self.checks)
            else ("abstain" if any(c.result == "unresolved" for c in self.checks) else "accept")
        )
        if self.verdict != expected:
            raise ValueError("Inconsistent verdict")
        return self


class Timing(ExperimentalModel):
    component: Literal["resolver", "buyer", "validator", "backend", "evidence", "sink", "total"]
    elapsed_ns: Minor


class Admission(ExperimentalModel):
    status: Literal["blocked", "simulated_admitted"]
    code: Literal[
        "SIMULATED_ONLY",
        "BUYER_INVALID",
        "BUYER_UNAVAILABLE",
        "CONTEXT_UNAVAILABLE",
        "VALIDATOR_UNAVAILABLE",
        "VALIDATOR_INVALID",
        "VALIDATOR_REJECTED",
        "BACKEND_DENIED",
        "BACKEND_UNAVAILABLE",
        "CONTEXT_CHANGED",
        "EVIDENCE_UNAVAILABLE",
        "SINK_UNAVAILABLE",
        "RECEIPT_UNAVAILABLE",
    ]
    backend_failures: tuple[CheckId, ...]


class ReviewRecord(ExperimentalModel):
    experiment_id: Id
    condition: Condition
    execution_kind: Literal["scripted_offline"] = "scripted_offline"
    original_context: TrustedReviewContext | None
    proposal: ProposalEnvelope | None
    assessment: ValidatorAssessment | None
    # timeout: no answer before the deadline (experimental, fail closed);
    # unavailable: adapter/provider/harness failure (infrastructure, fail closed).
    assessment_status: Literal["not_called", "admitted", "invalid", "timeout", "unavailable"]
    configuration_ref: SourceRef | None
    admission: Admission
    configuration_digest: Digest
    required_checks: tuple[CheckId, ...] = CHECKS
    executed_policy_ids: tuple[PolicyId, ...] = ()
    timings: tuple[Timing, ...]

    @model_validator(mode="after")
    def no_fabricated_policy(self) -> Self:
        if self.required_checks != CHECKS:
            raise ValueError("Backend check list cannot be weakened")
        if self.executed_policy_ids:
            raise ValueError("This harness does not execute a PDP")
        if self.condition == "A" and self.assessment is not None:
            raise ValueError("Control condition has no validator")
        if self.admission.status == "simulated_admitted":
            if self.admission.backend_failures or self.admission.code != "SIMULATED_ONLY":
                raise ValueError("Failed controls cannot admit")
            if self.proposal is None or self.original_context is None:
                raise ValueError("Missing admission evidence")
            if self.condition == "B" and (
                self.assessment is None
                or self.assessment.verdict != "accept"
                or self.assessment_status != "admitted"
                or self.assessment.coverage != CHECKS
                or self.assessment.proposal_digest != self.proposal.proposal_digest
                or self.assessment.context_ref != self.original_context.context_ref
            ):
                raise ValueError("Missing independent acceptance")
        return self


class DemoResult(ExperimentalModel):
    record: ReviewRecord
    record_digest: Digest
    effect_occurred: bool
    effective_admission: Admission
    delivery_code: Literal[
        "NONE", "EMITTED", "EVIDENCE_UNAVAILABLE", "SINK_UNAVAILABLE", "RECEIPT_UNAVAILABLE"
    ]
    timings: tuple[Timing, ...]

    @model_validator(mode="after")
    def observed_effect(self) -> Self:
        if self.record_digest != digest("hnp-review-record/v1", self.record):
            raise ValueError("Record digest mismatch")
        if self.effect_occurred != (self.delivery_code in ("EMITTED", "RECEIPT_UNAVAILABLE")):
            raise ValueError("Effect observation mismatch")
        if self.effect_occurred and self.record.admission.status != "simulated_admitted":
            raise ValueError("Effect lacks admission")
        return self
