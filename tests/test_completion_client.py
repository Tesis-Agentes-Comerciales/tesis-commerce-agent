import json
from typing import Any

import httpx
from pydantic import SecretStr

from commerce_agent.acp import ACPClient
from commerce_agent.acp.models import (
    PaymentCredential,
    SandboxPaymentData,
    SandboxPaymentInstrument,
)
from commerce_agent.acp.schema import validate_acp


def completed_checkout() -> dict[str, Any]:
    return {
        "id": "cs_test_001",
        "protocol": {"version": "2026-04-17"},
        "status": "completed",
        "currency": "usd",
        "capabilities": {"payment": {"handlers": []}},
        "line_items": [],
        "totals": [{"type": "total", "display_text": "Total", "amount": 74_000}],
        "fulfillment_options": [],
        "messages": [],
        "links": [],
        "created_at": "2026-09-20T12:00:00Z",
        "updated_at": "2026-09-20T12:01:00Z",
        "expires_at": "2026-09-20T12:15:00Z",
        "order": {
            "type": "order",
            "id": "ord_test_001",
            "checkout_session_id": "cs_test_001",
            "permalink_url": "http://127.0.0.1:4120/orders/ord_test_001",
            "status": "confirmed",
        },
    }


def test_complete_sends_schema_valid_payment_and_parses_schema_valid_order() -> None:
    token = "vt_" + "a" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/checkout_sessions/cs_test_001/complete"
        assert request.headers["Idempotency-Key"] == "trusted-complete-key"
        body = json.loads(request.read())
        validate_acp("CheckoutSessionCompleteRequest", body)
        assert body["payment_data"]["instrument"]["credential"]["token"] == token
        response = completed_checkout()
        validate_acp("CheckoutSessionWithOrder", response)
        return httpx.Response(200, json=response)

    payment = SandboxPaymentData(
        instrument=SandboxPaymentInstrument(credential=PaymentCredential(token=SecretStr(token)))
    )
    with ACPClient(
        "http://127.0.0.1:4120",
        SecretStr("bearer"),
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.complete_checkout(
            "cs_test_001", payment, idempotency_key="trusted-complete-key"
        )

    assert result.status == "completed"
    assert result.order.id == "ord_test_001"
