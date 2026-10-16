"""Private backend lookup transport. No API for arbitrary paths or commercial effects."""

import hashlib
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import SecretStr

from commerce_agent.config import validate_service_base_url
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.dispatch_envelope import CheckoutCreationEnvelope
from commerce_agent.governance.remote_creation import (
    LOOKUP_PATH,
    LOOKUP_SCHEMA,
    CreationAbsent,
    CreationFound,
    LookupError,
    SessionScope,
    Verification,
    verify_found,
)


@dataclass(frozen=True)
class BackendLookupSession:
    """Injected by trusted session provisioning; never built from buyer/tool arguments."""

    merchant_base_url: str
    scope: SessionScope | None
    bearer: SecretStr | None = field(repr=False)


class CreationLookupClient:
    def __init__(
        self,
        session: BackendLookupSession,
        *,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = 262144,
        transport: httpx.BaseTransport | None = None,
    ):
        self._base = validate_service_base_url(session.merchant_base_url)
        url = urlsplit(self._base)
        if (
            "@" in url.netloc
            or "\\" in self._base
            or "%" in url.netloc
            or any(ord(c) <= 32 or ord(c) == 127 for c in self._base)
        ):
            raise ValueError("Invalid backend destination")
        # Deliberately discard the ACP prefix; this lab endpoint is absolute at the origin.
        self._endpoint = urlunsplit((url.scheme, url.netloc, LOOKUP_PATH, "", ""))
        if not 0 < timeout_seconds <= 60 or not 1024 <= max_response_bytes <= 1048576:
            raise ValueError("Invalid lookup limits")
        self._session = session
        self._limit = max_response_bytes
        self._basis = (
            "controlled_transport"
            if transport is not None
            else "https_backend_session"
            if url.scheme == "https"
            else "loopback_backend_session"
        )
        self._http = httpx.Client(
            timeout=timeout_seconds, transport=transport, follow_redirects=False, trust_env=False
        )

    def close(self) -> None:
        self._http.close()

    def lookup(
        self, envelope: CheckoutCreationEnvelope, known_checkout_id: str | None
    ) -> Verification:
        # Revalidate caller models, including model_construct/model_copy bypasses.
        envelope = parse_model(CheckoutCreationEnvelope, envelope.storage_bytes())
        if envelope.merchant_origin != self._base:
            return Verification(outcome="unattributable", reason="ORIGIN_CONFLICT")
        scope = self._session.scope
        bearer = self._session.bearer
        if scope is None:
            return Verification(outcome="unattributable", reason="SESSION_BINDING_UNAVAILABLE")
        try:
            scope = parse_model(SessionScope, canonical_bytes(scope))
        except Exception:
            return Verification(outcome="unattributable", reason="SESSION_BINDING_UNAVAILABLE")
        expected = envelope.expected_remote_binding
        if expected is None:
            return Verification(outcome="unattributable", reason="EXPECTED_BINDING_UNAVAILABLE")
        if (scope.remote_run_id, scope.remote_actor_id) != (
            expected.remote_run_id,
            expected.remote_actor_id,
        ):
            return Verification(outcome="unattributable", reason="SCOPE_CONFLICT")
        if (
            bearer is None
            or not bearer.get_secret_value()
            or any(ord(c) <= 32 or ord(c) > 126 for c in bearer.get_secret_value())
        ):
            return Verification(outcome="authentication_error", reason="AUTHENTICATION_REJECTED")
        key_hash = hashlib.sha256(envelope.idempotency_key.get_secret_value().encode()).hexdigest()
        try:
            with self._http.stream(
                "POST",
                self._endpoint,
                headers={
                    "Authorization": "Bearer " + bearer.get_secret_value(),
                    "API-Version": "2026-04-17",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                json={"schema_version": LOOKUP_SCHEMA, "idempotency_key_sha256": key_hash},
            ) as response:
                if 300 <= response.status_code < 400:
                    return Verification(outcome="unattributable", reason="REDIRECT_REJECTED")
                if response.headers.get("cache-control", "").lower() != "no-store":
                    return Verification(outcome="malformed_response", reason="NO_STORE_REQUIRED")
                if (
                    response.headers.get("content-type", "").split(";")[0].strip().lower()
                    != "application/json"
                    or response.headers.get("content-encoding", "identity") != "identity"
                ):
                    return Verification(
                        outcome="malformed_response", reason="WIRE_CONTRACT_INVALID"
                    )
                payload = bytearray()
                # iter_bytes also supports pre-consumed MockTransport responses. The
                # production endpoint must use identity encoding; decompression is disallowed.
                for chunk in response.iter_bytes(chunk_size=8192):
                    if len(payload) + len(chunk) > self._limit:
                        return Verification(
                            outcome="malformed_response", reason="RESPONSE_TOO_LARGE"
                        )
                    payload.extend(chunk)
                raw = bytes(payload)
                digest = sha256_digest(raw)
                if response.status_code != 200:
                    error = parse_model(LookupError, raw)
                    if (response.status_code, error.code) not in {
                        (400, "invalid_api_version"),
                        (401, "unauthenticated"),
                        (422, "invalid_request"),
                        (503, "lookup_unavailable"),
                    }:
                        raise ValueError("Status/code mismatch")
                    return Verification(
                        outcome="authentication_error"
                        if response.status_code == 401
                        else "transport_error",
                        reason="AUTHENTICATION_REJECTED"
                        if response.status_code == 401
                        else "LOOKUP_UNAVAILABLE",
                        remote_request_id=error.request_id,
                        lookup_response_digest=digest,
                    )
                # Discriminator parsed without accepting duplicates/unknown metadata.
                import json

                kind = json.loads(raw).get("result")
                found = (
                    parse_model(CreationFound, raw)
                    if kind == "found"
                    else parse_model(CreationAbsent, raw)
                )
                facts = dict(
                    basis=self._basis,
                    session_scope=scope,
                    remote_request_id=found.request_id,
                    lookup_response_digest=digest,
                )

                def result(outcome, reason, **extra):
                    return Verification.model_validate(
                        {"outcome": outcome, "reason": reason, **facts, **extra}
                    )

                if (found.run_id, found.actor_id) != (scope.remote_run_id, scope.remote_actor_id):
                    return result("identity_conflict", "SCOPE_CONFLICT")
                if found.idempotency_key_sha256 != key_hash:
                    return result("identity_conflict", "KEY_CONFLICT")
                if isinstance(found, CreationAbsent):
                    return result(
                        found.result,
                        (
                            "NOT_FOUND_NOT_ABSENCE"
                            if found.result == "not_found"
                            else "UNRECOVERABLE_NO_BACKFILL"
                        ),
                    )
                outcome, reason = verify_found(found, envelope, known_checkout_id)
                return result(
                    outcome,
                    reason,
                    **(
                        {
                            "original_response_digest": "sha256:" + found.original_response_sha256,
                            "checkout_id": found.checkout_id,
                        }
                        if outcome == "found_verified"
                        else {}
                    ),
                )
        except httpx.TimeoutException:
            return Verification(outcome="timeout", reason="LOOKUP_TIMEOUT")
        except httpx.TransportError:
            return Verification(outcome="transport_error", reason="TRANSPORT_FAILED")
        except Exception:
            return Verification(outcome="malformed_response", reason="WIRE_CONTRACT_INVALID")
