from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from commerce_agent.api import create_app
from commerce_agent.config import Settings
from commerce_agent.llm import CommerceDecision, ConversationMessage

FEED = Path(__file__).parent / "fixtures" / "feed"


class StubConversationRouter:
    def __init__(self, decision: CommerceDecision) -> None:
        self.decision = decision
        self.messages: tuple[ConversationMessage, ...] | None = None

    def route(self, messages: tuple[ConversationMessage, ...]) -> CommerceDecision:
        self.messages = messages
        return self.decision


def checkout(status: str, *, with_details: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "cs_api_1",
        "protocol": {"version": "2026-04-17"},
        "status": status,
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
        "totals": [{"type": "total", "display_text": "Total", "amount": 74_000}],
        "fulfillment_options": [
            {
                "type": "shipping",
                "id": "ship_1",
                "title": "Envio simulado a Bogota",
                "earliest_delivery_time": "2099-09-22T13:00:00Z",
                "latest_delivery_time": "2099-09-22T13:00:00Z",
                "totals": [{"type": "total", "display_text": "Envio", "amount": 2_000}],
            }
        ],
        "selected_fulfillment_options": [],
        "messages": [],
        "links": [],
        "created_at": "2026-09-20T11:00:00Z",
        "updated_at": "2026-09-20T11:30:00Z",
        "expires_at": "2099-09-20T13:00:00Z",
    }
    if with_details:
        payload["buyer"] = {
            "email": "buyer@example.test",
            "first_name": "Buyer",
            "last_name": "Test",
        }
        payload["fulfillment_details"] = {
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
        }
        payload["selected_fulfillment_options"] = [
            {"type": "shipping", "option_id": "ship_1", "item_ids": ["li_1"]}
        ]
    return payload


def test_api_exposes_explicit_human_gate_before_completion(tmp_path: Path) -> None:
    ready = checkout("ready_for_payment", with_details=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/acp.json":
            return httpx.Response(
                200,
                json={
                    "protocol": {
                        "name": "acp",
                        "version": "2026-04-17",
                        "supported_versions": ["2026-04-17"],
                    },
                    "api_base_url": "http://127.0.0.1:4120",
                    "transports": ["rest"],
                    "capabilities": {
                        "services": ["checkout"],
                        "supported_currencies": ["usd"],
                        "supported_locales": ["es-CO"],
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/checkout_sessions":
            return httpx.Response(201, json=checkout("not_ready_for_payment", with_details=False))
        if request.method == "POST" and request.url.path == "/checkout_sessions/cs_api_1":
            return httpx.Response(200, json=ready)
        if request.method == "GET" and request.url.path == "/checkout_sessions/cs_api_1":
            return httpx.Response(200, json=ready)
        if request.url.path == "/checkout_sessions/cs_api_1/complete":
            completed = deepcopy(ready)
            completed["status"] = "completed"
            completed["order"] = {
                "type": "order",
                "id": "ord_api_1",
                "checkout_session_id": "cs_api_1",
                "permalink_url": "http://127.0.0.1:4120/orders/ord_api_1",
                "status": "confirmed",
                "line_items": [],
                "totals": ready["totals"],
            }
            return httpx.Response(200, json=completed)
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    settings = Settings(
        merchant_bearer_token=SecretStr("merchant-token"),
        merchant_webhook_secret=SecretStr("webhook-secret-with-at-least-32-characters"),
        agent_approval_secret=SecretStr("approval-secret-with-at-least-32-characters"),
        agent_payment_secret=SecretStr("payment-secret-with-at-least-32-characters"),
        agent_database_path=tmp_path / "agent.db",
        agent_feed_path=FEED,
        agent_principal_id="human-api-1",
    )
    app = create_app(settings=settings, transport=httpx.MockTransport(handler))

    with TestClient(app) as client:
        preflight = client.options(
            "/health",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"

        start = client.post(
            "/purchases",
            json={
                "request": "Quiero los audifonos disponibles",
                "intent": {},
                "details": {
                    "buyer": {
                        "email": "buyer@example.test",
                        "first_name": "Buyer",
                        "last_name": "Test",
                    },
                    "fulfillment": ready["fulfillment_details"],
                },
            },
        )
        assert start.status_code == 201, start.text
        run_id = start.json()["run_id"]

        blocked = client.post(f"/purchases/{run_id}/complete")
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "invalid_stage"

        terms = client.get(f"/purchases/{run_id}/approval")
        assert terms.status_code == 200
        assert terms.json()["merchant_origin"] == "http://127.0.0.1:4120"
        assert terms.json()["fulfillment_details"]["address"]["city"] == "Bogota"

        approval = client.post(
            f"/purchases/{run_id}/approval",
            json={"expected_fingerprint": terms.json()["fingerprint"]},
        )
        assert approval.status_code == 200
        assert approval.json()["principal_id"] == "human-api-1"
        assert "signature" not in approval.json()

        completion = client.post(f"/purchases/{run_id}/complete")
        assert completion.status_code == 200
        assert completion.json()["order"]["id"] == "ord_api_1"
        assert client.get(f"/purchases/{run_id}").json()["stage"] == "completed"


def test_conversation_route_can_reply_without_contacting_merchant(tmp_path: Path) -> None:
    def unexpected_request(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Conversation-only turns must not contact the merchant")

    settings = Settings(
        merchant_bearer_token=SecretStr("merchant-token"),
        merchant_webhook_secret=SecretStr("webhook-secret-with-at-least-32-characters"),
        agent_approval_secret=SecretStr("approval-secret-with-at-least-32-characters"),
        agent_payment_secret=SecretStr("payment-secret-with-at-least-32-characters"),
        agent_database_path=tmp_path / "agent.db",
        agent_feed_path=FEED,
    )
    router = StubConversationRouter(
        CommerceDecision(
            action="clarify",
            assistant_message="¿Qué producto estás buscando?",
        )
    )
    app = create_app(
        settings=settings,
        transport=httpx.MockTransport(unexpected_request),
        conversation_router=router,
    )

    with TestClient(app) as client:
        response = client.post(
            "/conversation/route",
            json={"messages": [{"role": "user", "content": "Quiero comprar algo"}]},
        )

    assert response.status_code == 200
    assert response.json() == {
        "action": "clarify",
        "assistant_message": "¿Qué producto estás buscando?",
        "purchase_request": None,
        "intent": None,
    }
    assert router.messages == (ConversationMessage(role="user", content="Quiero comprar algo"),)
