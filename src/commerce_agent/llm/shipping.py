"""The shipping the buyer's model reads before it chooses (hp-shipping-before-choice.md).

The feed has no shipping, so it comes from each variant's merchant offer for the lab destination
(`/lab/offers/get`), the read the conditional branches already make. Only the offer's integer
pricing becomes text here: no offer text reaches the model. A failed read, or an offer that is not
the candidate's variant at its feed price and currency, is unknown shipping (None), never a guess.
These are observations like the feed price; the checkout stays the authority on the total.
"""

from collections.abc import Iterable
from typing import TypedDict

from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.feed import ProductCandidate
from commerce_agent.application.offer_freshness import LAB_DESTINATION, OfferReader
from commerce_agent.llm.prices import format_minor_amount

FREE_SHIPPING = "gratis"
# A merchant that does not answer: each read left would wait for the same timeout.
_UNREACHABLE = frozenset({"timeout", "transport_error"})


class ShippingView(TypedDict):
    # "gratis", the formatted amount, or None when unknown.
    shipping: str | None
    display_total_with_shipping: str | None


class _Unreachable(Exception):
    """The merchant did not answer a read."""


def _unknown() -> ShippingView:
    return ShippingView(shipping=None, display_total_with_shipping=None)


def _read(offers: OfferReader, candidate: ProductCandidate) -> ShippingView:
    """One read, never retried; any failure or mismatch is unknown shipping."""
    try:
        offer = offers.get_offer(candidate.variant_id, LAB_DESTINATION)
    except ACPClientError as error:
        if error.code in _UNREACHABLE:
            raise _Unreachable from error
        return _unknown()
    except Exception:
        return _unknown()
    pricing = offer.pricing
    if (
        offer.id != candidate.variant_id
        or offer.delivery_context != LAB_DESTINATION
        or pricing.currency.casefold() != candidate.currency.casefold()
        or pricing.items_total_minor != candidate.observed_unit_amount_minor
    ):
        return _unknown()
    amount = pricing.shipping_total_minor
    return ShippingView(
        shipping=FREE_SHIPPING if amount == 0 else format_minor_amount(amount, candidate.currency),
        display_total_with_shipping=format_minor_amount(pricing.total_minor, candidate.currency),
    )


def shipping_by_variant(
    offers: OfferReader | None, candidates: Iterable[ProductCandidate]
) -> dict[str, ShippingView]:
    """Each candidate's shipping, one read per variant. Callers pass at most ten candidates; after
    a timeout or a transport failure the rest stay unknown without a read, so a merchant that is
    down costs one timeout, not ten."""
    views: dict[str, ShippingView] = {}
    reader = offers
    for candidate in candidates:
        if candidate.variant_id in views:
            continue
        view = _unknown()
        if reader is not None:
            try:
                view = _read(reader, candidate)
            except _Unreachable:
                reader = None
        views[candidate.variant_id] = view
    return views
