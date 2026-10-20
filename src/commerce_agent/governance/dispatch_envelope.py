"""Backend-only reproducible creation intent; never a dispatch/complete capability.

The key is operational storage, redacted in ordinary model dumps. No bearer or
authenticated remote identity is represented. Expected scope is a backend assertion.
"""

import hashlib
import hmac
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretBytes,
    SecretStr,
    field_validator,
    model_validator,
)

from commerce_agent.acp.models import CheckoutCreateRequest
from commerce_agent.acp.schema import validate_acp
from commerce_agent.config import validate_service_base_url

from .canonical import canonical_bytes, parse_model, sha256_digest
from .models import Digest, Id, ObjectRef, UtcTime

Identity = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")]


class EnvelopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class ExpectedRemoteBinding(EnvelopeModel):
    """Supplied through trusted backend injection, not by buyer/model tools.

    source_ref identifies that assertion's provenance; it is not authentication
    evidence and does not attest a token, deployment or actual HTTP destination.
    """

    assurance: Literal["backend_expected_not_authenticated"]
    remote_run_id: str
    remote_actor_id: Identity
    source_ref: ObjectRef

    @field_validator("remote_run_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("Expected remote run must be a canonical UUID")
        return value


def creation_body(payload: bytes) -> dict[str, Any]:
    # parse_model rejects duplicate JSON keys and coercible/unknown fields.
    request = parse_model(CheckoutCreateRequest, payload)
    import json

    raw = json.loads(payload)
    if (
        not {"line_items", "currency", "capabilities"} <= raw.keys()
        or request.currency != "cop"
        or len(request.line_items) != 1
        or request.capabilities.payment.handlers
    ):
        raise ValueError("Unsupported Merchant COP/one-unit creation profile")
    validate_acp("CheckoutSessionCreateRequest", raw)
    return raw


def merchant_creation_digest(body: bytes) -> str:
    """Same fingerprint algorithm as Merchant 71f9dba, with explicit digest label."""
    return sha256_digest(
        b"merchant-acp-create/v1\0"
        + canonical_bytes(
            {
                "api_version": "2026-04-17",
                "profile": "merchant-p0-create/1",
                "request": creation_body(body),
            }
        )
    )


def creation_idempotency_key(
    secret: SecretBytes,
    *,
    run_id: str,
    owner_id: str,
    merchant_origin: str,
    request: CheckoutCreateRequest,
) -> str:
    """Idempotency-Key of one creation intent, reproducible without local state.

    HMAC-SHA256 under the backend pseudonym key over run, owner, Merchant origin
    and the exact canonical ACP body. Attempt, decision, evidence, operation and
    clock identifiers are excluded: they are fresh after a lost store, and the
    Merchant only deduplicates the same key. Merchant 0dca108 scopes keys by
    remote run/actor without TTL; the same key and fingerprint replay the durable
    original, while another body under that key is IDEMPOTENCY_CONFLICT. A
    different candidate or body thus receives a different key. Rotating the
    pseudonym key starts a new key space, as it already does for the vault.
    """
    value = canonical_bytes(
        [run_id, owner_id, merchant_origin, canonical_bytes(request).decode("utf-8")]
    )
    return (
        "agent_"
        + hmac.new(
            secret.get_secret_value(),
            b"commerce-acp-create-idempotency/v1\0" + value,
            hashlib.sha256,
        ).hexdigest()
    )


class CheckoutCreationEnvelope(EnvelopeModel):
    schema_version: Literal["commerce-acp-create-envelope/1"]
    operation_id: Id
    run_id: Id
    owner_id: Identity = Field(repr=False)
    merchant_origin: str
    merchant_ref: Id
    api_version: Literal["2026-04-17"]
    creation_profile: Literal["merchant-p0-create/1"]
    method: Literal["POST"]
    path: Literal["/checkout_sessions"]
    body_canonical: Annotated[str, Field(min_length=1, max_length=16384)] = Field(repr=False)
    body_digest: Digest
    idempotency_key: SecretStr = Field(repr=False, min_length=1, max_length=128)
    decision_id: Id
    evidence_id: Id
    evidence_digest: Digest
    d1_input_digest: Digest
    context_ref: ObjectRef
    prepared_at: UtcTime
    binding_status: Literal["unavailable", "backend_expected_unverified"]
    expected_remote_binding: ExpectedRemoteBinding | None

    @field_validator("merchant_origin")
    @classmethod
    def exact_origin(cls, value: str) -> str:
        if validate_service_base_url(value) != value:
            raise ValueError("Merchant origin must already be normalized")
        return value

    @model_validator(mode="after")
    def consistent(self) -> Self:
        body = self.body_canonical.encode("utf-8")
        if canonical_bytes(creation_body(body)) != body or merchant_creation_digest(body) != (
            self.body_digest
        ):
            raise ValueError("Noncanonical or inconsistent ACP creation body")
        if self.binding_status != (
            "unavailable" if self.expected_remote_binding is None else "backend_expected_unverified"
        ):
            raise ValueError("Expected remote scope is not verified authentication")
        return self

    def storage_bytes(self) -> bytes:
        """Explicit private serialization; callers must not log or export it."""
        payload = self.model_dump(mode="json", exclude={"idempotency_key"})
        payload["idempotency_key"] = self.idempotency_key.get_secret_value()
        return canonical_bytes(payload)

    def digest(self) -> str:
        return sha256_digest(b"commerce-acp-create-envelope/v1\0" + self.storage_bytes())

    def body(self) -> dict[str, Any]:
        return creation_body(self.body_canonical.encode("utf-8"))
