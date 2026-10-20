"""Live-offer freshness before Human Present terms are shown or signed (b2-governed-pilot).

The merchant checkout keeps a snapshot of its offer, so its GET and the approval fingerprint do
not move when the offer changes. The guard re-reads the live offer and applies the predicate the
merchant uses at completion, so stale terms stop before AP2 mandates exist. Revision is
evidence only: a new revision with the same terms is still fresh.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol

from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.acp.offer_snapshot import DeliveryContext, MerchantOffer
from commerce_agent.approval import ApprovalError
from commerce_agent.config import Settings
from commerce_agent.persistence.hp_evidence_sqlite import SqliteHpEvidenceStore

# The only destination the lab serves; ACP checkout creation sends none.
LAB_DESTINATION = DeliveryContext(country="CO", city="Bogota", postal_code="110111")
FreshnessMode = Literal["shadow", "enforce"]
FreshnessStage = Literal["present", "approve"]


class OfferReader(Protocol):
    def get_offer(self, offer_id: str, delivery_context: DeliveryContext) -> MerchantOffer: ...


@dataclass(frozen=True)
class FreshnessResult:
    outcome: Literal["fresh", "changed", "unavailable"]
    changed_fields: tuple[str, ...] = ()
    compared: dict[str, dict[str, str | int | None]] = field(default_factory=dict)
    offer_revision: int | None = None
    reason: str | None = None


def compare_offer(offer: MerchantOffer, checkout: CheckoutSession) -> FreshnessResult:
    """Merchant completion predicate (commerce_lab/checkout.py), seen through ACP fields."""
    terms: dict[str, tuple[str | int | None, str | int | None]] = {
        "currency": (checkout.currency, offer.pricing.currency),
        "subtotal_minor": (_total(checkout, "subtotal"), offer.pricing.items_total_minor),
        "shipping_minor": (_total(checkout, "fulfillment"), offer.pricing.shipping_total_minor),
        "total_minor": (checkout.total_minor, offer.pricing.total_minor),
        "expires_at": (checkout.expires_at, offer.expires_at),
    }
    changed = [name for name, (presented, live) in terms.items() if presented != live]
    sellable = offer.product_status == "active" and offer.availability == "in_stock"
    available = offer.available_quantity if sellable else 0
    quantity = _offer_line(checkout)[1]
    if available < quantity:
        changed.append("stock")
    compared = {name: {"checkout": pair[0], "offer": pair[1]} for name, pair in terms.items()}
    compared["stock"] = {"checkout": quantity, "offer": available}
    return FreshnessResult(
        "changed" if changed else "fresh", tuple(changed), compared, offer.revision
    )


class OfferFreshnessGuard:
    """Backend injection only. Shadow records; enforce also blocks changed or unread offers."""

    def __init__(
        self,
        reader: OfferReader,
        evidence: SqliteHpEvidenceStore,
        *,
        mode: FreshnessMode,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.mode: FreshnessMode = mode
        self._reader = reader
        self._evidence = evidence
        self._clock = clock or (lambda: datetime.now(UTC))

    def check(self, *, run_id: str, checkout: CheckoutSession, stage: FreshnessStage) -> None:
        result = self._observe(checkout)
        block = self.mode == "enforce" and result.outcome != "fresh"
        recorded = self._record(run_id, self._body(run_id, checkout, stage, result, block))
        if block and result.outcome == "changed":
            raise ApprovalError(
                "Live offer no longer matches the checkout terms", code="checkout_terms_changed"
            )
        if block or (self.mode == "enforce" and not recorded):
            raise ApprovalError(
                "Live offer freshness could not be established",
                code="offer_freshness_unavailable",
            )

    def _observe(self, checkout: CheckoutSession) -> FreshnessResult:
        try:
            offer_id = _offer_line(checkout)[0]
            offer = self._reader.get_offer(offer_id, LAB_DESTINATION)
            if offer.id != offer_id:
                return FreshnessResult("unavailable", reason="offer_mismatch")
            return compare_offer(offer, checkout)
        except ACPClientError as error:
            return FreshnessResult("unavailable", reason=error.code)
        except Exception:
            return FreshnessResult("unavailable", reason="freshness_check_failed")

    def _record(self, run_id: str, body: dict[str, object]) -> bool:
        try:
            self._evidence.append(run_id, "freshness_check", body)
        except Exception:
            logging.getLogger(__name__).warning("HP freshness evidence unavailable")
            return False
        return True

    def _body(
        self,
        run_id: str,
        checkout: CheckoutSession,
        stage: FreshnessStage,
        result: FreshnessResult,
        block: bool,
    ) -> dict[str, object]:
        return {
            "run_id": run_id,
            "checkout_id": checkout.id,
            "stage": stage,
            "observed_revision": result.offer_revision,
            "compared": result.compared,
            "outcome": result.outcome,
            "changed_fields": list(result.changed_fields),
            "reason": result.reason,
            "governance_mode": self.mode,
            "decision": "block" if block else "allow",
            "checked_at": self._clock().astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }


def build_offer_freshness(
    settings: Settings,
    reader: OfferReader,
    clock: Callable[[], datetime] | None = None,
) -> OfferFreshnessGuard | None:
    """Off, and enforce without the governed HP flow, never read the live offer."""
    mode = settings.agent_governance_mode
    if mode == "off" or (mode == "enforce" and settings.agent_hp_flow != "governed"):
        return None
    evidence = SqliteHpEvidenceStore(settings.agent_database_path)
    evidence.initialize()
    return OfferFreshnessGuard(reader, evidence, mode=mode, clock=clock)


def _offer_line(checkout: CheckoutSession) -> tuple[str, int]:
    [line] = checkout.line_items
    item, quantity = line["item"], line["quantity"]
    if not isinstance(item["id"], str) or type(quantity) is not int or quantity < 1:
        raise ValueError("Checkout line is not a lab offer")
    return item["id"], quantity


def _total(checkout: CheckoutSession, kind: str) -> int | None:
    amounts = [row.get("amount") for row in checkout.totals if row.get("type") == kind]
    return amounts[0] if len(amounts) == 1 and type(amounts[0]) is int else None
