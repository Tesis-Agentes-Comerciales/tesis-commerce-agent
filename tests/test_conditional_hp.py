"""Human Present conditional intent end to end (docs/design/p0-conditional-intent.md §3.2-§3.8).

The production app over the real SDK and the scripted Responses API of
tests/support/model_server.py, real SQLite in tmp_path and the lab merchant of
tests/assistant_support.py, which serves `POST /lab/offers/get` and counts reads by offer and
creations. The router (and, in UX07, the selector) is scripted, so these tests prove the product
and the harness, never the interpretation (§4.6). Every §4.2 cell runs in B2-HP (enforce, governed
flow) on REST and on the chat; the conditional edge after `prepare_checkout` also runs in B0 and
shadow. No model is called and nothing is spent.
"""

import json
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import SecretStr

from commerce_agent.acp import load_product_feed
from commerce_agent.acp.client import ACPClient
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.api import create_app
from commerce_agent.application import CheckoutDetails
from commerce_agent.application.conversation import (
    ConversationService,
    scoped_id,
    turn_run_id,
)
from commerce_agent.domain import HardConstraints, OptionConstraint, PurchaseIntent
from commerce_agent.domain.conditional import BranchMatch, ConditionalIntent, PurchaseBranch
from commerce_agent.domain.intent import SoftPreferences
from commerce_agent.llm import CandidateDecision, CommerceDecision, ConversationMessage
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.conditional_sqlite import ConditionalRecord
from tests.assistant_support import (
    ASSISTANT_FEED,
    CONDITIONAL_A,
    CONDITIONAL_B,
    CONDITIONAL_C,
    FEED_CANDIDATES,
    HNP_ON,
    SHIPPING_MINOR,
    Merchant,
    TwoBuyers,
    add_message,
    assistant_body,
    assistant_client,
    by_variant,
    checkout_details,
    conditional_decision,
    counter_ids,
    lab_offers,
    ordered_branches,
    post_assistant,
    settings,
)
from tests.governance.test_enforce_runtime import decisions as d1_decisions
from tests.support.model_server import ModelServer, structured

A, B, C = CONDITIONAL_A, CONDITIONAL_B, CONDITIONAL_C
AIRPODS = "APL-APP3"
TITLE = {variant_id: candidate.variant_title for variant_id, candidate in FEED_CANDIDATES.items()}
T_A = FEED_CANDIDATES[A].observed_unit_amount_minor + SHIPPING_MINOR
T_B = FEED_CANDIDATES[B].observed_unit_amount_minor + SHIPPING_MINOR
assert T_B < T_A  # UX03-C needs B cheaper than A; C is cheaper than both.
assert FEED_CANDIDATES[C].observed_unit_amount_minor + SHIPPING_MINOR < T_B
REQUEST = "Compra A si el total es de hasta X; si no, B si es de hasta Y."
TURN = "turn-conditional"
RUN = turn_run_id(TURN)
# Not an Anthropic key: the scripted transport answers before any credential is checked.
MODEL = {"anthropic_api_key": SecretStr("scripted-conditional-model"), "agent_model": "test-model"}
D1_KEYS: dict[str, Any] = {
    "agent_d1_key_id": "fixture-key",
    "agent_d1_policy_set_id": "test-backend-d1",
    "agent_d1_policy_revision": "1",
    "agent_d1_hmac_key_hex": SecretStr("1" * 64),
    "agent_d1_aes_key_hex": SecretStr("2" * 64),
}
MODES: dict[str, dict[str, Any]] = {
    "B0": {},
    "shadow": {"agent_governance_mode": "shadow", **D1_KEYS},
    "B2": {"agent_governance_mode": "enforce", "agent_hp_flow": "governed", **D1_KEYS},
}
# What a turn may add to the agent's ledger without an effect (spec §4.4): its own cache, the
# model requests, their timings and the conditional decision. Everything else keeps its rows.
NO_EFFECT_TABLES = {
    "conversation_turns",
    "model_requests",
    "stage_timings",
    "conditional_decisions",
}
NO_CHECKOUT = "No preparé ningún checkout ni compré otra alternativa."
NOT_YET = "Todavía no preparé ningún checkout."
SURFACES = ("rest", "chat")


def money(amount: int) -> str:
    units, cents = divmod(amount, 100)
    rendered = f"{units:,}".replace(",", ".")
    return f"COP {rendered},{cents:02d}" if cents else f"COP {rendered}"


def over_total(variant_id: str, total: int, limit: int) -> str:
    return (
        f"{TITLE[variant_id]}: su total con envío es {money(total)} y tu límite para esa opción "
        f"es {money(limit)}."
    )


# The harness


@dataclass
class Turn:
    """What one turn returned and left behind, read after the app closed."""

    surface: str
    status: int
    body: dict[str, Any]
    state: dict[str, Any] | None
    merchant: Merchant
    server: ModelServer
    before: dict[str, int]
    after: dict[str, int]
    records: tuple[ConditionalRecord, ...]
    database: Path

    @property
    def action(self) -> str:
        if self.state is None:
            return self.body["action"]
        return "purchase" if self.state["purchase"] else "respond-or-clarify"

    @property
    def text(self) -> str:
        if self.state is None:
            return self.body["assistant_message"]
        parts = self.state["messages"][-1]["content"]
        return "".join(part["text"] for part in parts if part["type"] == "text")

    @property
    def decision(self) -> ConditionalRecord:
        found = [record for record in self.records if record.kind == "decision"]
        assert len(found) == 1, found
        return found[0]

    @property
    def statuses(self) -> list[str]:
        return [branch["status"] for branch in self.decision.body["branches"]]

    def steps(self) -> dict[str, dict[str, Any]]:
        assert self.state is not None
        message = self.state["messages"][-1]
        return {
            part["toolName"]: part for part in message["content"] if part["type"] == "tool-call"
        }

    def added(self) -> dict[str, int]:
        return {
            name: self.after.get(name, 0) - count
            for name, count in {**self.after, **self.before}.items()
            if self.after.get(name, 0) != self.before.get(name, 0)
        }


def table_counts(database: Path) -> dict[str, int]:
    with closing(sqlite3.connect(database)) as connection:
        names = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in names
        }


@contextmanager
def app(database: Path, merchant: Merchant, server: ModelServer, mode: str) -> Iterator[TestClient]:
    configured = settings(database).model_copy(update={**MODES[mode], **MODEL})
    with TestClient(
        create_app(
            settings=configured,
            transport=merchant.transport(),
            payment_transport=httpx.MockTransport(
                lambda request: httpx.Response(201, json={"id": "vt_" + "a" * 64})
            ),
            id_factory=counter_ids(),
        )
    ) as client:
        yield client


def run_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    routed: CommerceDecision,
    merchant: Merchant,
    *,
    surface: str = "rest",
    mode: str = "B2",
    selection: Any = None,
    request: str = REQUEST,
) -> Turn:
    answers = [structured(routed)]
    if selection is not None:
        answers.append(structured(selection))
    server = ModelServer(*answers)
    server.install(monkeypatch)
    database = tmp_path / "agent.db"
    state = None
    with app(database, merchant, server, mode) as client:
        before = table_counts(database)
        if surface == "rest":
            response = client.post(
                "/conversation/turn",
                json={
                    "turn_id": TURN,
                    "messages": [{"role": "user", "content": request}],
                    "details": checkout_details(),
                },
            )
            status, body = response.status_code, response.json()
        else:
            _, state = post_assistant(client, assistant_body([add_message(request)], turn_id=TURN))
            status, body = 200, {}
    after = table_counts(database)
    records = SqliteAgentStore(database).conditional_records(RUN)
    return Turn(surface, status, body, state, merchant, server, before, after, records, database)


def assert_zero_effects(turn: Turn, reads: dict[str, int], outcome: str) -> None:
    """§4.4: outside the agent, only the counted offer reads; in its ledger, nothing but the
    turn's cache, its model requests and exactly one new `decision` row."""
    assert turn.merchant.requests == ["POST /lab/offers/get"] * sum(reads.values())
    assert Counter(turn.merchant.offer_reads) == Counter(reads)
    assert turn.merchant.effects() == [] and turn.merchant.created_variants == []
    assert set(turn.added()) <= NO_EFFECT_TABLES, turn.added()
    assert turn.added().get("conditional_decisions") == 1
    assert [(record.kind, record.outcome) for record in turn.records] == [("decision", outcome)]


def assert_answered(turn: Turn, action: str, text: str) -> None:
    """The REST action, or the chat's text with no purchase; the copy is the same on both."""
    if turn.surface == "rest":
        assert turn.status == 200, turn.body
        assert turn.body["action"] == action
    else:
        assert turn.state is not None and turn.state["purchase"] is None
        assert turn.state["messages"][-1]["status"] == {"type": "complete", "reason": "stop"}
    assert turn.text == text


def assert_prepared(turn: Turn, variant_id: str, *, checked: bool = True) -> None:
    """One checkout, of `variant_id`; with a chosen branch, checked against its offer (§3.4)."""
    if turn.surface == "rest":
        assert turn.status == 200, turn.body
        assert turn.body["action"] == "purchase"
        purchase = turn.body["purchase"]
        assert purchase["stage"] == "awaiting_confirmation"
        assert purchase["selected_candidate"]["variant_id"] == variant_id
    else:
        assert turn.state is not None
        assert turn.state["purchase"]["stage"] == "awaiting_confirmation"
        assert tuple(turn.steps()) == (
            "interpret_request",
            "search_catalog",
            "select_product",
            "prepare_checkout",
            "request_approval",
        )
    assert turn.merchant.created_variants == [variant_id]
    checks = [record for record in turn.records if record.kind == "checkout_check"]
    expected = [("checkout_matches", variant_id)] if checked else []
    assert [(check.outcome, check.variant_id) for check in checks] == expected


def lab(**changes: dict[str, Any]) -> Merchant:
    return Merchant(offers=lab_offers(A, B, C, AIRPODS, **changes))


def ux03(first: int | None, second: int | None) -> CommerceDecision:
    return conditional_decision(ordered_branches(first, second), request=REQUEST)


def ux06(*, limit: int | None, hard_total: int | None = None) -> CommerceDecision:
    """«Si A se agota, compra B», with a global limit or only the turn's own total (D14)."""
    return conditional_decision(
        ConditionalIntent(
            branches=(
                PurchaseBranch(match=by_variant(A)),
                PurchaseBranch(match=by_variant(B), only_if_previous_out_of_stock=True),
            ),
            max_total_amount_minor=limit,
            currency="COP",
        ),
        request=REQUEST,
        hard=HardConstraints(
            currency="COP", categories=("headphones",), max_total_amount_minor=hard_total
        ),
    )


# §4.2: B2-HP, REST and chat


@pytest.mark.parametrize("surface", SURFACES)
def test_ux03_a_buys_the_first_option_without_reading_the_second(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    turn = run_turn(monkeypatch, tmp_path, ux03(T_A, T_B - 1), lab(), surface=surface)

    assert_prepared(turn, A)
    assert turn.decision.outcome == "branch" and turn.decision.body["label"] == "A"
    assert (turn.decision.branch_index, turn.decision.variant_id) == (0, A)
    assert turn.statuses == ["eligible", "not_evaluated"]
    assert turn.merchant.reads_before_creation() == [A]
    assert B not in turn.merchant.offer_reads
    assert turn.text == (
        f"Elegí {TITLE[A]}: es tu primera opción y el comercio confirma un total de "
        f"{money(T_A)} con envío, dentro de tu límite de {money(T_A)}."
    )


@pytest.mark.parametrize("surface", SURFACES)
def test_ux03_b_reads_a_over_its_limit_then_buys_b(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    turn = run_turn(monkeypatch, tmp_path, ux03(T_A - 1, T_B), lab(), surface=surface)

    assert_prepared(turn, B)
    assert turn.decision.body["label"] == "B"
    assert turn.statuses == ["over_limit", "eligible"]
    assert turn.decision.body["branches"][0]["offer"]["total_minor"] == T_A
    assert turn.merchant.reads_before_creation() == [A, B]
    assert turn.text == (
        f"{TITLE[A]} no cumplía tu condición: su total con envío es {money(T_A)} y tu límite "
        f"para esa opción es {money(T_A - 1)}. Elegí {TITLE[B]}: total de {money(T_B)} con "
        f"envío, dentro de tu límite de {money(T_B)}."
    )


@pytest.mark.parametrize("surface", SURFACES)
def test_ux03_c_ux04_hp_keeps_the_priority_although_b_is_cheaper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    turn = run_turn(monkeypatch, tmp_path, ux03(T_A, T_B), lab(), surface=surface)

    assert_prepared(turn, A)
    assert turn.decision.body["label"] == "A"
    assert turn.merchant.reads_before_creation() == [A]
    # The selection model is never asked: the router's is the only request.
    assert len(turn.server.requests) == 1
    if surface == "chat":
        select = turn.steps()["select_product"]["result"]
        assert select["decision"] == "select" and select["product"]["variant_id"] == A
        assert select["conditional"]["outcome"] == "branch"
        assert select["conditional"]["chosen_index"] == 0


@pytest.mark.parametrize("surface", SURFACES)
def test_ux03_d_ux05_hp_buys_nothing_and_never_reads_c(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    turn = run_turn(monkeypatch, tmp_path, ux03(T_A - 1, T_B - 1), lab(), surface=surface)

    assert_zero_effects(turn, {A: 1, B: 1}, "none")
    assert turn.decision.body["label"] == "none"
    assert turn.statuses == ["over_limit", "over_limit"]
    text = (
        "No compré nada: ninguna de tus opciones cumple tus condiciones. "
        f"{over_total(A, T_A, T_A - 1)} {over_total(B, T_B, T_B - 1)} {NO_CHECKOUT}"
    )
    assert_answered(turn, "respond", text)
    assert "Sennheiser" not in turn.text and C not in turn.merchant.offer_reads
    if surface == "chat":
        view = turn.steps()["select_product"]["result"]["conditional"]
        assert view["outcome"] == "none" and view["chosen_index"] is None
        assert [branch["total_minor"] for branch in view["branches"]] == [T_A, T_B]


@pytest.mark.parametrize("surface", SURFACES)
def test_ux03_unknown_a_stops_at_the_failed_read_without_trying_b(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    merchant = lab()
    merchant.offer_faults[A] = "drop"
    turn = run_turn(monkeypatch, tmp_path, ux03(T_A, T_B), merchant, surface=surface)

    # Without the fault the cell is invalid, not passed.
    assert merchant.faults_hit == [A]
    assert_zero_effects(turn, {A: 1}, "undetermined")
    assert turn.decision.body["label"] == "undetermined"
    assert turn.statuses == ["unknown", "not_evaluated"]
    assert turn.decision.body["branches"][0]["error_code"] == "transport_error"
    assert_answered(
        turn,
        "respond",
        f"No pude verificar {TITLE[A]} con el comercio. Como {TITLE[A]} va primero, no pasé a "
        f"{TITLE[B]} sin saber si {TITLE[A]} cumplía. No preparé ningún checkout. Intenta de "
        "nuevo en un momento.",
    )


@pytest.mark.parametrize("surface", SURFACES)
def test_ux06_hp_informs_that_a_is_sold_out_and_b_is_over_and_buys_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    turn = run_turn(
        monkeypatch, tmp_path, ux06(limit=T_B - 1), lab(**{A: {"stock": 0}}), surface=surface
    )

    assert_zero_effects(turn, {A: 1, B: 1}, "none")
    assert turn.statuses == ["out_of_stock", "over_limit"]
    assert_answered(
        turn,
        "respond",
        "No compré nada: ninguna de tus opciones cumple tus condiciones. "
        f"{TITLE[A]}: está agotado. {over_total(B, T_B, T_B - 1)} {NO_CHECKOUT}",
    )


@pytest.mark.parametrize("surface", SURFACES)
def test_ux06_hp_ctl_buys_b_once_a_is_sold_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    turn = run_turn(
        monkeypatch, tmp_path, ux06(limit=T_B), lab(**{A: {"stock": 0}}), surface=surface
    )

    assert_prepared(turn, B)
    assert turn.decision.body["label"] == "B"
    assert turn.statuses == ["out_of_stock", "eligible"]
    assert turn.merchant.reads_before_creation() == [A, B]
    assert turn.text == (
        f"{TITLE[A]} no cumplía tu condición: está agotado. Elegí {TITLE[B]}: total de "
        f"{money(T_B)} con envío, dentro de tu límite de {money(T_B)}."
    )


@pytest.mark.parametrize("surface", SURFACES)
def test_ux06_hp_hard_takes_the_limit_from_the_turn_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    """No limit in `conditional`: B's effective limit is `intent.hard`'s total (D14)."""
    routed = ux06(limit=None, hard_total=T_B - 1)
    turn = run_turn(monkeypatch, tmp_path, routed, lab(**{A: {"stock": 0}}), surface=surface)

    assert_zero_effects(turn, {A: 1, B: 1}, "none")
    assert turn.statuses == ["out_of_stock", "over_limit"]
    assert turn.decision.body["branches"][1]["limit_minor"] == T_B - 1


def test_ux06_hp_never_reaches_b_while_a_is_for_sale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """«Solo si A se agota»: A over its limit, but in stock, leaves B unread."""
    turn = run_turn(monkeypatch, tmp_path, ux06(limit=T_A - 1), lab())

    assert_zero_effects(turn, {A: 1}, "none")
    assert turn.statuses == ["over_limit", "not_triggered"]
    assert turn.text == (
        "No compré nada: ninguna de tus opciones cumple tus condiciones. "
        f"{over_total(A, T_A, T_A - 1)} {TITLE[B]}: solo autorizaste {TITLE[B]} si {TITLE[A]} "
        f"se agotaba. {NO_CHECKOUT}"
    )


def ux07() -> CommerceDecision:
    """«Prefiero A, nunca compres B», with B cheaper than A."""
    return conditional_decision(
        ConditionalIntent(excluded=(by_variant(B),)),
        request=REQUEST,
        hard=HardConstraints(
            currency="COP",
            categories=("headphones",),
            required_options=(OptionConstraint(name="color", value="negro"),),
        ),
        preferences=SoftPreferences(terms=("sony",)),
    )


def first_of_the_enum(body: dict[str, Any]) -> dict[str, Any]:
    schema = body["output_config"]["format"]["schema"]["properties"]["selected_variant_id"]
    return {
        "action": "purchase",
        "assistant_message": "Elijo la primera opción.",
        "selected_variant_id": schema["enum"][0],
    }


def selection_enum(server: ModelServer) -> list[str]:
    schema = server.bodies()[1]["output_config"]["format"]["schema"]["properties"][
        "selected_variant_id"
    ]
    return schema["enum"]


@pytest.mark.parametrize("surface", SURFACES)
def test_ux07_hp_a_removes_b_from_the_shortlist_and_from_d1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    turn = run_turn(
        monkeypatch, tmp_path, ux07(), lab(), surface=surface, selection=first_of_the_enum
    )

    # Without branches no offer chose the variant, so there is nothing to check it against.
    assert_prepared(turn, A, checked=False)
    assert turn.decision.outcome == "exclusions_only"
    assert turn.decision.body["excluded_variant_ids"] == [B]
    assert turn.decision.body["branches"] == []
    # No decision read: only the governed freshness read of the checkout, after its creation.
    assert turn.merchant.reads_before_creation() == []
    assert B not in turn.merchant.offer_reads
    assert B not in selection_enum(turn.server) and selection_enum(turn.server)[0] == A
    (d1,) = d1_decisions(turn.database)
    assert B not in d1.policy_input().eligible_variant_ids
    assert A in d1.policy_input().eligible_variant_ids
    if surface == "chat":
        search = turn.steps()["search_catalog"]["result"]
        assert search["rejections"]["excluded"] == 1
        assert B not in [product["variant_id"] for product in search["products"]]


@pytest.mark.parametrize("surface", SURFACES)
def test_ux07_hp_b_a_selector_that_names_b_gets_the_governed_explanation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    def names_b(body: dict[str, Any]) -> dict[str, Any]:
        return {"action": "purchase", "assistant_message": "Elijo B.", "selected_variant_id": B}

    turn = run_turn(monkeypatch, tmp_path, ux07(), lab(), surface=surface, selection=names_b)

    assert B not in selection_enum(turn.server)
    assert turn.merchant.created_variants == [] and turn.merchant.effects() == []
    assert turn.merchant.offer_reads == []
    assert turn.decision.outcome == "exclusions_only"
    assert "verificadas" in turn.text and TITLE[B] not in turn.text
    if surface == "rest":
        assert (turn.status, turn.body["action"]) == (200, "respond")
    else:
        assert turn.steps()["select_product"]["result"]["decision"] == "clarify"


def mismatch(first: str, second: str, *, second_limit: int) -> CommerceDecision:
    """The turn asks for black headphones; the AirPods are white (`intent_mismatch`)."""
    return conditional_decision(
        ConditionalIntent(
            branches=(
                PurchaseBranch(match=by_variant(first)),
                PurchaseBranch(match=by_variant(second), max_total_amount_minor=second_limit),
            ),
            currency="COP",
        ),
        request=REQUEST,
        hard=HardConstraints(
            currency="COP",
            categories=("headphones",),
            required_options=(OptionConstraint(name="color", value="negro"),),
        ),
    )


@pytest.mark.parametrize("surface", SURFACES)
def test_intent_mismatch_before_an_eligible_branch_asks_after_reading_b_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    """A is never skipped without a commercial fact: its offer is not read, B's is, once, and
    the decision is `clarify`, not B (§3.3, step 2; D14)."""
    turn = run_turn(
        monkeypatch, tmp_path, mismatch(AIRPODS, B, second_limit=T_B), lab(), surface=surface
    )

    assert_zero_effects(turn, {B: 1}, "clarify")
    assert turn.statuses == ["intent_mismatch", "eligible"]
    assert turn.decision.body["clarify"] == {
        "cause": "intent_mismatch",
        "branch_index": 0,
        "variant_ids": [AIRPODS],
    }
    assert_answered(
        turn,
        "clarify",
        f"{TITLE[AIRPODS]} no cumple otra condición de tu pedido, así que no pasé a {TITLE[B]}. "
        f"¿Quieres revisar tu pedido? {NOT_YET}",
    )
    if surface == "chat":
        select = turn.steps()["select_product"]["result"]
        assert select["decision"] == "clarify" and "conditional" not in select


def test_intent_mismatch_with_no_eligible_branch_is_none_after_reading_b_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    turn = run_turn(monkeypatch, tmp_path, mismatch(AIRPODS, B, second_limit=T_B - 1), lab())

    assert_zero_effects(turn, {B: 1}, "none")
    assert turn.statuses == ["intent_mismatch", "over_limit"]
    assert f"{TITLE[AIRPODS]}: no cumple otra condición de tu pedido." in turn.text


def test_intent_mismatch_after_the_chosen_branch_does_not_matter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    turn = run_turn(monkeypatch, tmp_path, mismatch(B, AIRPODS, second_limit=T_A), lab())

    assert_prepared(turn, B)
    assert turn.statuses == ["eligible", "not_evaluated"]
    assert turn.merchant.reads_before_creation() == [B]


# §3.2 and D5: what the agent asks instead of evaluating


@pytest.mark.parametrize(
    ("conditional", "hard", "cause", "text"),
    [
        (
            ordered_branches(T_A, T_B, first_match=BranchMatch(variant_id="SON-XM9-BLK")),
            None,
            "branch_not_found",
            f"No encontré tu opción 1 en el catálogo. {NOT_YET}",
        ),
        (
            ordered_branches(T_A, T_B, first_match=BranchMatch(required_terms=("sony wh",))),
            None,
            "branch_ambiguous",
            "Varias opciones coinciden con «sony wh»: Sony WH-1000XM6 negro y Sony WH-1000XM6 "
            f"plata; ¿cuál quieres? {NOT_YET}",
        ),
        (
            ConditionalIntent(
                branches=(PurchaseBranch(match=by_variant(A)),),
                excluded=(BranchMatch(required_terms=("sony",)),),
            ),
            None,
            "branch_excluded",
            f"{TITLE[A]} está entre tus opciones, pero también pediste no comprarlo; ¿cuál de las "
            f"dos indicaciones vale? {NOT_YET}",
        ),
        (
            ordered_branches(T_A, T_B, first_match=by_variant(B)),
            None,
            "duplicate_branch",
            f"Dos de tus opciones son {TITLE[B]}; ¿cuál es la otra? {NOT_YET}",
        ),
        (
            ordered_branches(T_A, T_B),
            HardConstraints(currency="USD", categories=("headphones",)),
            "currency",
            "Solo puedo comparar totales en pesos colombianos (COP); ¿me confirmas tus límites en "
            f"COP? {NOT_YET}",
        ),
    ],
)
def test_an_unresolved_branch_asks_without_reading_any_offer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    conditional: ConditionalIntent,
    hard: HardConstraints | None,
    cause: str,
    text: str,
) -> None:
    routed = conditional_decision(conditional, request=REQUEST, hard=hard)
    turn = run_turn(monkeypatch, tmp_path, routed, lab())

    assert_zero_effects(turn, {}, "clarify")
    assert turn.decision.body["clarify"]["cause"] == cause
    assert_answered(turn, "clarify", text)


# §3.4: one checkout, its total checked; the conditional edge in every mode


@pytest.mark.parametrize("mode", MODES)
def test_a_checkout_whose_total_differs_from_the_offer_is_canceled_and_explained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    """Within the limit and lower than before, it is still a change (D4): no other branch."""
    merchant = lab()
    merchant.checkout_totals[A] = T_A - 10_000_00
    turn = run_turn(monkeypatch, tmp_path, ux03(T_A, T_B), merchant, mode=mode)

    assert merchant.created_variants == [A]
    assert merchant.effects()[-1] == "POST /checkout_sessions/cs_storybook_1/cancel"
    assert merchant.offer_reads == [A]  # neither B nor a freshness read
    assert [(record.kind, record.outcome) for record in turn.records] == [
        ("decision", "branch"),
        ("checkout_check", "offer_changed"),
    ]
    assert turn.records[1].body["checkout_total_minor"] == T_A - 10_000_00
    assert_answered(
        turn,
        "respond",
        f"El total de {TITLE[A]} cambió entre la verificación ({money(T_A)}) y el checkout "
        f"({money(T_A - 10_000_00)}); cancelé ese checkout y no probé otra opción. No se hizo "
        "ningún cobro.",
    )
    store = SqliteAgentStore(turn.database)
    purchase = store.purchase(RUN)
    assert purchase is not None and purchase["stage"] == "canceled"
    assert "approvals" not in turn.added()


def test_the_chat_shows_the_changed_offer_on_the_checkout_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    merchant = lab()
    merchant.checkout_totals[A] = T_A + 10_000_00
    turn = run_turn(monkeypatch, tmp_path, ux03(T_A + 50_000_00, T_B), merchant, surface="chat")

    steps = turn.steps()
    assert tuple(steps) == (
        "interpret_request",
        "search_catalog",
        "select_product",
        "prepare_checkout",
    )
    assert steps["select_product"]["result"]["conditional"]["outcome"] == "branch"
    failed = steps["prepare_checkout"]
    assert failed["isError"] is True
    assert failed["result"]["error"]["code"] == "offer_changed"
    assert failed["result"]["error"]["message"] == turn.text
    assert turn.state is not None and turn.state["purchase"] is None


@pytest.mark.parametrize("mode", MODES)
def test_a_checkout_over_the_limit_is_an_offer_change_not_a_409(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    """The offer fit the limit; the checkout did not. `start` already canceled it (§3.4)."""
    merchant = lab()
    merchant.checkout_totals[A] = T_A + 1
    turn = run_turn(monkeypatch, tmp_path, ux03(T_A, T_B - 1), merchant, mode=mode)

    assert merchant.created_variants == [A]
    assert merchant.effects()[-1] == "POST /checkout_sessions/cs_storybook_1/cancel"
    assert merchant.offer_reads == [A]
    # The canceled checkout is read back so its check is recorded too (§3.4, §3.7).
    assert [(record.kind, record.outcome) for record in turn.records] == [
        ("decision", "branch"),
        ("checkout_check", "offer_changed"),
    ]
    check = turn.records[-1].body
    assert (check["offer_total_minor"], check["checkout_total_minor"]) == (T_A, T_A + 1)
    assert_answered(
        turn,
        "respond",
        f"El total de {TITLE[A]} cambió entre la verificación ({money(T_A)}) y el checkout, y "
        f"superó tu límite de {money(T_A)}; cancelé ese checkout y no probé otra opción. No se "
        "hizo ningún cobro.",
    )


@pytest.mark.parametrize("mode", ["B0", "shadow"])
def test_a_checkout_of_another_variant_is_recorded_before_it_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    """`start` refuses it (`checkout_mismatch`) before `verify_checkout` sees it, which would
    raise without a row: the turn reads it back and records it first. In B2, D1's creation
    check stops it earlier as `d1_dispatch_uncertain`, and its journal is the trace."""
    merchant = lab()
    merchant.checkout_variants[A] = C
    turn = run_turn(monkeypatch, tmp_path, ux03(T_A, T_B), merchant, mode=mode)

    assert (turn.status, turn.body["code"]) == (409, "checkout_mismatch")
    assert merchant.created_variants == [A] and merchant.offer_reads == [A]
    assert [(record.kind, record.outcome) for record in turn.records] == [
        ("decision", "branch"),
        ("checkout_check", "offer_changed"),
    ]
    check = turn.records[-1]
    assert (check.variant_id, check.body["checkout_variant_ids"]) == (A, [C])
    assert check.body["checkout_id"] == "cs_storybook_1"


def test_a_rejected_checkout_that_cannot_be_read_back_still_explains_the_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unreadable(checkout_id: str) -> CheckoutSession:
        raise RuntimeError("synthetic read failure")

    merchant = lab()
    merchant.checkout_totals[A] = T_A + 1
    with patch.object(ACPClient, "get_checkout", unreadable):
        turn = run_turn(monkeypatch, tmp_path, ux03(T_A, T_B - 1), merchant, mode="B0")

    assert [(record.kind, record.outcome) for record in turn.records] == [("decision", "branch")]
    assert turn.body["action"] == "respond" and "superó tu límite" in turn.text


@pytest.mark.parametrize("mode", ["B0", "shadow"])
def test_every_mode_reads_the_offers_and_buys_the_chosen_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    """D1: a product capability, not a governance control."""
    turn = run_turn(monkeypatch, tmp_path, ux03(T_A - 1, T_B), lab(), mode=mode)

    assert_prepared(turn, B)
    # Shadow also runs the freshness read after the creation; it is not a decision read (§4.4).
    assert turn.merchant.reads_before_creation() == [A, B]
    assert turn.decision.body["governance_mode"] == ("off" if mode == "B0" else "shadow")


def test_a_repeated_turn_is_answered_from_its_cache_without_new_rows_or_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = ModelServer(structured(ux03(T_A - 1, T_B - 1)))
    server.install(monkeypatch)
    merchant = lab()
    body = {
        "turn_id": TURN,
        "messages": [{"role": "user", "content": REQUEST}],
        "details": checkout_details(),
    }
    with app(tmp_path / "agent.db", merchant, server, "B2") as client:
        first = client.post("/conversation/turn", json=body)
        again = client.post("/conversation/turn", json=body)

    assert first.json() == again.json()
    assert merchant.offer_reads == [A, B]
    records = SqliteAgentStore(tmp_path / "agent.db").conditional_records(RUN)
    assert [record.outcome for record in records] == ["none"]


# Without a reader nothing is verified, so nothing is bought (Studio builds none)


class StaticRouter:
    def __init__(self, decision: CommerceDecision) -> None:
        self.decision = decision
        self.selections = 0

    def route(self, messages: tuple[ConversationMessage, ...]) -> CommerceDecision:
        return self.decision

    def choose_candidate(self, messages: Any, intent: Any, candidates: Any) -> CandidateDecision:
        self.selections += 1
        return CandidateDecision(
            action="purchase", assistant_message="Elijo B.", selected_variant_id=B
        )

    def recover_no_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        rejection_counts: dict[str, int],
        alternatives: tuple[Any, ...],
    ) -> Any:
        raise AssertionError("No recovery is expected")


def test_without_an_offer_reader_the_decision_is_undetermined(tmp_path: Path) -> None:
    store = SqliteAgentStore(tmp_path / "agent.db")
    store.initialize()
    router = StaticRouter(ux03(T_A, T_B))
    service = ConversationService(
        feed=load_product_feed(ASSISTANT_FEED),
        store=store,
        purchases=None,
        completion=None,
        discover=None,
        router=router,
        checkpointer=SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
        allow_checkout=False,
    )
    response = service.execute(
        (ConversationMessage(role="user", content=REQUEST),),
        CheckoutDetails.model_validate(checkout_details()),
        turn_id=TURN,
    )

    assert response.action == "respond"
    assert response.assistant_message.startswith(f"No pude verificar {TITLE[A]} con el comercio.")
    record = store.latest_conditional_decision(RUN)
    assert record is not None and record.outcome == "undetermined"
    assert record.body["branches"][0]["error_code"] == "offer_read_failed"
    assert router.selections == 0


def test_b0_refuses_an_excluded_variant_before_any_checkout(tmp_path: Path) -> None:
    """Defense in depth (§3.6): a router that names B after the shortlist left it out."""
    merchant = lab()
    router = StaticRouter(ux07())
    with assistant_client(tmp_path / "agent.db", router=router, merchant=merchant) as client:
        response = client.post(
            "/conversation/turn",
            json={
                "turn_id": TURN,
                "messages": [{"role": "user", "content": REQUEST}],
                "details": checkout_details(),
            },
        )

    assert (response.status_code, response.json()["code"]) == (409, "invalid_candidate")
    assert router.selections == 1
    assert merchant.created_variants == [] and merchant.effects() == []
    assert SqliteAgentStore(tmp_path / "agent.db").purchase(RUN) is None


# §3.9: the delegation draft from the chat


@dataclass
class Delegated:
    draft: dict[str, Any]
    records: tuple[ConditionalRecord, ...]
    merchant: Merchant = field(repr=False)


def delegate(tmp_path: Path, conditional: ConditionalIntent) -> Delegated:
    merchant = lab()
    routed = conditional_decision(
        conditional,
        request=REQUEST,
        hard=HardConstraints(
            currency="COP", categories=("headphones",), max_total_amount_minor=2_000_000_00
        ),
    )
    with assistant_client(
        tmp_path / "agent.db",
        router=StaticRouter(routed),
        merchant=merchant,
        governance=HNP_ON,
        buyer_authenticator=TwoBuyers(),
    ) as client:
        _, state = post_assistant(
            client, assistant_body([add_message(REQUEST)], turn_id=TURN, mode="delegate")
        )
    run_id = turn_run_id(scoped_id("buyer-a", TURN))
    records = SqliteAgentStore(tmp_path / "agent.db").conditional_records(run_id)
    return Delegated(state["delegation_draft"], records, merchant)


def test_the_draft_drops_the_exclusions_and_proposes_the_branch_order(tmp_path: Path) -> None:
    conditional = ConditionalIntent(
        branches=(PurchaseBranch(match=by_variant(B)), PurchaseBranch(match=by_variant(A))),
        excluded=(by_variant(C),),
    )
    delegated = delegate(tmp_path, conditional)

    offered = [offer["variant_id"] for offer in delegated.draft["offers"]]
    assert C not in offered and {A, B} <= set(offered)
    assert delegated.draft["priority"] == [B, A]
    # A delegation turn evaluates no branch: no offer is read, and the row lists the exclusion.
    assert delegated.merchant.offer_reads == []
    assert [
        (record.outcome, record.body["excluded_variant_ids"]) for record in delegated.records
    ] == [("exclusions_only", [C])]


def test_a_draft_without_exclusions_writes_no_row(tmp_path: Path) -> None:
    conditional = ConditionalIntent(
        branches=(PurchaseBranch(match=by_variant(A)), PurchaseBranch(match=by_variant(B)))
    )
    delegated = delegate(tmp_path, conditional)

    assert delegated.draft["priority"] == [A, B]
    assert delegated.records == ()


def test_a_draft_with_an_unresolved_branch_proposes_no_order(tmp_path: Path) -> None:
    conditional = ConditionalIntent(
        branches=(PurchaseBranch(match=BranchMatch(required_terms=("sony wh",))),),
        excluded=(by_variant(B),),
    )
    delegated = delegate(tmp_path, conditional)

    assert "priority" not in delegated.draft
    assert B not in [offer["variant_id"] for offer in delegated.draft["offers"]]
    assert [record.outcome for record in delegated.records] == ["exclusions_only"]


def test_the_feed_without_exclusions_keeps_every_other_turn_as_it_was(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A purchase without `conditional` reads no offer to decide and writes no row."""
    routed = CommerceDecision(
        action="purchase",
        assistant_message="Busco audífonos negros.",
        purchase_request=REQUEST,
        intent=PurchaseIntent(
            hard=HardConstraints(
                currency="COP",
                categories=("headphones",),
                required_options=(OptionConstraint(name="color", value="negro"),),
            ),
            preferences=SoftPreferences(terms=("sony",)),
        ),
    )
    turn = run_turn(monkeypatch, tmp_path, routed, lab(), selection=first_of_the_enum)

    assert turn.status == 200 and turn.body["action"] == "purchase"
    assert turn.records == ()
    assert turn.merchant.reads_before_creation() == []
    assert json.dumps(turn.body).count("excluded") == 0
