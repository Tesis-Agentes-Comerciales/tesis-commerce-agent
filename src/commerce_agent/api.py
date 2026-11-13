import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
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
    error_message,
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
from commerce_agent.delegation_api import register_delegation_api
from commerce_agent.governance.models import GovernanceMode
from commerce_agent.llm import ConversationRouter, IntentInterpretationError
from commerce_agent.runtime import build_runtime
from commerce_agent.trace import PurchaseHistory, PurchaseTrace, PurchaseTraces, TraceUnavailable
from commerce_agent.webhooks import register_webhook_receiver

if TYPE_CHECKING:
    from commerce_agent.application.hnp_selection import HnpSelector


def _absent(value: object) -> bool:
    return value is None


class Health(BaseModel):
    """The condition the Agent runs; evaluation runners compare it before each repetition.

    The `hnp_*` fields exist only with AGENT_HNP_ENABLED (hnp-governed.md §5.4); with the flag off
    they are omitted, so the HP body stays byte for byte what the runners already read.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["ok"]
    governance_mode: GovernanceMode
    hp_flow: Literal["blocked", "governed"]
    hnp_enabled: bool | None = Field(default=None, exclude_if=_absent)
    hnp_kill_switch: bool | None = Field(default=None, exclude_if=_absent)
    hnp_active_grants: int | None = Field(default=None, ge=0, exclude_if=_absent)
    hnp_running_tasks: int | None = Field(default=None, ge=0, exclude_if=_absent)
    hnp_uncertain_tasks: int | None = Field(default=None, ge=0, exclude_if=_absent)
    # AGENT_HNP_REVIEWER (hnp-intent-review.md §9): the runners compare it with their condition.
    hnp_reviewer: Literal["off", "llm"] | None = Field(default=None, exclude_if=_absent)


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


TermsStatus = Literal[
    "changed", "expired", "unavailable", "authorization_denied", "authorization_unverified"
]

# Refusals to present the terms that leave a pending purchase visible: the live offer changed,
# expired or could not be read, or the checkout expired or stopped being ready for payment. The
# purchase is not refreshed; the buyer cancels it and asks again (b2-governed-pilot,
# hp-p5a-definitive-failures).
_STALE_TERMS: dict[str, TermsStatus] = {
    "checkout_terms_changed": "changed",
    "offer_expired": "expired",
    "checkout_expired": "expired",
    # The checkout is no longer payable (e.g. the merchant canceled it): the terms changed.
    "checkout_not_ready": "changed",
    "offer_freshness_unavailable": "unavailable",
}


class PendingPurchase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    purchase: PurchaseStatus
    approval_terms: ApprovalTerms | None
    # Why `approval_terms` is null for a purchase awaiting confirmation; null otherwise. The
    # `authorization_*` ones: D2 refused the approval (hp-p3-d2-authorization), only cancel remains.
    terms_status: TermsStatus | None = None


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


# Merchant answers the governed flow reports itself: the offer expired (freshness or creation),
# or the creation was refused before anything existed (governance/runtime_dispatch.py).
_GOVERNED_MERCHANT_CODES = frozenset({"offer_expired", "out_of_stock", "invalid_item"})


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
    # Governance stops reach REST clients with the chat's Spanish copy, never internal names.
    # So does a refusal of D1's creation POST, which arrives as a service error, not ACP's.
    governed = code.startswith(("d1_", "d2_")) or code in _GOVERNED_MERCHANT_CODES
    message = error_message(code) if governed else str(error)
    body: dict[str, str | None] = {"code": code, "message": message}
    if isinstance(error, IntentInterpretationError):
        # Why the model failed (p0-model-failures.md §3.4); null when it was not the cause.
        body["model_failure"] = error.failure_kind
    return JSONResponse(body, status_code=status)


def create_app(
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    payment_transport: httpx.BaseTransport | None = None,
    clock: Callable[[], datetime] | None = None,
    conversation_router: ConversationRouter | None = None,
    id_factory: IdFactory = default_id_factory,
    buyer_authenticator: BuyerAuthenticator | None = None,
    authority_clock: Callable[[], datetime] | None = None,
    wrap_hnp_selector: "Callable[[HnpSelector], HnpSelector] | None" = None,
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
        authority_clock=authority_clock,
        wrap_hnp_selector=wrap_hnp_selector,
    )
    store = runtime.store
    purchase_service = runtime.purchases
    conversation = runtime.conversation
    traces = PurchaseTraces(configuration, merchant_names=runtime.merchant_names)

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
    def health() -> Health:
        condition = Health(
            status="ok",
            governance_mode=configuration.agent_governance_mode,
            hp_flow=configuration.agent_hp_flow,
        )
        if runtime.delegations is None:
            return condition
        kill_switch, counts = runtime.delegations.health()
        return condition.model_copy(
            update={
                "hnp_enabled": True,
                "hnp_kill_switch": kill_switch,
                "hnp_active_grants": counts.active_grants,
                "hnp_running_tasks": counts.running_tasks,
                "hnp_uncertain_tasks": counts.uncertain_tasks,
                "hnp_reviewer": configuration.agent_hnp_reviewer,
            }
        )

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
        app,
        service=conversation,
        new_id=id_factory,
        current_buyer=current_buyer,
        delegation_enabled=runtime.delegations is not None,
    )
    if runtime.delegations is not None:
        # The bootstrap required both tokens and built the executor (delegation/bootstrap.py).
        assert configuration.agent_hnp_service_token is not None
        assert configuration.agent_hnp_operator_token is not None
        assert runtime.hnp_executor is not None
        register_delegation_api(
            app,
            runtime.delegations,
            executor=runtime.hnp_executor,
            current_buyer=current_buyer,
            service_token=configuration.agent_hnp_service_token,
            operator_token=configuration.agent_hnp_operator_token,
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
        terms_status: TermsStatus | None = None
        refusal = (
            runtime.d2_gate.refusal(run_id)
            if runtime.d2_gate is not None and purchase["stage"] == "awaiting_confirmation"
            else None
        )
        if refusal is not None:
            terms = None
            terms_status = (
                "authorization_denied" if refusal == "denied" else "authorization_unverified"
            )
        elif purchase["stage"] == "awaiting_confirmation" and (
            terms is None or runtime.mandate_signer is not None
        ):
            try:
                terms = purchase_service.approval_terms(run_id).model_dump(mode="json")
            except ApprovalError as error:
                if error.code not in _STALE_TERMS:
                    raise
                terms, terms_status = None, _STALE_TERMS[error.code]
            else:
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
            terms_status=terms_status,
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

    @app.get("/purchases/{run_id}/trace", response_model=PurchaseTrace)
    def purchase_trace(
        run_id: str, buyer: Annotated[BuyerPrincipal, Depends(current_buyer)]
    ) -> PurchaseTrace:
        owned_purchase(run_id, buyer)
        try:
            return traces.trace(run_id)
        except TraceUnavailable as error:
            # A lock another connection held: the same read can work again in a moment.
            headers = {"Retry-After": "1"} if error.transient else None
            raise HTTPException(
                status_code=503, detail="Purchase trace unavailable", headers=headers
            ) from None

    @app.get("/me/purchases", response_model=PurchaseHistory)
    def purchase_history(
        buyer: Annotated[BuyerPrincipal, Depends(current_buyer)],
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        before: Annotated[str | None, Query(max_length=256)] = None,
    ) -> PurchaseHistory:
        # A purchase whose trace cannot be built marks its own row (`trace_unavailable`).
        history = traces.history(buyer.id, limit=limit, before=before)
        if history is None:
            raise HTTPException(status_code=400, detail="Unknown purchase cursor")
        return history

    @app.get("/purchases/{run_id}/approval", response_model=ApprovalTerms)
    def approval_terms(
        run_id: str, buyer: Annotated[BuyerPrincipal, Depends(current_buyer)]
    ) -> ApprovalTerms:
        owned_purchase(run_id, buyer)
        terms = purchase_service.approval_terms(run_id)
        # The terms the server presented, which D2 binds the click to. The same terms keep the
        # time they were first shown: the UI rereads them right before approving.
        shown = store.approval_terms([run_id]).get(run_id)
        if shown is None or shown.get("fingerprint") != terms.fingerprint:
            store.save_approval_terms(run_id, terms.model_dump(mode="json"))
        return terms

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
