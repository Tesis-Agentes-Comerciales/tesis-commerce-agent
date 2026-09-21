import hashlib
import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.webhooks import WEBHOOK_PATH, create_webhook_app

NOW = 1_800_000_000
SECRET = "webhook-test-secret"


def order_event(*, status: str = "confirmed") -> dict[str, object]:
    return {
        "type": "order_create",
        "data": {
            "type": "order",
            "id": "ord_1",
            "checkout_session_id": "cs_1",
            "permalink_url": "https://merchant.example/orders/ord_1",
            "status": status,
        },
    }


def raw_event(*, status: str = "confirmed") -> bytes:
    return json.dumps(order_event(status=status), separators=(",", ":")).encode()


def signature(body: bytes, *, timestamp: int = NOW, secret: str = SECRET) -> str:
    digest = hmac.new(
        secret.encode(),
        str(timestamp).encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


@pytest.fixture
def receiver(tmp_path: Path) -> tuple[TestClient, SqliteAgentStore]:
    store = SqliteAgentStore(tmp_path / "agent.db")
    store.initialize()
    store.create_purchase("run-1", "buy")
    store.update_purchase("run-1", stage="approved", checkout_id="cs_1")
    app = create_webhook_app(store=store, secret=SECRET, clock=lambda: NOW)
    return TestClient(app), store


def post(
    client: TestClient,
    body: bytes,
    *,
    merchant_signature: str | None = None,
    request_id: str = "whd_1",
):
    headers = {"Content-Type": "application/json", "Request-Id": request_id}
    if merchant_signature is not None:
        headers["Merchant-Signature"] = merchant_signature
    return client.post(WEBHOOK_PATH, content=body, headers=headers)


def test_accepts_signed_order_and_projects_it(
    receiver: tuple[TestClient, SqliteAgentStore],
) -> None:
    client, store = receiver
    body = raw_event()

    response = post(client, body, merchant_signature=signature(body))

    assert response.status_code == 200
    assert response.json() == {"received": True, "request_id": "whd_1"}
    assert store.order("ord_1") == order_event()


@pytest.mark.parametrize(
    "merchant_signature",
    [None, "", "bad", "t=1800000000,v1=short"],
)
def test_rejects_missing_or_malformed_signature(
    receiver: tuple[TestClient, SqliteAgentStore], merchant_signature: str | None
) -> None:
    client, _ = receiver
    body = raw_event()

    response = post(client, body, merchant_signature=merchant_signature)

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_signature"


def test_rejects_incorrect_signature(receiver: tuple[TestClient, SqliteAgentStore]) -> None:
    client, _ = receiver
    body = raw_event()

    response = post(client, body, merchant_signature=signature(body, secret="wrong"))

    assert response.status_code == 401


@pytest.mark.parametrize("timestamp", [NOW - 301, NOW + 301])
def test_rejects_timestamp_outside_past_or_future_window(
    receiver: tuple[TestClient, SqliteAgentStore], timestamp: int
) -> None:
    client, _ = receiver
    body = raw_event()

    response = post(client, body, merchant_signature=signature(body, timestamp=timestamp))

    assert response.status_code == 401


def test_rejects_body_changed_after_signing(receiver: tuple[TestClient, SqliteAgentStore]) -> None:
    client, _ = receiver
    original = raw_event()
    altered = raw_event(status="processing")

    response = post(client, altered, merchant_signature=signature(original))

    assert response.status_code == 401


def test_replay_same_body_with_different_request_id_is_idempotent(
    receiver: tuple[TestClient, SqliteAgentStore],
) -> None:
    client, store = receiver
    body = raw_event()
    signed = signature(body)

    first = post(client, body, merchant_signature=signed, request_id="whd_1")
    replay = post(client, body, merchant_signature=signed, request_id="whd_2")

    assert first.status_code == replay.status_code == 200
    assert replay.json() == {"received": True, "request_id": "whd_2"}
    assert store.order("ord_1") == order_event()


def test_authenticates_before_rejecting_invalid_payload(
    receiver: tuple[TestClient, SqliteAgentStore],
) -> None:
    client, store = receiver
    body = b'{"type":"order_create","data":{"id":"ord_1"}}'

    unsigned = post(client, body)
    signed = post(client, body, merchant_signature=signature(body))

    assert unsigned.status_code == 401
    assert signed.status_code == 400
    assert signed.json()["code"] == "invalid_payload"
    assert store.order("ord_1") is None


def test_processing_order_does_not_regress_to_confirmed(
    receiver: tuple[TestClient, SqliteAgentStore],
) -> None:
    client, store = receiver
    processing = raw_event(status="processing")
    confirmed = raw_event(status="confirmed")

    assert post(client, processing, merchant_signature=signature(processing)).status_code == 200
    assert (
        post(
            client,
            confirmed,
            merchant_signature=signature(confirmed),
            request_id="whd_2",
        ).status_code
        == 200
    )
    assert store.order("ord_1") == order_event(status="processing")


def test_rejects_wrong_content_type_and_oversized_body(
    receiver: tuple[TestClient, SqliteAgentStore],
) -> None:
    client, _ = receiver
    body = raw_event()
    wrong_type = client.post(
        WEBHOOK_PATH,
        content=body,
        headers={"Content-Type": "text/plain", "Merchant-Signature": signature(body)},
    )
    oversized = b"{" + b" " * 1_048_576

    too_large = post(client, oversized, merchant_signature=signature(oversized))

    assert wrong_type.status_code == 415
    assert too_large.status_code == 413
