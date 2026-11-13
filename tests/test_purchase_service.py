import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from commerce_agent.acp import load_product_feed
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.application import PurchaseService, PurchaseServiceError
from commerce_agent.approval import ApprovalService
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.persistence import SqliteAgentStore
from commerce_evaluation.stack.fake_checkout import FakeCheckoutGateway, details

FIXTURE = Path(__file__).parent / "fixtures" / "feed"


def failure_code(tmp_path: Path, run_id: str) -> str | None:
    """`purchases.failure_code`, which only the purchase trace reads."""
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection:
        return connection.execute(
            "SELECT failure_code FROM purchases WHERE run_id = ?", (run_id,)
        ).fetchone()[0]


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
    gateway.current = gateway.current.model_copy(update={"currency": "usd"})
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
    assert failure_code(tmp_path, "run-currency") == "checkout_mismatch"


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
    assert failure_code(tmp_path, "run-1") == "invalid_candidate"
    assert gateway.calls == []


def test_no_candidate_stores_its_failure_code(tmp_path: Path) -> None:
    gateway = FakeCheckoutGateway()
    service, store = make_service(tmp_path, gateway)

    with pytest.raises(PurchaseServiceError) as captured:
        service.start(
            run_id="run-1",
            request="buy",
            intent=PurchaseIntent(hard=HardConstraints(max_unit_amount_minor=1)),
            selected_variant_id="SON-01",
            details=details(),
        )

    assert captured.value.code == "no_candidate"
    assert store.purchase("run-1")["stage"] == "failed"  # type: ignore[index]
    assert failure_code(tmp_path, "run-1") == "no_candidate"


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
