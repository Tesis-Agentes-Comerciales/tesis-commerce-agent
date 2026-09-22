import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from threading import Lock
from typing import Literal

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from commerce_agent.acp import ACPClient, ACPClientError, load_product_feed
from commerce_agent.acp.models import CheckoutSessionWithOrder
from commerce_agent.application import CheckoutDetails, PurchasePreparation, PurchaseService
from commerce_agent.application.purchase import PurchaseServiceError
from commerce_agent.approval import ApprovalError, ApprovalService, ApprovalTerms
from commerce_agent.completion import CompletionError, CompletionService
from commerce_agent.config import Settings, get_settings
from commerce_agent.domain import rank_candidates
from commerce_agent.llm import (
    ConversationMessage,
    ConversationRouter,
    IntentInterpretationError,
    OpenAIConversationRouter,
)
from commerce_agent.payment import SandboxPaymentProvider
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.webhooks import register_webhook_receiver


class ConversationTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    turn_id: str = Field(min_length=1, max_length=128)
    messages: list[ConversationMessage] = Field(min_length=1, max_length=20)
    details: CheckoutDetails


class ConversationTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action: Literal["respond", "clarify", "purchase"]
    assistant_message: str = Field(min_length=1, max_length=1_000)
    purchase: PurchasePreparation | None = None
    approval_terms: ApprovalTerms | None = None

    @model_validator(mode="after")
    def validate_purchase_payload(self) -> "ConversationTurnResponse":
        has_purchase = self.purchase is not None and self.approval_terms is not None
        if self.action == "purchase" and not has_purchase:
            raise ValueError("Purchase responses require preparation and approval terms")
        if self.action != "purchase" and (
            self.purchase is not None or self.approval_terms is not None
        ):
            raise ValueError("Non-purchase responses cannot contain purchase data")
        return self


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


def _turn_request_sha256(body: ConversationTurnRequest) -> str:
    payload = body.model_dump(mode="json", exclude={"turn_id"})
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _turn_run_id(turn_id: str) -> str:
    digest = hashlib.sha256(f"purchase:{turn_id}".encode()).hexdigest()
    return f"run_{digest[:32]}"


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
    purchase_service = PurchaseService(
        feed=feed,
        checkout=client,
        store=store,
        approvals=approvals,
    )
    # ponytail: one local lock is enough for this single-process demo; use DB leases when scaling.
    conversation_turn_lock = Lock()
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
        )
        conversation_router = owned_conversation_router

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        client.close()
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

    def process_conversation_turn(body: ConversationTurnRequest) -> ConversationTurnResponse:
        request_sha256 = _turn_request_sha256(body)
        try:
            cached = store.claim_conversation_turn(body.turn_id, request_sha256)
        except ValueError as error:
            raise PurchaseServiceError(str(error), code="turn_conflict") from error
        if cached is not None:
            return ConversationTurnResponse.model_validate_json(json.dumps(cached))
        if body.messages[-1].role != "user":
            raise IntentInterpretationError("Conversation must end with a user message")
        if conversation_router is None:
            raise IntentInterpretationError("No conversation model is configured")
        decision = conversation_router.route(tuple(body.messages))
        if decision.action != "purchase":
            response = ConversationTurnResponse(
                action=decision.action,
                assistant_message=decision.assistant_message,
            )
            store.save_conversation_turn(
                body.turn_id, request_sha256, response.model_dump(mode="json")
            )
            return response
        request = decision.purchase_request or body.messages[-1].content
        intent = decision.intent
        if intent is None:
            raise IntentInterpretationError("Purchase decision is incomplete")
        selection = rank_candidates(feed, intent)
        if not selection.ranked:
            recovery = conversation_router.recover_no_candidate(
                tuple(body.messages),
                intent,
                selection.rejection_counts,
                feed.available_candidates[:10],
            )
            response = ConversationTurnResponse(
                action="clarify",
                assistant_message=recovery.assistant_message,
            )
            store.save_conversation_turn(
                body.turn_id, request_sha256, response.model_dump(mode="json")
            )
            return response
        candidate_decision = conversation_router.choose_candidate(
            tuple(body.messages),
            intent,
            selection.ranked[:10],
        )
        if candidate_decision.action == "clarify":
            response = ConversationTurnResponse(
                action="clarify",
                assistant_message=candidate_decision.assistant_message,
            )
            store.save_conversation_turn(
                body.turn_id, request_sha256, response.model_dump(mode="json")
            )
            return response
        selected_variant_id = candidate_decision.selected_variant_id
        if selected_variant_id is None:
            raise IntentInterpretationError("Candidate selection is incomplete")
        client.discover()
        purchase = purchase_service.start(
            run_id=_turn_run_id(body.turn_id),
            request=request,
            intent=intent,
            selected_variant_id=selected_variant_id,
            details=body.details,
        )
        response = ConversationTurnResponse(
            action="purchase",
            assistant_message=candidate_decision.assistant_message,
            purchase=purchase,
            approval_terms=purchase_service.approval_terms(purchase.run_id),
        )
        store.save_conversation_turn(body.turn_id, request_sha256, response.model_dump(mode="json"))
        return response

    @app.post("/conversation/turn", response_model=ConversationTurnResponse)
    def conversation_turn(body: ConversationTurnRequest) -> ConversationTurnResponse:
        with conversation_turn_lock:
            return process_conversation_turn(body)

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
