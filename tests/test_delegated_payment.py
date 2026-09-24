import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from commerce_agent.acp.models import CheckoutSession
from commerce_agent.delegated_payment import DelegatedPaymentClient, DelegationError


def checkout() -> CheckoutSession:
    payload: dict[str, Any] = {
        "id": "cs_test_1",
        "protocol": {"version": "2026-04-17"},
        "status": "ready_for_payment",
        "currency": "cop",
        "capabilities": {"payment": {"handlers": []}},
        "line_items": [],
        "totals": [{"type": "total", "display_text": "Total", "amount": 62000}],
        "fulfillment_options": [],
        "messages": [],
        "links": [],
        "created_at": "2026-09-10T14:00:00Z",
        "updated_at": "2026-09-10T14:01:00Z",
        "expires_at": "2026-09-10T14:15:00Z",
    }
    return CheckoutSession.model_validate(payload)


def test_delegation_uses_checkout_scope_and_returns_opaque_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/agentic_commerce/delegate_payment"
        assert request.headers["Authorization"] == "Bearer agent-payment-bearer"
        assert request.headers["API-Version"] == "2026-04-17"
        assert request.headers["Idempotency-Key"] == "delegate-1"
        body = json.loads(request.read())
        assert body["allowance"] == {
            "reason": "one_time",
            "max_amount": 62000,
            "currency": "cop",
            "checkout_session_id": "cs_test_1",
            "merchant_id": "tesis_merchant",
            "expires_at": "2026-09-10T14:15:00+00:00",
        }
        assert body["payment_method"]["number"] == "4242424242424242"
        return httpx.Response(201, json={"id": "vt_" + "a" * 64})

    with httpx.MockTransport(handler) as transport:
        client = DelegatedPaymentClient(
            "http://127.0.0.1:4130",
            SecretStr("agent-payment-bearer"),
            transport=transport,
        )
        result = client.payment_data(checkout=checkout(), idempotency_key="delegate-1")
        client.close()
    assert result.instrument.credential.type == "vault_token"
    assert result.instrument.credential.token.get_secret_value() == "vt_" + "a" * 64


def test_delegation_rejection_never_constructs_payment_data() -> None:
    client = DelegatedPaymentClient(
        "http://127.0.0.1:4130",
        SecretStr("agent-payment-bearer"),
        transport=httpx.MockTransport(lambda _: httpx.Response(422)),
    )
    with pytest.raises(DelegationError) as captured:
        client.payment_data(checkout=checkout(), idempotency_key="delegate-1")
    client.close()
    assert captured.value.code == "delegation_rejected"
