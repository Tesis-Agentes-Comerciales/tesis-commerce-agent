import json
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from commerce_agent.acp.feed import ProductFeed, load_product_feed_payload
from commerce_agent.acp.models import Address, Buyer, FulfillmentDetails
from commerce_agent.application.conversation import ConversationService
from commerce_agent.application.purchase import CheckoutDetails
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.llm import (
    CandidateDecision,
    CatalogRecovery,
    CommerceDecision,
    ConversationMessage,
)
from commerce_agent.persistence import SqliteAgentStore

FIXTURE = Path(__file__).parent / "fixtures" / "feed"


def _feed(*, available: bool) -> ProductFeed:
    payload = {
        "metadata": json.loads((FIXTURE / "metadata.json").read_text(encoding="utf-8")),
        "products": [
            json.loads(line)
            for line in (FIXTURE / "products.jsonl").read_text(encoding="utf-8").splitlines()
        ],
    }
    payload["products"][0]["variants"][0]["availability"] = {
        "available": available,
        "status": "in_stock" if available else "out_of_stock",
    }
    return load_product_feed_payload(payload)


class Router:
    def __init__(self, feed: ProductFeed, calls: list[str]) -> None:
        self.feed = feed
        self.calls = calls

    def route(self, messages):
        return CommerceDecision(
            action="purchase",
            assistant_message="Busco audífonos.",
            intent=PurchaseIntent(hard=HardConstraints(categories=("headphones",))),
        )

    def choose_candidate(self, messages, intent, candidates):
        self.calls.append(f"select:{len(candidates)}")
        return CandidateDecision(action="clarify", assistant_message="Hay una opción disponible.")

    def recover_no_candidate(self, messages, intent, rejection_counts, alternatives):
        self.calls.append("recover")
        return CatalogRecovery(action="clarify", assistant_message="Se agotó esa opción.")

    def close(self):
        self.calls.append("closed")


def _details() -> CheckoutDetails:
    return CheckoutDetails(
        buyer=Buyer(email="buyer@example.test"),
        fulfillment=FulfillmentDetails(
            name="Buyer",
            email="buyer@example.test",
            phone_number="+573000000000",
            address=Address(
                name="Buyer",
                line_one="Calle 1",
                city="Bogotá",
                state="DC",
                country="CO",
                postal_code="110111",
            ),
        ),
    )


def test_each_graph_turn_uses_one_fresh_feed_snapshot(tmp_path: Path) -> None:
    feeds = [_feed(available=True), _feed(available=False)]
    calls: list[str] = []
    store = SqliteAgentStore(tmp_path / "agent.db")
    store.initialize()
    checkpointer = SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    conversation = ConversationService(
        feed=feeds[0],
        store=store,
        purchases=None,
        completion=None,
        discover=None,
        router=None,
        checkpointer=checkpointer,
        allow_checkout=False,
        feed_provider=lambda: feeds.pop(0),
        router_factory=lambda feed: Router(feed, calls),
    )
    message = (ConversationMessage(role="user", content="Quiero comprar audífonos"),)

    first = conversation.execute(message, _details(), turn_id="first")
    second = conversation.execute(message, _details(), turn_id="second")

    assert first.assistant_message == "Hay una opción disponible."
    assert second.assistant_message == "Se agotó esa opción."
    assert calls == ["select:1", "closed", "recover", "closed"]
