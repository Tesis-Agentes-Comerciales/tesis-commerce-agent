from datetime import datetime
from typing import Any

import httpx
import jwt
from ap2.sdk.sdjwt import common
from pydantic import SecretStr

from commerce_agent.acp.models import (
    ACP_VERSION,
    CheckoutSession,
    PaymentCredential,
    SandboxPaymentData,
    SandboxPaymentInstrument,
)
from commerce_agent.config import validate_service_base_url


class DelegationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DelegatedPaymentClient:
    """Obtain a scoped test token; the provider alone decides its payment outcome."""

    def __init__(
        self,
        base_url: str,
        bearer_token: SecretStr,
        *,
        merchant_id: str = "tesis_merchant",
        test_card: str = "4242424242424242",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=validate_service_base_url(base_url),
            timeout=5,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )
        self._bearer_token = bearer_token
        self._merchant_id = merchant_id
        self._test_card = test_card

    def close(self) -> None:
        self._http.close()

    def payment_data(
        self,
        *,
        checkout: CheckoutSession,
        idempotency_key: str,
        checkout_jwt: str | None = None,
        payment_mandate: str | None = None,
    ) -> SandboxPaymentData:
        try:
            expiry = datetime.fromisoformat(checkout.expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                raise ValueError("Checkout expiry must include timezone")
        except ValueError as error:
            raise DelegationError("invalid_checkout_expiry") from error
        body: dict[str, Any] = {
            "payment_method": {
                "type": "card",
                "card_number_type": "fpan",
                "number": self._test_card,
                "exp_month": "12",
                "exp_year": "2035",
                "display_card_funding_type": "credit",
                "metadata": {},
            },
            "allowance": {
                "reason": "one_time",
                "max_amount": checkout.total_minor,
                "currency": checkout.currency,
                "checkout_session_id": checkout.id,
                "merchant_id": self._merchant_id,
                "expires_at": expiry.isoformat(),
            },
            "risk_signals": [{"type": "card_testing", "score": 0, "action": "authorized"}],
            "metadata": {},
        }
        if checkout_jwt is not None or payment_mandate is not None:
            if checkout_jwt is None or payment_mandate is None:
                raise DelegationError("invalid_ap2_mandates")
            body["ap2_checkout_jwt"] = checkout_jwt
            body["ap2_payment_mandate"] = payment_mandate
        try:
            response = self._http.post(
                "/agentic_commerce/delegate_payment",
                headers={
                    "Authorization": f"Bearer {self._bearer_token.get_secret_value()}",
                    "API-Version": ACP_VERSION,
                    "Idempotency-Key": idempotency_key,
                },
                json=body,
            )
        except httpx.HTTPError as error:
            raise DelegationError("provider_unavailable") from error
        if response.status_code != 201:
            raise DelegationError(
                "delegation_rejected" if response.status_code < 500 else "provider_unavailable"
            )
        try:
            token = response.json()["id"]
        except (ValueError, KeyError, TypeError) as error:
            raise DelegationError("invalid_provider_response") from error
        if not isinstance(token, str) or not token.startswith("vt_") or len(token) != 67:
            raise DelegationError("invalid_provider_response")
        return SandboxPaymentData(
            instrument=SandboxPaymentInstrument(
                credential=PaymentCredential(token=SecretStr(token))
            )
        )

    def payment_receipt(self, *, token: str, mandate: str) -> str:
        try:
            response = self._http.post(
                "/agentic_commerce/payment_receipt",
                headers={"Authorization": f"Bearer {self._bearer_token.get_secret_value()}"},
                json={"token": token},
            )
            keys_response = self._http.get("/.well-known/ap2/jwks.json")
            if response.status_code != 200 or keys_response.status_code != 200:
                raise ValueError("Provider receipt is unavailable")
            result = response.json()
            receipt = result["result"]["payment_receipt_jwt"]
            keys = keys_response.json()["keys"]
            if not isinstance(receipt, str) or not isinstance(keys, list) or len(keys) != 1:
                raise ValueError("Provider receipt is invalid")
            claims = jwt.decode(
                receipt,
                jwt.PyJWK.from_dict(keys[0]).key,
                algorithms=["ES256"],
                issuer="tesis_payment_provider",
                options={
                    "verify_aud": False,
                    "require": ["iss", "iat", "status", "reference", "payment_id"],
                },
            )
            expected = common.compute_sd_hash(common.parse_token(mandate))
            if (
                result["status"] != "approved"
                or claims["status"] != "Success"
                or claims["reference"] != expected
                or claims["payment_id"] != result["result"]["id"]
            ):
                raise ValueError("Provider receipt does not match payment")
            return receipt
        except (httpx.HTTPError, jwt.PyJWTError, KeyError, TypeError, ValueError) as error:
            raise DelegationError("invalid_payment_receipt") from error
