"""Private bounded reads only; offer.read is observable, not PostgreSQL READ ONLY."""

import secrets
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr, model_validator

from commerce_agent.acp.feed import ProductFeed, load_product_feed_payload
from commerce_agent.acp.offer_snapshot import (
    DeliveryContext,
    MerchantOffer,
    OfferResponse,
    SnapshotModel,
)
from commerce_agent.canonical_json import canonical_original_response
from commerce_agent.config import validate_service_base_url
from commerce_agent.governance.canonical import canonical_bytes, parse_model
from commerce_agent.governance.models import ObjectRef
from commerce_agent.governance.remote_creation import CreationAbsent, SessionScope


class ObservationBlocked(ValueError):
    """Sanitized reason only, no response, URL query, credentials or exception chain."""


class CatalogBinding(SnapshotModel):
    """Trusted provisioning receipt, never inferred from feed IDs or a token echo.

    source_ref must resolve in the backend registry to the managed_catalog_context
    and credential issuance record for this deployment. Constructing this object
    is not a remote attestation. Missing registry evidence must block assembly.
    """

    assurance: Literal["backend_managed_catalog_registry/1"]
    merchant_origin: str
    merchant_id: str
    catalog_kind: Literal["merchant"]
    feed_id: Literal["feed_merchant_catalog"]
    scope: SessionScope
    source_ref: ObjectRef

    @model_validator(mode="after")
    def origin(self):
        parsed = urlsplit(self.merchant_origin)
        if (
            validate_service_base_url(self.merchant_origin) != self.merchant_origin
            or parsed.path
            or any(c in self.merchant_origin for c in ("\\", "%", "@"))
            or any(ord(c) <= 32 or ord(c) == 127 for c in self.merchant_origin)
        ):
            raise ValueError("Invalid observation origin")
        return self


@dataclass(frozen=True)
class AuthoritySession:
    binding: CatalogBinding
    bearer: SecretStr = field(repr=False)


class AuthorityObservationClient:
    """Backend injection, absent from runtime/tools. No general request method."""

    def __init__(
        self,
        session: AuthoritySession,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.binding = parse_model(CatalogBinding, canonical_bytes(session.binding))
        if not 0 < timeout_seconds <= 30:
            raise ValueError("Invalid observation timeout")
        self._bearer = session.bearer
        self._timeout = timeout_seconds
        token = self._bearer.get_secret_value()
        if not token or len(token) > 512 or any(ord(c) <= 32 or ord(c) > 126 for c in token):
            raise ObservationBlocked("SESSION_INVALID")
        self._http = httpx.Client(
            timeout=timeout_seconds, follow_redirects=False, trust_env=False, transport=transport
        )

    def close(self) -> None:
        self._http.close()

    def _read(self, path: str, body: dict | None, limit: int) -> tuple[bytes, str]:
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if body is not None:
            headers.update(
                Authorization="Bearer " + self._bearer.get_secret_value(),
                **{"API-Version": "2026-04-17", "Content-Type": "application/json"},
            )
        try:
            deadline = monotonic() + self._timeout
            with self._http.stream(
                "GET" if body is None else "POST",
                self.binding.merchant_origin + path,
                headers=headers,
                content=None if body is None else canonical_bytes(body),
            ) as response:
                if response.status_code in (401, 403):
                    raise ObservationBlocked("SESSION_REJECTED")
                if response.status_code != 200:
                    raise ObservationBlocked("READ_UNAVAILABLE")
                if (
                    response.headers.get("content-type", "").split(";")[0].lower()
                    != "application/json"
                    or response.headers.get("content-encoding", "identity") != "identity"
                ):
                    raise ObservationBlocked("INVALID_RESPONSE")
                raw = bytearray()
                # Do not buffer 8192 bytes before checking the deadline: a peer
                # could otherwise drip bytes below the per-read HTTP timeout.
                for chunk in response.iter_bytes():
                    if monotonic() > deadline:
                        raise ObservationBlocked("READ_TIMEOUT")
                    if len(raw) + len(chunk) > limit:
                        raise ObservationBlocked("RESPONSE_TOO_LARGE")
                    raw.extend(chunk)
                request_id = response.headers.get("Request-Id", "")
                if (
                    not request_id
                    or len(request_id) > 128
                    or any(ord(c) <= 32 or ord(c) > 126 for c in request_id)
                    or self._bearer.get_secret_value() in request_id
                ):
                    raise ObservationBlocked("REQUEST_ID_UNAVAILABLE")
                import json

                normalized = canonical_original_response(json.loads(raw))
                if self._bearer.get_secret_value().encode() in normalized:
                    raise ObservationBlocked("CREDENTIAL_REFLECTION")
                return bytes(raw), request_id
        except ObservationBlocked:
            raise
        except Exception:
            raise ObservationBlocked("READ_UNAVAILABLE") from None

    def discover(self) -> ProductFeed:
        raw, _ = self._read("/feeds/current", None, 8 * 1024 * 1024)
        try:
            # Duplicate keys are rejected before ACP schema validation.
            class Payload(SnapshotModel):
                metadata: dict
                products: list[dict]

            data = parse_model(Payload, raw).model_dump(mode="json")
            feed = load_product_feed_payload(data)
            if feed.metadata.id != self.binding.feed_id:
                raise ValueError("Catalog mismatch")
            return feed
        except Exception:
            raise ObservationBlocked("INVALID_FEED") from None

    def verify_scope(self) -> CreationAbsent:
        challenge = secrets.token_hex(32)
        raw, request_id = self._read(
            "/lab/recovery/checkout-creation/lookup",
            {
                "schema_version": "merchant-acp-creation-lookup/1",
                "idempotency_key_sha256": challenge,
            },
            4096,
        )
        try:
            result = parse_model(CreationAbsent, raw)
            if (
                result.result != "not_found"
                or result.idempotency_key_sha256 != challenge
                or result.run_id != self.binding.scope.remote_run_id
                or result.actor_id != self.binding.scope.remote_actor_id
                or result.request_id != request_id
            ):
                raise ValueError("Wrong authenticated scope")
            return result
        except Exception:
            raise ObservationBlocked("SCOPE_CONFLICT") from None

    def observe(self, variant_id: str, destination: DeliveryContext) -> tuple[MerchantOffer, str]:
        raw, request_id = self._read(
            "/lab/offers/get",
            {"offer_id": variant_id, "delivery_context": destination.model_dump(mode="json")},
            65536,
        )
        try:
            offer = parse_model(OfferResponse, raw).data
            if self._bearer.get_secret_value().encode() in canonical_bytes(offer):
                raise ValueError("Credential reflection rejected")
            return offer, request_id
        except Exception:
            raise ObservationBlocked("INVALID_OFFER") from None
