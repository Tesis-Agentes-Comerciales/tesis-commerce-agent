"""Historical creation verification only; no commercial authorization or replay."""

import hashlib
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from commerce_agent.acp.models import CheckoutSession, Identifier
from commerce_agent.acp.schema import validate_acp
from commerce_agent.canonical_json import canonical_original_response

from .dispatch_envelope import (
    CheckoutCreationEnvelope,
    EnvelopeModel,
    ExpectedRemoteBinding,
    Identity,
)
from .models import Digest, Id, ObjectRef, UtcTime
from .reconciliation import RecoveredOperation

LOOKUP_PATH = "/lab/recovery/checkout-creation/lookup"
LOOKUP_SCHEMA = "merchant-acp-creation-lookup/1"
HexDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]


def original_response_sha256(value: object) -> str:
    return hashlib.sha256(
        b"merchant-acp-original-response/v1\0" + canonical_original_response(value)
    ).hexdigest()


class LookupScope(EnvelopeModel):
    schema_version: Literal["merchant-acp-creation-lookup/1"]
    run_id: Identifier
    actor_id: Identifier
    idempotency_key_sha256: HexDigest
    request_id: Identifier


class CreationFound(LookupScope):
    result: Literal["found"]
    acp_api_version: Literal["2026-04-17"]
    acp_create_fingerprint: HexDigest
    checkout_id: Identifier
    original_response: dict[str, Any] = Field(repr=False)
    original_response_profile: Literal["merchant-acp-original-response/1"]
    original_response_sha256: HexDigest


class CreationAbsent(LookupScope):
    result: Literal["not_found", "unrecoverable"]


class LookupError(EnvelopeModel):
    schema_version: Literal["merchant-acp-creation-lookup/1"]
    result: Literal["error"]
    code: Literal["invalid_request", "unauthenticated", "invalid_api_version", "lookup_unavailable"]
    request_id: Identifier


class SessionScope(EnvelopeModel):
    """Backend registry assertion about THIS credential, not an expected envelope binding.

    The trusted caller must obtain this from session provisioning/registry. Neither
    JSON echoes nor constructing this model verifies a credential cryptographically.
    """

    assurance: Literal["backend_session_registry/1"]
    remote_run_id: str
    remote_actor_id: Identifier
    source_ref: ObjectRef

    @model_validator(mode="after")
    def canonical_run(self):
        ExpectedRemoteBinding.canonical_uuid(self.remote_run_id)
        return self


Outcome = Literal[
    "found_verified",
    "identity_conflict",
    "fingerprint_conflict",
    "response_invalid",
    "not_found",
    "unrecoverable",
    "authentication_error",
    "transport_error",
    "timeout",
    "malformed_response",
    "unattributable",
    "local_invalid",
    "persistence_error",
    "cas_conflict",
]
Reason = Literal[
    "HISTORICAL_CREATION_ONLY",
    "ORIGIN_CONFLICT",
    "SESSION_BINDING_UNAVAILABLE",
    "EXPECTED_BINDING_UNAVAILABLE",
    "SCOPE_CONFLICT",
    "KEY_CONFLICT",
    "FINGERPRINT_CONFLICT",
    "CHECKOUT_CONFLICT",
    "ORIGINAL_RESPONSE_INVALID",
    "WIRE_CONTRACT_INVALID",
    "AUTHENTICATION_REJECTED",
    "TRANSPORT_FAILED",
    "LOOKUP_TIMEOUT",
    "REDIRECT_REJECTED",
    "LOOKUP_UNAVAILABLE",
    "NO_STORE_REQUIRED",
    "RESPONSE_TOO_LARGE",
    "NOT_FOUND_NOT_ABSENCE",
    "UNRECOVERABLE_NO_BACKFILL",
    "LOCAL_INTEGRITY_FAILED",
    "LOCAL_PERSISTENCE_FAILED",
    "LOCAL_STATE_CHANGED",
]


class Verification(EnvelopeModel):
    schema_version: Literal["commerce-remote-creation-verification/1"] = (
        "commerce-remote-creation-verification/1"
    )
    outcome: Outcome
    reason: Reason
    basis: Literal[
        "none", "https_backend_session", "loopback_backend_session", "controlled_transport"
    ] = "none"
    session_scope: SessionScope | None = None
    remote_request_id: Identifier | None = None
    lookup_response_digest: Digest | None = None
    original_response_digest: Digest | None = None
    checkout_id: Identifier | None = None
    replay_allowed: Literal[False] = False
    continuation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def coherent(self):
        if self.outcome == "found_verified" and (
            self.reason != "HISTORICAL_CREATION_ONLY"
            or self.basis == "none"
            or self.session_scope is None
            or self.remote_request_id is None
            or self.lookup_response_digest is None
            or self.original_response_digest is None
            or self.checkout_id is None
        ):
            raise ValueError("Incomplete historical verification")
        if self.outcome != "found_verified" and (
            self.original_response_digest is not None or self.checkout_id is not None
        ):
            raise ValueError("Unverified creation facts")
        return self


class VerificationRequest(EnvelopeModel):
    schema_version: Literal["commerce-remote-creation/1"] = "commerce-remote-creation/1"
    query_id: Id
    operation_id: Id
    run_id: Id
    owner_id: Identity = Field(repr=False)
    expected_version: Annotated[int, Field(ge=0)]


class LocalCreation(EnvelopeModel):
    recovered: RecoveredOperation
    envelope_digest: Digest
    post_create_history_digest: Digest
    historical_checkout_id: Identifier | None


class VerificationEvent(EnvelopeModel):
    schema_version: Literal["commerce-remote-creation/1"] = "commerce-remote-creation/1"
    query_id: Id
    request_digest: Digest
    version: Annotated[int, Field(ge=1)]
    previous_digest: Digest | None
    recorded_at: UtcTime
    local: LocalCreation
    verification: Verification


class VerificationAttempt(EnvelopeModel):
    verification: Verification
    durable: bool
    event: VerificationEvent | None

    @model_validator(mode="after")
    def committed(self):
        if self.durable != (self.event is not None) or (
            self.event is not None and self.event.verification != self.verification
        ):
            raise ValueError("Inconsistent durability")
        return self


def verify_found(
    found: CreationFound,
    envelope: CheckoutCreationEnvelope,
    known_checkout_id: str | None,
) -> tuple[Outcome, Reason]:
    key_hash = hashlib.sha256(envelope.idempotency_key.get_secret_value().encode()).hexdigest()
    if found.idempotency_key_sha256 != key_hash:
        return "identity_conflict", "KEY_CONFLICT"
    # Only at this boundary: the same merchant-acp-create/v1 domain, bare vs labelled SHA256.
    if "sha256:" + found.acp_create_fingerprint != envelope.body_digest:
        return "fingerprint_conflict", "FINGERPRINT_CONFLICT"
    try:
        if original_response_sha256(found.original_response) != found.original_response_sha256:
            raise ValueError("Response digest mismatch")
        # Response capabilities describe the seller, not an echo of the agent's
        # requested capabilities. Validate them through the frozen response schema.
        validate_acp("CheckoutSession", found.original_response)
        checkout = CheckoutSession.model_validate(found.original_response)
        body = envelope.body()
        if checkout.id != found.checkout_id or (
            known_checkout_id is not None and known_checkout_id != found.checkout_id
        ):
            return "identity_conflict", "CHECKOUT_CONFLICT"
        if (
            envelope.creation_profile != "merchant-p0-create/1"
            or checkout.currency != body["currency"]
            or len(checkout.line_items) != 1
            or checkout.line_items[0].get("item", {}).get("id") != body["line_items"][0]["id"]
            or checkout.line_items[0].get("quantity") != 1
            or checkout.status != "not_ready_for_payment"
        ):
            raise ValueError("Creation identity mismatch")
    except Exception:
        return "response_invalid", "ORIGINAL_RESPONSE_INVALID"
    return "found_verified", "HISTORICAL_CREATION_ONLY"
