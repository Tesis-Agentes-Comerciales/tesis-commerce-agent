"""`POST /assistant` with `mode: "delegate"` (docs/design/hnp-chat-delegate.md).

The delegate turn interprets and searches, then stops before choosing a product: no checkout, no
purchase run and no merchant write. Its shortlist goes to the `delegation_draft` view.
"""

from pathlib import Path
from typing import Any

import pytest

from commerce_agent.application.conversation import (
    PENDING_STAGES,
    delegation_offers_message,
    scoped_id,
    turn_run_id,
)
from commerce_agent.persistence import SqliteAgentStore
from tests.assistant_support import (
    GREETING_REQUEST,
    HNP_ON,
    NO_CANDIDATE_REQUEST,
    PURCHASE_REQUEST,
    Merchant,
    ScriptedRouter,
    TwoBuyers,
    add_message,
    assistant_body,
    assistant_client,
    parse_sse,
    post_assistant,
    rebuild_state,
    sse_body,
)


def delegate_body(text: str, *, turn_id: str = "turn-delegate", **extra: Any) -> dict[str, Any]:
    return assistant_body([add_message(text)], turn_id=turn_id, mode="delegate", **extra)


def tool_names(state: dict[str, Any]) -> list[str]:
    return [
        part["toolName"]
        for message in state["messages"]
        for part in message["content"]
        if part["type"] == "tool-call"
    ]


def test_delegate_searches_and_drafts_without_checkout(tmp_path: Path) -> None:
    router = ScriptedRouter()
    merchant = Merchant()
    with assistant_client(
        tmp_path / "agent.db",
        router=router,
        merchant=merchant,
        governance=HNP_ON,
        buyer_authenticator=TwoBuyers(),
    ) as client:
        _, state = post_assistant(client, delegate_body(PURCHASE_REQUEST))

    draft = state["delegation_draft"]
    searched = next(
        part["result"]["products"]
        for part in state["messages"][-1]["content"]
        if part.get("toolName") == "search_catalog"
    )
    assert [offer["variant_id"] for offer in draft["offers"]] == [
        product["variant_id"] for product in searched
    ]
    assert {"SON-XM6-BLK", "BOS-QCU-BLK"} <= {offer["variant_id"] for offer in draft["offers"]}
    assert all(offer["available"] and offer["currency"] == "COP" for offer in draft["offers"])
    assert draft["budget_minor"] == 2_000_000_00
    assert draft["currency"] == "COP"
    assert draft["preference_text"] == PURCHASE_REQUEST
    assert state["purchase"] is None
    assert tool_names(state) == ["interpret_request", "search_catalog"]
    assert state["messages"][-1]["status"] == {"type": "complete", "reason": "stop"}
    assert [name for name, _ in router.calls] == ["route"]
    assert merchant.requests == []
    store = SqliteAgentStore(tmp_path / "agent.db")
    assert store.purchase(turn_run_id(scoped_id("buyer-a", "turn-delegate"))) is None
    assert store.pending_purchase(PENDING_STAGES, "buyer-a") is None


def test_the_message_that_offers_the_delegation_says_one_unit(tmp_path: Path) -> None:
    """D3 (docs/design/p0-ux08-ux10.md §3.3): whoever asked for two units reads that it is one."""
    with assistant_client(
        tmp_path / "agent.db", governance=HNP_ON, buyer_authenticator=TwoBuyers()
    ) as client:
        _, state = post_assistant(client, delegate_body(PURCHASE_REQUEST))

    assert state["delegation_draft"]["offers"]
    text = state["messages"][-1]["content"][-1]
    assert text["type"] == "text" and "1 unidad" in text["text"]


@pytest.mark.parametrize("count", [1, 3])
def test_the_offer_says_one_unit_with_one_option_or_several(count: int) -> None:
    assert "1 unidad" in delegation_offers_message(count)


def test_delegate_without_candidates_drafts_no_offers(tmp_path: Path) -> None:
    router = ScriptedRouter()
    with assistant_client(
        tmp_path / "agent.db", router=router, governance=HNP_ON, buyer_authenticator=TwoBuyers()
    ) as client:
        _, state = post_assistant(client, delegate_body(NO_CANDIDATE_REQUEST))

    assert state["delegation_draft"]["offers"] == []
    assert state["delegation_draft"]["budget_minor"] == 50_000_00
    assert state["purchase"] is None
    assert [name for name, _ in router.calls] == ["route"]
    text = state["messages"][-1]["content"][-1]
    assert text["type"] == "text" and "No encontré" in text["text"]


def test_delegate_is_refused_while_hnp_is_off(tmp_path: Path) -> None:
    router = ScriptedRouter()
    with assistant_client(tmp_path / "agent.db", router=router) as client:
        response = client.post("/assistant", json=delegate_body(PURCHASE_REQUEST))

    assert response.status_code == 422
    assert response.json() == {
        "code": "delegation_disabled",
        "message": "La compra delegada no está habilitada en este entorno.",
    }
    assert router.calls == []


def test_unknown_mode_is_rejected(tmp_path: Path) -> None:
    with assistant_client(
        tmp_path / "agent.db", governance=HNP_ON, buyer_authenticator=TwoBuyers()
    ) as client:
        body = assistant_body([add_message(PURCHASE_REQUEST)], turn_id="turn-x", mode="auto")
        response = client.post("/assistant", json=body)

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_buyers_never_see_each_other_draft(tmp_path: Path) -> None:
    with assistant_client(
        tmp_path / "agent.db", governance=HNP_ON, buyer_authenticator=TwoBuyers()
    ) as client:
        _, first = post_assistant(client, delegate_body(PURCHASE_REQUEST, turn_id="turn-shared"))
        # The same turn ID from the other buyer is a different turn: no replay of buyer-a's draft.
        body = delegate_body(NO_CANDIDATE_REQUEST, turn_id="turn-shared")
        response = client.post("/assistant", json=body, headers={"Authorization": "Bearer buyer-b"})
        assert response.status_code == 200
        other = rebuild_state(parse_sse(sse_body(response)), body["state"])
        # A draft echoed back in the client state is never forwarded.
        _, greeted = post_assistant(
            client,
            assistant_body([add_message(GREETING_REQUEST)], turn_id="turn-greeting", state=first),
        )

    assert first["delegation_draft"]["offers"]
    assert other["delegation_draft"]["offers"] == []
    assert other["delegation_draft"]["preference_text"] == NO_CANDIDATE_REQUEST
    assert "delegation_draft" not in greeted


def test_delegate_turn_replays_its_draft(tmp_path: Path) -> None:
    router = ScriptedRouter()
    with assistant_client(
        tmp_path / "agent.db", router=router, governance=HNP_ON, buyer_authenticator=TwoBuyers()
    ) as client:
        _, first = post_assistant(client, delegate_body(PURCHASE_REQUEST))
        _, again = post_assistant(client, delegate_body(PURCHASE_REQUEST))

    assert again["delegation_draft"] == first["delegation_draft"]
    assert [name for name, _ in router.calls] == ["route"]


def test_explicit_present_mode_is_the_hp_turn(tmp_path: Path) -> None:
    with assistant_client(tmp_path / "default.db") as client:
        default_body, _ = post_assistant(
            client, assistant_body([add_message(PURCHASE_REQUEST)], turn_id="turn-p")
        )
    with assistant_client(tmp_path / "explicit.db") as client:
        explicit_body, explicit = post_assistant(
            client,
            assistant_body([add_message(PURCHASE_REQUEST)], turn_id="turn-p", mode="present"),
        )

    assert explicit_body == default_body
    assert "delegation_draft" not in explicit
    assert explicit["purchase"]["stage"] == "awaiting_confirmation"
