import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime

from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.merchant import Merchant
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument
from ap2.sdk.sdjwt import sd_jwt
from jwcrypto.jwk import JWK

from commerce_agent.acp.models import CheckoutSession


def checkout_hash(checkout_jwt: str) -> str:
    from jwt.utils import base64url_encode

    return base64url_encode(hashlib.sha256(checkout_jwt.encode("ascii")).digest()).decode()


class AgentMandateSigner:
    def __init__(
        self,
        private_key_pem: str,
        *,
        merchant_id: str,
        merchant_name: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        key = JWK.from_pem(private_key_pem.encode())
        if key.get("kty") != "EC" or key.get("crv") != "P-256" or not key.get("d"):
            raise ValueError("AP2 mandate signing requires an ES256 private key")
        self._key = key
        self._merchant_id = merchant_id
        self._merchant_name = merchant_name
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def public_jwk(self) -> dict[str, object]:
        return json.loads(self._key.export_public())

    def sign(self, checkout_jwt: str, checkout: CheckoutSession) -> tuple[str, str]:
        now = int(self._clock().timestamp())
        expires = now + 300
        if checkout.status != "ready_for_payment":
            raise ValueError("Checkout is not approvable")
        transaction_id = checkout_hash(checkout_jwt)
        checkout_mandate = CheckoutMandate(
            checkout_jwt=checkout_jwt,
            checkout_hash=transaction_id,
            iat=now,
            exp=expires,
        )
        payment_mandate = PaymentMandate(
            transaction_id=transaction_id,
            payee=Merchant(id=self._merchant_id, name=self._merchant_name),
            payment_amount=Amount(amount=checkout.total_minor, currency=checkout.currency.upper()),
            payment_instrument=PaymentInstrument(id="tesis_sandbox", type="sandbox_token"),
            iat=now,
            exp=expires,
        )
        return (
            sd_jwt.create(checkout_mandate, self._key).sd_jwt_issuance,
            sd_jwt.create(payment_mandate, self._key).sd_jwt_issuance,
        )
