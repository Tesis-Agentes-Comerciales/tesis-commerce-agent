import re
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import httpx
import jwt
from ap2.sdk.generated.checkout_receipt import CheckoutReceiptError
from pydantic import SecretStr, ValidationError

from commerce_agent.acp.creation import build_checkout_create
from commerce_agent.acp.models import (
    ACP_VERSION,
    CheckoutCompleteRequest,
    CheckoutSession,
    CheckoutSessionWithOrder,
    CheckoutUpdateRequest,
    DiscoveryResponse,
    Identifier,
    SandboxPaymentData,
)
from commerce_agent.acp.offer_snapshot import DeliveryContext, MerchantOffer, OfferResponse
from commerce_agent.acp.schema import ACPContractError, validate_acp
from commerce_agent.ap2_receipt import AP2_ERRORS, chain_reference
from commerce_agent.config import validate_service_base_url


class ACPClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str = "acp_client_error",
        retry_after: str | None = None,
        request_id: str | None = None,
        receipt: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after
        self.request_id = request_id
        # The AP2 error receipt of a Human Not Present rejection, still unverified.
        self.receipt = receipt


class ACPClient:
    """Deterministic ACP transport. Secrets and idempotency never come from model tools."""

    def __init__(
        self,
        base_url: str,
        bearer_token: SecretStr | None,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = validate_service_base_url(base_url)
        self._bearer_token = bearer_token
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    def __enter__(self) -> "ACPClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def discover(self) -> DiscoveryResponse:
        payload = self._request("GET", "/.well-known/acp.json", authenticated=False)
        try:
            validate_acp("DiscoveryResponse", payload)
            discovery = DiscoveryResponse.model_validate(payload)
        except (ACPContractError, ValidationError) as error:
            raise ACPClientError("Merchant returned invalid ACP discovery") from error
        advertised = validate_service_base_url(discovery.api_base_url)
        if advertised != self._base_url:
            raise ACPClientError("Merchant discovery advertises a different API base URL")
        return discovery

    def create_checkout(
        self,
        variant_id: str,
        *,
        currency: str = "usd",
        idempotency_key: str,
        request_id: str | None = None,
    ) -> CheckoutSession:
        request = build_checkout_create(variant_id, currency)
        return self._checkout_mutation(
            "POST",
            "/checkout_sessions",
            request.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    def get_checkout(self, checkout_id: str, *, request_id: str | None = None) -> CheckoutSession:
        checked_id = self._identifier(checkout_id)
        payload = self._request(
            "GET",
            f"/checkout_sessions/{checked_id}",
            request_id=request_id,
        )
        return self._parse_checkout(payload)

    def get_offer(self, offer_id: str, delivery_context: DeliveryContext) -> MerchantOffer:
        """Lab extension, not ACP. One attempt: a failed read must surface, never be retried.

        The answer must be the offer asked for, at the destination asked for: shipping, total
        and delivery days depend on it. A well-formed offer of another id or destination is
        `offer_mismatch`, not `invalid_offer`, so the trace tells it from a malformed answer.
        """
        requested = self._identifier(offer_id)
        payload = self._request(
            "POST",
            "/lab/offers/get",
            json={
                "offer_id": requested,
                "delivery_context": delivery_context.model_dump(mode="json"),
            },
        )
        failure = payload.get("error") if payload.get("ok") is False else None
        if isinstance(failure, Mapping) and failure.get("code") == "OFFER_EXPIRED":
            # A typed answer, not a malformed one: the merchant no longer sells at these terms.
            raise ACPClientError("Merchant offer has expired", code="offer_expired")
        try:
            offer = OfferResponse.model_validate(payload).data
        except ValidationError as error:
            raise ACPClientError(
                "Merchant returned an invalid offer", code="invalid_offer"
            ) from error
        if offer.id != requested or offer.delivery_context != delivery_context:
            raise ACPClientError(
                "Merchant returned a different offer than requested", code="offer_mismatch"
            )
        return offer

    def signed_checkout(self, checkout: CheckoutSession) -> str:
        checked_id = self._identifier(checkout.id)
        payload = self._request("GET", f"/checkout_sessions/{checked_id}/ap2/checkout-jwt")
        token = payload.get("checkout_jwt")
        if not isinstance(token, str) or len(token) > 32_768:
            raise ACPClientError("Merchant returned an invalid AP2 checkout JWT")
        self.verify_signed_checkout(token, checkout)
        return token

    def verify_signed_checkout(self, token: str, checkout: CheckoutSession) -> None:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "ES256" or not isinstance(header.get("kid"), str):
                raise ValueError("Unexpected AP2 checkout signing key")
            jwks = self._request("GET", "/.well-known/ap2/jwks.json", authenticated=False)
            keys = jwks.get("keys")
            if not isinstance(keys, list):
                raise ValueError("Merchant JWKS is invalid")
            matches = [
                key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]
            ]
            if len(matches) != 1:
                raise ValueError("Merchant AP2 key is not unique")
            claims = jwt.decode(
                token,
                jwt.PyJWK.from_dict(matches[0]).key,
                algorithms=["ES256"],
                issuer=self._base_url,
                audience="ap2-shopping-agent",
                options={"require": ["iss", "aud", "iat", "exp", "jti", "checkout"]},
            )
            signed = CheckoutSession.model_validate(claims["checkout"])
        except (jwt.PyJWTError, ValueError, TypeError, ValidationError) as error:
            raise ACPClientError("Merchant AP2 checkout signature is invalid") from error
        if signed != checkout:
            raise ACPClientError("Merchant AP2 checkout no longer matches the current checkout")

    def update_checkout(
        self,
        checkout_id: str,
        update: CheckoutUpdateRequest,
        *,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> CheckoutSession:
        checked_id = self._identifier(checkout_id)
        return self._checkout_mutation(
            "POST",
            f"/checkout_sessions/{checked_id}",
            update.model_dump(mode="json", exclude_none=True),
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    def cancel_checkout(
        self,
        checkout_id: str,
        *,
        idempotency_key: str,
        reason_code: str | None = None,
        request_id: str | None = None,
    ) -> CheckoutSession:
        checked_id = self._identifier(checkout_id)
        body: dict[str, object] = {}
        if reason_code is not None:
            body["intent_trace"] = {"reason_code": reason_code}
        return self._checkout_mutation(
            "POST",
            f"/checkout_sessions/{checked_id}/cancel",
            body,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    def complete_checkout(
        self,
        checkout_id: str,
        payment_data: SandboxPaymentData,
        *,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> CheckoutSessionWithOrder:
        checked_id = self._identifier(checkout_id)
        request = CheckoutCompleteRequest(payment_data=payment_data)
        body = request.acp_payload()
        try:
            validate_acp("CheckoutSessionCompleteRequest", body)
        except ACPContractError as error:
            raise ACPClientError("Payment data violates the ACP contract") from error
        if not 1 <= len(idempotency_key) <= 255:
            raise ValueError("Idempotency key must contain between 1 and 255 characters")
        payload = self._request(
            "POST",
            f"/checkout_sessions/{checked_id}/complete",
            json=body,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
        try:
            validate_acp("CheckoutSessionWithOrder", payload)
            return CheckoutSessionWithOrder.model_validate(payload)
        except (ACPContractError, ValidationError) as error:
            raise ACPClientError("Merchant returned an invalid completed checkout") from error

    def complete_checkout_ap2(
        self,
        checkout_id: str,
        payment_data: SandboxPaymentData,
        checkout_mandate: str,
        *,
        idempotency_key: str,
    ) -> tuple[CheckoutSessionWithOrder, str]:
        """`checkout_mandate` is the closed checkout mandate (Human Present) or the
        `open~~closed` checkout chain (Human Not Present), which the merchant tells by `~~`."""
        checked_id = self._identifier(checkout_id)
        request = CheckoutCompleteRequest(payment_data=payment_data).acp_payload()
        validate_acp("CheckoutSessionCompleteRequest", request)
        payload = self._request(
            "POST",
            f"/checkout_sessions/{checked_id}/ap2/complete",
            json={
                "payment_data": request["payment_data"],
                "checkout_mandate": checkout_mandate,
            },
            idempotency_key=idempotency_key,
        )
        try:
            checkout_data = payload["checkout"]
            validate_acp("CheckoutSessionWithOrder", checkout_data)
            completed = CheckoutSessionWithOrder.model_validate(checkout_data)
            receipt = payload["checkout_receipt_jwt"]
            if not isinstance(receipt, str) or len(receipt) > 16_384:
                raise ValueError("Invalid AP2 checkout receipt")
        except (KeyError, TypeError, ValueError, ACPContractError, ValidationError) as error:
            raise ACPClientError("Merchant returned an invalid AP2 completion") from error
        self.verify_checkout_receipt(receipt, checkout_mandate, completed.order.id)
        return completed, receipt

    def verify_checkout_receipt(self, receipt: str, mandate: str, order_id: str) -> None:
        try:
            claims = self._receipt_claims(receipt, required=("order_id",))
            expected = chain_reference(mandate)
            if (
                claims["status"] != "Success"
                or claims["reference"] != expected
                or claims["order_id"] != order_id
            ):
                raise ValueError("AP2 checkout receipt does not match the purchase")
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as error:
            raise ACPClientError("Merchant AP2 checkout receipt is invalid") from error

    def verify_checkout_error_receipt(self, receipt: str, *, mandate: str) -> dict[str, Any]:
        """The claims of a Human Not Present error receipt, only if it validates as
        `CheckoutReceiptError`, its signature verifies with the merchant's JWKS and its
        `reference` is the closed checkout mandate of `mandate` (docs/ap2-profile.md, Recibos)."""
        try:
            claims = self._receipt_claims(receipt, required=("error",))
            CheckoutReceiptError.model_validate(claims)
            if claims["error"] not in AP2_ERRORS or claims["reference"] != chain_reference(mandate):
                raise ValueError("AP2 checkout error receipt does not match the chain")
            return claims
        except ACPClientError:
            raise
        except (jwt.PyJWTError, KeyError, TypeError, ValueError, ValidationError) as error:
            raise ACPClientError("Merchant AP2 checkout error receipt is invalid") from error

    def _receipt_claims(self, receipt: str, *, required: tuple[str, ...]) -> dict[str, Any]:
        """A merchant receipt's claims, its signature verified with the key its `kid` names."""
        header = jwt.get_unverified_header(receipt)
        if header.get("alg") != "ES256" or not isinstance(header.get("kid"), str):
            raise ValueError("Invalid AP2 receipt signing key")
        jwks = self._request("GET", "/.well-known/ap2/jwks.json", authenticated=False)
        keys = jwks.get("keys")
        if not isinstance(keys, list):
            raise ValueError("Invalid merchant JWKS")
        matches = [key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]]
        if len(matches) != 1:
            raise ValueError("Unknown merchant receipt key")
        return jwt.decode(
            receipt,
            jwt.PyJWK.from_dict(matches[0]).key,
            algorithms=["ES256"],
            issuer=self._base_url,
            options={
                "verify_aud": False,
                "require": ["iss", "iat", "status", "reference", *required],
            },
        )

    def _checkout_mutation(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
        *,
        idempotency_key: str,
        request_id: str | None,
    ) -> CheckoutSession:
        if not 1 <= len(idempotency_key) <= 255:
            raise ValueError("Idempotency key must contain between 1 and 255 characters")
        payload = self._request(
            method,
            path,
            json=body,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
        return self._parse_checkout(payload)

    def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        json: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        headers = {"API-Version": ACP_VERSION, "Request-Id": request_id or str(uuid4())}
        if authenticated:
            if self._bearer_token is None or not self._bearer_token.get_secret_value():
                raise ACPClientError("Merchant Bearer token is not configured", code="missing_auth")
            headers["Authorization"] = f"Bearer {self._bearer_token.get_secret_value()}"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._http.request(method, path, headers=headers, json=json)
        except httpx.TimeoutException as error:
            raise ACPClientError("Merchant request timed out", code="timeout") from error
        except httpx.TransportError as error:
            raise ACPClientError("Merchant transport failed", code="transport_error") from error
        if len(response.content) > self._MAX_RESPONSE_BYTES:
            raise ACPClientError(
                "Merchant response exceeded the configured size limit",
                status_code=response.status_code,
                code="response_too_large",
            )
        if response.is_error:
            raise self._response_error(response)
        try:
            payload = response.json()
        except ValueError as error:
            raise ACPClientError(
                "Merchant returned non-JSON content", status_code=response.status_code
            ) from error
        if not isinstance(payload, dict):
            raise ACPClientError(
                "Merchant returned a non-object response", status_code=response.status_code
            )
        return payload

    @staticmethod
    def _parse_checkout(payload: dict[str, Any]) -> CheckoutSession:
        try:
            validate_acp("CheckoutSession", payload)
            return CheckoutSession.model_validate(payload)
        except (ACPContractError, ValidationError) as error:
            raise ACPClientError("Merchant returned an invalid checkout session") from error

    @staticmethod
    def _identifier(value: str) -> str:
        from pydantic import TypeAdapter

        return TypeAdapter(Identifier).validate_python(value)

    @staticmethod
    def _response_error(response: httpx.Response) -> ACPClientError:
        code = "http_error"
        message = f"Merchant request failed with HTTP {response.status_code}"
        receipt = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, Mapping):
            detail = payload.get("detail", payload)
            if isinstance(detail, Mapping):
                raw_code = detail.get("code")
                if isinstance(raw_code, str) and ACPClient._ERROR_CODE.fullmatch(raw_code):
                    code = raw_code
            # Human Not Present: `{"code": <AP2 code>, "checkout_receipt_jwt": <receipt>}`.
            raw_receipt = payload.get("checkout_receipt_jwt")
            if isinstance(raw_receipt, str) and len(raw_receipt) <= 16_384:
                receipt = raw_receipt
        return ACPClientError(
            message,
            status_code=response.status_code,
            code=code,
            retry_after=response.headers.get("Retry-After"),
            request_id=ACPClient._merchant_request_id(response),
            receipt=receipt,
        )

    @staticmethod
    def _merchant_request_id(response: httpx.Response) -> str | None:
        request_id = response.headers.get("Request-Id")
        if request_id is not None and ACPClient._REQUEST_ID.fullmatch(request_id):
            return request_id
        return None

    _MAX_RESPONSE_BYTES = 2 * 1024 * 1024
    _ERROR_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
    _REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
