import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from commerce_agent.acp.models import CheckoutSession
from commerce_agent.persistence import ApprovalRecord, SqliteAgentStore


class ApprovalError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ApprovalLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    item_id: str
    name: str
    quantity: int = Field(ge=1)
    unit_amount_minor: int = Field(ge=0)


class ApprovalTerms(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    merchant_origin: str
    checkout_id: str
    currency: str
    total_minor: int = Field(ge=0)
    lines: tuple[ApprovalLine, ...]
    selected_fulfillment_options: tuple[dict[str, Any], ...]
    buyer: dict[str, Any] | None
    fulfillment_details: dict[str, Any] | None
    payment_handler: str
    action: str
    expires_at: str
    fingerprint_version: str
    fingerprint: str


class ApprovalService:
    FINGERPRINT_VERSION = "v1"

    def __init__(
        self,
        store: SqliteAgentStore,
        secret: SecretStr,
        *,
        merchant_origin: str = "configured-merchant",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if len(secret.get_secret_value()) < 32:
            raise ValueError("Approval secret must contain at least 32 characters")
        self._store = store
        self._secret = secret
        self._merchant_origin = merchant_origin
        self._clock = clock or (lambda: datetime.now(UTC))

    def present(self, checkout: CheckoutSession) -> ApprovalTerms:
        self._require_approvable(checkout)
        lines = tuple(_approval_line(line) for line in checkout.line_items)
        selected = _checkout_list(checkout, "selected_fulfillment_options")
        checkout_data = checkout.model_dump(mode="json")
        return ApprovalTerms(
            merchant_origin=self._merchant_origin,
            checkout_id=checkout.id,
            currency=checkout.currency,
            total_minor=checkout.total_minor,
            lines=lines,
            selected_fulfillment_options=selected,
            buyer=_optional_mapping(checkout_data.get("buyer")),
            fulfillment_details=_optional_mapping(checkout_data.get("fulfillment_details")),
            payment_handler="tesis_sandbox",
            action="complete_purchase",
            expires_at=checkout.expires_at,
            fingerprint_version=self.FINGERPRINT_VERSION,
            fingerprint=self._fingerprint(checkout),
        )

    def approve(
        self,
        *,
        run_id: str,
        actor_id: str,
        expected_fingerprint: str,
        checkout: CheckoutSession,
    ) -> ApprovalRecord:
        terms = self.present(checkout)
        if not hmac.compare_digest(expected_fingerprint, terms.fingerprint):
            raise ApprovalError(
                "Displayed checkout terms no longer match",
                code="checkout_terms_changed",
            )
        approved_at = _timestamp(self._clock())
        record = ApprovalRecord(
            run_id=run_id,
            checkout_id=checkout.id,
            checkout_fingerprint=terms.fingerprint,
            actor_id=actor_id,
            approved_at=approved_at,
            signature=self._signature(
                run_id, checkout.id, terms.fingerprint, actor_id, approved_at
            ),
            consumed_at=None,
        )
        self._store.save_approval(record)
        return record

    def authorize(self, *, run_id: str, checkout: CheckoutSession) -> ApprovalRecord:
        self._require_approvable(checkout)
        record = self._store.approval(run_id)
        if record is None:
            raise ApprovalError("Human approval is required", code="approval_required")
        if record.consumed_at is not None:
            raise ApprovalError("Approval was already consumed", code="approval_consumed")
        if record.checkout_id != checkout.id:
            raise ApprovalError("Approval belongs to another checkout", code="approval_mismatch")
        fingerprint = self._fingerprint(checkout)
        if not hmac.compare_digest(record.checkout_fingerprint, fingerprint):
            raise ApprovalError(
                "Checkout terms changed after approval", code="checkout_terms_changed"
            )
        expected_signature = self._signature(
            record.run_id,
            record.checkout_id,
            record.checkout_fingerprint,
            record.actor_id,
            record.approved_at,
        )
        if not hmac.compare_digest(record.signature, expected_signature):
            raise ApprovalError("Approval signature is invalid", code="invalid_approval")
        return record

    def consume(self, run_id: str) -> None:
        self._store.consume_approval(run_id)

    def _require_approvable(self, checkout: CheckoutSession) -> None:
        if checkout.status != "ready_for_payment":
            raise ApprovalError("Checkout is not ready for payment", code="checkout_not_ready")
        if _parse_timestamp(checkout.expires_at) <= self._clock():
            raise ApprovalError("Checkout has expired", code="checkout_expired")

    def _signature(
        self,
        run_id: str,
        checkout_id: str,
        fingerprint: str,
        actor_id: str,
        approved_at: str,
    ) -> str:
        content = "\x1f".join((run_id, checkout_id, fingerprint, actor_id, approved_at)).encode()
        return hmac.new(
            self._secret.get_secret_value().encode(), content, hashlib.sha256
        ).hexdigest()

    def _fingerprint(self, checkout: CheckoutSession) -> str:
        canonical = _canonical_approval_terms(checkout)
        return hmac.new(
            self._secret.get_secret_value().encode(),
            b"checkout-approval\x1f" + self._merchant_origin.encode() + b"\x1f" + canonical,
            hashlib.sha256,
        ).hexdigest()


def _canonical_approval_terms(checkout: CheckoutSession) -> bytes:
    checkout_data = checkout.model_dump(mode="json")
    commercial_terms = {
        "fingerprint_version": ApprovalService.FINGERPRINT_VERSION,
        "id": checkout.id,
        "protocol": checkout.protocol.model_dump(mode="json"),
        "status": checkout.status,
        "currency": checkout.currency,
        "line_items": checkout.line_items,
        "totals": checkout.totals,
        "selected_fulfillment_options": _checkout_list(checkout, "selected_fulfillment_options"),
        "buyer": checkout_data.get("buyer"),
        "fulfillment_details": checkout_data.get("fulfillment_details"),
        "updated_at": checkout.updated_at,
        "expires_at": checkout.expires_at,
    }
    return json.dumps(
        commercial_terms, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _approval_line(raw: dict[str, Any]) -> ApprovalLine:
    item = raw.get("item")
    if not isinstance(item, dict):
        raise ApprovalError("Checkout line item is invalid", code="invalid_checkout")
    item_id = item.get("id")
    name = item.get("name")
    quantity = raw.get("quantity")
    unit_amount = item.get("unit_amount")
    if (
        not isinstance(item_id, str)
        or not isinstance(name, str)
        or not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or not isinstance(unit_amount, int)
        or isinstance(unit_amount, bool)
    ):
        raise ApprovalError("Checkout line item is invalid", code="invalid_checkout")
    return ApprovalLine(
        item_id=item_id,
        name=name,
        quantity=quantity,
        unit_amount_minor=unit_amount,
    )


def _checkout_list(checkout: CheckoutSession, field: str) -> tuple[dict[str, Any], ...]:
    raw = getattr(checkout, field, [])
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ApprovalError("Checkout terms are invalid", code="invalid_checkout")
    return tuple(dict(item) for item in raw)


def _optional_mapping(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ApprovalError("Checkout terms are invalid", code="invalid_checkout")
    return dict(value)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApprovalError("Checkout expiration is invalid", code="invalid_checkout") from error
    if parsed.tzinfo is None:
        raise ApprovalError("Checkout expiration requires a timezone", code="invalid_checkout")
    return parsed.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
