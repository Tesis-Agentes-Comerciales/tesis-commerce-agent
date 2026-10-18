"""Pure request construction shared by ordinary ACP and durable D1 preparation."""

from typing import Literal

from .models import CheckoutCreateRequest, CheckoutItemRequest


def build_checkout_create(variant_id: str, currency: str) -> CheckoutCreateRequest:
    normalized: Literal["usd", "cop"]
    if currency.lower() == "usd":
        normalized = "usd"
    elif currency.lower() == "cop":
        normalized = "cop"
    else:
        raise ValueError("Checkout currency is not supported")
    return CheckoutCreateRequest(
        line_items=[CheckoutItemRequest(id=variant_id)], currency=normalized
    )
