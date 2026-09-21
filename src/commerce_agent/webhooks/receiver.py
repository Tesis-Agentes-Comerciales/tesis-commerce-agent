import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from commerce_agent.acp.schema import ACPContractError, validate_acp
from commerce_agent.config import Settings, get_settings
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.webhooks.signature import WebhookSignatureError, verify_signature

WEBHOOK_PATH = "/agentic_checkout/webhooks/order_events"
MAX_WEBHOOK_BODY_BYTES = 1_048_576


def create_webhook_app(
    *,
    store: SqliteAgentStore | None = None,
    secret: str | None = None,
    clock: Callable[[], float] = time.time,
    settings: Settings | None = None,
) -> FastAPI:
    configuration = settings or get_settings()
    resolved_secret = secret
    if resolved_secret is None and configuration.merchant_webhook_secret is not None:
        resolved_secret = configuration.merchant_webhook_secret.get_secret_value()
    if not resolved_secret:
        raise RuntimeError("MERCHANT_WEBHOOK_SECRET is required by the webhook receiver")

    resolved_store = store or SqliteAgentStore(configuration.agent_database_path)
    resolved_store.initialize()
    app = FastAPI(title="Tesis Commerce Agent", version="0.1.0")
    register_webhook_receiver(
        app,
        store=resolved_store,
        secret=resolved_secret,
        clock=clock,
    )
    return app


def register_webhook_receiver(
    app: FastAPI,
    *,
    store: SqliteAgentStore,
    secret: str,
    clock: Callable[[], float] = time.time,
) -> None:
    if not secret:
        raise RuntimeError("Webhook secret is required")

    @app.post(WEBHOOK_PATH)
    async def receive_order_event(request: Request) -> JSONResponse:
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            return _error(415, "invalid_content_type", "Content-Type must be application/json")

        try:
            raw_body = await _read_limited_body(request)
        except BodyTooLargeError:
            return _error(413, "payload_too_large", "Webhook payload exceeds the size limit")

        try:
            verify_signature(
                request.headers.get("merchant-signature"),
                secret=secret,
                raw_body=raw_body,
                now=clock(),
            )
        except WebhookSignatureError:
            return _error(401, "invalid_signature", "Webhook signature verification failed")

        try:
            payload = _parse_and_validate_event(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError, ACPContractError, ValueError):
            return _error(400, "invalid_payload", "Webhook payload violates the ACP contract")

        request_id = request.headers.get("request-id")
        if request_id is not None and len(request_id) > 256:
            return _error(400, "invalid_request_id", "Request-Id exceeds the size limit")
        data = payload["data"]
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        store.record_webhook(
            request_id=request_id,
            event_type=str(payload["type"]),
            order_id=str(data["id"]),
            payload_sha256=body_sha256,
            payload=payload,
        )
        response: dict[str, object] = {"received": True}
        if request_id is not None:
            response["request_id"] = request_id
        return JSONResponse(response)


class BodyTooLargeError(ValueError):
    pass


async def _read_limited_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_WEBHOOK_BODY_BYTES:
                raise BodyTooLargeError
        except ValueError as error:
            raise BodyTooLargeError from error

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_WEBHOOK_BODY_BYTES:
            raise BodyTooLargeError
    return bytes(body)


def _parse_and_validate_event(raw_body: bytes) -> dict[str, Any]:
    value = json.loads(raw_body.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != {"type", "data"}:
        raise ValueError("Webhook event must contain exactly type and data")
    event_type = value["type"]
    data = value["data"]
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("Webhook event type must be a non-empty string")
    if not isinstance(data, dict):
        raise ValueError("Webhook data must be an object")
    validate_acp("Order", data)
    if data.get("type") != "order" or not isinstance(data.get("status"), str):
        raise ValueError("Webhook data must be a full order projection")
    return value


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"type": "invalid_request", "code": code, "message": message},
        status_code=status_code,
    )
