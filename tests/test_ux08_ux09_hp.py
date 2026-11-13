"""UX08-HP and UX09-HP in CI, in B0 and B2-HP (docs/design/p0-ux08-ux10.md §4.2 and §5.1).

The runtime's own Claude router and candidate chooser over the scripted transport of
tests/support/model_server.py: the real SDK, no model. B0 is the assistant harness of
tests/assistant_support.py, whose Merchant prepares the variant the creation asks for. B2-HP is
enforce with the governed flow, the built-in `b2-hp-v2` and AP2 Human Present, over the AP2
Merchant and payment provider doubles of tests/support/ap2_purchase.py, here selling two in-stock
variants. Each cell's oracle is its row of §4.2.

- UX08-HP: with A awaiting approval, asking for B gets the pending-purchase guard before any model
  call; once A is canceled, B is a new run with its own checkout and fingerprint, and nothing
  approved for A serves B. The control approves B with its own fingerprint and buys B.
- UX09-HP-a/-b: «Detener respuesta» while the model interprets (a) or chooses (b): the answer lands
  after the stream closed and nothing is prepared; the next turn reaches the model, not the guard.
  UX09-HP-race (a real disconnect over uvicorn) lives in tests/test_assistant_transport.py.
"""

import asyncio
import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from assistant_stream import create_run
from fastapi.testclient import TestClient
from pydantic import SecretStr

from commerce_agent.api import create_app
from commerce_agent.application.conversation import PENDING_PURCHASE_MESSAGE
from commerce_agent.assistant_transport import AssistantRequest
from commerce_agent.buyer_auth import LocalBuyerAuthenticator
from commerce_agent.domain import PurchaseIntent
from commerce_agent.llm import CommerceDecision
from tests.assistant_support import (
    Merchant,
    add_message,
    assistant_body,
    checkout_details,
    counter_ids,
    post_assistant,
    settings,
)
from tests.governance.test_runtime_dispatch import CREATE, LIVE_OFFER, OFFER_READ
from tests.governance.test_shadow_integration import FIXTURE
from tests.governance.test_stale_terms_pending import ap2
from tests.support.ap2_purchase import BUYER, Ap2PaymentProvider, CompletingMerchant
from tests.support.model_server import Answer, Held, ModelServer, held, structured

MODEL_KEY = "sk-test-ux08-ux09-0123456789abcdef"
REQUEST_A = "Quiero los audífonos negros"
REQUEST_B = "Mejor quiero los blancos"
RESPOND = "Entendido. Dime qué quieres buscar."


def intent(color: str) -> PurchaseIntent:
    return PurchaseIntent.model_validate(
        {
            "hard": {
                "categories": ["headphones"],
                "required_options": [{"name": "color", "value": color}],
            }
        }
    )


def route(request: str, color: str) -> Answer:
    """`route_a` / `route_b` (§4.1): the router's purchase decision for one variant."""
    return structured(
        CommerceDecision(
            action="purchase",
            assistant_message=f"Busco los audífonos {color}.",
            purchase_request=request,
            intent=intent(color),
        )
    )


def choose(variant_id: str) -> Answer:
    """`choose_a` / `choose_b`: the variant, which the request's schema must admit."""

    def answer(body: dict[str, Any]) -> dict[str, Any]:
        schema = body["output_config"]["format"]["schema"]["properties"]["selected_variant_id"]
        assert variant_id in (schema.get("enum") or schema["anyOf"][0]["enum"])
        return {
            "action": "purchase",
            "assistant_message": f"Elijo {variant_id}.",
            "selected_variant_id": variant_id,
        }

    return structured(answer)


ROUTE_RESPOND = structured(CommerceDecision(action="respond", assistant_message=RESPOND))


# The two conditions -----------------------------------------------------------------------------


# The prices of the governance fixture feed.
PRICES = {"SON-01": 72_000, "SON-07": 56_000}


class TwoVariantMerchant(CompletingMerchant):
    """The AP2 Merchant double selling SON-01 (black) and SON-07 (white): the live offer, the
    session's line and its price are the variant's."""

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self.offers = {
            variant: {
                **LIVE_OFFER,
                "id": variant,
                "name": f"Audifonos Sonora {variant}",
                "pricing": {
                    **LIVE_OFFER["pricing"],
                    "items_total_minor": price,
                    "total_minor": price + LIVE_OFFER["pricing"]["shipping_total_minor"],
                },
            }
            for variant, price in PRICES.items()
        }
        self.variant = "SON-01"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        call = (request.method, request.url.path)
        if call == OFFER_READ:
            self.requests.append(call)
            offer = self.offers[json.loads(request.content)["offer_id"]]
            return httpx.Response(200, json={"ok": True, "data": offer})
        if call == CREATE:
            self.variant = json.loads(request.content)["line_items"][0]["id"]
            self.offer = self.offers[self.variant]
        return super().__call__(request)

    def _priced(self, session_id: str) -> dict[str, Any]:
        created = super()._priced(session_id)
        item = created["line_items"][0]["item"]
        item.update(id=self.variant, name=f"Audifonos Sonora {self.variant}")
        return created


@dataclass
class Shop:
    """One condition's app: its client, its doubles and the buyer the chat runs as."""

    client: TestClient
    database: Path
    owner: str
    variants: tuple[str, str]  # A (black) and B (white)
    merchant: Any
    payments: list[str]

    def posts(self) -> int:
        return self.calls().count("POST /checkout_sessions")

    def calls(self) -> list[str]:
        if isinstance(self.merchant, Merchant):
            return list(self.merchant.requests)
        return [f"{method} {path}" for method, path in self.merchant.requests]


def two_variant_feed(directory: Path) -> Path:
    """The governance fixture feed with SON-07 back in stock: two variants to change between."""
    shutil.copytree(FIXTURE, directory)
    products = directory / "products.jsonl"
    lines = []
    for line in products.read_text(encoding="utf-8").splitlines():
        product = json.loads(line)
        for variant in product["variants"]:
            variant["availability"] = {"available": True, "status": "in_stock"}
        lines.append(json.dumps(product, ensure_ascii=False))
    products.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory


@contextmanager
def shop(tmp_path: Path, condition: str) -> Iterator[Shop]:
    database = tmp_path / "agent.db"
    model = {"anthropic_api_key": SecretStr(MODEL_KEY), "agent_model": "test-model"}
    if condition == "B0":
        merchant: Any = Merchant()
        payments: list[str] = []

        def token(request: httpx.Request) -> httpx.Response:
            payments.append(request.url.path)
            return httpx.Response(201, json={"id": "vt_" + "a" * 64})

        app = create_app(
            settings=settings(database).model_copy(update=model),
            transport=merchant.transport(),
            payment_transport=httpx.MockTransport(token),
            id_factory=counter_ids(),
        )
        owner = "local-human"
    else:
        configured = ap2(database).model_copy(
            update={
                **model,
                "agent_d1_policy_set_id": "b2-hp-v2",
                "agent_feed_path": two_variant_feed(tmp_path / "feed"),
            }
        )
        condition_of = (
            configured.agent_governance_mode,
            configured.agent_hp_flow,
            configured.agent_d1_policy_set_id,
            configured.agent_ap2_required,
        )
        assert condition_of == ("enforce", "governed", "b2-hp-v2", True)
        merchant = TwoVariantMerchant(configured.merchant_base_url)
        provider = Ap2PaymentProvider()
        payments = []

        def psp(request: httpx.Request) -> httpx.Response:
            payments.append(request.url.path)
            return provider(request)

        app = create_app(
            settings=configured,
            transport=httpx.MockTransport(merchant),
            payment_transport=httpx.MockTransport(psp),
            buyer_authenticator=LocalBuyerAuthenticator(BUYER),
        )
        owner = BUYER
    with TestClient(app) as client:
        yield Shop(client, database, owner, VARIANTS[condition], merchant, payments)


# A (black) and B (white) of each condition's feed, with their colors there.
VARIANTS = {"B0": ("SON-XM6-BLK", "APL-APP3"), "B2-HP": ("SON-01", "SON-07")}
COLORS = {"B0": ("negro", "blanco"), "B2-HP": ("black", "white")}


def scripted(monkeypatch: pytest.MonkeyPatch, *answers: Answer) -> ModelServer:
    """The runtime builds its model clients with the app: install before `shop`."""
    model = ModelServer(*answers)
    model.install(monkeypatch)
    return model


def a_and_b(condition: str) -> list[Answer]:
    """`route_a`, `choose_a`, `route_b`, `choose_b`."""
    (variant_a, variant_b), (a, b) = VARIANTS[condition], COLORS[condition]
    return [route(REQUEST_A, a), choose(variant_a), route(REQUEST_B, b), choose(variant_b)]


def rows(database: Path, query: str) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(database)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        table = query.split(" FROM ", 1)[1].split()[0]
        return connection.execute(query).fetchall() if table in tables else []


def count(database: Path, table: str) -> int:
    found = rows(database, f"SELECT count(*) FROM {table}")
    return found[0][0] if found else 0


def steps(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    content = state["messages"][-1]["content"]
    return {part["toolName"]: part for part in content if part["type"] == "tool-call"}


def texts(state: dict[str, Any]) -> list[str]:
    return [part["text"] for part in state["messages"][-1]["content"] if part["type"] == "text"]


def approve(shop: Shop, run_id: str, fingerprint: str, checkout_jwt: str | None) -> Any:
    body: dict[str, Any] = {"expected_fingerprint": fingerprint}
    if checkout_jwt is not None:
        body["checkout_jwt"] = checkout_jwt
    return shop.client.post(f"/purchases/{run_id}/approval", json=body)


def refused(response: Any, code: str) -> bool:
    return response.status_code == 409 and response.json()["code"] == code


CONDITIONS = pytest.mark.parametrize("condition", ["B0", "B2-HP"])


# UX08-HP ----------------------------------------------------------------------------------------


@dataclass
class Switch:
    """What UX08-HP leaves before any approval: A canceled, B awaiting with its own terms."""

    run_a: str
    run_b: str
    terms_a: dict[str, Any]
    terms_b: dict[str, Any]


def a_to_b(shop: Shop, model: ModelServer, condition: str) -> Switch:
    """Turn 1 «Quiero A»; turn 2 «Mejor quiero B» on the chat and on REST, with A pending; cancel
    A; turn 3 «Mejor quiero B»: the oracle of UX08-HP up to the three refused actions."""
    variant_a, variant_b = shop.variants
    _, first = post_assistant(shop.client, assistant_body([add_message(REQUEST_A)], turn_id="t1"))
    run_a = first["purchase"]["run_id"]
    terms_a = steps(first)["request_approval"]["result"]["terms"]
    assert first["purchase"]["stage"] == "awaiting_confirmation"
    assert [line["item_id"] for line in terms_a["lines"]] == [variant_a]
    assert (len(model.requests), shop.posts()) == (2, 1)

    # Turn 2: the guard answers before the model, on both surfaces; route_b stays queued.
    _, guarded = post_assistant(
        shop.client, assistant_body([add_message(REQUEST_B)], turn_id="t2", state=first)
    )
    rest = shop.client.post(
        "/conversation/turn",
        json={
            "turn_id": "t2-rest",
            "messages": [{"role": "user", "content": REQUEST_B}],
            "details": checkout_details(),
        },
    )
    assert texts(guarded) == [PENDING_PURCHASE_MESSAGE]
    assert rest.status_code == 200, rest.text
    assert (rest.json()["action"], rest.json()["assistant_message"]) == (
        "clarify",
        PENDING_PURCHASE_MESSAGE,
    )
    assert (len(model.requests), shop.posts()) == (2, 1)
    pending = shop.client.get("/me/pending-purchase").json()
    assert pending["purchase"]["run_id"] == run_a
    assert pending["approval_terms"]["fingerprint"] == terms_a["fingerprint"]

    canceled = shop.client.post(f"/purchases/{run_a}/cancel")
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["stage"] == "canceled"
    assert sum(call.endswith("/cancel") for call in shop.calls()) == 1

    _, third = post_assistant(
        shop.client, assistant_body([add_message(REQUEST_B)], turn_id="t3", state=guarded)
    )
    run_b = third["purchase"]["run_id"]
    terms_b = steps(third)["request_approval"]["result"]["terms"]
    assert third["purchase"]["stage"] == "awaiting_confirmation"
    assert [line["item_id"] for line in terms_b["lines"]] == [variant_b]
    assert run_b != run_a and terms_b["checkout_id"] != terms_a["checkout_id"]
    assert terms_b["fingerprint"] != terms_a["fingerprint"]
    assert (len(model.requests), shop.posts()) == (4, 2)
    if condition == "B2-HP":
        assert terms_a["checkout_jwt"] and terms_b["checkout_jwt"]
    return Switch(run_a, run_b, terms_a, terms_b)


def refuse_everything_of_a(shop: Shop, switch: Switch) -> None:
    """Approve B with A's fingerprint (and B's signed checkout), approve the canceled A, complete
    B without approval: three 409s, and nothing reaches `/complete` or the payment provider."""
    jwt_b = switch.terms_b.get("checkout_jwt")
    with_a = approve(shop, switch.run_b, switch.terms_a["fingerprint"], jwt_b)
    old = approve(
        shop, switch.run_a, switch.terms_a["fingerprint"], switch.terms_a.get("checkout_jwt")
    )
    unapproved = shop.client.post(f"/purchases/{switch.run_b}/complete")

    assert refused(with_a, "checkout_terms_changed"), with_a.text
    assert refused(old, "invalid_stage"), old.text
    assert refused(unapproved, "invalid_stage"), unapproved.text
    assert not any(call.endswith("/complete") for call in shop.calls())
    assert shop.payments == []
    for table in ("approvals", "completion_results", "orders", "ap2_evidence"):
        assert count(shop.database, table) == 0, table
    assert rows(shop.database, "SELECT phase FROM governance_evidence WHERE phase = 'D2'") == []


@CONDITIONS
def test_ux08_hp_nothing_approved_for_a_serves_b(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, condition: str
) -> None:
    model = scripted(monkeypatch, *a_and_b(condition))
    with shop(tmp_path, condition) as current:
        switch = a_to_b(current, model, condition)
        refuse_everything_of_a(current, switch)

        assert current.client.get(f"/purchases/{switch.run_b}").json()["stage"] == (
            "awaiting_confirmation"
        )
    assert rows(current.database, "SELECT run_id, stage FROM purchases ORDER BY rowid") == [
        (switch.run_a, "canceled"),
        (switch.run_b, "awaiting_confirmation"),
    ]


@CONDITIONS
def test_ux08_hp_control_b_approved_with_its_own_fingerprint_buys_b(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, condition: str
) -> None:
    model = scripted(monkeypatch, *a_and_b(condition))
    with shop(tmp_path, condition) as current:
        variant_b = current.variants[1]
        switch = a_to_b(current, model, condition)
        refuse_everything_of_a(current, switch)

        # The UI rereads the terms before the click (GET), then approves with them.
        terms = current.client.get(f"/purchases/{switch.run_b}/approval").json()
        assert terms["fingerprint"] == switch.terms_b["fingerprint"]
        approved = approve(current, switch.run_b, terms["fingerprint"], terms.get("checkout_jwt"))
        assert approved.status_code == 200, approved.text
        completed = current.client.post(f"/purchases/{switch.run_b}/complete")
        assert completed.status_code == 200, completed.text
        order = completed.json()["order"]
        assert order["checkout_session_id"] == switch.terms_b["checkout_id"]
        assert [line["item"]["id"] for line in completed.json()["line_items"]] == [variant_b]
        status_a = current.client.get(f"/purchases/{switch.run_a}").json()
        assert (status_a["stage"], status_a["order_id"]) == ("canceled", None)

    assert sum(call.endswith("/complete") for call in current.calls()) == 1
    assert current.payments.count("/agentic_commerce/delegate_payment") == 1
    assert rows(current.database, "SELECT run_id FROM approvals") == [(switch.run_b,)]
    assert rows(current.database, "SELECT run_id, stage FROM purchases ORDER BY rowid") == [
        (switch.run_a, "canceled"),
        (switch.run_b, "completed"),
    ]


# UX09-HP ----------------------------------------------------------------------------------------


def stop_while(shop: Shop, answer: Held, turn_id: str) -> None:
    """«Detener respuesta»: the chat's stream closes while `answer` is held, then it lands."""
    transport = shop.client.app.state.assistant_transport  # type: ignore[attr-defined]
    request = AssistantRequest.model_validate_json(
        json.dumps(assistant_body([add_message(REQUEST_A)], turn_id=turn_id))
    )

    async def scenario() -> None:
        async def callback(controller: Any) -> None:
            await transport.run(controller, request, shop.owner)

        stream = create_run(callback, state=None)
        await stream.__anext__()
        assert await asyncio.to_thread(answer.entered.wait, 10)
        await stream.aclose()
        answer.release()
        await asyncio.get_running_loop().shutdown_default_executor()

    asyncio.run(scenario())


@CONDITIONS
@pytest.mark.parametrize("held_at", ["a-interpreting", "b-choosing"])
def test_ux09_hp_stopping_while_the_model_answers_prepares_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, condition: str, held_at: str
) -> None:
    """UX09-HP-a holds `route_a`; UX09-HP-b answers `route_a` and holds `choose_a`, with the
    catalog already read. The next turn is answered by the model (`respond`), not the guard."""
    routed, chosen = route(REQUEST_A, COLORS[condition][0]), choose(VARIANTS[condition][0])
    answer = held(routed if held_at.startswith("a") else chosen)
    # A stop while interpreting never reaches the choice: `choose_a` is not in -a's script.
    script = [answer] if held_at.startswith("a") else [routed, answer]
    model = scripted(monkeypatch, *script, ROUTE_RESPOND)
    with shop(tmp_path, condition) as current:
        stop_while(current, answer, "t-stop")

        assert current.posts() == 0
        assert rows(current.database, "SELECT stage FROM purchases") == []
        assert rows(
            current.database, "SELECT response FROM conversation_turns WHERE turn_id LIKE '%t-stop'"
        ) == [(None,)]
        stopped_at = len(model.requests)
        assert stopped_at == (1 if held_at.startswith("a") else 2)
        _, after = post_assistant(
            current.client, assistant_body([add_message("hola")], turn_id="t2")
        )
        assert texts(after) == [RESPOND]
        assert len(model.requests) == stopped_at + 1
        assert current.client.get("/me/pending-purchase").json() is None
    assert current.posts() == 0
    assert current.payments == []


@CONDITIONS
def test_ux09_hp_control_the_same_script_without_stopping_prepares_the_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, condition: str
) -> None:
    scripted(monkeypatch, route(REQUEST_A, COLORS[condition][0]), choose(VARIANTS[condition][0]))
    with shop(tmp_path, condition) as current:
        _, state = post_assistant(
            current.client, assistant_body([add_message(REQUEST_A)], turn_id="t")
        )

        assert state["purchase"]["stage"] == "awaiting_confirmation"
    assert current.posts() == 1
    assert current.payments == []
