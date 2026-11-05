"""Technical observer only. Never imported by product or used to choose UX03 branches."""

from commerce_agent.acp.offer_snapshot import MerchantOffer, merchant_time

CASES = (
    "stable-zero",
    "stable-positive",
    "budget-minus",
    "budget-equal",
    "budget-plus",
    "revision-only",
    "shipping-feed",
    "price-change",
    "out-of-stock",
    "inactive",
    "expiry-before",
    "expiry-at",
    "expiry-after",
    "timeout-before",
    "lost-read-after",
    "invalid-response",
    "different-offer",
    "network-error",
    "foreign-run",
    "foreign-actor",
    "missing-auth",
    "create-idempotency",
    "lost-create",
    "lost-cancel",
)
AGENT_BASE = "a11f78bd5e5c18cd3a81ab74c2edc96e274367af"
INTEGRATED_AGENT_BASE = "9d932647f77f61135bd503874b266a96661854c8"
INTEGRATED_PRODUCT_TREE = "3316c4ce2a6f88f9222eeafd0a66bbf64148c2ce"
MERCHANT = "abf30df3b6a611284dedcc26c00accac6a853329"
PSP = "e6ab67b5963434a9a3caae2012b70232a3cca65d"


def condition(
    offer: MerchantOffer | None,
    *,
    expected_id: str,
    at: str,
    budget: int,
    scope_verified: bool = True,
) -> str:
    """A technical failure or missing binding is UNKNOWN, never commercial FALSE."""
    if offer is None or offer.id != expected_id or not scope_verified:
        return "unknown"
    return str(
        offer.product_status == "active"
        and offer.availability == "in_stock"
        and offer.available_quantity >= 1
        and merchant_time(at) < merchant_time(offer.expires_at)
        and offer.pricing.total_minor <= budget
    ).lower()
