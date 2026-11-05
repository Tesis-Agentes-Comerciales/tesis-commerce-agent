import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Annotated, Any, Literal, NotRequired, Protocol, Self, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from commerce_agent.acp.feed import ProductFeed
from commerce_agent.acp.models import CheckoutSession, CheckoutSessionWithOrder
from commerce_agent.acp.offer_snapshot import DeliveryContext, MerchantOffer
from commerce_agent.application.conditional import (
    EXCLUSIONS_ONLY,
    BranchEvaluation,
    ConditionalClarification,
    ConditionalDecision,
    GovernanceMode,
    PurchaseMode,
    ResolvedConditional,
    checkout_check_record,
    conditional_view,
    effective_intent,
    evaluate_branches,
    excluded_variant_ids,
    record_decision,
    resolve_branches,
    verify_checkout,
    without_variants,
)
from commerce_agent.application.governed_recovery import (
    GovernedRecovery,
    RecoveryContext,
    RecoveryEvidenceUnavailable,
    outside_shortlist,
)
from commerce_agent.application.offer_freshness import LAB_DESTINATION, OfferReader
from commerce_agent.application.product_cards import ProductCard, product_card
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
from commerce_agent.domain import HardConstraints, PurchaseIntent, RankedCandidate, rank_candidates
from commerce_agent.domain.conditional import BranchMatch, ConditionalIntent
from commerce_agent.llm import (
    CommerceDecision,
    ConversationMessage,
    ConversationRouter,
    IntentInterpretationError,
    UnknownVariantSelection,
)
from commerce_agent.llm.model_requests import model_request_scope
from commerce_agent.llm.prices import format_minor_amount
from commerce_agent.llm.reasoning import reasoning_listener
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.conditional_sqlite import ConditionalEntry

StepName = Literal[
    "interpret_request",
    "search_catalog",
    "select_product",
    "prepare_checkout",
    "request_approval",
]

_logger = logging.getLogger(__name__)

MAX_SEARCH_PRODUCTS = 6
MAX_MODEL_CANDIDATES = 10
LOCAL_OWNER_ID = "local-human"
PENDING_PURCHASE_MESSAGE = (
    "Ya hay una compra pendiente. Revísala o cancélala antes de iniciar otra."
)
DELEGATION_CURRENCY = "COP"
NO_DELEGATION_OFFERS_MESSAGE = (
    "No encontré opciones del catálogo que cumplan lo que pediste, así que todavía no hay nada "
    "que delegar. Ajusta la búsqueda (por ejemplo, el modelo o el presupuesto) y vuelvo a buscar."
)


def delegation_offers_message(count: int) -> str:
    """D3 (docs/design/p0-ux08-ux10.md §3.3): it names the one unit every scope allows
    (`quantity_max` is `One`), so whoever asked for two reads what the delegation buys."""
    options = "1 opción" if count == 1 else f"{count} opciones"
    return (
        f"Encontré {options} del catálogo para lo que pediste. Abajo puedes delegar la compra de "
        "1 unidad: revisa las opciones y el total máximo y autorízala; después el agente la compra "
        "solo."
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


class DelegationDraftView(BaseModel):
    """What a delegate turn found, for the in-chat form (docs/design/hnp-chat-delegate.md).

    Display data only: `POST /delegations/proposals` validates every variant and the total again.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    offers: tuple[ProductCard, ...]
    budget_minor: int | None = Field(default=None, gt=0)
    currency: Literal["COP"] = "COP"
    preference_text: str = Field(min_length=1)
    # The order the turn read among its offers (p0-conditional-intent.md §3.9), at most one per
    # evaluator branch; the form proposes it and the buyer consents to it on the card. Absent
    # when the turn read none; never null.
    priority: Annotated[tuple[str, ...], Field(min_length=1, max_length=3)] | None = None

    @field_validator("priority")
    @classmethod
    def _priority_is_absent_not_null(cls, value: tuple[str, ...] | None) -> tuple[str, ...]:
        # Runs only on a given value, never on the default: a null is a second form of absent.
        if value is None:
            raise ValueError("An absent priority is omitted, not null")
        return value

    @model_validator(mode="after")
    def _priority_is_among_the_offers(self) -> Self:
        offered = {offer.variant_id for offer in self.offers}
        if self.priority is not None and (
            len(set(self.priority)) != len(self.priority) or not set(self.priority) <= offered
        ):
            raise ValueError("A priority orders distinct variants of the offers")
        return self

    @model_serializer(mode="wrap")
    def _without_absent_priority(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        if data.get("priority") is None:
            data.pop("priority", None)
        return data


class CheckoutReader(Protocol):
    """Reads a checkout back: the one `start` rejected still owes its `checkout_check` row."""

    def get_checkout(self, checkout_id: str) -> CheckoutSession: ...


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
    delegate: NotRequired[bool]
    delegation_draft: NotRequired[dict[str, Any] | None]
    # The branches resolved against the feed and the decision their offers gave, from
    # `select_product` to `prepare_checkout` (p0-conditional-intent.md §3.4); None otherwise.
    conditional: NotRequired[dict[str, Any] | None]


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
    # A delegate turn stops after the search: no selection, checkout or purchase run.
    delegate: bool = False


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
        offer_reader: OfferReader | None = None,
        governance_mode: GovernanceMode = "off",
        checkout_reader: CheckoutReader | None = None,
    ) -> None:
        self._feed = feed
        # A conditional turn reads its branches' offers in every mode (p0-conditional-intent.md
        # D1); without a reader every read fails, so nothing is verified and nothing is bought.
        self._offer_reader: OfferReader = offer_reader or _NoOfferReader()
        self._governance_mode: GovernanceMode = governance_mode
        self._checkout_reader = checkout_reader
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
        state = self._run(
            messages,
            details,
            turn_id=turn_id,
            conversation_id=conversation_id,
            observer=observer,
            owner_id=owner_id,
        )
        return ConversationTurnResponse.model_validate_json(json.dumps(state["response"]))

    def draft_delegation(
        self,
        messages: tuple[ConversationMessage, ...],
        details: CheckoutDetails,
        *,
        turn_id: str,
        conversation_id: str | None = None,
        observer: TurnObserver | None = None,
        owner_id: str = LOCAL_OWNER_ID,
    ) -> tuple[ConversationTurnResponse, DelegationDraftView | None]:
        """Interpret and search only; the shortlist goes to the in-chat delegation form."""
        state = self._run(
            messages,
            details,
            turn_id=turn_id,
            conversation_id=conversation_id,
            observer=observer,
            owner_id=owner_id,
            delegate=True,
        )
        response = ConversationTurnResponse.model_validate_json(json.dumps(state["response"]))
        draft = state.get("delegation_draft")
        if not isinstance(draft, dict):
            return response, None
        return response, DelegationDraftView.model_validate_json(json.dumps(draft))

    def _run(
        self,
        messages: tuple[ConversationMessage, ...],
        details: CheckoutDetails,
        *,
        turn_id: str,
        conversation_id: str | None,
        observer: TurnObserver | None,
        owner_id: str,
        delegate: bool = False,
    ) -> dict[str, Any]:
        """Run one turn through the graph and return its final state values."""
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
            return dict(snapshot.values)
        if snapshot.next in {("await_approval",), ("complete_purchase",)}:
            pending = ConversationTurnResponse(
                action="clarify",
                assistant_message=(
                    "Ya hay una compra pendiente. Revísala o cancélala antes de continuar."
                ),
            )
            return {"response": pending.model_dump(mode="json")}
        listener = observer.reasoning_delta if observer is not None else None
        try:
            with (
                reasoning_listener(listener),
                observe_turn(observer),
                # The model requests of the turn go under the run its purchase has or would have.
                model_request_scope(run_id=turn_run_id(internal_turn_id)),
            ):
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
                        delegate=delegate,
                    ),
                )
        finally:
            if self._router_factory is not None and router is not None:
                close = getattr(router, "close", None)
                if callable(close):
                    close()
        return state

    def _build_graph(self, checkpointer: SqliteSaver | None):
        builder = StateGraph(GraphState, input_schema=GraphInput, context_schema=GraphContext)
        builder.add_node("ingest", self._ingest)
        builder.add_node("interpret_request", self._interpret_request)
        builder.add_node("search_catalog", self._search_catalog)
        builder.add_node("recover_no_candidate", self._recover_no_candidate)
        builder.add_node("offer_delegation", self._offer_delegation)
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
            _after_search,
            ["select_product", "recover_no_candidate", "offer_delegation"],
        )
        builder.add_edge("recover_no_candidate", "finalize")
        builder.add_edge("offer_delegation", "finalize")
        builder.add_conditional_edges(
            "select_product",
            lambda state: "prepare_checkout" if state["selected_variant_id"] else "finalize",
            ["prepare_checkout", "finalize"],
        )
        if self._recovery is None:
            # A conditional checkout canceled here already answered: `request_approval` passes
            # it through, so the B0 graph keeps this edge (and the batch-2 anchor on it).
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
            "delegate": runtime.context.delegate,
            "delegation_draft": None,
            "conditional": None,
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
        catalog = runtime.context.feed or self._feed
        conditional = self._conditional(state)
        # «Nunca B» leaves the turn's feed (§3.6): the shortlist, the recovery and D1 never see B.
        excluded = excluded_variant_ids(conditional, catalog) if conditional is not None else ()
        feed = without_variants(catalog, excluded)
        selection = rank_candidates(feed, intent)
        rejections = dict(selection.rejection_counts)
        if excluded:
            rejections["excluded"] = len(excluded)
        # Without branches nothing is evaluated (UX07), nor in a delegation turn (§3.9).
        if (
            conditional is not None
            and conditional.excluded
            and (state.get("delegate") or not conditional.branches)
        ):
            self._record_exclusions(state, conditional, intent.hard, excluded)
        _finished(
            observer,
            "search_catalog",
            lambda: {
                "matches": len(selection.ranked),
                "rejections": rejections,
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

    def _offer_delegation(
        self, state: GraphState, runtime: Runtime[GraphContext]
    ) -> dict[str, Any]:
        """End a delegate turn with the shortlist: no selection, checkout or purchase run.

        Without offers the reply is fixed: the model recovery and the governed explanation write
        under a purchase run, and a delegate turn has none.
        """
        feed = self._turn_feed(state, runtime)
        variant_ids = [item["candidate"]["variant_id"] for item in state["ranked_candidates"]]
        offers = tuple(
            card
            for variant_id in variant_ids[:MAX_SEARCH_PRODUCTS]
            if (card := product_card(feed, variant_id)) is not None
            and card.available
            and card.currency.upper() == DELEGATION_CURRENCY
        )
        hard = self._intent(state).hard
        budget = hard.max_total_amount_minor or hard.max_unit_amount_minor or None
        priority = self._draft_priority(state, runtime, offers)
        # Absent, never null, when the branches propose no order among the offers.
        ordered: dict[str, Any] = {"priority": priority} if priority else {}
        draft = DelegationDraftView(
            offers=offers,
            budget_minor=budget,
            preference_text=self._messages(state)[-1].content,
            **ordered,
        )
        message = delegation_offers_message(len(offers)) if offers else NO_DELEGATION_OFFERS_MESSAGE
        return {
            "delegation_draft": draft.model_dump(mode="json"),
            **_explained(message),
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
            self._turn_feed(state, runtime).available_candidates[:10],
        )
        return {
            "response": ConversationTurnResponse(
                action="clarify", assistant_message=recovery.assistant_message
            ).model_dump(mode="json")
        }

    def _select_product(self, state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
        conditional = self._conditional(state)
        if conditional is not None and conditional.branches:
            return self._select_branch(state, runtime, conditional)
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
            return self._studio_only(selected, runtime.context.feed)
        return {"selected_variant_id": selected, "selection_message": decision.assistant_message}

    def _studio_only(self, selected: str, feed: ProductFeed | None) -> dict[str, Any]:
        card = self._card(selected, feed)
        name = card["title"] if card is not None else selected
        return {
            "selected_variant_id": None,
            **_asked(
                f"Encontré {name}. Studio solo permite explorar el catálogo; "
                "usa la interfaz del agente para preparar y aprobar una compra."
            ),
        }

    def _select_branch(
        self, state: GraphState, runtime: Runtime[GraphContext], conditional: ConditionalIntent
    ) -> dict[str, Any]:
        """§3.2-§3.3: the backend decides the branch from the merchant's offers, in the buyer's
        order; the selection model is never asked. Every decision is recorded (§3.7)."""
        observer = _observer.get()
        shortlist = [item["candidate"]["variant_id"] for item in state["ranked_candidates"]]
        _started(observer, "select_product", lambda: {"candidate_variant_ids": shortlist})
        hard = self._intent(state).hard
        # The whole feed: an exclusion must still see the variant a branch names (§3.2, step 4).
        resolution = resolve_branches(conditional, runtime.context.feed or self._feed, hard=hard)
        if isinstance(resolution, ConditionalClarification):
            text = _clarification_text(resolution, conditional)
            self._record(state, conditional, hard, resolution, None, text)
            _finished(
                observer,
                "select_product",
                lambda: {"decision": "clarify", "product": None, "rationale": text},
            )
            return {"selected_variant_id": None, **_asked(text)}
        decision = evaluate_branches(resolution, self._offer_reader, LAB_DESTINATION)
        titles = [branch.title for branch in resolution.branches]
        text = _decision_text(decision, titles)
        self._record(state, conditional, hard, resolution, decision, text)
        chosen = decision.chosen
        result: dict[str, Any] = {
            "decision": "clarify" if chosen is None else "select",
            "product": None
            if chosen is None
            else self._card(chosen.variant_id, self._turn_feed(state, runtime)),
            "rationale": text,
        }
        if decision.outcome != "clarify":
            # `offer_changed` only exists after the checkout, which this step never sees: a
            # changed checkout fails `prepare_checkout` with that code instead (§3.4).
            result["conditional"] = conditional_view(resolution, decision).model_dump(mode="json")
        _finished(observer, "select_product", lambda: result)
        if chosen is None:
            answer = _asked(text) if decision.outcome == "clarify" else _explained(text)
            return {"selected_variant_id": None, **answer}
        if not self._allow_checkout:
            return self._studio_only(chosen.variant_id, runtime.context.feed)
        return {
            "selected_variant_id": chosen.variant_id,
            "selection_message": text,
            "conditional": {
                "resolution": resolution.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json"),
            },
        }

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
        conditional = self._conditional(state)
        catalog = runtime.context.feed or self._feed
        if conditional is not None and selected in excluded_variant_ids(conditional, catalog):
            # Defense in depth (§3.6): the shortlist already left it out.
            raise invalid_candidate()
        chosen = _ChosenBranch.of(state.get("conditional"))
        self._discover()
        decision = CommerceDecision.model_validate_json(json.dumps(state["decision"]))
        details = runtime.context.details
        if details is None:
            raise IntentInterpretationError("Checkout details are required")
        try:
            purchase = self._purchases.start(
                run_id=run_id,
                request=decision.purchase_request or self._messages(state)[-1].content,
                # The branch's effective intent: `intent.hard`, its identity and its limit (§3.4).
                intent=self._intent(state) if chosen is None else chosen.intent,
                selected_variant_id=selected,
                details=details,
                graph_thread_id=runtime.context.thread_id,
                owner_id=runtime.context.owner_id,
                feed=runtime.context.feed
                if conditional is None
                else self._turn_feed(state, runtime),
            )
        except PurchaseServiceError as error:
            if chosen is not None and error.code in {"budget_exceeded", "checkout_mismatch"}:
                # `start` rejected the checkout before `verify_checkout` saw it: its row first.
                self._trace_rejected_checkout(run_id, chosen, error)
            if chosen is not None and error.code == "budget_exceeded":
                # `start` canceled it: the offer fit the limit and the checkout did not, so the
                # offer changed in between. In every mode, never a 409 (§3.4).
                return _checkout_stopped(observer, chosen.over_limit_text())
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
        if chosen is not None:
            changed = self._checkout_changed(run_id, chosen, purchase)
            if changed is not None:
                self._purchases.cancel(run_id=run_id)
                return _checkout_stopped(observer, changed)
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
        if state["response"] is not None:
            # `prepare_checkout` canceled a conditional checkout and explained it (§3.4): there
            # is nothing to approve, and no step to show.
            return {}
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
            feed=self._turn_feed(state, runtime),
            intent=self._intent(state),
            rejection_counts=state["rejection_counts"],
        )

    @staticmethod
    def _conditional(state: GraphState) -> ConditionalIntent | None:
        raw = state["decision"].get("conditional")
        return None if raw is None else ConditionalIntent.model_validate_json(json.dumps(raw))

    def _turn_feed(self, state: GraphState, runtime: Runtime[GraphContext]) -> ProductFeed:
        """The turn's feed without what the buyer excluded (§3.6); the feed itself otherwise."""
        feed = runtime.context.feed or self._feed
        conditional = self._conditional(state)
        if conditional is None or not conditional.excluded:
            return feed
        return without_variants(feed, excluded_variant_ids(conditional, feed))

    def _record(
        self,
        state: GraphState,
        conditional: ConditionalIntent,
        hard: HardConstraints,
        resolution: ResolvedConditional | ConditionalClarification,
        decision: ConditionalDecision | None,
        explanation: str,
        *,
        purchase_mode: PurchaseMode = "HP",
    ) -> None:
        """The turn's `decision` row, under the run its purchase has or would have (§3.7)."""
        record_decision(
            self._store,
            turn_run_id(state["turn_id"]),
            purchase_mode=purchase_mode,
            governance_mode=self._governance_mode,
            intent=conditional,
            hard=hard,
            resolution=resolution,
            decision=decision,
            explanation=explanation,
        )

    def _record_exclusions(
        self,
        state: GraphState,
        conditional: ConditionalIntent,
        hard: HardConstraints,
        excluded: tuple[str, ...],
    ) -> None:
        """`exclusions_only`: no branch evaluated, the excluded variants listed (§3.3, §3.9).
        It explains nothing by itself (the normal selection's reply follows), so its
        explanation is empty. A delegation turn drafts an HNP grant: its row is UX07-HNP's."""
        unevaluated = ConditionalIntent(
            excluded=conditional.excluded,
            max_total_amount_minor=conditional.max_total_amount_minor,
            currency=conditional.currency,
        )
        self._record(
            state,
            conditional,
            hard,
            ResolvedConditional(
                intent=unevaluated, hard=hard, branches=(), excluded_variant_ids=excluded
            ),
            EXCLUSIONS_ONLY,
            "",
            purchase_mode="HNP" if state.get("delegate") else "HP",
        )

    def _draft_priority(
        self,
        state: GraphState,
        runtime: Runtime[GraphContext],
        offers: tuple[ProductCard, ...],
    ) -> tuple[str, ...] | None:
        """The branches' order among the draft's offers, when every branch resolves (§3.9).
        Only the order passes to the draft: its limits do not, the buyer writes the total."""
        conditional = self._conditional(state)
        if conditional is None or not conditional.branches:
            return None
        resolution = resolve_branches(
            conditional, runtime.context.feed or self._feed, hard=self._intent(state).hard
        )
        if isinstance(resolution, ConditionalClarification):
            return None
        offered = {offer.variant_id for offer in offers}
        order = tuple(
            branch.variant_id for branch in resolution.branches if branch.variant_id in offered
        )
        return order or None

    def _checkout_changed(
        self, run_id: str, chosen: "_ChosenBranch", purchase: PurchasePreparation
    ) -> str | None:
        """§3.4: the prepared checkout against the offer that chose its branch, recorded either
        way; the reason to cancel it, or None when it matches."""
        # `start` already refused another variant or currency (`checkout_mismatch`), so
        # `verify_checkout` finds the chosen line and only the total can differ.
        check = verify_checkout(self._store, run_id, chosen.decision, purchase.checkout)
        if check.matches:
            return None
        if check.checkout_total_minor == check.offer_total_minor:
            # Same amount, another currency: the copy cannot speak of a new total.
            return MISMATCHED_CHECKOUT.format(title=chosen.title)
        return OFFER_CHANGED.format(
            title=chosen.title,
            offer=_money(check.offer_total_minor),
            checkout=_money(check.checkout_total_minor),
        )

    def _trace_rejected_checkout(
        self, run_id: str, chosen: "_ChosenBranch", error: PurchaseServiceError
    ) -> None:
        """The `checkout_check` of a checkout `start` rejected (§3.4, §3.7): over the limit, or
        of another variant or currency. It is read back from the merchant; when it cannot be,
        the purchase keeps the failure and the turn its explanation."""
        stored = self._store.purchase(run_id) or {}
        checkout_id = error.checkout_id or stored.get("checkout_id")
        if self._checkout_reader is None or not isinstance(checkout_id, str):
            return
        try:
            checkout = self._checkout_reader.get_checkout(checkout_id)
        except Exception:
            _logger.warning("Rejected checkout %s could not be read back", checkout_id)
            return
        lines = [line.get("item", {}).get("id") for line in checkout.line_items]
        if lines == [chosen.evaluation.variant_id]:
            entry = checkout_check_record(
                chosen.decision,
                checkout_id=checkout.id,
                checkout_total_minor=checkout.total_minor,
                checkout_currency=checkout.currency,
            )
        else:
            # `verify_checkout` refuses another variant without a row: the row names it.
            entry = _foreign_checkout(chosen, checkout, lines)
        self._store.append_conditional(run_id, entry)

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


def _after_search(state: GraphState) -> str:
    if state.get("delegate"):
        return "offer_delegation"
    if (state["decision"].get("conditional") or {}).get("branches"):
        # The branches decide with the merchant's offers, whatever the shortlist holds: a
        # recovery would name alternatives the buyer did not authorize (§3.3).
        return "select_product"
    return "select_product" if state["ranked_candidates"] else "recover_no_candidate"


def _explained(message: str) -> dict[str, Any]:
    response = ConversationTurnResponse(action="respond", assistant_message=message)
    return {"response": response.model_dump(mode="json")}


def _asked(message: str) -> dict[str, Any]:
    response = ConversationTurnResponse(action="clarify", assistant_message=message)
    return {"response": response.model_dump(mode="json")}


class _NoOfferReader:
    """Without a reader (Studio) no read succeeds, so no branch is verified (§3.3, step 3)."""

    def get_offer(self, offer_id: str, delivery_context: DeliveryContext) -> MerchantOffer:
        raise RuntimeError("No offer reader is configured")


@dataclass(frozen=True)
class _ChosenBranch:
    """The branch `select_product` chose, as the graph state carries it to the checkout."""

    resolved: ResolvedConditional
    decision: ConditionalDecision

    @classmethod
    def of(cls, stored: dict[str, Any] | None) -> "_ChosenBranch | None":
        if not stored:
            return None
        return cls(
            ResolvedConditional.model_validate_json(json.dumps(stored["resolution"])),
            ConditionalDecision.model_validate_json(json.dumps(stored["decision"])),
        )

    @property
    def evaluation(self) -> BranchEvaluation:
        chosen = self.decision.chosen
        if chosen is None or chosen.offer is None:
            raise ValueError("A chosen branch carries its offer")
        return chosen

    @property
    def intent(self) -> PurchaseIntent:
        return effective_intent(self.resolved, self.evaluation.index)

    @property
    def title(self) -> str:
        return self.resolved.branches[self.evaluation.index].title

    def over_limit_text(self) -> str:
        evaluation = self.evaluation
        assert evaluation.offer is not None
        if evaluation.limit_minor is None:
            return MISMATCHED_CHECKOUT.format(title=self.title)
        return OVER_LIMIT_CHECKOUT.format(
            title=self.title,
            offer=_money(evaluation.offer.total_minor),
            limit=_money(evaluation.limit_minor),
        )


def _foreign_checkout(
    chosen: _ChosenBranch, checkout: CheckoutSession, lines: list[Any]
) -> ConditionalEntry:
    """The `checkout_check` of a checkout that is not the chosen variant: an `offer_changed`
    that also names the variants the merchant put in it."""
    entry = checkout_check_record(
        chosen.decision,
        checkout_id=checkout.id,
        checkout_total_minor=checkout.total_minor,
        checkout_currency=checkout.currency,
    )
    return ConditionalEntry(
        kind="checkout_check",
        outcome="offer_changed",
        body={**entry.body, "outcome": "offer_changed", "checkout_variant_ids": lines},
        branch_index=entry.branch_index,
        variant_id=entry.variant_id,
    )


def _checkout_stopped(observer: TurnObserver | None, message: str) -> dict[str, Any]:
    """A conditional checkout canceled because it is not its offer: the step fails with
    `offer_changed` and the turn explains it; no other branch is tried (§3.4)."""
    if observer is not None:
        observer.step_failed("prepare_checkout", {"code": "offer_changed", "message": message})
    return _explained(message)


# What the buyer reads of a conditional decision (p0-conditional-intent.md §3.8): deterministic,
# in Spanish, amounts by `format_minor_amount`. Titles are the feed's, never model text.

NOT_YET_PREPARED = "Todavía no preparé ningún checkout."
OFFER_CHANGED = (
    "El total de {title} cambió entre la verificación ({offer}) y el checkout ({checkout}); "
    "cancelé ese checkout y no probé otra opción. No se hizo ningún cobro."
)
OVER_LIMIT_CHECKOUT = (
    "El total de {title} cambió entre la verificación ({offer}) y el checkout, y superó tu "
    "límite de {limit}; cancelé ese checkout y no probé otra opción. No se hizo ningún cobro."
)
MISMATCHED_CHECKOUT = (
    "El checkout que preparó el comercio para {title} no coincide con la oferta que verifiqué; "
    "cancelé ese checkout y no probé otra opción. No se hizo ningún cobro."
)


def _money(amount: int) -> str:
    return format_minor_amount(amount, "COP")


def _listed(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} y {items[-1]}"


def _reason(branch: BranchEvaluation, titles: Sequence[str]) -> str:
    """Why a branch was false, from the merchant's facts (§3.8)."""
    offer = branch.offer
    if branch.status == "out_of_stock":
        return "está agotado"
    if branch.status == "inactive":
        return "no está a la venta"
    if branch.status == "intent_mismatch":
        return "no cumple otra condición de tu pedido"
    if branch.status == "not_triggered":
        earlier = titles[: branch.index]
        verb = "se agotaba" if len(earlier) == 1 else "se agotaban"
        return f"solo autorizaste {titles[branch.index]} si {_listed(earlier)} {verb}"
    if branch.status != "over_limit" or offer is None:
        raise ValueError(f"A {branch.status} branch is not false")
    unit = branch.unit_limit_minor
    if unit is not None and offer.items_total_minor > unit:
        return (
            f"su precio sin envío es {_money(offer.items_total_minor)} y tu límite por unidad es "
            f"{_money(unit)}"
        )
    if branch.limit_minor is None:
        raise ValueError("A branch over its total has a limit")
    return (
        f"su total con envío es {_money(offer.total_minor)} y tu límite para esa opción es "
        f"{_money(branch.limit_minor)}"
    )


def _skipped(branches: Sequence[BranchEvaluation], titles: Sequence[str]) -> str:
    return " ".join(
        f"{titles[branch.index]} no cumplía tu condición: {_reason(branch, titles)}."
        for branch in branches
    )


def _decision_text(decision: ConditionalDecision, titles: Sequence[str]) -> str:
    branches = decision.branches
    chosen = decision.chosen
    if chosen is not None:
        if chosen.offer is None:
            raise ValueError("A chosen branch carries its offer")
        title, total = titles[chosen.index], _money(chosen.offer.total_minor)
        limit = chosen.limit_minor
        within = "" if limit is None else f", dentro de tu límite de {_money(limit)}"
        if chosen.index == 0:
            return (
                f"Elegí {title}: es tu primera opción y el comercio confirma un total de {total} "
                f"con envío{within}."
            )
        skipped = _skipped(branches[: chosen.index], titles)
        return f"{skipped} Elegí {title}: total de {total} con envío{within}."
    if decision.outcome == "none":
        reasons = " ".join(f"{titles[b.index]}: {_reason(b, titles)}." for b in branches)
        return (
            f"No compré nada: ninguna de tus opciones cumple tus condiciones. {reasons} No "
            "preparé ningún checkout ni compré otra alternativa."
        )
    if decision.outcome == "undetermined":
        unknown = next(branch for branch in branches if branch.status == "unknown")
        title = titles[unknown.index]
        parts = [_skipped(branches[: unknown.index], titles)] if unknown.index else []
        parts.append(f"No pude verificar {title} con el comercio.")
        if unknown.index + 1 < len(titles):
            place = "primero" if unknown.index == 0 else "antes"
            parts.append(
                f"Como {title} va {place}, no pasé a {titles[unknown.index + 1]} sin saber si "
                f"{title} cumplía."
            )
        parts.append("No preparé ningún checkout. Intenta de nuevo en un momento.")
        return " ".join(parts)
    # `clarify`: a branch against `intent.hard` came before the eligible one (§3.3, step 2).
    mismatch = next(branch for branch in branches if branch.status == "intent_mismatch")
    eligible = next(branch for branch in branches if branch.status == "eligible")
    return (
        f"{titles[mismatch.index]} no cumple otra condición de tu pedido, así que no pasé a "
        f"{titles[eligible.index]}. ¿Quieres revisar tu pedido? {NOT_YET_PREPARED}"
    )


def _described(match: BranchMatch, index: int) -> str:
    """A branch that resolved to no variant, by the words that name it."""
    words = [*match.required_terms, *(option.value for option in match.required_options)]
    return f"«{' '.join(words)}»" if words else f"tu opción {index + 1}"


def _clarification_text(
    clarification: ConditionalClarification, conditional: ConditionalIntent
) -> str:
    index = clarification.branch_index or 0
    named = _described(conditional.branches[index].match, index) if conditional.branches else ""
    if clarification.cause == "branch_not_found":
        question = f"No encontré {named} en el catálogo."
    elif clarification.cause == "branch_ambiguous":
        question = (
            f"Varias opciones coinciden con {named}: {_listed(clarification.titles)}; "
            "¿cuál quieres?"
        )
    elif clarification.cause == "branch_excluded":
        question = (
            f"{clarification.titles[0]} está entre tus opciones, pero también pediste no "
            "comprarlo; ¿cuál de las dos indicaciones vale?"
        )
    elif clarification.cause == "duplicate_branch":
        question = f"Dos de tus opciones son {clarification.titles[0]}; ¿cuál es la otra?"
    else:
        question = (
            "Solo puedo comparar totales en pesos colombianos (COP); ¿me confirmas tus límites "
            "en COP?"
        )
    return f"{question} {NOT_YET_PREPARED}"


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
