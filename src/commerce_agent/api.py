from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from commerce_agent.acp import ACPClient, ACPClientError, load_product_feed
from commerce_agent.acp.models import CheckoutSessionWithOrder
from commerce_agent.application import PurchaseService
from commerce_agent.application.conversation import (
    ConversationService,
    ConversationTurnRequest,
    ConversationTurnResponse,
)
from commerce_agent.application.purchase import PurchaseServiceError
from commerce_agent.approval import ApprovalError, ApprovalService, ApprovalTerms
from commerce_agent.assistant_transport import (
    IdFactory,
    default_id_factory,
    register_assistant_transport,
)
from commerce_agent.completion import CompletionError, CompletionService
from commerce_agent.config import Settings, get_settings
from commerce_agent.delegated_payment import DelegatedPaymentClient
from commerce_agent.llm import (
    ConversationRouter,
    IntentInterpretationError,
    OpenAIConversationRouter,
)
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.webhooks import register_webhook_receiver


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class PurchaseStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str
    stage: str
    checkout_id: str | None
    order_id: str | None


class ApprovalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str
    checkout_id: str
    principal_id: str
    approved_at: str
    stage: str = "approved"


_ACP_ERROR_RESPONSES: dict[str, tuple[int, str]] = {
    "payment_declined": (
        422,
        "El pago sandbox fue rechazado. Esta compra no se completó; inicia una nueva compra.",
    ),
    "out_of_stock": (409, "El producto se agotó antes de completar la compra. Elige otro."),
    "invalid_item": (404, "El producto ya no está disponible en el comercio."),
    "PROVIDER_UNAVAILABLE": (
        503,
        "El proveedor de pagos del comercio no está disponible. Puedes reintentar más tarde.",
    ),
    "idempotency_in_flight": (
        409,
        "El comercio sigue procesando esta compra. Espera un momento y consulta de nuevo.",
    ),
    "idempotency_conflict": (422, "El comercio rechazó un reintento inconsistente."),
    "timeout": (504, "No se pudo confirmar el resultado con el comercio. Consulta de nuevo."),
    "transport_error": (
        502,
        "No se pudo confirmar el resultado con el comercio. Consulta de nuevo.",
    ),
    "INVALID_INPUT": (400, "El comercio rechazó los datos de la compra."),
}


def error_response(error: Exception) -> JSONResponse:
    code = getattr(error, "code", "agent_error")
    if isinstance(error, ACPClientError):
        if code in {"payment_declined", "out_of_stock"} and (
            code,
            error.status_code,
        ) not in {("payment_declined", 422), ("out_of_stock", 409)}:
            code = "http_error"
        status, message = _ACP_ERROR_RESPONSES.get(
            code, (502, "El ecommerce no pudo procesar la solicitud.")
        )
        body = {"code": code, "message": message}
        if error.request_id is not None:
            body["merchant_request_id"] = error.request_id
        return JSONResponse(body, status_code=status)
    if code == "intent_model_error":
        status = 502
    elif code == "provider_unavailable":
        status = 503
    elif code == "invalid_provider_response":
        status = 502
    elif code == "delegation_rejected":
        status = 422
    else:
        status = 409 if code not in {"purchase_not_found", "checkout_missing"} else 404
    return JSONResponse({"code": code, "message": str(error)}, status_code=status)


def create_app(
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    payment_transport: httpx.BaseTransport | None = None,
    clock: Callable[[], datetime] | None = None,
    conversation_router: ConversationRouter | None = None,
    id_factory: IdFactory = default_id_factory,
) -> FastAPI:
    configuration = settings or get_settings()
    if configuration.merchant_bearer_token is None:
        raise RuntimeError("MERCHANT_BEARER_TOKEN is required")
    if configuration.merchant_webhook_secret is None:
        raise RuntimeError("MERCHANT_WEBHOOK_SECRET is required")
    if configuration.agent_approval_secret is None:
        raise RuntimeError("AGENT_APPROVAL_SECRET is required")
    if configuration.payment_agent_bearer_token is None:
        raise RuntimeError("PAYMENT_AGENT_BEARER_TOKEN is required")

    store = SqliteAgentStore(configuration.agent_database_path)
    store.initialize()
    feed = load_product_feed(configuration.agent_feed_path)
    client = ACPClient(
        configuration.merchant_base_url,
        configuration.merchant_bearer_token,
        timeout_seconds=configuration.merchant_timeout_seconds,
        transport=transport,
    )
    approval_clock = clock
    if approval_clock is None and configuration.agent_sandbox_clock_at is not None:
        sandbox_now = configuration.agent_sandbox_clock_at

        def fixed_sandbox_clock() -> datetime:
            return sandbox_now

        approval_clock = fixed_sandbox_clock
    approvals = ApprovalService(
        store,
        configuration.agent_approval_secret,
        merchant_origin=configuration.merchant_base_url,
        clock=approval_clock,
    )
    payments = DelegatedPaymentClient(
        configuration.payment_provider_url,
        configuration.payment_agent_bearer_token,
        transport=payment_transport,
    )
    completion = CompletionService(
        client,
        approvals,
        payments,
        store,
    )
    purchase_service = PurchaseService(
        feed=feed,
        checkout=client,
        store=store,
        approvals=approvals,
    )
    owned_conversation_router = None
    if (
        configuration.openai_api_key is not None
        and configuration.openai_model is not None
        and conversation_router is None
    ):
        owned_conversation_router = OpenAIConversationRouter(
            api_key=configuration.openai_api_key,
            model=configuration.openai_model,
            feed=feed,
            timeout_seconds=configuration.openai_timeout_seconds,
            reasoning_summary=configuration.openai_reasoning_summary,
            reasoning_effort=configuration.openai_reasoning_effort,
        )
        conversation_router = owned_conversation_router
    conversation = ConversationService(
        feed=feed,
        store=store,
        purchases=purchase_service,
        discover=client.discover,
        router=conversation_router,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        client.close()
        payments.close()
        if owned_conversation_router is not None:
            owned_conversation_router.close()

    app = FastAPI(title="Tesis Commerce Agent", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[configuration.agent_ui_origin],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    register_webhook_receiver(
        app,
        store=store,
        secret=configuration.merchant_webhook_secret.get_secret_value(),
    )

    @app.exception_handler(PurchaseServiceError)
    @app.exception_handler(ApprovalError)
    @app.exception_handler(CompletionError)
    @app.exception_handler(ACPClientError)
    @app.exception_handler(IntentInterpretationError)
    async def handled_error(_, error: Exception) -> JSONResponse:
        return error_response(error)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.state.assistant_transport = register_assistant_transport(
        app, service=conversation, new_id=id_factory
    )

    @app.post("/conversation/turn", response_model=ConversationTurnResponse)
    def conversation_turn(body: ConversationTurnRequest) -> ConversationTurnResponse:
        return conversation.respond(body)

    @app.get("/purchases/{run_id}", response_model=PurchaseStatus)
    def purchase_status(run_id: str) -> PurchaseStatus:
        purchase = store.purchase(run_id)
        if purchase is None:
            raise PurchaseServiceError("Purchase run does not exist", code="purchase_not_found")
        return PurchaseStatus(
            run_id=run_id,
            stage=str(purchase["stage"]),
            checkout_id=purchase["checkout_id"],
            order_id=purchase["order_id"],
        )

    @app.get("/purchases/{run_id}/approval", response_model=ApprovalTerms)
    def approval_terms(run_id: str) -> ApprovalTerms:
        return purchase_service.approval_terms(run_id)

    @app.post("/purchases/{run_id}/approval", response_model=ApprovalReceipt)
    def approve_purchase(run_id: str, body: ApprovalRequest) -> ApprovalReceipt:
        record = purchase_service.approve(
            run_id=run_id,
            actor_id=configuration.agent_principal_id,
            expected_fingerprint=body.expected_fingerprint,
        )
        return ApprovalReceipt(
            run_id=record.run_id,
            checkout_id=record.checkout_id,
            principal_id=record.actor_id,
            approved_at=record.approved_at,
        )

    @app.post("/purchases/{run_id}/cancel", response_model=PurchaseStatus)
    def cancel_purchase(run_id: str) -> PurchaseStatus:
        purchase_service.cancel(run_id=run_id)
        purchase = store.purchase(run_id)
        if purchase is None:
            raise PurchaseServiceError("Purchase run does not exist", code="purchase_not_found")
        return PurchaseStatus(
            run_id=run_id,
            stage=str(purchase["stage"]),
            checkout_id=purchase["checkout_id"],
            order_id=purchase["order_id"],
        )

    @app.post("/purchases/{run_id}/complete", response_model=CheckoutSessionWithOrder)
    def complete_purchase(run_id: str) -> CheckoutSessionWithOrder:
        return completion.complete(run_id=run_id)

    return app


def app_factory() -> FastAPI:
    return create_app()
