from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import cast
from uuid import uuid4

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from commerce_agent.acp import ACPClient, ACPClientError, load_product_feed
from commerce_agent.acp.models import CheckoutSessionWithOrder
from commerce_agent.application import CheckoutDetails, PurchasePreparation, PurchaseService
from commerce_agent.application.purchase import PurchaseServiceError
from commerce_agent.approval import ApprovalError, ApprovalService, ApprovalTerms
from commerce_agent.completion import CompletionError, CompletionService
from commerce_agent.config import Settings, get_settings
from commerce_agent.domain import PurchaseIntent
from commerce_agent.llm import (
    CommerceDecision,
    ConversationMessage,
    ConversationRouter,
    IntentInterpretationError,
    OpenAIConversationRouter,
    OpenAIIntentInterpreter,
)
from commerce_agent.payment import SandboxPaymentProvider
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.webhooks import register_webhook_receiver
from commerce_agent.workflow import build_selection_workflow
from commerce_agent.workflow.selection import SelectionOutput


class StartPurchaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request: str = Field(min_length=1, max_length=2_000)
    intent: PurchaseIntent | None = None
    details: CheckoutDetails | None = None


class RouteConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    messages: list[ConversationMessage] = Field(min_length=1, max_length=20)


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


class _ProvidedIntentInterpreter:
    def __init__(self, intent: PurchaseIntent) -> None:
        self._intent = intent

    def interpret(self, request: str) -> PurchaseIntent:
        if not request:
            raise ValueError("Purchase request must not be empty")
        return self._intent


def create_app(
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], datetime] | None = None,
    conversation_router: ConversationRouter | None = None,
) -> FastAPI:
    configuration = settings or get_settings()
    if configuration.merchant_bearer_token is None:
        raise RuntimeError("MERCHANT_BEARER_TOKEN is required")
    if configuration.merchant_webhook_secret is None:
        raise RuntimeError("MERCHANT_WEBHOOK_SECRET is required")
    if configuration.agent_approval_secret is None:
        raise RuntimeError("AGENT_APPROVAL_SECRET is required")
    if configuration.agent_payment_secret is None:
        raise RuntimeError("AGENT_PAYMENT_SECRET is required")

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
    completion = CompletionService(
        client,
        approvals,
        SandboxPaymentProvider(configuration.agent_payment_secret),
        store,
    )
    model_interpreter = None
    owned_conversation_router = None
    if configuration.openai_api_key is not None and configuration.openai_model is not None:
        model_interpreter = OpenAIIntentInterpreter(
            api_key=configuration.openai_api_key,
            model=configuration.openai_model,
            timeout_seconds=configuration.openai_timeout_seconds,
        )
        if conversation_router is None:
            owned_conversation_router = OpenAIConversationRouter(
                api_key=configuration.openai_api_key,
                model=configuration.openai_model,
                timeout_seconds=configuration.openai_timeout_seconds,
            )
            conversation_router = owned_conversation_router

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        client.close()
        if model_interpreter is not None:
            model_interpreter.close()
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
        code = getattr(error, "code", "agent_error")
        if code == "intent_model_error":
            status = 502
        else:
            status = 409 if code not in {"purchase_not_found", "checkout_missing"} else 404
        return JSONResponse({"code": code, "message": str(error)}, status_code=status)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/conversation/route", response_model=CommerceDecision)
    def route_conversation(body: RouteConversationRequest) -> CommerceDecision:
        if body.messages[-1].role != "user":
            raise IntentInterpretationError("Conversation must end with a user message")
        if conversation_router is None:
            raise IntentInterpretationError("No conversation model is configured")
        return conversation_router.route(tuple(body.messages))

    @app.post("/purchases", response_model=PurchasePreparation, status_code=201)
    def start_purchase(body: StartPurchaseRequest) -> PurchasePreparation:
        client.discover()
        interpreter = (
            _ProvidedIntentInterpreter(body.intent)
            if body.intent is not None
            else model_interpreter
        )
        if interpreter is None:
            raise IntentInterpretationError("No intent model is configured")
        workflow = build_selection_workflow(
            feed=feed,
            interpreter=interpreter,
        )

        def selection(request: str) -> SelectionOutput:
            return cast(SelectionOutput, workflow.invoke({"request": request}))

        service = PurchaseService(
            selection=selection,
            checkout=client,
            store=store,
            approvals=approvals,
        )
        return service.start(
            run_id=f"run_{uuid4().hex}",
            request=body.request,
            details=body.details,
        )

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

    def purchase_service() -> PurchaseService:
        def unavailable_selection(_: str) -> SelectionOutput:
            raise RuntimeError("Selection is unavailable after purchase creation")

        return PurchaseService(
            selection=unavailable_selection,
            checkout=client,
            store=store,
            approvals=approvals,
        )

    @app.get("/purchases/{run_id}/approval", response_model=ApprovalTerms)
    def approval_terms(run_id: str) -> ApprovalTerms:
        return purchase_service().approval_terms(run_id)

    @app.post("/purchases/{run_id}/approval", response_model=ApprovalReceipt)
    def approve_purchase(run_id: str, body: ApprovalRequest) -> ApprovalReceipt:
        record = purchase_service().approve(
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

    @app.post("/purchases/{run_id}/complete", response_model=CheckoutSessionWithOrder)
    def complete_purchase(run_id: str) -> CheckoutSessionWithOrder:
        return completion.complete(run_id=run_id)

    return app


def app_factory() -> FastAPI:
    return create_app()
