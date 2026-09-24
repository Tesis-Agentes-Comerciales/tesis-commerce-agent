import asyncio
from pathlib import Path

from pydantic import SecretStr

from commerce_agent import studio
from commerce_agent.application.conversation import GraphContext, turn_run_id
from commerce_agent.config import Settings
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.llm import CandidateDecision, CommerceDecision
from commerce_agent.persistence import SqliteAgentStore

FEED = Path(__file__).parent / "fixtures" / "feed"


def test_studio_uses_same_graph_without_opening_checkout(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(
        agent_database_path=tmp_path / "agent.db",
        agent_feed_source="file",
        agent_feed_path=FEED,
        openai_api_key=SecretStr("test-key"),
        openai_model="test-model",
    )

    class Router:
        def route(self, _messages):
            return CommerceDecision(
                action="purchase",
                assistant_message="Buscando audífonos.",
                intent=PurchaseIntent(
                    hard=HardConstraints(categories=("headphones",), max_total_amount_minor=80_000)
                ),
            )

        def choose_candidate(self, _messages, _intent, _candidates):
            return CandidateDecision(
                action="purchase",
                assistant_message="Encontré SON-01.",
                selected_variant_id="SON-01",
            )

        def close(self):
            pass

    monkeypatch.setattr(studio, "get_settings", lambda: settings)
    monkeypatch.setattr(studio, "OpenAIConversationRouter", lambda **_: Router())

    async def run() -> dict:
        async with studio.make_graph() as graph:
            assert "select_product" in graph.get_graph().nodes
            conditional_edges = {
                (edge.source, edge.target) for edge in graph.get_graph().edges if edge.conditional
            }
            assert conditional_edges == {
                ("interpret_request", "search_catalog"),
                ("interpret_request", "finalize"),
                ("search_catalog", "select_product"),
                ("search_catalog", "recover_no_candidate"),
                ("select_product", "prepare_checkout"),
                ("select_product", "finalize"),
                ("finalize", "await_approval"),
                ("finalize", "__end__"),
                ("await_approval", "complete_purchase"),
                ("await_approval", "__end__"),
            }
            assert graph.checkpointer is None
            return await graph.ainvoke(
                {
                    "incoming_messages": [
                        {"role": "user", "content": "Quiero audífonos Sonora negros hasta 800 USD"}
                    ],
                    "turn_id": "studio-read-only",
                },
                context=GraphContext(),
            )

    state = asyncio.run(run())
    assert state["response"]["action"] == "clarify"
    assert "Studio solo permite explorar" in state["response"]["assistant_message"]
    assert state["response"]["purchase"] is None
    assert (
        SqliteAgentStore(tmp_path / "agent.studio.db").purchase(turn_run_id("studio-read-only"))
        is None
    )
