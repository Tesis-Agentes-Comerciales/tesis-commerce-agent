from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.models import (
    CheckoutSession,
    CheckoutSessionWithOrder,
    PaymentCredential,
    SandboxPaymentData,
    SandboxPaymentInstrument,
)
from commerce_agent.approval import ApprovalError, ApprovalService
from commerce_agent.completion import CompletionService
from commerce_agent.delegated_payment import DelegatedPaymentClient
from commerce_agent.persistence import SqliteAgentStore

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
APPROVAL_SECRET = SecretStr("approval-secret-with-at-least-32-characters")
TEST_TOKEN = "vt_" + "a" * 64


class FakePayments(DelegatedPaymentClient):
    def __init__(self) -> None:
        pass

    def payment_data(
        self, *, checkout: CheckoutSession, idempotency_key: str
    ) -> SandboxPaymentData:
        assert checkout.id == "cs_completion_1"
        assert idempotency_key
        return SandboxPaymentData(
            instrument=SandboxPaymentInstrument(
                credential=PaymentCredential(token=SecretStr(TEST_TOKEN))
            )
        )


def checkout_payload(*, total: int = 74_000) -> dict[str, Any]:
    return {
        "id": "cs_completion_1",
        "protocol": {"version": "2026-04-17"},
        "status": "ready_for_payment",
        "currency": "usd",
        "capabilities": {"payment": {"handlers": []}},
        "line_items": [
            {
                "id": "li_1",
                "item": {"id": "SON-01", "name": "Sonora", "unit_amount": 72_000},
                "quantity": 1,
                "totals": [{"type": "total", "display_text": "Item", "amount": 72_000}],
            }
        ],
        "totals": [{"type": "total", "display_text": "Total", "amount": total}],
        "fulfillment_options": [{"type": "shipping", "id": "ship_1"}],
        "selected_fulfillment_options": [
            {"type": "shipping", "option_id": "ship_1", "item_ids": ["li_1"]}
        ],
        "messages": [],
        "links": [],
        "created_at": "2026-09-20T11:00:00Z",
        "updated_at": "2026-09-20T11:30:00Z",
        "expires_at": "2026-09-20T13:00:00Z",
    }


def checkout(*, total: int = 74_000) -> CheckoutSession:
    return CheckoutSession.model_validate(checkout_payload(total=total))


def completed(current: CheckoutSession) -> CheckoutSessionWithOrder:
    payload = current.model_dump(mode="json")
    payload["status"] = "completed"
    payload["order"] = {
        "type": "order",
        "id": "ord_completion_1",
        "checkout_session_id": current.id,
        "permalink_url": "http://127.0.0.1:4120/orders/ord_completion_1",
        "status": "confirmed",
        "line_items": [],
        "totals": deepcopy(current.totals),
    }
    return CheckoutSessionWithOrder.model_validate(payload)


class FakeGateway:
    def __init__(
        self,
        current: CheckoutSession,
        *,
        error: Exception | None = None,
    ) -> None:
        self.current = current
        self.error = error
        self.complete_calls: list[tuple[str, SandboxPaymentData, str]] = []
        self.get_calls = 0

    def get_checkout(self, checkout_id: str) -> CheckoutSession:
        assert checkout_id == self.current.id
        self.get_calls += 1
        return self.current

    def complete_checkout(
        self,
        checkout_id: str,
        payment_data: SandboxPaymentData,
        *,
        idempotency_key: str,
    ) -> CheckoutSessionWithOrder:
        self.complete_calls.append((checkout_id, payment_data, idempotency_key))
        if self.error is not None:
            raise self.error
        return completed(self.current)


def dependencies(
    tmp_path: Path,
    gateway: FakeGateway,
) -> tuple[SqliteAgentStore, ApprovalService, CompletionService]:
    store = SqliteAgentStore(tmp_path / "agent.db")
    store.initialize()
    store.create_purchase("run-1", "buy")
    store.update_purchase("run-1", stage="approved", checkout_id=gateway.current.id)
    approvals = ApprovalService(store, APPROVAL_SECRET, clock=lambda: NOW)
    service = CompletionService(
        gateway,
        approvals,
        FakePayments(),
        store,
    )
    return store, approvals, service


def approve(approvals: ApprovalService, current: CheckoutSession) -> None:
    terms = approvals.present(current)
    approvals.approve(
        run_id="run-1",
        actor_id="actor-1",
        expected_fingerprint=terms.fingerprint,
        checkout=current,
    )


def test_completion_without_approval_never_calls_complete(tmp_path: Path) -> None:
    gateway = FakeGateway(checkout())
    _, _, service = dependencies(tmp_path, gateway)

    with pytest.raises(ApprovalError) as captured:
        service.complete(run_id="run-1")

    assert captured.value.code == "approval_required"
    assert gateway.complete_calls == []


def test_changed_terms_never_call_complete(tmp_path: Path) -> None:
    original = checkout()
    gateway = FakeGateway(original)
    _, approvals, service = dependencies(tmp_path, gateway)
    approve(approvals, original)
    gateway.current = checkout(total=75_000)

    with pytest.raises(ApprovalError) as captured:
        service.complete(run_id="run-1")

    assert captured.value.code == "checkout_terms_changed"
    assert gateway.complete_calls == []


def test_success_persists_order_consumes_approval_and_replays_locally(tmp_path: Path) -> None:
    current = checkout()
    gateway = FakeGateway(current)
    store, approvals, service = dependencies(tmp_path, gateway)
    approve(approvals, current)

    result = service.complete(run_id="run-1")
    key = gateway.complete_calls[0][2]
    replay_store = SqliteAgentStore(tmp_path / "agent.db")
    replay_store.initialize()
    replay_gateway = FakeGateway(current)
    replay_service = CompletionService(
        replay_gateway,
        ApprovalService(replay_store, APPROVAL_SECRET, clock=lambda: NOW),
        FakePayments(),
        replay_store,
    )
    replay = replay_service.complete(run_id="run-1")

    assert result.order.id == replay.order.id == "ord_completion_1"
    assert replay_gateway.get_calls == 0
    assert replay_gateway.complete_calls == []
    assert replay_store.idempotency_key("run-1", "checkout.complete") == key
    assert store.purchase("run-1")["stage"] == "completed"  # type: ignore[index]
    assert store.purchase("run-1")["order_id"] == "ord_completion_1"  # type: ignore[index]
    assert store.approval("run-1").consumed_at is not None  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("error", "expected_stage"),
    [
        (ACPClientError("Declined", status_code=422, code="PAYMENT_DECLINED"), "approved"),
        (ACPClientError("Timed out", code="timeout"), "uncertain"),
    ],
)
def test_failure_keeps_key_and_approval_without_inventing_order(
    tmp_path: Path, error: ACPClientError, expected_stage: str
) -> None:
    current = checkout()
    gateway = FakeGateway(current, error=error)
    _, approvals, service = dependencies(tmp_path, gateway)
    approve(approvals, current)

    with pytest.raises(ACPClientError) as captured:
        service.complete(run_id="run-1")

    key = gateway.complete_calls[0][2]
    token = gateway.complete_calls[0][1].instrument.credential.token.get_secret_value().encode()
    reopened = SqliteAgentStore(tmp_path / "agent.db")
    reopened.initialize()
    assert captured.value is error
    assert reopened.idempotency_key("run-1", "checkout.complete") == key
    assert reopened.purchase("run-1")["stage"] == expected_stage  # type: ignore[index]
    assert reopened.purchase("run-1")["order_id"] is None  # type: ignore[index]
    assert reopened.approval("run-1").consumed_at is None  # type: ignore[union-attr]
    assert token not in (tmp_path / "agent.db").read_bytes()


def test_sandbox_token_is_stable_without_being_persisted(tmp_path: Path) -> None:
    current = checkout()
    gateway = FakeGateway(current, error=ACPClientError("Timed out", code="timeout"))
    _, approvals, service = dependencies(tmp_path, gateway)
    approve(approvals, current)

    with pytest.raises(ACPClientError):
        service.complete(run_id="run-1")
    first_payment = gateway.complete_calls[0][1]

    reopened = SqliteAgentStore(tmp_path / "agent.db")
    reopened.initialize()
    second_gateway = FakeGateway(current, error=ACPClientError("Timed out", code="timeout"))
    second_service = CompletionService(
        second_gateway,
        ApprovalService(reopened, APPROVAL_SECRET, clock=lambda: NOW),
        FakePayments(),
        reopened,
    )
    with pytest.raises(ACPClientError):
        second_service.complete(run_id="run-1")
    second_payment = second_gateway.complete_calls[0][1]

    first_token = first_payment.instrument.credential.token.get_secret_value()
    second_token = second_payment.instrument.credential.token.get_secret_value()
    assert first_token == second_token
    assert first_token.encode() not in (tmp_path / "agent.db").read_bytes()


def test_uncertain_completion_reconciles_from_authoritative_checkout(tmp_path: Path) -> None:
    current = checkout()
    gateway = FakeGateway(current, error=ACPClientError("Timed out", code="timeout"))
    store, approvals, service = dependencies(tmp_path, gateway)
    approve(approvals, current)

    with pytest.raises(ACPClientError):
        service.complete(run_id="run-1")

    reconciled_gateway = FakeGateway(
        CheckoutSession.model_validate(completed(current).model_dump(mode="json"))
    )
    reconciled = CompletionService(
        reconciled_gateway,
        ApprovalService(store, APPROVAL_SECRET, clock=lambda: NOW),
        FakePayments(),
        store,
    ).complete(run_id="run-1")

    assert reconciled.order.id == "ord_completion_1"
    assert reconciled_gateway.complete_calls == []
    assert store.purchase("run-1")["stage"] == "completed"  # type: ignore[index]
