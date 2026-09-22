from datetime import datetime
from typing import Any

import httpx
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
