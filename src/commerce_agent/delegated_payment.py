from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx
import jwt
from ap2.sdk.generated.payment_receipt import PaymentReceiptError
from ap2.sdk.sdjwt import common
from pydantic import SecretStr, ValidationError

from commerce_agent.acp.models import (
    ACP_VERSION,
    CheckoutSession,
    PaymentCredential,
    SandboxPaymentData,
    SandboxPaymentInstrument,
)
from commerce_agent.config import validate_service_base_url

# The AP2 error codes a Human Not Present error receipt may carry (docs/ap2-profile.md, Errores).
AP2_ERRORS = frozenset(
    {"invalid_credential", "invalid_mandate", "unresolved_constraint", "mandates_not_supported"}
)
RECEIPT_ISSUER = "tesis_payment_provider"


class DelegationError(RuntimeError):
    """`status_code` is the provider's HTTP status (None without an answer); `receipt` is the AP2
    error receipt a Human Not Present rejection carries, still unverified."""

    def __init__(
        self, code: str, *, status_code: int | None = None, receipt: str | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.receipt = receipt


def chain_reference(mandate: str) -> str:
    """The `reference` of a receipt: `sd_hash` of the mandate, or of the last segment of an
    `open~~closed` chain (docs/ap2-profile.md, Recibos)."""
    return common.compute_sd_hash(common.parse_token(mandate.rsplit("~~", 1)[-1]))


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
        ap2 = {"ap2_checkout_jwt": checkout_jwt, "ap2_payment_mandate": payment_mandate}
        return self._delegate(checkout, idempotency_key, ap2)

    def hnp_payment_data(
        self,
        *,
        checkout: CheckoutSession,
        idempotency_key: str,
        checkout_jwt: str,
        payment_mandate: str,
        checkout_mandate: str,
    ) -> SandboxPaymentData:
        """Human Not Present: the token for both `open~~closed` chains. The checkout chain travels
        as `ap2_checkout_mandate`, which the PSP needs to check `payment.reference`
        (docs/ap2-profile.md, Envío); none of the three goes empty."""
        ap2 = {
            "ap2_checkout_jwt": checkout_jwt,
            "ap2_payment_mandate": payment_mandate,
            "ap2_checkout_mandate": checkout_mandate,
        }
        if not all(ap2.values()):
            raise DelegationError("invalid_ap2_mandates")
        return self._delegate(checkout, idempotency_key, ap2)

    def _delegate(
        self, checkout: CheckoutSession, idempotency_key: str, ap2: Mapping[str, str | None]
    ) -> SandboxPaymentData:
        """`delegate_payment` with the AP2 fields of `ap2`: all of them or none."""
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
        if any(value is not None for value in ap2.values()):
            if any(value is None for value in ap2.values()):
                raise DelegationError("invalid_ap2_mandates")
            body.update(ap2)
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
                "delegation_rejected" if response.status_code < 500 else "provider_unavailable",
                status_code=response.status_code,
                receipt=_error_receipt(response),
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
        """The verified success receipt of a confirmed payment; `mandate` is the closed payment
        mandate, or its `open~~closed` chain in Human Not Present. Any failure is
        `invalid_payment_receipt`, as Human Present expects; its `status_code` is the provider's
        answer to the receipt request, None without one."""
        status = None
        try:
            response = self._http.post(
                "/agentic_commerce/payment_receipt",
                headers={"Authorization": f"Bearer {self._bearer_token.get_secret_value()}"},
                json={"token": token},
            )
            status = response.status_code
            if status != 200:
                raise ValueError("Provider receipt is unavailable")
            result = response.json()
            receipt = result["result"]["payment_receipt_jwt"]
            if not isinstance(receipt, str):
                raise ValueError("Provider receipt is invalid")
            claims = self._receipt_claims(receipt)
            expected = chain_reference(mandate)
            if (
                result["status"] != "approved"
                or claims["status"] != "Success"
                or claims["reference"] != expected
                or claims["payment_id"] != result["result"]["id"]
            ):
                raise ValueError("Provider receipt does not match payment")
            return receipt
        except httpx.HTTPError as error:
            raise DelegationError("invalid_payment_receipt") from error
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as error:
            raise DelegationError("invalid_payment_receipt", status_code=status) from error

    def verify_error_receipt(self, receipt: str, *, mandate: str) -> dict[str, Any]:
        """The claims of a Human Not Present error receipt, only if it validates as
        `PaymentReceiptError`, its signature verifies with the provider's JWKS and its
        `reference` is the closed payment mandate of `mandate` (docs/ap2-profile.md, Recibos)."""
        try:
            claims = self._receipt_claims(receipt)
            PaymentReceiptError.model_validate(claims)
            if claims["error"] not in AP2_ERRORS or claims["reference"] != chain_reference(mandate):
                raise ValueError("Provider error receipt does not match the payment chain")
            return claims
        except httpx.HTTPError as error:
            raise DelegationError("provider_unavailable") from error
        except (jwt.PyJWTError, KeyError, TypeError, ValueError, ValidationError) as error:
            raise DelegationError("invalid_error_receipt") from error

    def _receipt_claims(self, receipt: str) -> dict[str, Any]:
        keys_response = self._http.get("/.well-known/ap2/jwks.json")
        if keys_response.status_code != 200:
            raise ValueError("Provider receipt key is unavailable")
        keys = keys_response.json()["keys"]
        if not isinstance(keys, list) or len(keys) != 1:
            raise ValueError("Provider receipt key is invalid")
        return jwt.decode(
            receipt,
            jwt.PyJWK.from_dict(keys[0]).key,
            algorithms=["ES256"],
            issuer=RECEIPT_ISSUER,
            options={
                "verify_aud": False,
                "require": ["iss", "iat", "status", "reference", "payment_id"],
            },
        )


def _error_receipt(response: httpx.Response) -> str | None:
    """The error receipt of a Human Not Present rejection: `{"code", "payment_receipt_jwt"}`."""
    try:
        payload = response.json()
    except ValueError:
        return None
    receipt = payload.get("payment_receipt_jwt") if isinstance(payload, dict) else None
    return receipt if isinstance(receipt, str) and len(receipt) <= 16_384 else None
