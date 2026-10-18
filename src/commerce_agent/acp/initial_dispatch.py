"""Backend-injected first POST only. Admission and durable claim belong to D1 UoW."""

import json
from typing import Protocol

import httpx

from commerce_agent.acp.client import ACPClient, ACPClientError
from commerce_agent.acp.creation_lookup import BackendLookupSession
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.acp.schema import validate_acp
from commerce_agent.governance.canonical import canonical_bytes, parse_model
from commerce_agent.governance.dispatch_envelope import CheckoutCreationEnvelope
from commerce_agent.governance.remote_creation import SessionScope


class EnvelopeInitialDispatch(Protocol):
    def validate(self, envelope: CheckoutCreationEnvelope) -> None: ...

    def create_once(self, envelope: CheckoutCreationEnvelope) -> CheckoutSession: ...


class HttpInitialCheckoutDispatch:
    """A single HTTP invocation; never retry, lookup, redirect or subsequent effect.

    The session is provisioned by the backend, not reconstructed from a response.
    This transport is not a standalone authorization capability: only the gate's
    fresh committed claim admits a call. Recovered reservations cannot use it.
    """

    def __init__(
        self,
        session: BackendLookupSession,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ):
        if not 0 < timeout_seconds <= 60:
            raise ValueError("Invalid initial dispatch timeout")
        self._session = session
        self._http = httpx.Client(
            timeout=timeout_seconds, transport=transport, follow_redirects=False, trust_env=False
        )

    def close(self) -> None:
        self._http.close()

    def validate(self, envelope: CheckoutCreationEnvelope) -> None:
        envelope = parse_model(CheckoutCreationEnvelope, envelope.storage_bytes())
        expected = envelope.expected_remote_binding
        scope = self._session.scope
        bearer = self._session.bearer
        try:
            if scope is None or expected is None:
                raise ValueError("Missing backend scope")
            scope = parse_model(SessionScope, canonical_bytes(scope))
            if (
                self._session.merchant_base_url != envelope.merchant_origin
                or (scope.remote_run_id, scope.remote_actor_id)
                != (expected.remote_run_id, expected.remote_actor_id)
                or bearer is None
                or not bearer.get_secret_value()
                or any(ord(c) <= 32 or ord(c) > 126 for c in bearer.get_secret_value())
            ):
                raise ValueError("Invalid backend session")
        except Exception:
            raise ACPClientError(
                "Initial dispatch binding unavailable", code="initial_binding"
            ) from None

    def create_once(self, envelope: CheckoutCreationEnvelope) -> CheckoutSession:
        self.validate(envelope)
        envelope = parse_model(CheckoutCreationEnvelope, envelope.storage_bytes())
        assert self._session.bearer is not None
        key = envelope.idempotency_key.get_secret_value()
        try:
            with self._http.stream(
                envelope.method,
                envelope.merchant_origin + envelope.path,
                headers={
                    "Authorization": "Bearer " + self._session.bearer.get_secret_value(),
                    "API-Version": envelope.api_version,
                    "Idempotency-Key": key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                content=envelope.body_canonical.encode("utf-8"),
            ) as response:
                if (
                    response.status_code != 201
                    or response.headers.get("Idempotency-Key") != key
                    or ACPClient._merchant_request_id(response) is None
                    or response.headers.get("content-type", "").split(";")[0].strip().lower()
                    != "application/json"
                    or response.headers.get("content-encoding", "identity") != "identity"
                ):
                    raise ValueError("Unverified creation response")
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=8192):
                    if len(raw) + len(chunk) > ACPClient._MAX_RESPONSE_BYTES:
                        raise ValueError("Oversized creation response")
                    raw.extend(chunk)
                checkout = parse_model(CheckoutSession, bytes(raw))
                validate_acp("CheckoutSession", json.loads(raw))
                body = envelope.body()
                if (
                    checkout.status != "not_ready_for_payment"
                    or checkout.currency != body["currency"]
                    or len(checkout.line_items) != 1
                    or checkout.line_items[0].get("item", {}).get("id")
                    != body["line_items"][0]["id"]
                    or checkout.line_items[0].get("quantity") != 1
                ):
                    raise ValueError("Creation identity conflict")
                return checkout
        except Exception:
            # Even HTTP rejection is not promoted to proven absence of remote effects.
            # Never include a response, header, token or raw key in exception chains.
            raise ACPClientError(
                "Initial dispatch requires reconciliation", code="initial_uncertain"
            ) from None
