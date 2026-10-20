import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from commerce_agent.acp import ACPClientError, ProductFeedError
from commerce_agent.acp.models import CheckoutSessionWithOrder
from commerce_agent.application.conversation import (
    ConversationTurnRequest,
    ConversationTurnResponse,
)
from commerce_agent.application.purchase import PurchaseServiceError
from commerce_agent.approval import ApprovalError, ApprovalTerms
from commerce_agent.assistant_transport import (
    IdFactory,
    default_id_factory,
    register_assistant_transport,
)
from commerce_agent.buyer_auth import (
    BuyerAuthenticator,
    BuyerPrincipal,
    JwtBuyerAuthenticator,
    LocalBuyerAuthenticator,
)
from commerce_agent.completion import CompletionError
from commerce_agent.config import Settings, get_settings
from commerce_agent.llm import ConversationRouter, IntentInterpretationError
from commerce_agent.runtime import build_runtime
from commerce_agent.webhooks import register_webhook_receiver


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    checkout_jwt: str | None = Field(default=None, max_length=32_768)


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


class PendingPurchase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    purchase: PurchaseStatus
    approval_terms: ApprovalTerms | None


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
    if isinstance(error, ProductFeedError):
        return JSONResponse(
            {
                "code": "catalog_unavailable",
                "message": "El catálogo del comercio no está disponible en este momento.",
            },
            status_code=503,
        )
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
    elif code in {"provider_unavailable", "offer_freshness_unavailable"}:
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
    buyer_authenticator: BuyerAuthenticator | None = None,
) -> FastAPI:
    configuration = settings or get_settings()
    if configuration.agent_auth_mode == "local" and (
        os.getenv("RAILWAY_ENVIRONMENT_ID")
        or urlparse(configuration.agent_ui_origin).scheme == "https"
    ):
        raise RuntimeError("Public deployment requires AGENT_AUTH_MODE=jwt")
    authenticator = buyer_authenticator or (
        JwtBuyerAuthenticator(configuration.agent_auth_issuer or "")
        if configuration.agent_auth_mode == "jwt"
        else LocalBuyerAuthenticator(configuration.agent_principal_id)
    )
    runtime = build_runtime(
        configuration,
        transport=transport,
        payment_transport=payment_transport,
        clock=clock,
        conversation_router=conversation_router,
    )
    store = runtime.store
    purchase_service = runtime.purchases
    conversation = runtime.conversation

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        runtime.close()

    app = FastAPI(title="Tesis Commerce Agent", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[configuration.agent_ui_origin],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
    )
    webhook_secret = configuration.merchant_webhook_secret
    assert webhook_secret is not None
    register_webhook_receiver(
        app,
        store=store,
        secret=webhook_secret.get_secret_value(),
    )

    @app.exception_handler(PurchaseServiceError)
    @app.exception_handler(ApprovalError)
    @app.exception_handler(CompletionError)
    @app.exception_handler(ACPClientError)
    @app.exception_handler(ProductFeedError)
    @app.exception_handler(IntentInterpretationError)
    async def handled_error(_, error: Exception) -> JSONResponse:
        return error_response(error)

    @app.get("/health")
    def health() -> dict[str, str]:
        # Evaluation runners check the condition they started before each repetition.
        return {
            "status": "ok",
            "governance_mode": configuration.agent_governance_mode,
            "hp_flow": configuration.agent_hp_flow,
        }

    @app.get("/.well-known/ap2/jwks.json")
    def ap2_jwks() -> dict[str, object]:
        if runtime.mandate_signer is None:
            raise HTTPException(status_code=503, detail="AP2 signer is unavailable")
        return {"keys": [runtime.mandate_signer.public_jwk]}

    def current_buyer(
        authorization: Annotated[str | None, Header()] = None,
    ) -> BuyerPrincipal:
        principal = authenticator.authenticate(authorization)
        if principal is None:
            raise HTTPException(status_code=401, detail="Buyer authentication required")
        return principal

    def owned_purchase(run_id: str, buyer: BuyerPrincipal) -> dict[str, str | None]:
        purchase = store.purchase_for_owner(run_id, buyer.id)
        if purchase is None:
            raise PurchaseServiceError("Purchase run does not exist", code="purchase_not_found")
        return purchase

    app.state.assistant_transport = register_assistant_transport(
        app, service=conversation, new_id=id_factory, current_buyer=current_buyer
    )

    @app.post("/conversation/turn", response_model=ConversationTurnResponse)
    def conversation_turn(
        body: ConversationTurnRequest,
        buyer: Annotated[BuyerPrincipal, Depends(current_buyer)],
    ) -> ConversationTurnResponse:
        return conversation.respond(body, owner_id=buyer.id)

    @app.get("/me/pending-purchase", response_model=PendingPurchase | None)
    def pending_purchase(
        buyer: Annotated[BuyerPrincipal, Depends(current_buyer)],
    ) -> PendingPurchase | None:
        view = conversation.pending_purchase_view(buyer.id)
        if view is None:
            return None
        run_id = view["run_id"]
        purchase = owned_purchase(run_id, buyer)
        snapshots = conversation.approval_terms_snapshots([run_id], buyer.id)
        terms = snapshots.get(run_id)
        if purchase["stage"] == "awaiting_confirmation" and (
            terms is None or runtime.mandate_signer is not None
        ):
            terms = purchase_service.approval_terms(run_id).model_dump(mode="json")
            store.save_approval_terms(run_id, terms)
        return PendingPurchase(
            purchase=PurchaseStatus(
                run_id=run_id,
                stage=str(purchase["stage"]),
                checkout_id=purchase["checkout_id"],
                order_id=purchase["order_id"],
            ),
            approval_terms=(
                ApprovalTerms.model_validate_json(json.dumps(terms))
                if terms is not None and purchase["stage"] == "awaiting_confirmation"
                else None
            ),
        )

    @app.get("/purchases/{run_id}", response_model=PurchaseStatus)
    def purchase_status(
        run_id: str, buyer: Annotated[BuyerPrincipal, Depends(current_buyer)]
    ) -> PurchaseStatus:
        purchase = owned_purchase(run_id, buyer)
        return PurchaseStatus(
            run_id=run_id,
            stage=str(purchase["stage"]),
            checkout_id=purchase["checkout_id"],
            order_id=purchase["order_id"],
        )

    @app.get("/purchases/{run_id}/approval", response_model=ApprovalTerms)
    def approval_terms(
        run_id: str, buyer: Annotated[BuyerPrincipal, Depends(current_buyer)]
    ) -> ApprovalTerms:
        owned_purchase(run_id, buyer)
        return purchase_service.approval_terms(run_id)

    @app.post("/purchases/{run_id}/approval", response_model=ApprovalReceipt)
    def approve_purchase(
        run_id: str,
        body: ApprovalRequest,
        buyer: Annotated[BuyerPrincipal, Depends(current_buyer)],
    ) -> ApprovalReceipt:
        owned_purchase(run_id, buyer)
        record = purchase_service.approve(
            run_id=run_id,
            actor_id=buyer.id,
            expected_fingerprint=body.expected_fingerprint,
            checkout_jwt=body.checkout_jwt,
        )
        return ApprovalReceipt(
            run_id=record.run_id,
            checkout_id=record.checkout_id,
            principal_id=record.actor_id,
            approved_at=record.approved_at,
        )

    @app.post("/purchases/{run_id}/cancel", response_model=PurchaseStatus)
    def cancel_purchase(
        run_id: str, buyer: Annotated[BuyerPrincipal, Depends(current_buyer)]
    ) -> PurchaseStatus:
        owned_purchase(run_id, buyer)
        purchase_service.cancel(run_id=run_id)
        conversation.close_canceled_graph(run_id, buyer.id)
        purchase = owned_purchase(run_id, buyer)
        return PurchaseStatus(
            run_id=run_id,
            stage=str(purchase["stage"]),
            checkout_id=purchase["checkout_id"],
            order_id=purchase["order_id"],
        )

    @app.post("/purchases/{run_id}/complete", response_model=CheckoutSessionWithOrder)
    def complete_purchase(
        run_id: str, buyer: Annotated[BuyerPrincipal, Depends(current_buyer)]
    ) -> CheckoutSessionWithOrder:
        owned_purchase(run_id, buyer)
        return conversation.complete_purchase(run_id, buyer.id)

    return app


def app_factory() -> FastAPI:
    return create_app()
