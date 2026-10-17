"""Backend-only D17 transport. One commercial call, independent read-only lookup."""

import hashlib

import httpx

from commerce_agent.acp.authority_observation import AuthoritySession, ObservationBlocked
from commerce_agent.config import validate_service_base_url
from commerce_agent.governance.canonical import canonical_bytes, parse_model
from commerce_agent.governance.dispatch_envelope import CheckoutCreationEnvelope
from commerce_agent.governance.guarded_creation import (
    CREATE_PATH,
    LOOKUP_PATH,
    LOOKUP_SCHEMA,
    GuardedCreated,
    GuardedEnvelope,
    GuardedHistory,
    GuardedObservation,
    verify_proof,
)


class HttpGuardedCheckout:
    """Not a standalone authority. The coordinator must commit and verify its claim."""

    def __init__(
        self,
        session: AuthoritySession,
        *,
        timeout_seconds: float = 5,
        transport: httpx.BaseTransport | None = None,
    ):
        if not 0 < timeout_seconds <= 60:
            raise ValueError("Invalid D17 timeout")
        self.session = session
        self._base = validate_service_base_url(session.binding.merchant_origin)
        self._http = httpx.Client(
            timeout=timeout_seconds, follow_redirects=False, trust_env=False, transport=transport
        )

    def close(self):
        self._http.close()

    def validate(self, guard: GuardedEnvelope, legacy: CheckoutCreationEnvelope):
        guard = parse_model(GuardedEnvelope, canonical_bytes(guard))
        legacy = parse_model(CheckoutCreationEnvelope, legacy.storage_bytes())
        binding = self.session.binding
        secret = self.session.bearer
        if (
            binding != guard.shadow_binding.merchant
            or self._base != legacy.merchant_origin
            or secret is None
            or not secret.get_secret_value()
            or any(ord(c) <= 32 or ord(c) > 126 for c in secret.get_secret_value())
            or legacy.expected_remote_binding is None
            or legacy.expected_remote_binding.remote_run_id != binding.scope.remote_run_id
            or legacy.expected_remote_binding.remote_actor_id != binding.scope.remote_actor_id
            or guard.legacy_envelope_digest != legacy.digest()
            or (guard.operation_id, guard.run_id, guard.owner_id)
            != (legacy.operation_id, legacy.run_id, legacy.owner_id)
            or canonical_bytes(guard.request.acp_request) != legacy.body_canonical.encode()
        ):
            raise ObservationBlocked("D17_SESSION_UNAVAILABLE")

    def _request(self, path, body, key=None):
        assert self.session.bearer is not None
        headers = {
            "Authorization": "Bearer " + self.session.bearer.get_secret_value(),
            "API-Version": "2026-04-17",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if key is not None:
            headers["Idempotency-Key"] = key
        with self._http.stream(
            "POST", self._base + path, headers=headers, content=body
        ) as response:
            if (
                response.status_code not in ({200} if path == LOOKUP_PATH else {200, 201})
                or response.headers.get("content-type", "").split(";")[0].strip().lower()
                != "application/json"
                or response.headers.get("content-encoding", "identity") != "identity"
                or response.headers.get("cache-control") != "no-store"
            ):
                raise ValueError("Unverified D17 response")
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=8192):
                if len(data) + len(chunk) > 262144:
                    raise ValueError("Oversized D17 response")
                data.extend(chunk)
            return bytes(data)

    def create_once(self, guard: GuardedEnvelope, legacy: CheckoutCreationEnvelope):
        guard = parse_model(GuardedEnvelope, canonical_bytes(guard))
        legacy = parse_model(CheckoutCreationEnvelope, legacy.storage_bytes())
        self.validate(guard, legacy)
        try:
            result = parse_model(
                GuardedCreated,
                self._request(
                    CREATE_PATH,
                    canonical_bytes(guard.request),
                    legacy.idempotency_key.get_secret_value(),
                ),
            )
            verify_proof(result.proof, guard, legacy)
            return GuardedObservation(
                result="found", proof=result.proof, reason="historical_creation"
            )
        except Exception:
            return GuardedObservation(result="error", reason="invalid_or_unavailable")

    def lookup(self, guard: GuardedEnvelope, legacy: CheckoutCreationEnvelope):
        guard = parse_model(GuardedEnvelope, canonical_bytes(guard))
        legacy = parse_model(CheckoutCreationEnvelope, legacy.storage_bytes())
        self.validate(guard, legacy)
        try:
            key_hash = hashlib.sha256(
                legacy.idempotency_key.get_secret_value().encode()
            ).hexdigest()
            result = parse_model(
                GuardedHistory,
                self._request(
                    LOOKUP_PATH,
                    canonical_bytes(
                        {
                            "schema_version": LOOKUP_SCHEMA,
                            "idempotency_key_sha256": key_hash,
                        }
                    ),
                ),
            )
            scope = self.session.binding.scope
            if (result.run_id, result.actor_id, result.idempotency_key_sha256) != (
                scope.remote_run_id,
                scope.remote_actor_id,
                key_hash,
            ):
                raise ValueError("D17 history scope conflict")
            if result.result == "found":
                assert result.proof is not None
                verify_proof(result.proof, guard, legacy)
                if result.proof.digest() != result.proof_sha256:
                    raise ValueError("D17 proof digest conflict")
                return GuardedObservation(
                    result="found", proof=result.proof, reason="historical_creation"
                )
            if result.result == "rejected":
                rejection = result.rejection
                assert rejection is not None
                if (
                    rejection.digest() != result.rejection_sha256
                    or (rejection.run_id, rejection.actor_id, rejection.idempotency_sha256)
                    != (result.run_id, result.actor_id, key_hash)
                    or rejection.guard_fingerprint != guard.guard_fingerprint
                ):
                    raise ValueError("D17 rejection conflict")
                return GuardedObservation(
                    result="rejected", rejection=rejection, reason="historical_rejection"
                )
            return GuardedObservation(result=result.result, reason="uncertain")
        except Exception:
            return GuardedObservation(result="error", reason="invalid_or_unavailable")
