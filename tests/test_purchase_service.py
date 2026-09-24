from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from commerce_agent.acp import load_product_feed
from commerce_agent.acp.models import Address, Buyer, CheckoutSession, FulfillmentDetails
from commerce_agent.application import CheckoutDetails, PurchaseService, PurchaseServiceError
from commerce_agent.approval import ApprovalService
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.persistence import SqliteAgentStore

FIXTURE = Path(__file__).parent / "fixtures" / "feed"


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
            "currency": "usd",
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


def make_service(
    tmp_path: Path,
    gateway: FakeCheckoutGateway,
) -> tuple[PurchaseService, SqliteAgentStore]:
    store = SqliteAgentStore(tmp_path / "agent.db")
    store.initialize()
    approvals = ApprovalService(
        store,
        SecretStr("approval-secret-with-at-least-32-characters"),
        clock=lambda: datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
    )
    return (
        PurchaseService(
            feed=load_product_feed(FIXTURE),
            checkout=gateway,
            store=store,
            approvals=approvals,
        ),
        store,
    )


def test_purchase_reaches_human_confirmation_with_backend_only_details(tmp_path: Path) -> None:
    gateway = FakeCheckoutGateway()
    service, store = make_service(tmp_path, gateway)

    result = service.start(
        run_id="run-1",
        request="buy headphones",
        intent=PurchaseIntent(),
        selected_variant_id="SON-01",
        details=details(),
    )

    assert result.stage == "awaiting_confirmation"
    assert result.checkout.status == "ready_for_payment"
    assert result.price_changed is False
    assert store.purchase("run-1")["stage"] == "awaiting_confirmation"  # type: ignore[index]
    assert [call[0] for call in gateway.calls] == ["create", "update", "get"]
    assert "buyer@example.test" not in repr(result.selected_candidate)


def test_checkout_currency_must_match_feed_candidate(tmp_path: Path) -> None:
    gateway = FakeCheckoutGateway()
    gateway.current = gateway.current.model_copy(update={"currency": "cop"})
    service, store = make_service(tmp_path, gateway)

    with pytest.raises(PurchaseServiceError) as captured:
        service.start(
            run_id="run-currency",
            request="buy headphones",
            intent=PurchaseIntent(),
            selected_variant_id="SON-01",
            details=details(),
        )
    assert captured.value.code == "checkout_mismatch"
    assert store.purchase("run-currency")["stage"] == "failed"  # type: ignore[index]


def test_authoritative_total_above_hard_budget_cancels(tmp_path: Path) -> None:
    gateway = FakeCheckoutGateway(total=90_000)
    service, store = make_service(tmp_path, gateway)

    with pytest.raises(PurchaseServiceError) as captured:
        service.start(
            run_id="run-1",
            request="under budget",
            intent=PurchaseIntent(hard=HardConstraints(max_total_amount_minor=80_000)),
            selected_variant_id="SON-01",
            details=details(),
        )
    assert captured.value.code == "budget_exceeded"
    assert store.purchase("run-1")["stage"] == "canceled"  # type: ignore[index]
    assert [call[0] for call in gateway.calls] == ["create", "cancel"]


def test_total_increase_after_delivery_update_cancels_before_approval(tmp_path: Path) -> None:
    gateway = FakeCheckoutGateway()
    gateway.total = 90_000
    service, store = make_service(tmp_path, gateway)

    with pytest.raises(PurchaseServiceError) as captured:
        service.start(
            run_id="run-1",
            request="under budget including delivery",
            intent=PurchaseIntent(hard=HardConstraints(max_total_amount_minor=80_000)),
            selected_variant_id="SON-01",
            details=details(),
        )

    assert captured.value.code == "budget_exceeded"
    assert store.purchase("run-1")["stage"] == "canceled"  # type: ignore[index]
    assert [call[0] for call in gateway.calls] == ["create", "update", "get", "cancel"]


def test_update_cannot_switch_to_another_checkout(tmp_path: Path) -> None:
    class SwappedCheckoutGateway(FakeCheckoutGateway):
        def update_checkout(self, checkout_id, update, *, idempotency_key):
            updated = super().update_checkout(checkout_id, update, idempotency_key=idempotency_key)
            return updated.model_copy(update={"id": "cs_other"})

    gateway = SwappedCheckoutGateway()
    service, store = make_service(tmp_path, gateway)

    with pytest.raises(PurchaseServiceError) as captured:
        service.start(
            run_id="run-1",
            request="buy",
            intent=PurchaseIntent(),
            selected_variant_id="SON-01",
            details=details(),
        )

    assert captured.value.code == "checkout_mismatch"
    assert store.purchase("run-1")["stage"] == "failed"  # type: ignore[index]
    assert [call[0] for call in gateway.calls] == ["create", "update"]


def test_checkout_cannot_add_another_item_after_delivery_update(tmp_path: Path) -> None:
    class ExtraItemGateway(FakeCheckoutGateway):
        def get_checkout(self, checkout_id: str) -> CheckoutSession:
            current = super().get_checkout(checkout_id)
            extra = {**current.line_items[0], "id": "li_extra", "item": {"id": "OTHER"}}
            return current.model_copy(update={"line_items": [*current.line_items, extra]})

    gateway = ExtraItemGateway()
    service, store = make_service(tmp_path, gateway)

    with pytest.raises(PurchaseServiceError) as captured:
        service.start(
            run_id="run-1",
            request="buy one pair",
            intent=PurchaseIntent(),
            selected_variant_id="SON-01",
            details=details(),
        )

    assert captured.value.code == "checkout_mismatch"
    assert store.purchase("run-1")["stage"] == "failed"  # type: ignore[index]


def test_feed_price_change_is_reported_but_checkout_remains_authoritative(tmp_path: Path) -> None:
    gateway = FakeCheckoutGateway(unit_amount=73_000, total=75_000)
    service, _ = make_service(tmp_path, gateway)
    result = service.start(
        run_id="run-1",
        request="buy",
        intent=PurchaseIntent(),
        selected_variant_id="SON-01",
        details=details(),
    )
    assert result.price_changed is True
    assert result.checkout.total_minor == 75_000


def test_model_can_only_select_a_deterministically_eligible_candidate(tmp_path: Path) -> None:
    gateway = FakeCheckoutGateway()
    service, store = make_service(tmp_path, gateway)

    with pytest.raises(PurchaseServiceError) as captured:
        service.start(
            run_id="run-1",
            request="buy",
            intent=PurchaseIntent(),
            selected_variant_id="SON-07",
            details=details(),
        )

    assert captured.value.code == "invalid_candidate"
    assert store.purchase("run-1")["stage"] == "failed"  # type: ignore[index]
    assert gateway.calls == []


def test_approval_requires_exact_fingerprint_and_updates_stage(tmp_path: Path) -> None:
    gateway = FakeCheckoutGateway()
    service, store = make_service(tmp_path, gateway)
    service.start(
        run_id="run-1",
        request="buy",
        intent=PurchaseIntent(),
        selected_variant_id="SON-01",
        details=details(),
    )
    terms = service.approval_terms("run-1")

    service.approve(run_id="run-1", actor_id="human-1", expected_fingerprint=terms.fingerprint)
    assert store.purchase("run-1")["stage"] == "approved"  # type: ignore[index]


def test_pending_purchase_can_be_canceled_but_approved_purchase_cannot(tmp_path: Path) -> None:
    gateway = FakeCheckoutGateway()
    service, store = make_service(tmp_path, gateway)
    service.start(
        run_id="run-1",
        request="buy",
        intent=PurchaseIntent(),
        selected_variant_id="SON-01",
        details=details(),
    )

    service.cancel(run_id="run-1")
    service.cancel(run_id="run-1")

    assert store.purchase("run-1")["stage"] == "canceled"  # type: ignore[index]
    assert [call[0] for call in gateway.calls].count("cancel") == 1

    approved_service, _ = make_service(tmp_path / "approved", FakeCheckoutGateway())
    approved_service.start(
        run_id="run-2",
        request="buy again",
        intent=PurchaseIntent(),
        selected_variant_id="SON-01",
        details=details(),
    )
    terms = approved_service.approval_terms("run-2")
    approved_service.approve(
        run_id="run-2", actor_id="human-1", expected_fingerprint=terms.fingerprint
    )

    with pytest.raises(PurchaseServiceError) as captured:
        approved_service.cancel(run_id="run-2")
    assert captured.value.code == "invalid_stage"
