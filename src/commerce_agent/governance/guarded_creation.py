"""Private D17 wire contracts, independent of frozen ACP/07A envelopes."""

import hashlib
from typing import Literal, Self

from pydantic import Field, model_validator

from commerce_agent.acp.models import CheckoutCreateRequest
from commerce_agent.acp.offer_snapshot import MerchantOffer, offer_snapshot_sha256

from .canonical import canonical_bytes, parse_model, sha256_digest
from .dispatch_envelope import CheckoutCreationEnvelope, EnvelopeModel, creation_body
from .models import Digest, Id, ObjectRef, UtcTime
from .preselection import instant
from .preselection_d1 import PreselectionD1Binding
from .remote_creation import (
    CreationFound,
    HexDigest,
    canonical_original_response,
    verify_found,
)

CREATE_PATH = "/lab/governance/checkout-creation/guarded"
LOOKUP_PATH = "/lab/recovery/guarded-checkout-creation/lookup"
LOOKUP_SCHEMA = "merchant-guarded-creation-lookup/1"


def wire_digest(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + b"\0" + canonical_original_response(value)).hexdigest()


class GuardedInput(EnvelopeModel):
    schema_version: Literal["merchant-guarded-acp-create/1"] = "merchant-guarded-acp-create/1"
    acp_request: CheckoutCreateRequest
    offer_id: str
    expected_offer_revision: int = Field(ge=1)
    offer_snapshot_profile: Literal["merchant-offer-snapshot/1"] = "merchant-offer-snapshot/1"
    offer_snapshot_sha256: HexDigest

    def fingerprint(self) -> str:
        from .dispatch_envelope import merchant_creation_digest

        value = self.model_dump(mode="json", exclude={"acp_request"})
        body = canonical_bytes(self.acp_request.model_dump(mode="json", exclude_unset=True))
        value["acp_create_fingerprint"] = merchant_creation_digest(body)[7:]
        return wire_digest(b"merchant-guarded-acp-attempt/v1", value)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        body = self.acp_request.model_dump(mode="json", exclude_unset=True)
        creation_body(canonical_bytes(body))
        if body["line_items"][0]["id"] != self.offer_id:
            raise ValueError("Guard variant conflict")
        return self


class GuardedEnvelope(EnvelopeModel):
    schema_version: Literal["commerce-d17-envelope/1"] = "commerce-d17-envelope/1"
    operation_id: Id
    run_id: Id
    owner_id: str = Field(repr=False)
    merchant_origin: str
    api_version: Literal["2026-04-17"] = "2026-04-17"
    method: Literal["POST"] = "POST"
    path: Literal["/lab/governance/checkout-creation/guarded"] = CREATE_PATH
    idempotency_key_sha256: HexDigest
    decision_id: Id
    evidence_id: Id
    evidence_digest: Digest
    d1_input_digest: Digest
    context_ref: ObjectRef
    legacy_envelope_digest: Digest
    shadow_binding: PreselectionD1Binding
    shadow_binding_digest: Digest
    selected_offer: MerchantOffer
    request: GuardedInput
    guard_fingerprint: HexDigest
    prepared_at: UtcTime

    @model_validator(mode="after")
    def consistent(self) -> Self:
        shadow = self.shadow_binding
        if (
            self.shadow_binding_digest != sha256_digest(canonical_bytes(shadow))
            or self.guard_fingerprint != self.request.fingerprint()
            or shadow.request.run_id != self.run_id
            or shadow.request.owner_id != self.owner_id
            or shadow.offer_id != self.request.offer_id
            or shadow.offer_revision != self.request.expected_offer_revision
            or shadow.offer_snapshot_sha256 != self.request.offer_snapshot_sha256
            or shadow.merchant.merchant_origin != self.merchant_origin
            or shadow.evidence_id == self.evidence_id
            or shadow.decision_id == self.decision_id
            or self.selected_offer.id != self.request.offer_id
            or self.selected_offer.revision != self.request.expected_offer_revision
            or offer_snapshot_sha256(self.selected_offer) != self.request.offer_snapshot_sha256
        ):
            raise ValueError("D17 provenance conflict")
        return self


class GuardedProof(EnvelopeModel):
    schema_version: Literal["merchant-guarded-acp-proof/1"] = "merchant-guarded-acp-proof/1"
    offer_id: str
    accepted_offer_revision: int = Field(ge=1)
    offer_snapshot_profile: Literal["merchant-offer-snapshot/1"]
    accepted_offer_snapshot_sha256: HexDigest
    guard_fingerprint: HexDigest
    checked_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    creation: CreationFound

    def digest(self) -> str:
        return wire_digest(b"merchant-guarded-acp-proof/v1", self.model_dump(mode="json"))


class GuardedCreated(EnvelopeModel):
    schema_version: Literal["merchant-guarded-acp-create/1"]
    result: Literal["created", "replayed"]
    request_id: str
    proof: GuardedProof


class Rejection(EnvelopeModel):
    schema_version: Literal["merchant-guarded-acp-rejection/1"]
    run_id: str
    actor_id: str
    idempotency_sha256: HexDigest
    guard_fingerprint: HexDigest
    code: Literal[
        "guard_history_unavailable",
        "offer_not_found",
        "offer_revision_mismatch",
        "offer_snapshot_mismatch",
        "product_inactive",
        "out_of_stock",
        "offer_expired",
        "offer_invalid",
    ]

    def digest(self) -> str:
        return wire_digest(b"merchant-guarded-acp-rejection/v1", self.model_dump(mode="json"))


class GuardedHistory(EnvelopeModel):
    schema_version: Literal["merchant-guarded-creation-lookup/1"]
    result: Literal["found", "rejected", "not_found", "unrecoverable"]
    run_id: str
    actor_id: str
    idempotency_key_sha256: HexDigest
    request_id: str
    proof: GuardedProof | None = None
    proof_sha256: HexDigest | None = None
    rejection: Rejection | None = None
    rejection_sha256: HexDigest | None = None

    @model_validator(mode="after")
    def complete(self) -> Self:
        if (self.result == "found") != (self.proof is not None and self.proof_sha256 is not None):
            raise ValueError("Incomplete proof")
        if (self.result == "rejected") != (
            self.rejection is not None and self.rejection_sha256 is not None
        ):
            raise ValueError("Incomplete rejection")
        if self.result != "found" and (self.proof is not None or self.proof_sha256 is not None):
            raise ValueError("Unexpected proof")
        if self.result != "rejected" and (
            self.rejection is not None or self.rejection_sha256 is not None
        ):
            raise ValueError("Unexpected rejection")
        return self


class GuardedObservation(EnvelopeModel):
    schema_version: Literal["commerce-d17-observation/1"] = "commerce-d17-observation/1"
    result: Literal["found", "rejected", "not_found", "unrecoverable", "error"]
    proof: GuardedProof | None = None
    rejection: Rejection | None = None
    reason: Literal[
        "historical_creation", "historical_rejection", "uncertain", "invalid_or_unavailable"
    ]
    replay_allowed: Literal[False] = False
    continuation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.result == "found":
            valid = (
                self.proof is not None
                and self.rejection is None
                and self.reason == "historical_creation"
            )
        elif self.result == "rejected":
            valid = (
                self.rejection is not None
                and self.proof is None
                and self.reason == "historical_rejection"
            )
        else:
            valid = (
                self.proof is None
                and self.rejection is None
                and self.reason
                == ("invalid_or_unavailable" if self.result == "error" else "uncertain")
            )
        if not valid:
            raise ValueError("Incomplete historical observation")
        return self


def verify_proof(proof: GuardedProof, guard: GuardedEnvelope, legacy: CheckoutCreationEnvelope):
    proof = parse_model(GuardedProof, canonical_bytes(proof))
    expected = legacy.expected_remote_binding
    request = guard.request
    totals = proof.creation.original_response.get("totals", [])
    amounts = [(row.get("type"), row.get("amount")) for row in totals]
    pricing = guard.selected_offer.pricing
    if (
        expected is None
        or (proof.creation.run_id, proof.creation.actor_id)
        != (expected.remote_run_id, expected.remote_actor_id)
        or proof.offer_id != request.offer_id
        or proof.accepted_offer_revision != request.expected_offer_revision
        or proof.accepted_offer_snapshot_sha256 != request.offer_snapshot_sha256
        or proof.guard_fingerprint != guard.guard_fingerprint
        or instant(proof.checked_at) < instant(guard.prepared_at).replace(microsecond=0)
        or verify_found(proof.creation, legacy, None)[0] != "found_verified"
        or amounts
        != [
            ("subtotal", pricing.items_total_minor),
            ("fulfillment", pricing.shipping_total_minor),
            ("total", pricing.total_minor),
        ]
        or proof.creation.original_response["line_items"][0].get("item", {}).get("unit_amount")
        != pricing.items_total_minor
    ):
        raise ValueError("Historical guard proof mismatch")
