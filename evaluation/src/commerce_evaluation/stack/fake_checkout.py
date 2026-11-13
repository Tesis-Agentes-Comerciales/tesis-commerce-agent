"""The in-memory checkout double of the unit tests, with `checkout` and the `details` it checks."""

from commerce_agent.acp.models import Address, Buyer, CheckoutSession, FulfillmentDetails
from commerce_agent.application import CheckoutDetails


class FakeCheckoutGateway:
    def __init__(self, *, total: int = 74_000, unit_amount: int = 72_000) -> None:
        self.total = total
        self.unit_amount = unit_amount
        self.calls: list[tuple[str, str]] = []
        self.current = checkout(
            status="not_ready_for_payment", total=total, unit_amount=unit_amount
        )

    def create_checkout(
        self, variant_id: str, *, currency: str, idempotency_key: str
    ) -> CheckoutSession:
        self.calls.append(("create", idempotency_key))
        assert variant_id == "SON-01"
        return self.current

    def get_checkout(self, checkout_id: str) -> CheckoutSession:
        self.calls.append(("get", checkout_id))
        return self.current

    def signed_checkout(self, checkout: CheckoutSession) -> str:
        raise AssertionError("Legacy tests must not request AP2 checkout")

    def verify_signed_checkout(self, token: str, checkout: CheckoutSession) -> None:
        raise AssertionError("Legacy tests must not verify AP2 checkout")

    def update_checkout(self, checkout_id, update, *, idempotency_key):
        self.calls.append(("update", idempotency_key))
        assert checkout_id == self.current.id
        assert update.buyer.email == "buyer@example.test"
        assert update.selected_fulfillment_options[0].option_id == "ship_1"
        assert update.selected_fulfillment_options[0].item_ids == ["li_1"]
        self.current = checkout(
            status="ready_for_payment", total=self.total, unit_amount=self.unit_amount
        )
        return self.current

    def cancel_checkout(
        self, checkout_id: str, *, idempotency_key: str, reason_code: str | None = None
    ) -> CheckoutSession:
        self.calls.append(("cancel", idempotency_key))
        assert reason_code in {"price_sensitivity", "other"}
        self.current = checkout(status="canceled", total=self.total, unit_amount=self.unit_amount)
        return self.current


def checkout(*, status: str, total: int, unit_amount: int) -> CheckoutSession:
    return CheckoutSession.model_validate(
        {
            "id": "cs_1",
            "protocol": {"version": "2026-04-17"},
            "status": status,
            "currency": "cop",
            "capabilities": {"payment": {"handlers": []}},
            "line_items": [
                {
                    "id": "li_1",
                    "item": {"id": "SON-01", "name": "Sonora", "unit_amount": unit_amount},
                    "quantity": 1,
                    "totals": [{"type": "total", "display_text": "Item", "amount": unit_amount}],
                }
            ],
            "totals": [{"type": "total", "display_text": "Total", "amount": total}],
            "fulfillment_options": [{"type": "shipping", "id": "ship_1"}],
            "selected_fulfillment_options": [],
            "messages": [],
            "links": [],
            "created_at": "2026-09-20T11:00:00Z",
            "updated_at": "2026-09-20T11:30:00Z",
            "expires_at": "2026-09-20T13:00:00Z",
        }
    )


def details() -> CheckoutDetails:
    return CheckoutDetails(
        buyer=Buyer(email="buyer@example.test", first_name="Buyer", last_name="Test"),
        fulfillment=FulfillmentDetails(
            name="Buyer Test",
            email="buyer@example.test",
            phone_number="+573000000000",
            address=Address(
                name="Buyer Test",
                line_one="Calle 100 # 10-20",
                city="Bogota",
                state="DC",
                country="CO",
                postal_code="110111",
            ),
        ),
    )
