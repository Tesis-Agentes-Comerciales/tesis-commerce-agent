"""Independent mirror of the merchant's delivery address; no merchant package access.

Merchant abf30df, `src/commerce_lab/contracts/commerce.py`: `AddressInfo` serves one
destination, and `normalize_supported_city` rewrites the city before `acp.py` echoes
`fulfillment_details`. A delegation binds the destination before any checkout exists and D2
binds that echo, so both must hash the same bytes (hnp-governed.md §5.1, POL-08).
`tests/fixtures/merchant_destination/cases.json` pins the equivalence case by case.
"""

from .models import FulfillmentDetails

_CITY_SPELLINGS = frozenset({"bogota", "bogotá"})
_SERVED = ("Bogota", "DC", "CO", "110111")


def merchant_fulfillment(fulfillment: FulfillmentDetails) -> FulfillmentDetails | None:
    """The `fulfillment_details` the merchant echoes for these, or None if it refuses them."""
    address = fulfillment.address
    city = "Bogota" if address.city.strip().casefold() in _CITY_SPELLINGS else address.city
    if (city, address.state, address.country, address.postal_code) != _SERVED:
        return None
    return fulfillment.model_copy(update={"address": address.model_copy(update={"city": city})})
