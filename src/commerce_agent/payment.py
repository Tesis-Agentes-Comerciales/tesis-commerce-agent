import hashlib
import hmac
from typing import Literal

from pydantic import SecretStr

from commerce_agent.acp.models import (
    PaymentCredential,
    SandboxPaymentData,
    SandboxPaymentInstrument,
)


class SandboxPaymentProvider:
    def __init__(
        self,
        secret: SecretStr,
        *,
        outcome: Literal["success", "declined", "error"] = "success",
    ) -> None:
        if len(secret.get_secret_value()) < 32:
            raise ValueError("Payment secret must contain at least 32 characters")
        self._secret = secret
        self._outcome = outcome

    def payment_data(
        self,
        *,
        run_id: str,
        checkout_id: str,
        idempotency_key: str,
    ) -> SandboxPaymentData:
        context = "\x1f".join((run_id, checkout_id, idempotency_key)).encode()
        suffix = hmac.new(
            self._secret.get_secret_value().encode(), context, hashlib.sha256
        ).hexdigest()
        return SandboxPaymentData(
            instrument=SandboxPaymentInstrument(
                credential=PaymentCredential(token=SecretStr(f"spt_test_{self._outcome}_{suffix}"))
            )
        )
