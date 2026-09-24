import sqlite3
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr

from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.models import (
    CheckoutSession,
    CheckoutSessionWithOrder,
    PaymentCredential,
    SandboxPaymentData,
    SandboxPaymentInstrument,
)
from commerce_agent.ap2 import AgentMandateSigner
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
        self,
        *,
        checkout: CheckoutSession,
        idempotency_key: str,
        checkout_jwt: str | None = None,
        payment_mandate: str | None = None,
    ) -> SandboxPaymentData:
        assert checkout_jwt is None and payment_mandate is None
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
        "currency": "cop",
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

    def complete_checkout_ap2(
        self,
        checkout_id: str,
        payment_data: SandboxPaymentData,
        checkout_mandate: str,
        *,
        idempotency_key: str,
    ) -> tuple[CheckoutSessionWithOrder, str]:
        raise AssertionError("Legacy tests must not use AP2 completion")


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


def test_ap2_completion_uses_approved_mandates_and_persists_both_receipts(
    tmp_path: Path,
) -> None:
    current = checkout()
    store = SqliteAgentStore(tmp_path / "ap2.db")
    store.initialize()
    store.create_purchase("run-ap2", "headphones")
    store.update_purchase("run-ap2", stage="approved", checkout_id=current.id)
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    approvals = ApprovalService(
        store,
        APPROVAL_SECRET,
        clock=lambda: NOW,
        mandate_signer=AgentMandateSigner(
            pem,
            merchant_id="tesis_merchant",
            merchant_name="Merchant",
            clock=lambda: NOW,
        ),
    )
    approved = approvals.approve(
        run_id="run-ap2",
        actor_id="buyer-1",
        expected_fingerprint=approvals.present(current).fingerprint,
        checkout=current,
        checkout_jwt="signed-checkout",
    )

    class Ap2Payments(FakePayments):
        def payment_data(
            self,
            *,
            checkout: CheckoutSession,
            idempotency_key: str,
            checkout_jwt: str | None = None,
            payment_mandate: str | None = None,
        ) -> SandboxPaymentData:
            assert checkout == current
            assert checkout_jwt == approved.checkout_jwt
            assert payment_mandate == approved.payment_mandate
            assert idempotency_key
            return SandboxPaymentData(
                instrument=SandboxPaymentInstrument(
                    credential=PaymentCredential(token=SecretStr(TEST_TOKEN))
                )
            )

        def payment_receipt(self, *, token: str, mandate: str) -> str:
            assert token == TEST_TOKEN
            assert mandate == approved.payment_mandate
            return "signed-payment-receipt"

    class Ap2Gateway(FakeGateway):
        def complete_checkout(self, *args: Any, **kwargs: Any) -> CheckoutSessionWithOrder:
            raise AssertionError("AP2 must never use legacy completion")

        def complete_checkout_ap2(
            self,
            checkout_id: str,
            payment_data: SandboxPaymentData,
            checkout_mandate: str,
            *,
            idempotency_key: str,
        ) -> tuple[CheckoutSessionWithOrder, str]:
            assert checkout_id == current.id
            assert payment_data.instrument.credential.token.get_secret_value() == TEST_TOKEN
            assert checkout_mandate == approved.checkout_mandate
            assert idempotency_key
            self.complete_calls.append((checkout_id, payment_data, idempotency_key))
            return completed(current), "signed-checkout-receipt"

    gateway = Ap2Gateway(current)
    service = CompletionService(gateway, approvals, Ap2Payments(), store)
    result = service.complete(run_id="run-ap2")
    assert result.order.id == "ord_completion_1"
    assert len(gateway.complete_calls) == 1
    assert service.complete(run_id="run-ap2") == result
    assert len(gateway.complete_calls) == 1
    with sqlite3.connect(tmp_path / "ap2.db") as connection:
        receipts = connection.execute(
            "SELECT checkout_receipt_jwt, payment_receipt_jwt FROM completion_results"
        ).fetchone()
    assert receipts == ("signed-checkout-receipt", "signed-payment-receipt")


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
        (ACPClientError("Declined", status_code=422, code="payment_declined"), "failed"),
        (ACPClientError("Out of stock", status_code=409, code="out_of_stock"), "failed"),
        (ACPClientError("Ambiguous", status_code=503, code="payment_declined"), "uncertain"),
        (ACPClientError("Timed out", code="timeout"), "uncertain"),
        (
            ACPClientError(
                "Still processing",
                status_code=409,
                code="idempotency_in_flight",
                retry_after="1",
            ),
            "uncertain",
        ),
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


@pytest.mark.parametrize(
    "error",
    [
        ACPClientError("Timed out", code="timeout"),
        ACPClientError("Still processing", status_code=409, code="idempotency_in_flight"),
    ],
)
def test_uncertain_completion_reconciles_from_authoritative_checkout(
    tmp_path: Path, error: ACPClientError
) -> None:
    current = checkout()
    gateway = FakeGateway(current, error=error)
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
