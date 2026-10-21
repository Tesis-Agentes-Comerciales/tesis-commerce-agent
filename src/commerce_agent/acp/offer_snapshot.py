"""Independent mirror of Merchant 9743c9f Offer; no Merchant package/DB access.

Profile merchant-offer-snapshot/1. Defaults, normalization and integer bounds
match contracts/commerce.py and primitives.py at that frozen commit.
"""

import hashlib
import re
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from commerce_agent.canonical_json import canonical_original_response

SAFE = 9_007_199_254_740_991
Identifier = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
Minor = Annotated[int, Field(strict=True, ge=0, le=SAFE)]


def merchant_time(value: str) -> datetime:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise ValueError("Invalid Merchant time")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class DeliveryContext(SnapshotModel):
    country: Literal["CO"]
    city: Literal["Bogota"]
    postal_code: Literal["110111"]


class OfferPricing(SnapshotModel):
    currency: Literal["cop"]
    items_total_minor: Minor
    shipping_total_minor: Minor
    total_minor: Minor
    tax_included: Literal[True]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.total_minor != self.items_total_minor + self.shipping_total_minor:
            raise ValueError("Invalid Offer total")
        return self


class MerchantOffer(SnapshotModel):
    id: Identifier
    product_id: Identifier
    product_status: Literal["active", "inactive"] = "active"
    sku: Identifier = Field(default_factory=lambda data: data["id"])
    merchant_id: Identifier
    revision: Annotated[int, Field(strict=True, gt=0, le=SAFE)]
    category: Annotated[
        str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    ]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    product_title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    product_description: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    brand: Annotated[str, StringConstraints(min_length=1, max_length=100, strip_whitespace=True)]
    image_url: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    color: (
        Annotated[str, StringConstraints(min_length=1, max_length=64, strip_whitespace=True)] | None
    ) = None
    attributes: Annotated[
        dict[
            Identifier, Annotated[str, StringConstraints(min_length=1, max_length=200)] | int | bool
        ],
        Field(max_length=32),
    ] = Field(default_factory=dict)
    condition: Literal["new", "refurbished"]
    availability: Literal["in_stock", "out_of_stock"]
    available_quantity: Annotated[int, Field(strict=True, ge=0)]
    pricing: OfferPricing
    delivery_context: DeliveryContext
    delivery_days: Annotated[int, Field(strict=True, gt=0)]
    observed_at: str
    expires_at: str

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if merchant_time(self.expires_at) <= merchant_time(self.observed_at):
            raise ValueError("Invalid Offer lifetime")
        if (self.availability == "in_stock") != (self.available_quantity > 0):
            raise ValueError("Invalid Offer inventory")
        return self


class OfferResponse(SnapshotModel):
    ok: Literal[True]
    data: MerchantOffer


def offer_snapshot_sha256(offer: MerchantOffer) -> str:
    normalized = MerchantOffer.model_validate(offer.model_dump(mode="json"))
    return hashlib.sha256(
        b"merchant-offer-snapshot/v1\0"
        + canonical_original_response(normalized.model_dump(mode="json"))
    ).hexdigest()
