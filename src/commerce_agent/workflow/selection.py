from typing import NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from commerce_agent.acp.feed import ProductCandidate, ProductFeed
from commerce_agent.domain.intent import IntentInterpreter, PurchaseIntent
from commerce_agent.domain.selection import RankedCandidate, rank_candidates
from commerce_agent.workflow.state import PurchaseStage


class SelectionInput(TypedDict):
    request: str


class SelectionState(TypedDict):
    request: str
    stage: NotRequired[PurchaseStage]
    intent: NotRequired[PurchaseIntent]
    ranked_candidates: NotRequired[tuple[RankedCandidate, ...]]
    selected_candidate: NotRequired[ProductCandidate | None]
    rejection_counts: NotRequired[dict[str, int]]
    failure_code: NotRequired[str | None]


class SelectionOutput(TypedDict):
    stage: PurchaseStage
    intent: PurchaseIntent
    ranked_candidates: tuple[RankedCandidate, ...]
    selected_candidate: ProductCandidate | None
    rejection_counts: dict[str, int]
    failure_code: str | None


def build_selection_workflow(
    *, feed: ProductFeed, interpreter: IntentInterpreter
) -> CompiledStateGraph[SelectionState, None, SelectionInput, SelectionOutput]:
    def interpret_intent(state: SelectionState) -> dict[str, object]:
        request = state["request"].strip()
        if not request:
            raise ValueError("Purchase request must not be empty")
        return {
            "stage": PurchaseStage.DISCOVERING,
            "intent": interpreter.interpret(request),
        }

    def select_candidate(state: SelectionState) -> dict[str, object]:
        intent = state.get("intent")
        if intent is None:
            raise RuntimeError("Selection workflow reached ranking without an intent")
        result = rank_candidates(feed, intent)
        selected = result.selected
        return {
            "stage": PurchaseStage.SELECTED if selected is not None else PurchaseStage.FAILED,
            "ranked_candidates": result.ranked,
            "selected_candidate": selected,
            "rejection_counts": result.rejection_counts,
            "failure_code": None if selected is not None else "no_matching_candidates",
        }

    graph = StateGraph(
        SelectionState,
        input_schema=SelectionInput,
        output_schema=SelectionOutput,
    )
    graph.add_node("interpret_intent", interpret_intent)
    graph.add_node("select_candidate", select_candidate)
    graph.add_edge(START, "interpret_intent")
    graph.add_edge("interpret_intent", "select_candidate")
    graph.add_edge("select_candidate", END)
    return graph.compile()
