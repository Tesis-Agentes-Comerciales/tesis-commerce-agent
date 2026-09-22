import hashlib
import json
from collections.abc import Callable, Sequence
from threading import Lock
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from commerce_agent.acp.feed import ProductFeed
from commerce_agent.application.product_cards import product_card
from commerce_agent.application.purchase import (
    CheckoutDetails,
    PurchasePreparation,
    PurchaseService,
    PurchaseServiceError,
    PurchaseStage,
)
from commerce_agent.approval import ApprovalTerms
from commerce_agent.domain import rank_candidates
from commerce_agent.llm import ConversationMessage, ConversationRouter, IntentInterpretationError
from commerce_agent.llm.reasoning import reasoning_listener
from commerce_agent.persistence import SqliteAgentStore

StepName = Literal[
    "interpret_request",
    "search_catalog",
    "select_product",
    "prepare_checkout",
    "request_approval",
]

MAX_SEARCH_PRODUCTS = 6
MAX_MODEL_CANDIDATES = 10

# Etapas de una compra que todavía ocupa un checkout en el comercio: mientras haya una, no se
# empieza otra. `selected` y `checkout_prepared` solo sobreviven a una caída a mitad de turno.
PENDING_STAGES: tuple[str, ...] = (
    PurchaseStage.SELECTED,
    PurchaseStage.CHECKOUT_PREPARED,
    PurchaseStage.AWAITING_CONFIRMATION,
    PurchaseStage.APPROVED,
    PurchaseStage.COMPLETING,
    PurchaseStage.UNCERTAIN,
)


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


class TurnObserver(Protocol):
    """Receives pipeline progress. Called from the worker thread that runs the turn."""

    def reasoning_delta(self, delta: str) -> None: ...

    def step_started(self, step: StepName, args: dict[str, Any]) -> None: ...

    def step_finished(self, step: StepName, result: dict[str, Any]) -> None: ...


def request_sha256(payload: object) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def turn_run_id(turn_id: str) -> str:
    digest = hashlib.sha256(f"purchase:{turn_id}".encode()).hexdigest()
    return f"run_{digest[:32]}"


class ConversationService:
    """Shared conversation pipeline for `/conversation/turn` and `/assistant`."""

    def __init__(
        self,
        *,
        feed: ProductFeed,
        store: SqliteAgentStore,
        purchases: PurchaseService,
        discover: Callable[[], object],
        router: ConversationRouter | None,
    ) -> None:
        self._feed = feed
        self._store = store
        self._purchases = purchases
        self._discover = discover
        self._router = router
        # ponytail: one local lock is enough for this single-process demo; use DB leases when
        # scaling.
        self._lock = Lock()

    def respond(self, body: ConversationTurnRequest) -> ConversationTurnResponse:
        fingerprint = request_sha256(body.model_dump(mode="json", exclude={"turn_id"}))

        def compute() -> dict[str, Any]:
            response = self.execute(tuple(body.messages), body.details, turn_id=body.turn_id)
            return response.model_dump(mode="json")

        payload, _ = self.run_idempotent(body.turn_id, fingerprint, compute)
        return ConversationTurnResponse.model_validate_json(json.dumps(payload))

    def run_idempotent(
        self,
        turn_id: str,
        fingerprint: str,
        compute: Callable[[], dict[str, Any]],
        *,
        precheck: Callable[[], dict[str, Any] | None] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Run ``compute`` once per turn ID and payload; returns (payload, replayed).

        ``precheck`` may short-circuit a claimed turn with a payload that is not cached, so the
        same turn can run normally once the blocking condition disappears.
        """
        with self._lock:
            try:
                cached = self._store.claim_conversation_turn(turn_id, fingerprint)
            except ValueError as error:
                raise PurchaseServiceError(str(error), code="turn_conflict") from error
            if cached is not None:
                return cached, True
            if precheck is not None:
                blocked = precheck()
                if blocked is not None:
                    return blocked, False
            payload = compute()
            self._store.save_conversation_turn(turn_id, fingerprint, payload)
            return payload, False

    def purchase_view(self, run_id: str) -> dict[str, Any] | None:
        purchase = self._store.purchase(run_id)
        if purchase is None:
            return None
        return {
            "run_id": run_id,
            "stage": str(purchase["stage"]),
            "order_id": purchase["order_id"],
        }

    def pending_purchase_view(self) -> dict[str, Any] | None:
        """Compra pendiente según SQLite. No depende de ningún `run_id` que declare el cliente."""
        purchase = self._store.pending_purchase(PENDING_STAGES)
        if purchase is None:
            return None
        return {
            "run_id": str(purchase["run_id"]),
            "stage": str(purchase["stage"]),
            "order_id": purchase["order_id"],
        }

    def approval_terms_snapshots(self, run_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Términos tal como los presentó `request_approval`, para repintar las tarjetas."""
        return self._store.approval_terms(run_ids)

    def execute(
        self,
        messages: tuple[ConversationMessage, ...],
        details: CheckoutDetails,
        *,
        turn_id: str,
        observer: TurnObserver | None = None,
    ) -> ConversationTurnResponse:
        listener = observer.reasoning_delta if observer is not None else None
        with reasoning_listener(listener):
            return self._execute(messages, details, turn_id=turn_id, observer=observer)

    def _execute(
        self,
        messages: tuple[ConversationMessage, ...],
        details: CheckoutDetails,
        *,
        turn_id: str,
        observer: TurnObserver | None,
    ) -> ConversationTurnResponse:
        if not messages or messages[-1].role != "user":
            raise IntentInterpretationError("Conversation must end with a user message")
        router = self._router
        if router is None:
            raise IntentInterpretationError("No conversation model is configured")

        _started(observer, "interpret_request", lambda: {})
        decision = router.route(messages)
        intent = decision.intent
        constraints = intent.hard.model_dump(mode="json") if intent is not None else None
        preferences = intent.preferences.model_dump(mode="json") if intent is not None else None
        _finished(
            observer,
            "interpret_request",
            lambda: {
                "action": decision.action,
                "constraints": constraints,
                "preferences": preferences,
            },
        )
        if decision.action != "purchase":
            return ConversationTurnResponse(
                action=decision.action,
                assistant_message=decision.assistant_message,
            )
        request = decision.purchase_request or messages[-1].content
        if intent is None:
            raise IntentInterpretationError("Purchase decision is incomplete")

        _started(
            observer,
            "search_catalog",
            lambda: {"constraints": constraints, "preferences": preferences},
        )
        selection = rank_candidates(self._feed, intent)
        _finished(
            observer,
            "search_catalog",
            lambda: {
                "matches": len(selection.ranked),
                "rejections": dict(selection.rejection_counts),
                "products": self._cards(
                    item.candidate.variant_id for item in selection.ranked[:MAX_SEARCH_PRODUCTS]
                ),
            },
        )
        if not selection.ranked:
            recovery = router.recover_no_candidate(
                messages,
                intent,
                selection.rejection_counts,
                self._feed.available_candidates[:10],
            )
            return ConversationTurnResponse(
                action="clarify",
                assistant_message=recovery.assistant_message,
            )

        candidates = selection.ranked[:MAX_MODEL_CANDIDATES]
        _started(
            observer,
            "select_product",
            lambda: {"candidate_variant_ids": [item.candidate.variant_id for item in candidates]},
        )
        candidate_decision = router.choose_candidate(messages, intent, candidates)
        selected_variant_id = candidate_decision.selected_variant_id
        _finished(
            observer,
            "select_product",
            lambda: {
                "decision": "select" if candidate_decision.action == "purchase" else "clarify",
                "product": (
                    self._card(selected_variant_id) if selected_variant_id is not None else None
                ),
                "rationale": candidate_decision.assistant_message,
            },
        )
        if candidate_decision.action == "clarify":
            return ConversationTurnResponse(
                action="clarify",
                assistant_message=candidate_decision.assistant_message,
            )
        if selected_variant_id is None:
            raise IntentInterpretationError("Candidate selection is incomplete")

        _started(observer, "prepare_checkout", lambda: {"variant_id": selected_variant_id})
        self._discover()
        purchase = self._purchases.start(
            run_id=turn_run_id(turn_id),
            request=request,
            intent=intent,
            selected_variant_id=selected_variant_id,
            details=details,
        )
        _finished(
            observer,
            "prepare_checkout",
            lambda: {
                "run_id": purchase.run_id,
                "checkout_id": purchase.checkout.id,
                "checkout_status": purchase.checkout.status,
                "price_changed": purchase.price_changed,
                "total_minor": purchase.checkout.total_minor,
                "currency": purchase.checkout.currency,
            },
        )

        _started(observer, "request_approval", lambda: {"run_id": purchase.run_id})
        terms = self._purchases.approval_terms(purchase.run_id)
        # Lo que se mostró queda guardado: el cliente puede devolver el mensaje, pero los términos
        # de la tarjeta se vuelven a leer de aquí.
        self._store.save_approval_terms(purchase.run_id, terms.model_dump(mode="json"))
        _finished(
            observer,
            "request_approval",
            lambda: {
                "run_id": purchase.run_id,
                "stage": self._stage(purchase.run_id),
                "terms": terms.model_dump(mode="json"),
            },
        )
        return ConversationTurnResponse(
            action="purchase",
            assistant_message=candidate_decision.assistant_message,
            purchase=purchase,
            approval_terms=terms,
        )

    def _stage(self, run_id: str) -> str:
        view = self.purchase_view(run_id)
        if view is None:
            raise PurchaseServiceError("Purchase run does not exist", code="purchase_not_found")
        return view["stage"]

    def _card(self, variant_id: str) -> dict[str, Any] | None:
        card = product_card(self._feed, variant_id)
        return card.model_dump(mode="json") if card is not None else None

    def _cards(self, variant_ids: Any) -> list[dict[str, Any]]:
        cards = (self._card(variant_id) for variant_id in variant_ids)
        return [card for card in cards if card is not None]


def _started(
    observer: TurnObserver | None, step: StepName, args: Callable[[], dict[str, Any]]
) -> None:
    if observer is not None:
        observer.step_started(step, args())


def _finished(
    observer: TurnObserver | None, step: StepName, result: Callable[[], dict[str, Any]]
) -> None:
    if observer is not None:
        observer.step_finished(step, result())
