from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from commerce_agent.acp import ACPClient, ACPClientError, CheckoutUpdateRequest
from commerce_agent.acp.models import SelectedFulfillmentOption


def checkout(status: str = "not_ready_for_payment") -> dict[str, Any]:
    return {
        "id": "cs_test_001",
        "protocol": {"version": "2026-04-17"},
        "status": status,
        "currency": "usd",
        "capabilities": {"payment": {"handlers": []}},
        "line_items": [
            {
                "id": "li_test",
                "item": {
                    "id": "SON-01",
                    "name": "Audifonos Sonora SON-01",
                    "unit_amount": 72_000,
                },
                "quantity": 1,
                "totals": [
                    {"type": "subtotal", "display_text": "Producto", "amount": 72_000},
                    {"type": "total", "display_text": "Total", "amount": 72_000},
                ],
            }
        ],
        "totals": [{"type": "total", "display_text": "Total", "amount": 74000}],
        "fulfillment_options": [],
        "messages": [],
        "links": [],
        "created_at": "2026-09-20T12:00:00Z",
        "updated_at": "2026-09-20T12:00:00Z",
        "expires_at": "2026-09-20T12:15:00Z",
    }


def test_discovery_is_public_and_validated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/acp.json"
        assert "authorization" not in request.headers
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

    with ACPClient("http://127.0.0.1:4120", None, transport=httpx.MockTransport(handler)) as client:
        assert client.discover().protocol.version == "2026-04-17"


def test_create_supplies_trusted_headers_and_parses_total() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["API-Version"] == "2026-04-17"
        assert request.headers["Idempotency-Key"] == "backend-create-1"
        assert request.headers["Request-Id"] == "request-1"
        assert request.read().decode().find("test-token") == -1
        return httpx.Response(201, json=checkout())

    with ACPClient(
        "http://127.0.0.1:4120",
        SecretStr("test-token"),
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.create_checkout(
            "SON-01", idempotency_key="backend-create-1", request_id="request-1"
        )
    assert result.id == "cs_test_001"
    assert result.total_minor == 74000


def test_update_accepts_only_typed_supported_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/checkout_sessions/cs_test_001"
        assert b"line_items" not in request.read()
        return httpx.Response(200, json=checkout("ready_for_payment"))

    update = CheckoutUpdateRequest(
        selected_fulfillment_options=[
            SelectedFulfillmentOption(type="shipping", option_id="ship_1", item_ids=["li_test"])
        ]
    )
    with ACPClient(
        "http://127.0.0.1:4120",
        SecretStr("test-token"),
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.update_checkout("cs_test_001", update, idempotency_key="backend-update-1")
    assert result.status == "ready_for_payment"


def test_error_normalizes_nested_current_merchant_shape() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            headers={"Retry-After": "1"},
            json={"detail": {"code": "idempotency_in_flight", "message": "Try again."}},
        )

    with (
        ACPClient(
            "http://127.0.0.1:4120",
            SecretStr("test-token"),
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ACPClientError) as captured,
    ):
        client.get_checkout("cs_test_001")
    assert captured.value.code == "idempotency_in_flight"
    assert captured.value.status_code == 409
    assert captured.value.retry_after == "1"
    assert "Try again" not in str(captured.value)


def test_oversized_response_is_rejected_before_json_parsing() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))

    with (
        ACPClient(
            "http://127.0.0.1:4120",
            SecretStr("test-token"),
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ACPClientError) as captured,
    ):
        client.get_checkout("cs_test_001")
    assert captured.value.code == "response_too_large"


def test_authenticated_operation_fails_without_bearer() -> None:
    def unexpected_request(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Transport must not be called without authentication")

    with (
        ACPClient(
            "http://127.0.0.1:4120",
            None,
            transport=httpx.MockTransport(unexpected_request),
        ) as client,
        pytest.raises(ACPClientError, match="Bearer token") as captured,
    ):
        client.get_checkout("cs_test_001")
    assert captured.value.code == "missing_auth"
