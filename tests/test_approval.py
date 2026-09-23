from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from commerce_agent.acp.models import CheckoutSession
from commerce_agent.approval import ApprovalError, ApprovalService
from commerce_agent.persistence import ApprovalRecord, SqliteAgentStore

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def checkout(*, total: int = 74_000, status: str = "ready_for_payment") -> CheckoutSession:
    return CheckoutSession.model_validate(
        {
            "id": "cs_approval_1",
            "protocol": {"version": "2026-04-17"},
            "status": status,
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
            "buyer": {"email": "buyer@example.test"},
            "fulfillment_details": {
                "name": "Buyer Test",
                "email": "buyer@example.test",
                "phone_number": "+573000000000",
                "address": {
                    "name": "Buyer Test",
                    "line_one": "Calle 100 # 10-20",
                    "city": "Bogota",
                    "state": "DC",
                    "country": "CO",
                    "postal_code": "110111",
                },
            },
            "messages": [],
            "links": [],
            "created_at": "2026-09-20T11:00:00Z",
            "updated_at": "2026-09-20T11:30:00Z",
            "expires_at": "2026-09-20T13:00:00Z",
        }
    )


def service(tmp_path: Path) -> tuple[SqliteAgentStore, ApprovalService]:
    store = SqliteAgentStore(tmp_path / "agent.db")
    store.initialize()
    store.create_purchase("run-1", "buy")
    approval = ApprovalService(
        store,
        SecretStr("approval-secret-with-at-least-32-characters"),
        clock=lambda: NOW,
    )
    return store, approval


def test_approval_binds_human_action_to_exact_displayed_terms(tmp_path: Path) -> None:
    _, approval = service(tmp_path)
    current = checkout()
    terms = approval.present(current)
    record = approval.approve(
        run_id="run-1",
        actor_id="actor-1",
        expected_fingerprint=terms.fingerprint,
        checkout=current,
    )

    assert record.checkout_fingerprint == terms.fingerprint
    assert approval.authorize(run_id="run-1", checkout=current) == record


def test_changed_total_invalidates_approval(tmp_path: Path) -> None:
    _, approval = service(tmp_path)
    original = checkout()
    approval.approve(
        run_id="run-1",
        actor_id="actor-1",
        expected_fingerprint=approval.present(original).fingerprint,
        checkout=original,
    )

    with pytest.raises(ApprovalError) as captured:
        approval.authorize(run_id="run-1", checkout=checkout(total=75_000))
    assert captured.value.code == "checkout_terms_changed"


def test_changed_delivery_identity_invalidates_approval(tmp_path: Path) -> None:
    _, approval = service(tmp_path)
    original = checkout()
    approval.approve(
        run_id="run-1",
        actor_id="actor-1",
        expected_fingerprint=approval.present(original).fingerprint,
        checkout=original,
    )
    payload = original.model_dump(mode="json")
    payload["fulfillment_details"]["address"]["line_one"] = "Calle 101 # 10-20"

    with pytest.raises(ApprovalError) as captured:
        approval.authorize(
            run_id="run-1",
            checkout=CheckoutSession.model_validate(payload),
        )
    assert captured.value.code == "checkout_terms_changed"


def test_tampered_persisted_signature_is_rejected(tmp_path: Path) -> None:
    store, approval = service(tmp_path)
    current = checkout()
    valid = approval.approve(
        run_id="run-1",
        actor_id="actor-1",
        expected_fingerprint=approval.present(current).fingerprint,
        checkout=current,
    )
    store.save_approval(
        ApprovalRecord(
            run_id=valid.run_id,
            checkout_id=valid.checkout_id,
            checkout_fingerprint=valid.checkout_fingerprint,
            actor_id=valid.actor_id,
            approved_at=valid.approved_at,
            signature="0" * 64,
            consumed_at=None,
        )
    )

    with pytest.raises(ApprovalError) as captured:
        approval.authorize(run_id="run-1", checkout=current)
    assert captured.value.code == "invalid_approval"


def test_checkout_must_be_ready_and_unexpired(tmp_path: Path) -> None:
    _, approval = service(tmp_path)
    with pytest.raises(ApprovalError) as not_ready:
        approval.present(checkout(status="not_ready_for_payment"))
    assert not_ready.value.code == "checkout_not_ready"

    expired = checkout().model_copy(update={"expires_at": "2026-09-20T11:59:59Z"})
    with pytest.raises(ApprovalError) as expiration:
        approval.present(expired)
    assert expiration.value.code == "checkout_expired"
