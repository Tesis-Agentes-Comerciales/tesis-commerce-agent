"""Doubles for the conditional intent (docs/design/p0-conditional-intent.md): feed, offers, reader.

The pair follows the P2 `pair_rules` that P3 reuses (A = Sony Xperia 5 II, 256 GB, new; B =
OnePlus 8T, 256 GB, new), with the variants that make matching hard: the Xperia 5 III, whose title
contains «xperia 5 ii» as a substring, and a refurbished Xperia 5 II. C is the cheaper distractor
of UX03-D, which must never be read. Prices are COP minor units; shipping is the same for all.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.feed import ProductFeed, load_product_feed_payload
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.acp.offer_snapshot import DeliveryContext, MerchantOffer
from commerce_agent.domain.conditional import BranchMatch, ConditionalIntent, PurchaseBranch
from commerce_agent.domain.intent import HardConstraints, OptionConstraint

A, B, C = "SON-X5II-256-BLK", "ONP-8T-256-GRN", "MOT-G54-128-BLU"
A_III, A_REFURB = "SON-X5III-256-BLK", "SON-X5II-256-BLK-R"
SHIPPING = 2_500_000
PRICES = {
    A: 289_900_000,
    A_III: 349_900_000,
    A_REFURB: 199_000_000,
    B: 199_900_000,
    C: 79_900_000,
}
T_A, T_B = PRICES[A] + SHIPPING, PRICES[B] + SHIPPING
assert T_B < T_A  # UX03-C needs B cheaper than A.
# The turn's `intent.hard` when the buyer names only options: COP, nothing else.
SEARCH = HardConstraints(currency="COP")

_VARIANTS: dict[str, dict[str, Any]] = {
    A: {"product": "Sony Xperia 5 II", "title": "Sony Xperia 5 II 256 GB negro", "brand": "Sony"},
    A_III: {
        "product": "Sony Xperia 5 III",
        "title": "Sony Xperia 5 III 256 GB negro",
        "brand": "Sony",
    },
    A_REFURB: {
        "product": "Sony Xperia 5 II",
        "title": "Sony Xperia 5 II 256 GB negro reacondicionado",
        "brand": "Sony",
        "condition": "refurbished",
    },
    B: {
        "product": "OnePlus 8T",
        "title": "OnePlus 8T 256 GB verde",
        "brand": "OnePlus",
        "color": "verde",
    },
    C: {
        "product": "Motorola Moto G54",
        "title": "Motorola Moto G54 128 GB azul",
        "brand": "Motorola",
        "color": "azul",
        "storage": "128 GB",
    },
}


def _variant(variant_id: str, *, available: bool, currency: str) -> dict[str, Any]:
    spec = _VARIANTS[variant_id]
    return {
        "availability": {
            "available": available,
            "status": "in_stock" if available else "out_of_stock",
        },
        "categories": [
            {"taxonomy": "merchant", "value": "smartphones"},
            {"taxonomy": "brand", "value": spec["brand"]},
        ],
        "condition": [spec.get("condition", "new")],
        "id": variant_id,
        "price": {"amount": PRICES[variant_id], "currency": currency},
        "seller": {"name": "Synthetic Commerce Merchant"},
        "title": spec["title"],
        "url": f"https://merchant.example.test/products/{variant_id.lower()}",
        "variant_options": [
            {"name": "Color", "value": spec.get("color", "negro")},
            {"name": "Almacenamiento", "value": spec.get("storage", "256 GB")},
        ],
    }


def phone_feed(
    *,
    unavailable: Iterable[str] = (),
    currency: Mapping[str, str] | None = None,
    only: Iterable[str] | None = None,
) -> ProductFeed:
    """The pair, its look-alikes and C; `unavailable` variants say so in the feed."""
    hidden, currencies = set(unavailable), dict(currency or {})
    variant_ids = tuple(only) if only is not None else tuple(_VARIANTS)
    products: dict[str, dict[str, Any]] = {}
    for variant_id in variant_ids:
        title = _VARIANTS[variant_id]["product"]
        product = products.setdefault(
            title,
            {
                "id": "prod-" + title.lower().replace(" ", "-"),
                "title": title,
                "url": "https://merchant.example.test/products/" + title.lower().replace(" ", "-"),
                "variants": [],
            },
        )
        product["variants"].append(
            _variant(
                variant_id,
                available=variant_id not in hidden,
                currency=currencies.get(variant_id, "COP"),
            )
        )
    return load_product_feed_payload(
        {
            "metadata": {
                "id": "feed_conditional_phones",
                "target_country": "CO",
                "updated_at": "2026-11-05T12:00:00Z",
            },
            "products": list(products.values()),
        }
    )


def offer(
    variant_id: str,
    *,
    stock: int = 3,
    status: str = "active",
    price: int | None = None,
    shipping: int = SHIPPING,
    revision: int = 7,
    offer_id: str | None = None,
) -> MerchantOffer:
    """The Merchant Offer of `POST /lab/offers/get`; its total includes shipping."""
    items = PRICES[variant_id] if price is None else price
    return MerchantOffer.model_validate(
        {
            "id": offer_id or variant_id,
            "product_id": "prod-" + variant_id.lower(),
            "product_status": status,
            "merchant_id": "merchant_lab",
            "revision": revision,
            "category": "smartphones",
            "name": _VARIANTS[variant_id]["title"],
            "brand": _VARIANTS[variant_id]["brand"],
            "condition": _VARIANTS[variant_id].get("condition", "new"),
            "availability": "in_stock" if stock > 0 else "out_of_stock",
            "available_quantity": stock,
            "pricing": {
                "currency": "cop",
                "items_total_minor": items,
                "shipping_total_minor": shipping,
                "total_minor": items + shipping,
                "tax_included": True,
            },
            "delivery_context": {"country": "CO", "city": "Bogota", "postal_code": "110111"},
            "delivery_days": 2,
            "observed_at": "2026-11-05T12:00:00Z",
            "expires_at": "2026-11-05T13:00:00Z",
        }
    )


def match_a() -> BranchMatch:
    """Rule A as the router would write it: model, storage and condition, never the price."""
    return BranchMatch(
        required_terms=("Sony", "Xperia 5 II"),
        conditions=("new",),
        required_options=(OptionConstraint(name="Almacenamiento", value="256 GB"),),
    )


def match_b() -> BranchMatch:
    return BranchMatch(
        required_terms=("OnePlus 8T",),
        conditions=("new",),
        required_options=(OptionConstraint(name="Almacenamiento", value="256 GB"),),
    )


def ux03(limit_a: int, limit_b: int) -> ConditionalIntent:
    """«A si el total es ≤ X; si no, B si tiene stock y el total es ≤ Y» (P3 §5)."""
    return ConditionalIntent(
        branches=(
            PurchaseBranch(match=match_a(), max_total_amount_minor=limit_a),
            PurchaseBranch(match=match_b(), max_total_amount_minor=limit_b),
        ),
        currency="COP",
    )


@dataclass
class FakeOfferReader:
    """`OfferReader` double: serves one answer per offer and records every read, in order."""

    answers: dict[str, MerchantOffer | Exception] = field(default_factory=dict)
    reads: list[tuple[str, DeliveryContext]] = field(default_factory=list)

    def get_offer(self, offer_id: str, delivery_context: DeliveryContext) -> MerchantOffer:
        self.reads.append((offer_id, delivery_context))
        answer = self.answers.get(offer_id)
        if answer is None:
            raise AssertionError(f"Offer {offer_id} was not expected to be read")
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def read_ids(self) -> list[str]:
        return [offer_id for offer_id, _ in self.reads]


def acp_error(code: str, status_code: int | None = None) -> ACPClientError:
    return ACPClientError(f"synthetic {code}", code=code, status_code=status_code)


def checkout(variant_id: str, total: int, *, currency: str = "cop") -> CheckoutSession:
    """The ACP checkout `start` prepared for the chosen branch, with its authoritative total."""
    return CheckoutSession.model_validate(
        {
            "id": f"chk_{variant_id}",
            "protocol": {"version": "2026-04-17"},
            "status": "ready_for_payment",
            "currency": currency,
            "capabilities": {"payment": {"handlers": []}},
            "line_items": [
                {"id": "li_1", "item": {"id": variant_id, "name": variant_id}, "quantity": 1}
            ],
            "totals": [{"type": "total", "display_text": "Total", "amount": total}],
            "created_at": "2026-11-05T12:00:00Z",
            "updated_at": "2026-11-05T12:01:00Z",
            "expires_at": "2026-11-05T13:00:00Z",
        }
    )
