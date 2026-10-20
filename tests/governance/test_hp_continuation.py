"""Governed Human Present continuation through the API: a mock Merchant and PSP, no model.

A purchase D1 allowed and dispatched continues to approval, cancel and completion only
while its own facts hold; every other purchase keeps `d1_continuation_blocked`.
"""

import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient

from commerce_agent.api import create_app
from commerce_agent.application.conversation import turn_run_id
from commerce_agent.config import Settings
from commerce_agent.domain import PurchaseIntent
from commerce_agent.governance.hp_continuation import GovernedContinuation, governed_continuation
from commerce_agent.llm import CandidateDecision, CommerceDecision
from commerce_agent.persistence import SqliteAgentStore
from tests.governance.test_runtime_dispatch import UNLISTED, Merchant, governed
from tests.governance.test_shadow_integration import configuration
from tests.test_api import StubConversationRouter, checkout_details

DISCOVERY = {
    "protocol": {"name": "acp", "version": "2026-04-17", "supported_versions": ["2026-04-17"]},
    "api_base_url": "http://127.0.0.1:4120",
    "transports": ["rest"],
    "capabilities": {
        "services": ["checkout"],
        "supported_currencies": ["cop"],
        "supported_locales": ["es-CO"],
    },
}
PAYMENT_TOKEN = {"id": "vt_" + "a" * 64}


class StoreMerchant(Merchant):
    """The dispatch double plus discovery, buyer cancel and completion with an order."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/acp.json":
            return httpx.Response(200, json=DISCOVERY)
        details = (request.method, path) == ("POST", "/checkout_sessions/cs_api_1")
        if details and self.fault == "details_error":
            self.requests.append((request.method, path))
            return httpx.Response(500, json={"detail": {"code": "internal"}})
        if path == "/checkout_sessions/cs_api_1/cancel":
            self.requests.append((request.method, path))
            self.current = {**deepcopy(self.current), "status": "canceled"}
            return httpx.Response(200, json=self.current)
        if path == "/checkout_sessions/cs_api_1/complete":
            self.requests.append((request.method, path))
            return httpx.Response(200, json=self._completed())
        return super().__call__(request)

    def _completed(self) -> dict:
        completed = {**deepcopy(self.current), "status": "completed"}
        completed["order"] = {
            "type": "order",
            "id": "ord_api_1",
            "checkout_session_id": "cs_api_1",
            "permalink_url": "http://127.0.0.1:4120/orders/ord_api_1",
            "status": "confirmed",
            "line_items": [],
            "totals": completed["totals"],
        }
        return completed

    def effects(self) -> list[tuple[str, str]]:
        """Commercial writes after creation: buyer cancel and completion."""
        return [call for call in self.requests if call[1].endswith(("/cancel", "/complete"))]


def router() -> StubConversationRouter:
    return StubConversationRouter(
        CommerceDecision(action="purchase", assistant_message="Preparo.", intent=PurchaseIntent()),
        CandidateDecision(
            action="purchase", assistant_message="Elegí Sonora.", selected_variant_id="SON-01"
        ),
    )


def app_client(settings: Settings, merchant: StoreMerchant) -> TestClient:
    return TestClient(
        create_app(
            settings=settings,
            transport=httpx.MockTransport(merchant),
            payment_transport=httpx.MockTransport(
                lambda request: httpx.Response(201, json=PAYMENT_TOKEN)
            ),
            conversation_router=router(),
        )
    )


def ask(client: TestClient, turn_id: str):
    return client.post(
        "/conversation/turn",
        json={
            "turn_id": turn_id,
            "messages": [{"role": "user", "content": "Quiero los audífonos disponibles"}],
            "details": checkout_details(),
        },
    )


def approve(client: TestClient, run_id: str):
    terms = client.get(f"/purchases/{run_id}/approval")
    assert terms.status_code == 200, terms.text
    return client.post(
        f"/purchases/{run_id}/approval",
        json={"expected_fingerprint": terms.json()["fingerprint"]},
    )


def test_s01_governed_purchase_reaches_an_order(tmp_path: Path) -> None:
    merchant = StoreMerchant()
    with app_client(governed(tmp_path / "agent.db"), merchant) as client:
        turn = ask(client, "turn-s01")
        assert turn.status_code == 200, turn.text
        assert turn.json()["action"] == "purchase"
        # The conversation itself presented the terms (request_approval).
        assert turn.json()["approval_terms"]["total_minor"] == 74_000
        run_id = turn.json()["purchase"]["run_id"]
        assert approve(client, run_id).status_code == 200
        completed = client.post(f"/purchases/{run_id}/complete")
        assert completed.status_code == 200, completed.text
        assert completed.json()["order"]["id"] == "ord_api_1"
        assert client.get(f"/purchases/{run_id}").json()["stage"] == "completed"
    assert merchant.creations() == 1
    assert merchant.effects() == [("POST", "/checkout_sessions/cs_api_1/complete")]


def test_governed_purchase_can_be_canceled_and_frees_the_buyer(tmp_path: Path) -> None:
    merchant = StoreMerchant()
    with app_client(governed(tmp_path / "agent.db"), merchant) as client:
        run_id = ask(client, "turn-cancel").json()["purchase"]["run_id"]
        canceled = client.post(f"/purchases/{run_id}/cancel")
        assert canceled.status_code == 200 and canceled.json()["stage"] == "canceled"
        assert client.post(f"/purchases/{run_id}/cancel").status_code == 200
        assert client.get("/me/pending-purchase").json() is None
    assert merchant.effects() == [("POST", "/checkout_sessions/cs_api_1/cancel")]


def blocked_codes(client: TestClient, run_id: str) -> list[tuple[int, str]]:
    responses = [
        client.get(f"/purchases/{run_id}/approval"),
        client.post(f"/purchases/{run_id}/approval", json={"expected_fingerprint": "0" * 64}),
        client.post(f"/purchases/{run_id}/cancel"),
    ]
    return [(response.status_code, response.json()["code"]) for response in responses]


BLOCKED = [(409, "d1_continuation_blocked")] * 3


@pytest.mark.parametrize(
    ("scenario", "settings_change", "fault", "turn_code"),
    [
        ("deny", {"agent_d1_allowed_merchant_origins": UNLISTED}, None, "d1_denied"),
        ("creation_uncertain", {}, "timeout", "d1_dispatch_uncertain"),
        ("details_uncertain", {}, "details_error", "d1_post_create_uncertain"),
    ],
)
def test_purchases_without_every_governed_fact_stay_blocked(
    tmp_path: Path, scenario: str, settings_change: dict, fault: str, turn_code: str
) -> None:
    merchant = StoreMerchant(fault)
    with app_client(governed(tmp_path / "agent.db", **settings_change), merchant) as client:
        turn = ask(client, f"turn-{scenario}")
        assert turn.status_code == 409 and turn.json()["code"] == turn_code
        run_id = turn_run_id(f"turn-{scenario}")
        assert blocked_codes(client, run_id) == BLOCKED
        # Earlier stage checks refuse first (no checkout, no approved graph); still no order.
        assert client.post(f"/purchases/{run_id}/complete").is_client_error
    assert merchant.creations() == (0 if scenario == "deny" else 1)
    assert merchant.effects() == []


def test_another_checkout_id_breaks_the_continuation(tmp_path: Path) -> None:
    settings = governed(tmp_path / "agent.db")
    merchant = StoreMerchant()
    with app_client(settings, merchant) as client:
        run_id = ask(client, "turn-other-checkout").json()["purchase"]["run_id"]
        assert approve(client, run_id).status_code == 200
        with closing(sqlite3.connect(settings.agent_database_path)) as connection, connection:
            connection.execute(
                "UPDATE purchases SET checkout_id='cs_other',state_version=state_version+1 "
                "WHERE run_id=?",
                (run_id,),
            )
        assert blocked_codes(client, run_id) == BLOCKED
        completed = client.post(f"/purchases/{run_id}/complete")
        assert completed.status_code == 409
        assert completed.json()["code"] == "d1_continuation_blocked"
    assert merchant.effects() == []


def test_blocked_flow_keeps_blocking_a_governed_purchase(tmp_path: Path) -> None:
    settings = governed(tmp_path / "agent.db")
    merchant = StoreMerchant()
    with app_client(settings, merchant) as client:
        run_id = ask(client, "turn-flow-change").json()["purchase"]["run_id"]
        assert approve(client, run_id).status_code == 200
    blocked = settings.model_copy(update={"agent_hp_flow": "blocked"})
    with app_client(blocked, merchant) as client:
        assert blocked_codes(client, run_id) == BLOCKED
        completed = client.post(f"/purchases/{run_id}/complete")
        assert completed.status_code == 409
        assert completed.json()["code"] == "d1_continuation_blocked"
    assert merchant.effects() == []


def test_a_deny_leaves_nothing_pending_for_the_next_purchase(tmp_path: Path) -> None:
    denying = governed(tmp_path / "agent.db", agent_d1_allowed_merchant_origins=UNLISTED)
    merchant = StoreMerchant()
    with app_client(denying, merchant) as client:
        assert ask(client, "turn-denied").json()["code"] == "d1_denied"
        assert client.get("/me/pending-purchase").json() is None
    # Same keys and database; a new policy revision allows the configured merchant.
    allowing = denying.model_copy(
        update={"agent_d1_allowed_merchant_origins": None, "agent_d1_policy_revision": "2"}
    )
    with app_client(allowing, merchant) as client:
        turn = ask(client, "turn-after-deny")
        assert turn.status_code == 200, turn.text
        assert turn.json()["approval_terms"] is not None
    assert merchant.creations() == 1


def test_off_mode_never_builds_a_continuation(tmp_path: Path) -> None:
    settings = configuration(tmp_path / "agent.db", mode="off", agent_hp_flow="governed")
    merchant = StoreMerchant()
    with app_client(settings, merchant) as client:
        turn = ask(client, "turn-off")
        assert turn.status_code == 200, turn.text
        with closing(sqlite3.connect(settings.agent_database_path)) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "d1_checkout_operations" not in tables


@dataclass
class GateFacts:
    """The gate's three durable reads, as SQLite returns them for a governed purchase."""

    status: tuple[str, str | None, str] | None = ("confirmed", "cs_1", "operation:1")
    allow: str | None = "operation:1"
    journal: tuple[tuple[str, str], ...] = (("awaiting_confirmation", "cs_1"),)
    unreadable: bool = False

    def recovery_status(self, run_id: str):
        if self.unreadable:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return self.status

    def committed_allow(self, run_id: str):
        return self.allow

    def post_create_flow(self, run_id: str, owner_id: str):
        return self

    def inspect(self):
        return tuple(SimpleNamespace(phase=phase, checkout_id=id) for phase, id in self.journal)


@pytest.mark.parametrize(
    ("facts", "admitted"),
    [
        (GateFacts(), True),
        (GateFacts(allow=None), False),
        (GateFacts(allow="operation:other"), False),
        (GateFacts(status=None), False),
        (GateFacts(status=("uncertain", None, "operation:1")), False),
        (GateFacts(status=("confirmed", "cs_other", "operation:1")), False),
        (GateFacts(journal=()), False),
        (GateFacts(journal=(("uncertain", "cs_1"),)), False),
        (GateFacts(journal=(("canceled", "cs_1"),)), False),
        (GateFacts(journal=(("awaiting_confirmation", "cs_other"),)), False),
        (GateFacts(unreadable=True), False),
    ],
)
def test_each_governed_fact_is_required(tmp_path: Path, facts: GateFacts, admitted: bool) -> None:
    store = SqliteAgentStore(tmp_path / "agent.db")
    store.initialize()
    store.create_purchase("run-1", "request", "owner")
    store.update_purchase("run-1", stage="awaiting_confirmation", checkout_id="cs_1")
    rule = GovernedContinuation(cast(Any, facts), store)
    assert rule.admits("run-1") is admitted
    assert rule.admits("missing-run") is False


def test_a_purchase_without_checkout_is_never_admitted(tmp_path: Path) -> None:
    store = SqliteAgentStore(tmp_path / "agent.db")
    store.initialize()
    store.create_purchase("run-1", "request", "owner")
    assert GovernedContinuation(cast(Any, GateFacts()), store).admits("run-1") is False


@pytest.mark.parametrize(("flow", "built"), [("governed", True), ("blocked", False)])
def test_only_the_governed_flow_builds_the_rule(tmp_path: Path, flow: str, built: bool) -> None:
    settings = configuration(tmp_path / "agent.db", mode="enforce", agent_hp_flow=flow)
    store = SqliteAgentStore(settings.agent_database_path)
    rule = governed_continuation(settings, cast(Any, GateFacts()), store)
    assert isinstance(rule, GovernedContinuation) is built
    assert governed_continuation(settings, None, store) is None
