import hashlib
import json
from collections.abc import Callable, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field, model_validator

from commerce_agent.acp.feed import ProductFeed
from commerce_agent.acp.models import CheckoutSessionWithOrder
from commerce_agent.application.governed_recovery import (
    GovernedRecovery,
    RecoveryContext,
    RecoveryEvidenceUnavailable,
    outside_shortlist,
)
from commerce_agent.application.product_cards import product_card
from commerce_agent.application.purchase import (
    CheckoutDetails,
    PurchasePreparation,
    PurchaseService,
    PurchaseServiceError,
    PurchaseStage,
    invalid_candidate,
)
from commerce_agent.approval import ApprovalTerms
from commerce_agent.completion import CompletionError, CompletionService
from commerce_agent.domain import PurchaseIntent, RankedCandidate, rank_candidates
from commerce_agent.llm import (
    CommerceDecision,
    ConversationMessage,
    ConversationRouter,
    IntentInterpretationError,
    UnknownVariantSelection,
)
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
LOCAL_OWNER_ID = "local-human"
PENDING_PURCHASE_MESSAGE = (
    "Ya hay una compra pendiente. Revísala o cancélala antes de iniciar otra."
)

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
    conversation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")
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

    def step_failed(self, step: StepName, error: dict[str, str]) -> None: ...


_observer: ContextVar[TurnObserver | None] = ContextVar("turn_observer", default=None)


@contextmanager
def observe_turn(observer: TurnObserver | None):
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


class GraphState(TypedDict):
    incoming_messages: list[dict[str, str]]
    history: list[dict[str, str]]
    turn_id: str
    active_turn_id: NotRequired[str]
    last_turn_id: NotRequired[str]
    decision: dict[str, Any]
    ranked_candidates: list[dict[str, Any]]
    rejection_counts: dict[str, int]
    selected_variant_id: str | None
    selection_message: str
    purchase: dict[str, Any]
    response: dict[str, Any] | None
    approval_action: str
    completion_result: dict[str, Any]


class GraphInput(TypedDict):
    incoming_messages: list[dict[str, str]]
    turn_id: str


@dataclass(frozen=True)
class GraphContext:
    details: CheckoutDetails | None = None
    trust_incoming_history: bool = False
    thread_id: str = ""
    owner_id: str = LOCAL_OWNER_ID
    feed: ProductFeed | None = None
    router: ConversationRouter | None = None


def request_sha256(payload: object) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def turn_run_id(turn_id: str) -> str:
    digest = hashlib.sha256(f"purchase:{turn_id}".encode()).hexdigest()
    return f"run_{digest[:32]}"


def scoped_id(owner_id: str, identifier: str) -> str:
    if owner_id == LOCAL_OWNER_ID:
        return identifier
    digest = hashlib.sha256(owner_id.encode()).hexdigest()[:24]
    return f"owner:{digest}:{identifier}"


class ConversationService:
    """Shared conversation pipeline for `/conversation/turn` and `/assistant`."""

    def __init__(
        self,
        *,
        feed: ProductFeed,
        store: SqliteAgentStore,
        purchases: PurchaseService | None,
        completion: CompletionService | None,
        discover: Callable[[], object] | None,
        router: ConversationRouter | None,
        checkpointer: SqliteSaver | None,
        allow_checkout: bool = True,
        feed_provider: Callable[[], ProductFeed] | None = None,
        router_factory: Callable[[ProductFeed], ConversationRouter] | None = None,
        recovery: GovernedRecovery | None = None,
    ) -> None:
        self._feed = feed
        self._store = store
        self._purchases = purchases
        self._completion = completion
        self._discover = discover
        self._router = router
        self._feed_provider = feed_provider
        self._router_factory = router_factory
        self._allow_checkout = allow_checkout
        # Only the governed flow explains rejected selections; None keeps B0 and its errors.
        self._recovery = recovery
        self._graph = self._build_graph(checkpointer)
        # ponytail: one local lock is enough for this single-process demo; use DB leases when
        # scaling.
        self._lock = Lock()

    @property
    def graph(self) -> CompiledStateGraph[GraphState, GraphContext, GraphInput, GraphState]:
        return self._graph

    def respond(
        self, body: ConversationTurnRequest, owner_id: str = LOCAL_OWNER_ID
    ) -> ConversationTurnResponse:
        fingerprint = request_sha256(body.model_dump(mode="json", exclude={"turn_id"}))

        def precheck() -> dict[str, Any] | None:
            guarded = self.pending_purchase_guard(owner_id)
            return guarded[1].model_dump(mode="json") if guarded is not None else None

        def compute() -> dict[str, Any]:
            response = self.execute(
                tuple(body.messages),
                body.details,
                turn_id=body.turn_id,
                conversation_id=body.conversation_id,
                owner_id=owner_id,
            )
            return response.model_dump(mode="json")

        payload, _ = self.run_idempotent(
            body.turn_id, fingerprint, compute, precheck=precheck, owner_id=owner_id
        )
        return ConversationTurnResponse.model_validate_json(json.dumps(payload))

    def run_idempotent(
        self,
        turn_id: str,
        fingerprint: str,
        compute: Callable[[], dict[str, Any]],
        *,
        precheck: Callable[[], dict[str, Any] | None] | None = None,
        owner_id: str = LOCAL_OWNER_ID,
    ) -> tuple[dict[str, Any], bool]:
        """Run ``compute`` once per turn ID and payload; returns (payload, replayed).

        ``precheck`` may short-circuit a claimed turn with a payload that is not cached, so the
        same turn can run normally once the blocking condition disappears.
        """
        with self._lock:
            try:
                cached = self._store.claim_conversation_turn(
                    scoped_id(owner_id, turn_id), fingerprint
                )
            except ValueError as error:
                raise PurchaseServiceError(str(error), code="turn_conflict") from error
            if cached is not None:
                return cached, True
            if precheck is not None:
                blocked = precheck()
                if blocked is not None:
                    return blocked, False
            payload = compute()
            self._store.save_conversation_turn(scoped_id(owner_id, turn_id), fingerprint, payload)
            return payload, False

    def purchase_view(self, run_id: str, owner_id: str = LOCAL_OWNER_ID) -> dict[str, Any] | None:
        purchase = self._store.purchase_for_owner(run_id, owner_id)
        if purchase is None:
            return None
        return {
            "run_id": run_id,
            "stage": str(purchase["stage"]),
            "order_id": purchase["order_id"],
        }

    def order_number(self, run_id: str, owner_id: str = LOCAL_OWNER_ID) -> str | None:
        """El número de orden que muestra el recibo: `order_number` del comercio o, si falta, su id.

        Sale del resultado de `/complete` guardado, la misma fuente que la tarjeta.
        """
        if self.purchase_view(run_id, owner_id) is None:
            return None
        completion = self._store.completion_result(run_id)
        if completion is None:
            return None
        order = CheckoutSessionWithOrder.model_validate_json(json.dumps(completion)).order
        return order.order_number or order.id

    def pending_purchase_view(self, owner_id: str = LOCAL_OWNER_ID) -> dict[str, Any] | None:
        """Compra pendiente según SQLite. No depende de ningún `run_id` que declare el cliente."""
        purchase = self._store.pending_purchase(PENDING_STAGES, owner_id)
        if purchase is None:
            return None
        return {
            "run_id": str(purchase["run_id"]),
            "stage": str(purchase["stage"]),
            "order_id": purchase["order_id"],
        }

    def pending_purchase_guard(
        self, owner_id: str = LOCAL_OWNER_ID
    ) -> tuple[dict[str, Any], ConversationTurnResponse] | None:
        view = self.pending_purchase_view(owner_id)
        if view is None:
            return None
        return view, ConversationTurnResponse(
            action="clarify", assistant_message=PENDING_PURCHASE_MESSAGE
        )

    def approval_terms_snapshots(
        self, run_ids: Sequence[str], owner_id: str = LOCAL_OWNER_ID
    ) -> dict[str, dict[str, Any]]:
        """Términos tal como los presentó `request_approval`, para repintar las tarjetas."""
        owned = [run_id for run_id in run_ids if self.purchase_view(run_id, owner_id) is not None]
        return self._store.approval_terms(owned)

    def complete_purchase(
        self, run_id: str, owner_id: str = LOCAL_OWNER_ID
    ) -> CheckoutSessionWithOrder:
        if self._completion is None:
            raise CompletionError("Checkout is unavailable", code="invalid_stage")
        if self.purchase_view(run_id, owner_id) is None:
            raise CompletionError("Purchase run does not exist", code="purchase_not_found")
        persisted = self._store.completion_result(run_id)
        if persisted is not None:
            return CheckoutSessionWithOrder.model_validate_json(json.dumps(persisted))
        purchase_view = self._store.purchase(run_id)
        if purchase_view is None or purchase_view["stage"] not in {
            PurchaseStage.APPROVED,
            PurchaseStage.UNCERTAIN,
        }:
            return self._completion.complete(run_id=run_id)
        thread_id = self._store.graph_thread(run_id)
        if thread_id is None:
            return self._completion.complete(run_id=run_id)
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        context = GraphContext(thread_id=thread_id, owner_id=owner_id)
        with self._lock:
            snapshot = self._graph.get_state(config)
            purchase = snapshot.values.get("purchase")
            if not isinstance(purchase, dict) or purchase.get("run_id") != run_id:
                raise CompletionError("Graph purchase does not match", code="invalid_stage")
            if snapshot.next == ("await_approval",):
                self._graph.invoke(
                    Command(resume={"action": "complete", "run_id": run_id}),
                    config=config,
                    context=context,
                )
            elif snapshot.next == ("complete_purchase",):
                self._graph.invoke(None, config=config, context=context)
            result = self._store.completion_result(run_id)
            if result is None:
                if not self._graph.get_state(config).next:
                    return self._completion.complete(run_id=run_id)
                raise CompletionError("Completion has no confirmed order", code="invalid_stage")
            return CheckoutSessionWithOrder.model_validate_json(json.dumps(result))

    def close_canceled_graph(self, run_id: str, owner_id: str = LOCAL_OWNER_ID) -> None:
        if self.purchase_view(run_id, owner_id) is None:
            raise PurchaseServiceError("Purchase run does not exist", code="purchase_not_found")
        thread_id = self._store.graph_thread(run_id)
        if thread_id is None:
            return
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        with self._lock:
            snapshot = self._graph.get_state(config)
            if snapshot.next == ("await_approval",):
                self._graph.invoke(
                    Command(resume={"action": "cancel", "run_id": run_id}),
                    config=config,
                    context=GraphContext(thread_id=thread_id, owner_id=owner_id),
                )

    def execute(
        self,
        messages: tuple[ConversationMessage, ...],
        details: CheckoutDetails,
        *,
        turn_id: str,
        conversation_id: str | None = None,
        observer: TurnObserver | None = None,
        owner_id: str = LOCAL_OWNER_ID,
    ) -> ConversationTurnResponse:
        if not messages or messages[-1].role != "user":
            raise IntentInterpretationError("Conversation must end with a user message")
        if self._router is None and self._router_factory is None:
            raise IntentInterpretationError("No conversation model is configured")
        feed = self._feed_provider() if self._feed_provider is not None else self._feed
        router = self._router_factory(feed) if self._router_factory is not None else self._router
        internal_turn_id = scoped_id(owner_id, turn_id)
        thread_id = scoped_id(owner_id, conversation_id or f"turn:{turn_id}")
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        snapshot = self._graph.get_state(config)
        if snapshot.values.get("last_turn_id") == internal_turn_id:
            return ConversationTurnResponse.model_validate_json(
                json.dumps(snapshot.values["response"])
            )
        if snapshot.next in {("await_approval",), ("complete_purchase",)}:
            return ConversationTurnResponse(
                action="clarify",
                assistant_message=(
                    "Ya hay una compra pendiente. Revísala o cancélala antes de continuar."
                ),
            )
        listener = observer.reasoning_delta if observer is not None else None
        try:
            with reasoning_listener(listener), observe_turn(observer):
                state = self._graph.invoke(
                    {
                        "incoming_messages": [
                            message.model_dump(mode="json") for message in messages
                        ],
                        "turn_id": internal_turn_id,
                    },
                    config=config,
                    context=GraphContext(
                        details=details,
                        trust_incoming_history=conversation_id is None,
                        thread_id=thread_id,
                        owner_id=owner_id,
                        feed=feed,
                        router=router,
                    ),
                )
        finally:
            if self._router_factory is not None and router is not None:
                close = getattr(router, "close", None)
                if callable(close):
                    close()
        return ConversationTurnResponse.model_validate_json(json.dumps(state["response"]))

    def _build_graph(self, checkpointer: SqliteSaver | None):
        builder = StateGraph(GraphState, input_schema=GraphInput, context_schema=GraphContext)
        builder.add_node("ingest", self._ingest)
        builder.add_node("interpret_request", self._interpret_request)
        builder.add_node("search_catalog", self._search_catalog)
        builder.add_node("recover_no_candidate", self._recover_no_candidate)
        builder.add_node("select_product", self._select_product)
        builder.add_node("prepare_checkout", self._prepare_checkout)
        builder.add_node("request_approval", self._request_approval)
        builder.add_node("finalize", self._finalize)
        builder.add_node("await_approval", self._await_approval)
        builder.add_node("complete_purchase", self._complete_purchase)
        builder.add_edge(START, "ingest")
        builder.add_edge("ingest", "interpret_request")
        builder.add_conditional_edges(
            "interpret_request",
            lambda state: (
                "search_catalog" if state["decision"]["action"] == "purchase" else "finalize"
            ),
            ["search_catalog", "finalize"],
        )
        builder.add_conditional_edges(
            "search_catalog",
            lambda state: (
                "select_product" if state["ranked_candidates"] else "recover_no_candidate"
            ),
            ["select_product", "recover_no_candidate"],
        )
        builder.add_edge("recover_no_candidate", "finalize")
        builder.add_conditional_edges(
            "select_product",
            lambda state: "prepare_checkout" if state["selected_variant_id"] else "finalize",
            ["prepare_checkout", "finalize"],
        )
        if self._recovery is None:
            builder.add_edge("prepare_checkout", "request_approval")
        else:
            # Only the governed flow ends a turn here, after canceling an over-budget checkout.
            builder.add_conditional_edges(
                "prepare_checkout",
                lambda state: "finalize" if state["response"] else "request_approval",
                ["request_approval", "finalize"],
            )
        builder.add_edge("request_approval", "finalize")
        builder.add_conditional_edges(
            "finalize",
            lambda state: "await_approval" if state["response"]["action"] == "purchase" else END,
            ["await_approval", END],
        )
        builder.add_conditional_edges(
            "await_approval",
            lambda state: "complete_purchase" if state["approval_action"] == "complete" else END,
            ["complete_purchase", END],
        )
        builder.add_edge("complete_purchase", END)
        return builder.compile(checkpointer=checkpointer)

    @staticmethod
    def _messages(state: GraphState) -> tuple[ConversationMessage, ...]:
        return tuple(ConversationMessage.model_validate(item) for item in state["history"])

    @staticmethod
    def _intent(state: GraphState) -> PurchaseIntent:
        decision = CommerceDecision.model_validate_json(json.dumps(state["decision"]))
        if decision.intent is None:
            raise IntentInterpretationError("Purchase decision is incomplete")
        return decision.intent

    @staticmethod
    def _ingest(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
        turn_id = state["turn_id"]
        if state.get("active_turn_id") == turn_id and state.get("history"):
            history = state["history"]
        elif state.get("history"):
            history = [*state["history"], state["incoming_messages"][-1]]
        else:
            history = (
                state["incoming_messages"]
                if runtime.context.trust_incoming_history
                else state["incoming_messages"][-1:]
            )
        return {
            "history": history[-20:],
            "active_turn_id": turn_id,
            "selected_variant_id": None,
            "response": None,
        }

    def _interpret_request(
        self, state: GraphState, runtime: Runtime[GraphContext]
    ) -> dict[str, Any]:
        router = runtime.context.router or self._router
        if router is None:
            raise IntentInterpretationError("No conversation model is configured")
        observer = _observer.get()
        _started(observer, "interpret_request", lambda: {})
        decision = router.route(self._messages(state))
        intent = decision.intent
        _finished(
            observer,
            "interpret_request",
            lambda: {
                "action": decision.action,
                "constraints": intent.hard.model_dump(mode="json") if intent else None,
                "preferences": intent.preferences.model_dump(mode="json") if intent else None,
            },
        )
        response = None
        if decision.action != "purchase":
            response = ConversationTurnResponse(
                action=decision.action,
                assistant_message=decision.assistant_message,
            ).model_dump(mode="json")
        return {"decision": decision.model_dump(mode="json"), "response": response}

    def _search_catalog(self, state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
        intent = self._intent(state)
        observer = _observer.get()
        _started(
            observer,
            "search_catalog",
            lambda: {
                "constraints": intent.hard.model_dump(mode="json"),
                "preferences": intent.preferences.model_dump(mode="json"),
            },
        )
        feed = runtime.context.feed or self._feed
        selection = rank_candidates(feed, intent)
        _finished(
            observer,
            "search_catalog",
            lambda: {
                "matches": len(selection.ranked),
                "rejections": dict(selection.rejection_counts),
                "products": self._cards(
                    (item.candidate.variant_id for item in selection.ranked[:MAX_SEARCH_PRODUCTS]),
                    feed,
                ),
            },
        )
        return {
            "ranked_candidates": [
                item.model_dump(mode="json") for item in selection.ranked[:MAX_MODEL_CANDIDATES]
            ],
            "rejection_counts": selection.rejection_counts,
        }

    def _recover_no_candidate(
        self, state: GraphState, runtime: Runtime[GraphContext]
    ) -> dict[str, Any]:
        if self._recovery is not None:
            # Without evidence the turn falls back to the B0 model recovery below.
            with suppress(RecoveryEvidenceUnavailable):
                context = self._recovery_context(state, runtime)
                return _explained(self._recovery.no_candidate(context))
        router = runtime.context.router or self._router
        if router is None:
            raise IntentInterpretationError("No conversation model is configured")
        recovery = router.recover_no_candidate(
            self._messages(state),
            self._intent(state),
            state["rejection_counts"],
            (runtime.context.feed or self._feed).available_candidates[:10],
        )
        return {
            "response": ConversationTurnResponse(
                action="clarify", assistant_message=recovery.assistant_message
            ).model_dump(mode="json")
        }

    def _select_product(self, state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
        router = runtime.context.router or self._router
        if router is None:
            raise IntentInterpretationError("No conversation model is configured")
        candidates = tuple(
            RankedCandidate.model_validate_json(json.dumps(item))
            for item in state["ranked_candidates"]
        )
        observer = _observer.get()
        _started(
            observer,
            "select_product",
            lambda: {"candidate_variant_ids": [item.candidate.variant_id for item in candidates]},
        )
        try:
            decision = router.choose_candidate(
                self._messages(state), self._intent(state), candidates
            )
        except UnknownVariantSelection as error:
            if self._recovery is None:
                raise
            return self._explain_selection(state, runtime, error.variant_id, error)
        rejected = outside_shortlist(decision, candidates)
        if self._recovery is not None and rejected is not None:
            # B0 refuses this choice at checkout preparation, before any merchant call.
            return self._explain_selection(state, runtime, rejected, invalid_candidate())
        selected = decision.selected_variant_id
        _finished(
            observer,
            "select_product",
            lambda: {
                "decision": "select" if decision.action == "purchase" else "clarify",
                "product": self._card(selected, runtime.context.feed)
                if selected is not None
                else None,
                "rationale": decision.assistant_message,
            },
        )
        if decision.action == "clarify":
            return {
                "selected_variant_id": None,
                "response": ConversationTurnResponse(
                    action="clarify", assistant_message=decision.assistant_message
                ).model_dump(mode="json"),
            }
        if selected is None:
            raise IntentInterpretationError("Candidate selection is incomplete")
        if not self._allow_checkout:
            card = self._card(selected, runtime.context.feed)
            name = card["title"] if card is not None else selected
            return {
                "selected_variant_id": None,
                "response": ConversationTurnResponse(
                    action="clarify",
                    assistant_message=(
                        f"Encontré {name}. Studio solo permite explorar el catálogo; "
                        "usa la interfaz del agente para preparar y aprobar una compra."
                    ),
                ).model_dump(mode="json"),
            }
        return {"selected_variant_id": selected, "selection_message": decision.assistant_message}

    def _prepare_checkout(
        self, state: GraphState, runtime: Runtime[GraphContext]
    ) -> dict[str, Any]:
        if not self._allow_checkout or self._discover is None or self._purchases is None:
            raise PurchaseServiceError("Checkout is unavailable", code="invalid_stage")
        selected = state["selected_variant_id"]
        if selected is None:
            raise IntentInterpretationError("Candidate selection is incomplete")
        observer = _observer.get()
        run_id = turn_run_id(state["turn_id"])
        # The run goes in the args: a governance stop fails the step without a result, and the
        # chat still needs the run to read its trace.
        _started(observer, "prepare_checkout", lambda: {"variant_id": selected, "run_id": run_id})
        self._discover()
        decision = CommerceDecision.model_validate_json(json.dumps(state["decision"]))
        details = runtime.context.details
        if details is None:
            raise IntentInterpretationError("Checkout details are required")
        try:
            purchase = self._purchases.start(
                run_id=run_id,
                request=decision.purchase_request or self._messages(state)[-1].content,
                intent=self._intent(state),
                selected_variant_id=selected,
                details=details,
                graph_thread_id=runtime.context.thread_id,
                owner_id=runtime.context.owner_id,
                feed=runtime.context.feed,
            )
        except PurchaseServiceError as error:
            if self._recovery is None or error.code != "budget_exceeded":
                raise
            stored = self._store.purchase(run_id)
            try:
                message = self._recovery.budget_exceeded(
                    self._recovery_context(state, runtime),
                    selected,
                    stored["checkout_id"] if stored is not None else None,
                )
            except RecoveryEvidenceUnavailable:
                raise error from None
            if observer is not None:
                observer.step_failed("prepare_checkout", {"code": error.code, "message": message})
            return _explained(message)
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
        return {"purchase": purchase.model_dump(mode="json")}

    def _request_approval(
        self, state: GraphState, runtime: Runtime[GraphContext]
    ) -> dict[str, Any]:
        if not self._allow_checkout or self._purchases is None:
            raise PurchaseServiceError("Checkout is unavailable", code="invalid_stage")
        purchase = PurchasePreparation.model_validate_json(json.dumps(state["purchase"]))
        observer = _observer.get()
        _started(observer, "request_approval", lambda: {"run_id": purchase.run_id})
        terms = self._purchases.approval_terms(purchase.run_id)
        self._store.save_approval_terms(purchase.run_id, terms.model_dump(mode="json"))
        _finished(
            observer,
            "request_approval",
            lambda: {
                "run_id": purchase.run_id,
                "stage": self._stage(purchase.run_id, runtime.context.owner_id),
                "terms": terms.model_dump(mode="json"),
            },
        )
        response = ConversationTurnResponse(
            action="purchase",
            assistant_message=state["selection_message"],
            purchase=purchase,
            approval_terms=terms,
        )
        return {"response": response.model_dump(mode="json")}

    @staticmethod
    def _finalize(state: GraphState) -> dict[str, Any]:
        response = ConversationTurnResponse.model_validate_json(json.dumps(state["response"]))
        history = [
            *state["history"],
            ConversationMessage(role="assistant", content=response.assistant_message).model_dump(
                mode="json"
            ),
        ]
        return {"history": history[-20:], "last_turn_id": state["turn_id"]}

    def _await_approval(self, state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
        run_id = state["purchase"]["run_id"]
        signal = interrupt({"run_id": run_id, "reason": "human_approval"})
        if not isinstance(signal, dict) or signal.get("run_id") != run_id:
            raise CompletionError("Approval resume does not match", code="invalid_stage")
        purchase = self._store.purchase_for_owner(run_id, runtime.context.owner_id)
        if purchase is None:
            raise CompletionError("Purchase run does not exist", code="purchase_not_found")
        action = signal.get("action")
        if action == "cancel" and purchase["stage"] == PurchaseStage.CANCELED:
            return {"approval_action": "cancel"}
        if action == "complete" and purchase["stage"] in {
            PurchaseStage.APPROVED,
            PurchaseStage.UNCERTAIN,
            PurchaseStage.COMPLETED,
        }:
            return {"approval_action": "complete"}
        raise CompletionError("Purchase is not approved for completion", code="invalid_stage")

    def _complete_purchase(
        self, state: GraphState, runtime: Runtime[GraphContext]
    ) -> dict[str, Any]:
        if not self._allow_checkout or self._completion is None:
            raise CompletionError("Checkout is unavailable", code="invalid_stage")
        run_id = state["purchase"]["run_id"]
        if self.purchase_view(run_id, runtime.context.owner_id) is None:
            raise CompletionError("Purchase run does not exist", code="purchase_not_found")
        result = self._completion.complete(run_id=run_id)
        return {"completion_result": result.model_dump(mode="json")}

    def _explain_selection(
        self,
        state: GraphState,
        runtime: Runtime[GraphContext],
        variant_id: str,
        b0_error: Exception,
    ) -> dict[str, Any]:
        """Explain the rejected choice, or raise the error B0 gives when it leaves no evidence."""
        assert self._recovery is not None
        try:
            message = self._recovery.outside_shortlist(
                self._recovery_context(state, runtime), variant_id
            )
        except RecoveryEvidenceUnavailable:
            raise b0_error from None
        _finished(
            _observer.get(),
            "select_product",
            lambda: {"decision": "clarify", "product": None, "rationale": message},
        )
        return {"selected_variant_id": None, **_explained(message)}

    def _recovery_context(
        self, state: GraphState, runtime: Runtime[GraphContext]
    ) -> RecoveryContext:
        return RecoveryContext(
            run_id=turn_run_id(state["turn_id"]),
            feed=runtime.context.feed or self._feed,
            intent=self._intent(state),
            rejection_counts=state["rejection_counts"],
        )

    def _stage(self, run_id: str, owner_id: str) -> str:
        view = self.purchase_view(run_id, owner_id)
        if view is None:
            raise PurchaseServiceError("Purchase run does not exist", code="purchase_not_found")
        return view["stage"]

    def _card(self, variant_id: str, feed: ProductFeed | None = None) -> dict[str, Any] | None:
        card = product_card(feed or self._feed, variant_id)
        return card.model_dump(mode="json") if card is not None else None

    def _cards(self, variant_ids: Any, feed: ProductFeed | None = None) -> list[dict[str, Any]]:
        cards = (self._card(variant_id, feed) for variant_id in variant_ids)
        return [card for card in cards if card is not None]


def _explained(message: str) -> dict[str, Any]:
    response = ConversationTurnResponse(action="respond", assistant_message=message)
    return {"response": response.model_dump(mode="json")}


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
