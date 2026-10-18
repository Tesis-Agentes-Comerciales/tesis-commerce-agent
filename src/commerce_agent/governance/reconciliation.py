"""Backend reconciliation facts. No remote verifier or dispatch capability exists.

A future verifier must validate issuer/scope evidence, original scope binding,
request key/fingerprint/checkout membership, and retention for the whole uncertain
interval. An HTTP status, expected binding or fixture cannot satisfy that contract.
Reauthorization for a possibly first POST must be independently defined as well.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from commerce_agent.config import validate_service_base_url

from .dispatch_envelope import EnvelopeModel, ExpectedRemoteBinding, Identity
from .models import Digest, Id, ObjectRef, UtcTime

LocalOutcome = Literal[
    "reserved", "claimed_without_observation", "uncertain", "confirmed", "definitive_failure"
]
CheckoutStatus = Literal[
    "not_ready_for_payment", "ready_for_payment", "completed", "canceled", "expired"
]
Reason = Literal[
    "ENVELOPE_MISSING",
    "ENVELOPE_UNVERIFIABLE",
    "AUTHORIZATION_UNVERIFIABLE",
    "LOCAL_CHECKOUT_CONFLICT",
    "LOCAL_HISTORY_UNVERIFIABLE",
    "REMOTE_PROOF_UNAVAILABLE",
    "EXPECTED_BINDING_NOT_AUTHENTICATED",
    "CLAIM_NOT_DELIVERY_PROOF",
    "HISTORICAL_AUTHORIZATION_NOT_REPLAY_AUTHORITY",
    "RETENTION_UNPROVEN",
    "REMOTE_SCOPE_CONFLICT",
    "REQUEST_KEY_CONFLICT",
    "ACP_FINGERPRINT_CONFLICT",
    "CHECKOUT_ID_CONFLICT",
    "HISTORICAL_CURRENT_DIFFER",
    "HISTORICAL_CONFIRMATION_ONLY",
    "CURRENT_STATE_NOT_CONTINUATION_AUTHORITY",
]


class SnapshotObservation(EnvelopeModel):
    """Digest/ref of a supplied snapshot, never authenticated commercial facts."""

    checkout_id: Id
    status: CheckoutStatus
    snapshot_digest: Digest
    source_ref: ObjectRef
    observed_at: UtcTime


class UnverifiedMerchantObservation(EnvelopeModel):
    provenance: Literal["offline_fixture", "unverified_external"]
    source_ref: ObjectRef
    merchant_origin: str
    remote_run_id: str
    remote_actor_id: Identity
    idempotency_key_digest: Digest
    acp_fingerprint: Digest
    original_creation: SnapshotObservation | None
    current_checkout: SnapshotObservation | None

    @field_validator("merchant_origin")
    @classmethod
    def exact_origin(cls, value: str) -> str:
        if validate_service_base_url(value) != value:
            raise ValueError("Expected normalized origin without credentials")
        return value

    @field_validator("remote_run_id")
    @classmethod
    def exact_run(cls, value: str) -> str:
        return ExpectedRemoteBinding.canonical_uuid(value)


class ReconciliationRequest(EnvelopeModel):
    schema_version: Literal["commerce-d1-reconciliation/1"]
    request_id: Id
    operation_id: Id
    run_id: Id
    owner_id: Identity = Field(repr=False)
    expected_projection_version: Annotated[int, Field(ge=0)]
    observation: UnverifiedMerchantObservation | None


class RecoveredOperation(EnvelopeModel):
    operation_id: Id
    run_id: Id
    owner_ref: Id
    local_outcome: LocalOutcome
    purchase_stage: str
    purchase_version: Annotated[int, Field(ge=1)]
    historical_checkout_id: Id | None
    evidence_id: Id
    evidence_digest: Digest
    d1_input_digest: Digest
    authorization: Literal["historical_allow_only", "unverifiable"]
    envelope_status: Literal["available", "missing", "unverifiable"]
    envelope_digest: Digest | None
    binding_status: Literal["unavailable", "backend_expected_unverified"]
    projection_version: Annotated[int, Field(ge=0)]


class ReplayAssessment(EnvelopeModel):
    """Missing prerequisites are explicit; this increment cannot admit any replay."""

    eligible: Literal[False] = False
    required: tuple[
        Literal[
            "VERIFIED_REMOTE_SCOPE_AND_CREATION_BINDING",
            "CURRENT_REAUTHORIZATION_FOR_POSSIBLE_FIRST_POST",
            "IDEMPOTENCY_RETENTION_GUARANTEE",
        ],
        ...,
    ] = (
        "VERIFIED_REMOTE_SCOPE_AND_CREATION_BINDING",
        "CURRENT_REAUTHORIZATION_FOR_POSSIBLE_FIRST_POST",
        "IDEMPOTENCY_RETENTION_GUARANTEE",
    )

    @model_validator(mode="after")
    def complete(self) -> Self:
        if self.required != type(self).model_fields["required"].default:
            raise ValueError("Replay prerequisites cannot be removed")
        return self


class ReconciliationEvent(EnvelopeModel):
    schema_version: Literal["commerce-d1-reconciliation/1"]
    event_id: Id
    request_id: Id
    request_digest: Digest
    version: Annotated[int, Field(ge=1)]
    recorded_at: UtcTime
    source: Literal["local_sqlite_inspection/1"]
    recovered: RecoveredOperation
    observation: UnverifiedMerchantObservation | None
    outcome: Literal["blocked", "historical_confirmation_only"]
    reasons: Annotated[tuple[Reason, ...], Field(min_length=1)]
    replay: ReplayAssessment
    continuation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("Duplicate reconciliation reasons")
        if self.version != self.recovered.projection_version + 1:
            raise ValueError("Inconsistent reconciliation version")
        if self.outcome == "historical_confirmation_only" and (
            self.recovered.local_outcome != "confirmed"
            or self.recovered.authorization != "historical_allow_only"
            or self.recovered.envelope_status != "available"
            or "HISTORICAL_CONFIRMATION_ONLY" not in self.reasons
        ):
            raise ValueError("No verified local historical confirmation")
        return self
