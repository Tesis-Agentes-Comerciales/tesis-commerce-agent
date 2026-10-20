"""D2 test double: the synthetic human issuer behind the HumanAuthenticator port."""

import hashlib
import hmac
import secrets

from pydantic import SecretBytes, SecretStr

from commerce_agent.governance.canonical import canonical_bytes, parse_model
from commerce_agent.governance.human_consent import ConsentBlocked, HumanIdentity


class SyntheticHumanIssuer:
    """Explicit test issuer. Cannot emit real UI assurance; key stays backend-only."""

    def __init__(self, key: SecretBytes):
        if len(key.get_secret_value()) < 32:
            raise ValueError("Persistent authentication key required")
        self._key = key

    def issue(
        self,
        *,
        principal_id: str,
        owner_id: str,
        run_id: str,
        authenticated_at: str,
        expires_at: str,
    ) -> SecretStr:
        identity = HumanIdentity(
            principal_id=principal_id,
            owner_id=owner_id,
            run_id=run_id,
            issuer="d2-synthetic-test-issuer",
            authentication_id=secrets.token_hex(24),
            authenticated_at=authenticated_at,
            expires_at=expires_at,
            assurance="synthetic_test_human/1",
        )
        body = canonical_bytes(identity)
        return SecretStr(body.hex() + "." + self._mac(body))

    def _mac(self, body: bytes) -> str:
        return hmac.new(
            self._key.get_secret_value(), b"d2-test-auth/1\0" + body, hashlib.sha256
        ).hexdigest()

    def authenticate(self, credential: SecretStr) -> HumanIdentity:
        try:
            value = credential.get_secret_value()
            if len(value) > 8192:
                raise ValueError
            encoded, signature = value.split(".")
            body = bytes.fromhex(encoded)
            if not hmac.compare_digest(self._mac(body), signature):
                raise ValueError
            result = parse_model(HumanIdentity, body)
            if (
                result.assurance != "synthetic_test_human/1"
                or result.issuer != "d2-synthetic-test-issuer"
            ):
                raise ValueError
            return result
        except Exception:
            raise ConsentBlocked("HUMAN_AUTHENTICATION_REQUIRED") from None
