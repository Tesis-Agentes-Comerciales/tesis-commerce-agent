import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from commerce_agent.acp import ACPClientError, ProductFeedError
from commerce_agent.acp.feed import ProductCandidate
from commerce_agent.api import create_app, error_response
from commerce_agent.config import Settings
from commerce_agent.domain import PurchaseIntent, RankedCandidate
from commerce_agent.llm import (
    CandidateDecision,
    CatalogRecovery,
    CommerceDecision,
    ConversationMessage,
)

FEED = Path(__file__).parent / "fixtures" / "feed"


def test_unavailable_merchant_feed_has_safe_service_error() -> None:
    response = error_response(
        ProductFeedError("bad product with untrusted details", code="invalid_product")
    )
    assert response.status_code == 503
    assert json.loads(bytes(response.body)) == {
        "code": "catalog_unavailable",
        "message": "El catálogo del comercio no está disponible en este momento.",
    }


def test_merchant_request_id_is_exposed_without_merchant_error_text() -> None:
    response = error_response(
        ACPClientError(
            "Merchant request failed with HTTP 409",
            status_code=409,
            code="idempotency_in_flight",
            request_id="merchant-req-123",
        )
    )
    assert json.loads(bytes(response.body)) == {
        "code": "idempotency_in_flight",
        "message": " ".join(
            (
                "El comercio sigue procesando esta compra.",
                "Espera un momento y consulta de nuevo.",
            )
        ),
        "merchant_request_id": "merchant-req-123",
    }


@pytest.mark.parametrize(
    ("code", "merchant_status", "agent_status", "message_fragment"),
    [
        ("payment_declined", 422, 422, "rechazado"),
        ("out_of_stock", 409, 409, "agotó"),
        ("PROVIDER_UNAVAILABLE", 503, 503, "proveedor de pagos"),
        ("idempotency_in_flight", 409, 409, "procesando"),
        ("timeout", None, 504, "confirmar"),
    ],
)
def test_merchant_failures_have_safe_actionable_api_responses(
    code: str, merchant_status: int | None, agent_status: int, message_fragment: str
) -> None:
    response = error_response(
        ACPClientError(
            "Merchant request failed with HTTP 999: private merchant detail",
            status_code=merchant_status,
            code=code,
        )
    )
    body = json.loads(bytes(response.body))
    assert response.status_code == agent_status
    assert body["code"] == code
    assert message_fragment in body["message"]
    assert "private merchant detail" not in body["message"]


def test_terminal_error_code_with_unexpected_status_stays_nonterminal() -> None:
    response = error_response(
        ACPClientError("Ambiguous merchant error", status_code=503, code="payment_declined")
    )
    assert response.status_code == 502
    assert json.loads(bytes(response.body))["code"] == "http_error"


class StubConversationRouter:
    def __init__(
        self,
        decision: CommerceDecision,
        candidate_decision: CandidateDecision | None = None,
        recovery: CatalogRecovery | None = None,
    ) -> None:
        self.decision = decision
        self.candidate_decision = candidate_decision
        self.recovery = recovery
        self.messages: tuple[ConversationMessage, ...] | None = None
        self.route_calls = 0

    def route(self, messages: tuple[ConversationMessage, ...]) -> CommerceDecision:
        self.route_calls += 1
        self.messages = messages
        return self.decision

    def choose_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        candidates: tuple[RankedCandidate, ...],
    ) -> CandidateDecision:
        assert messages
        assert intent == self.decision.intent
        assert candidates
        assert self.candidate_decision is not None
        return self.candidate_decision

    def recover_no_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        rejection_counts: dict[str, int],
        alternatives: tuple[ProductCandidate, ...],
    ) -> CatalogRecovery:
        assert messages and intent == self.decision.intent
        assert rejection_counts and alternatives
        assert self.recovery is not None
        return self.recovery


def checkout(status: str, *, with_details: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "cs_api_1",
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


def checkout_details() -> dict[str, Any]:
    return {
        "buyer": {
            "email": "buyer@example.test",
            "first_name": "Buyer",
            "last_name": "Test",
        },
        "fulfillment": {
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
    }


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
                        "supported_currencies": ["cop"],
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
        payment_agent_bearer_token=SecretStr("payment-agent-token-with-at-least-32-characters"),
        agent_database_path=tmp_path / "agent.db",
        agent_feed_source="file",
        agent_feed_path=FEED,
        agent_principal_id="human-api-1",
    )
    router = StubConversationRouter(
        CommerceDecision(
            action="purchase",
            assistant_message="Voy a preparar esos audífonos.",
            intent=PurchaseIntent(),
        ),
        CandidateDecision(
            action="purchase",
            assistant_message="Elegí los Sonora negros porque son la opción disponible.",
            selected_variant_id="SON-01",
        ),
    )
    app = create_app(
        settings=settings,
        transport=httpx.MockTransport(handler),
        payment_transport=httpx.MockTransport(
            lambda request: httpx.Response(201, json={"id": "vt_" + "a" * 64})
        ),
        conversation_router=router,
    )

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
            "/conversation/turn",
            json={
                "turn_id": "turn-api-purchase",
                "conversation_id": "conversation-api",
                "messages": [{"role": "user", "content": "Quiero los audífonos disponibles"}],
                "details": checkout_details(),
            },
        )
        assert start.status_code == 200, start.text
        payload = start.json()
        assert payload["action"] == "purchase"
        assert payload["assistant_message"].startswith("Elegí los Sonora")
        assert "intent" not in payload
        assert payload["approval_terms"]["merchant_origin"] == "http://127.0.0.1:4120"
        run_id = payload["purchase"]["run_id"]

        replay = client.post(
            "/conversation/turn",
            json={
                "turn_id": "turn-api-purchase",
                "conversation_id": "conversation-api",
                "messages": [{"role": "user", "content": "Quiero los audífonos disponibles"}],
                "details": checkout_details(),
            },
        )
        assert replay.status_code == 200
        assert replay.json() == payload
        assert router.route_calls == 1

        pending_turn = client.post(
            "/conversation/turn",
            json={
                "turn_id": "turn-api-pending",
                "conversation_id": "conversation-api",
                "messages": [{"role": "user", "content": "También quiero otros audífonos"}],
                "details": checkout_details(),
            },
        )
        assert pending_turn.status_code == 200
        assert pending_turn.json()["action"] == "clarify"
        assert router.route_calls == 1

        other_thread = client.post(
            "/conversation/turn",
            json={
                "turn_id": "turn-api-other-thread",
                "conversation_id": "conversation-other",
                "messages": [{"role": "user", "content": "También quiero otros audífonos"}],
                "details": checkout_details(),
            },
        )
        assert other_thread.status_code == 200
        assert other_thread.json() == pending_turn.json()
        assert router.route_calls == 1
        with sqlite3.connect(tmp_path / "agent.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM purchases").fetchone() == (1,)

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

    with sqlite3.connect(tmp_path / "agent.graph.db") as connection:
        completed_nodes = connection.execute(
            "SELECT COUNT(*) FROM writes WHERE channel = 'completion_result'"
        ).fetchone()
    assert completed_nodes == (1,)


def test_conversation_turn_can_reply_without_contacting_merchant(tmp_path: Path) -> None:
    def unexpected_request(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Conversation-only turns must not contact the merchant")

    settings = Settings(
        merchant_bearer_token=SecretStr("merchant-token"),
        merchant_webhook_secret=SecretStr("webhook-secret-with-at-least-32-characters"),
        agent_approval_secret=SecretStr("approval-secret-with-at-least-32-characters"),
        payment_agent_bearer_token=SecretStr("payment-agent-token-with-at-least-32-characters"),
        agent_database_path=tmp_path / "agent.db",
        agent_feed_source="file",
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
            "/conversation/turn",
            json={
                "turn_id": "turn-api-clarify",
                "messages": [{"role": "user", "content": "Quiero comprar algo"}],
                "details": checkout_details(),
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "action": "clarify",
        "assistant_message": "¿Qué producto estás buscando?",
        "purchase": None,
        "approval_terms": None,
    }
    assert router.messages == (ConversationMessage(role="user", content="Quiero comprar algo"),)


@pytest.mark.parametrize(
    ("overrides", "condition"),
    [
        ({}, {"governance_mode": "off", "hp_flow": "blocked"}),
        (
            {"agent_governance_mode": "shadow", "agent_hp_flow": "governed"},
            {"governance_mode": "shadow", "hp_flow": "governed"},
        ),
    ],
)
def test_health_reports_the_governance_condition(
    tmp_path: Path, overrides: dict[str, str], condition: dict[str, str]
) -> None:
    def unexpected_request(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Health must not contact the merchant")

    settings = Settings(
        merchant_bearer_token=SecretStr("merchant-token"),
        merchant_webhook_secret=SecretStr("webhook-secret-with-at-least-32-characters"),
        agent_approval_secret=SecretStr("approval-secret-with-at-least-32-characters"),
        payment_agent_bearer_token=SecretStr("payment-agent-token-with-at-least-32-characters"),
        agent_database_path=tmp_path / "agent.db",
        agent_feed_source="file",
        agent_feed_path=FEED,
        **overrides,  # pyright: ignore[reportArgumentType]
    )
    app = create_app(settings=settings, transport=httpx.MockTransport(unexpected_request))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", **condition}


def test_pending_purchase_can_be_canceled_through_api(tmp_path: Path) -> None:
    ready = checkout("ready_for_payment", with_details=True)
    canceled = deepcopy(ready)
    canceled["status"] = "canceled"

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
                        "supported_currencies": ["cop"],
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
        if request.url.path == "/checkout_sessions/cs_api_1/cancel":
            assert json.loads(request.content)["intent_trace"]["reason_code"] == "other"
            return httpx.Response(200, json=canceled)
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    settings = Settings(
        merchant_bearer_token=SecretStr("merchant-token"),
        merchant_webhook_secret=SecretStr("webhook-secret-with-at-least-32-characters"),
        agent_approval_secret=SecretStr("approval-secret-with-at-least-32-characters"),
        payment_agent_bearer_token=SecretStr("payment-agent-token-with-at-least-32-characters"),
        agent_database_path=tmp_path / "agent.db",
        agent_feed_source="file",
        agent_feed_path=FEED,
    )
    router = StubConversationRouter(
        CommerceDecision(
            action="purchase",
            assistant_message="Prepararé los audífonos.",
            intent=PurchaseIntent(),
        ),
        CandidateDecision(
            action="purchase",
            assistant_message="Elegí los Sonora.",
            selected_variant_id="SON-01",
        ),
    )
    app = create_app(
        settings=settings,
        transport=httpx.MockTransport(handler),
        conversation_router=router,
    )

    with TestClient(app) as client:
        prepared = client.post(
            "/conversation/turn",
            json={
                "turn_id": "turn-api-cancel",
                "messages": [{"role": "user", "content": "Quiero audífonos"}],
                "details": checkout_details(),
            },
        )
        run_id = prepared.json()["purchase"]["run_id"]
        blocked_body = {
            "turn_id": "turn-api-after-cancel",
            "conversation_id": "conversation-after-cancel",
            "messages": [{"role": "user", "content": "¿Qué más tienes?"}],
            "details": checkout_details(),
        }
        blocked = client.post("/conversation/turn", json=blocked_body)
        assert blocked.status_code == 200
        assert blocked.json()["action"] == "clarify"
        assert router.route_calls == 1
        response = client.post(f"/purchases/{run_id}/cancel")
        replay = client.post(f"/purchases/{run_id}/cancel")
        router.decision = CommerceDecision(action="respond", assistant_message="Podemos seguir.")
        after_cancel = client.post("/conversation/turn", json=blocked_body)

    assert response.status_code == 200
    assert response.json()["stage"] == "canceled"
    assert replay.status_code == 200
    assert after_cancel.status_code == 200
    assert after_cancel.json()["action"] == "respond"
    assert router.route_calls == 2
    with sqlite3.connect(tmp_path / "agent.graph.db") as connection:
        resumed_nodes = connection.execute(
            "SELECT COUNT(*) FROM writes WHERE channel = 'approval_action'"
        ).fetchone()
    assert resumed_nodes == (1,)


def test_candidate_choice_can_ask_for_clarification_without_checkout(tmp_path: Path) -> None:
    def unexpected_request(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Clarification must not contact the merchant")

    settings = Settings(
        merchant_bearer_token=SecretStr("merchant-token"),
        merchant_webhook_secret=SecretStr("webhook-secret-with-at-least-32-characters"),
        agent_approval_secret=SecretStr("approval-secret-with-at-least-32-characters"),
        payment_agent_bearer_token=SecretStr("payment-agent-token-with-at-least-32-characters"),
        agent_database_path=tmp_path / "agent.db",
        agent_feed_source="file",
        agent_feed_path=FEED,
    )
    router = StubConversationRouter(
        CommerceDecision(
            action="purchase",
            assistant_message="Buscaré opciones.",
            intent=PurchaseIntent(),
        ),
        CandidateDecision(
            action="clarify",
            assistant_message="Solo tengo audífonos negros. ¿Te sirven?",
        ),
    )
    app = create_app(
        settings=settings,
        transport=httpx.MockTransport(unexpected_request),
        conversation_router=router,
    )

    with TestClient(app) as client:
        response = client.post(
            "/conversation/turn",
            json={
                "turn_id": "turn-api-candidate-clarify",
                "messages": [{"role": "user", "content": "Busco audífonos azules"}],
                "details": checkout_details(),
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "action": "clarify",
        "assistant_message": "Solo tengo audífonos negros. ¿Te sirven?",
        "purchase": None,
        "approval_terms": None,
    }


def test_no_candidate_returns_catalog_alternatives_instead_of_an_error(tmp_path: Path) -> None:
    def unexpected_request(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Catalog recovery must not contact the merchant")

    settings = Settings(
        merchant_bearer_token=SecretStr("merchant-token"),
        merchant_webhook_secret=SecretStr("webhook-secret-with-at-least-32-characters"),
        agent_approval_secret=SecretStr("approval-secret-with-at-least-32-characters"),
        payment_agent_bearer_token=SecretStr("payment-agent-token-with-at-least-32-characters"),
        agent_database_path=tmp_path / "agent.db",
        agent_feed_source="file",
        agent_feed_path=FEED,
    )
    intent = PurchaseIntent.model_validate({"hard": {"max_unit_amount_minor": 1}})
    router = StubConversationRouter(
        CommerceDecision(
            action="purchase",
            assistant_message="Buscaré opciones.",
            intent=intent,
        ),
        recovery=CatalogRecovery(
            action="clarify",
            assistant_message="No hay opciones en ese presupuesto; tengo Sonora por 720 USD.",
        ),
    )
    app = create_app(
        settings=settings,
        transport=httpx.MockTransport(unexpected_request),
        conversation_router=router,
    )

    with TestClient(app) as client:
        response = client.post(
            "/conversation/turn",
            json={
                "turn_id": "turn-api-recovery",
                "messages": [{"role": "user", "content": "Quiero audífonos por un centavo"}],
                "details": checkout_details(),
            },
        )

    assert response.status_code == 200
    assert response.json()["action"] == "clarify"
    assert response.json()["purchase"] is None
