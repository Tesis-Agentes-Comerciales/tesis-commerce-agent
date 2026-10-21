"""`GET /purchases/{run_id}/trace` and `GET /me/purchases` on real SQLite, built through the app.

Purchases go through the real conversation, purchase, approval, completion and D1 services; only
the merchant, the payment provider and the model are doubles (tests/assistant_support.py).
"""

import base64
import json
import logging
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from assistant_support import (
    CHECKOUT_ID,
    MERCHANT_ORIGIN,
    ORDER_ID,
    PURCHASE_REQUEST,
    Merchant,
    ScriptedRouter,
    checkout_details,
    counter_ids,
    settings,
)
from fastapi.testclient import TestClient
from pydantic import SecretStr

from commerce_agent.api import create_app
from commerce_agent.application.purchase import CheckoutDetails
from commerce_agent.buyer_auth import BuyerPrincipal
from commerce_agent.config import Settings
from commerce_agent.governance.evaluator import GovernanceError
from commerce_agent.llm import ConversationMessage
from commerce_agent.persistence import ApprovalRecord, SqliteAgentStore
from commerce_agent.persistence.hp_evidence_sqlite import SqliteHpEvidenceStore
from commerce_agent.trace import PurchaseTraces, sources
from commerce_agent.trace.assembler import utc
from commerce_agent.trace.models import PurchaseHistory, PurchaseTrace
from commerce_agent.trace.sources import GraphCheckpoints
from tests.governance.test_hp_continuation import StoreMerchant, ask
from tests.governance.test_hp_continuation import app_client as governed_client
from tests.governance.test_runtime_dispatch import governed as governed_settings
from tests.test_governed_recovery import (
    PIXEL_8,
    S02_REQUEST,
    S02_RUN_ID,
    PhoneMerchant,
    S02Router,
    conversation,
    hp_recovery_evidence,
    pick_pixel_8,
    recorded_recoveries,
    s02_intent,
)

BUYER = "buyer-a-owner@example.test"
TRACE_FIXTURES = Path(__file__).parents[1] / "ui" / "src" / "storybook" / "trace-fixtures"
# `seller.name` of every variant in the assistant feed: the runtime names the merchant with it.
MERCHANT_NAME = "Synthetic Commerce Merchant"
OTHER = "buyer-b-owner@example.test"
ALLIED_STORE = "https://tienda-aliada.example.test"
PAYMENT_TOKEN = "vt_" + "a" * 64
ELIGIBLE = "BOS-QCU-BLK, SEN-MOM4-BLK, SON-XM6-BLK"
UTC_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")


class TwoBuyers:
    def authenticate(self, authorization: str | None) -> BuyerPrincipal | None:
        return {"Bearer a": BuyerPrincipal(BUYER), "Bearer b": BuyerPrincipal(OTHER)}.get(
            authorization or ""
        )


def as_buyer(buyer: str = "a") -> dict[str, str]:
    return {"Authorization": f"Bearer {buyer}"}


def governed(database: Path, mode: str, **extra: Any) -> Settings:
    d1 = {
        "agent_d1_key_id": "trace-test-key",
        "agent_d1_policy_set_id": "d1-demo-policy",
        "agent_d1_policy_revision": "3",
        "agent_d1_hmac_key_hex": SecretStr(secrets.token_hex(32)),
        "agent_d1_aes_key_hex": SecretStr(secrets.token_hex(32)),
    }
    changes = {"agent_governance_mode": mode, **(d1 if mode != "off" else {}), **extra}
    return settings(database).model_copy(update=changes)


class OffsetMerchant(Merchant):
    """Merchant whose checkout expiry carries a UTC offset instead of `Z`."""

    def _current(self) -> dict[str, Any]:
        return {**super()._current(), "expires_at": "2099-09-20T18:00:00+05:00"}


class DecliningMerchant(Merchant):
    """Merchant that rejects the payment when the purchase is completed."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/complete"):
            return httpx.Response(422, json={"code": "payment_declined", "message": "Declined"})
        return super().handle(request)


class RetryableRejectionMerchant(Merchant):
    """Merchant that refuses /complete with a 4xx the buyer may retry.

    CompletionService already delegated the payment, so it releases the claim back to `approved`.
    """

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/complete"):
            return httpx.Response(400, json={"code": "invalid_request", "message": "Rejected"})
        return super().handle(request)


@dataclass
class EarlyWebhookMerchant(Merchant):
    """Merchant that sends the order webhook before it answers /complete.

    The webhook receiver runs on its own request, so the agent can store it before recording the
    completion the merchant had already confirmed.
    """

    before_answering: Callable[[], None] = lambda: None

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/complete"):
            self.before_answering()
        return super().handle(request)


@dataclass
class Shop:
    client: TestClient
    configuration: Settings

    @property
    def store(self) -> SqliteAgentStore:
        return SqliteAgentStore(self.configuration.agent_database_path)

    def buy(self, turn_id: str = "turn-1", buyer: str = "a", **conversation: str):
        return self.client.post(
            "/conversation/turn",
            json={
                "turn_id": turn_id,
                **conversation,
                "messages": [{"role": "user", "content": PURCHASE_REQUEST}],
                "details": checkout_details(),
            },
            headers=as_buyer(buyer),
        )

    def prepare(self, turn_id: str = "turn-1", **conversation: str) -> dict[str, Any]:
        response = self.buy(turn_id, **conversation)
        assert response.status_code == 200, response.text
        return response.json()

    def approve(self, prepared: dict[str, Any], *, completes: bool = True) -> None:
        run_id = prepared["purchase"]["run_id"]
        approval = self.client.post(
            f"/purchases/{run_id}/approval",
            json={"expected_fingerprint": prepared["approval_terms"]["fingerprint"]},
            headers=as_buyer(),
        )
        assert approval.status_code == 200, approval.text
        completion = self.client.post(f"/purchases/{run_id}/complete", headers=as_buyer())
        assert (completion.status_code == 200) is completes, completion.text

    def trace(self, run_id: str, buyer: str = "a") -> dict[str, Any]:
        response = self.client.get(f"/purchases/{run_id}/trace", headers=as_buyer(buyer))
        assert response.status_code == 200, response.text
        body = response.json()
        assert PurchaseTrace.model_validate_json(response.text).model_dump(mode="json") == body
        return body

    def history(self, buyer: str = "a", **params: Any) -> dict[str, Any]:
        response = self.client.get("/me/purchases", params=params, headers=as_buyer(buyer))
        assert response.status_code == 200, response.text
        body = response.json()
        assert PurchaseHistory.model_validate_json(response.text).model_dump(mode="json") == body
        return body


@contextmanager
def shop(
    tmp_path: Path,
    mode: str = "off",
    merchant: Merchant | None = None,
    *,
    payment_status: int = 201,
    **extra: Any,
) -> Iterator[Shop]:
    configuration = governed(tmp_path / "agent.db", mode, **extra)
    app = create_app(
        settings=configuration,
        transport=(merchant or Merchant()).transport(),
        payment_transport=httpx.MockTransport(
            lambda _: httpx.Response(payment_status, json={"id": PAYMENT_TOKEN})
        ),
        conversation_router=ScriptedRouter(),
        id_factory=counter_ids(),
        buyer_authenticator=TwoBuyers(),
    )
    with TestClient(app) as client:
        yield Shop(client, configuration)


def kinds(trace: dict[str, Any]) -> list[str]:
    return [step["kind"] for step in trace["steps"]]


def step(trace: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(item for item in trace["steps"] if item["kind"] == kind)


def assert_chronological(trace: dict[str, Any]) -> None:
    times = [item["at"] for item in trace["steps"] if item["at"] is not None]
    assert all(UTC_TIME.match(value) for value in [*times, trace["started_at"]])
    assert times == sorted(times)
    assert trace["started_at"] == trace["steps"][0]["at"]
    assert trace["updated_at"] >= times[-1]


def record_order_webhook(
    shop: Shop, order_id: str, event_type: str = "order_update", status: str = "shipped"
) -> None:
    payload = {
        "type": event_type,
        "data": {
            "type": "order",
            "id": order_id,
            "checkout_session_id": CHECKOUT_ID,
            "status": status,
        },
    }
    shop.store.record_webhook(
        request_id=f"req_{event_type}",
        event_type=event_type,
        order_id=order_id,
        payload_sha256="a" * 64,
        payload=payload,
    )


def test_completed_shadow_purchase_has_every_control_in_order(tmp_path: Path) -> None:
    with shop(tmp_path, "shadow") as current:
        prepared = current.prepare()
        run_id = prepared["purchase"]["run_id"]
        current.approve(prepared)
        record_order_webhook(current, "ord_storybook_1")
        trace = current.trace(run_id)
        row = current.history()["purchases"][0]
        # Without the runtime's feed nothing names the origin, and the origin is not a name.
        unnamed = PurchaseTraces(current.configuration).trace(run_id)

    assert (unnamed.merchant, unnamed.merchant_name) == (MERCHANT_ORIGIN, None)
    assert kinds(trace) == [
        "request",
        "selection",
        "policy_decision",
        "checkout_created",
        "freshness_check",
        "freshness_check",
        "approval",
        "payment_authorized",
        "order_confirmed",
        "order_event",
    ]
    assert_chronological(trace)
    assert trace["status"] == "completed" and trace["policy_mode"] == "shadow"
    # Shadow freshness only records: this merchant double serves no live offer to compare.
    assert [
        (item["stage"], item["mode"], item["outcome"], item["state"], item["read_error"])
        for item in freshness(trace)
    ] == [
        ("present", "shadow", "unavailable", "done", "freshness_check_failed"),
        ("approve", "shadow", "unavailable", "done", "freshness_check_failed"),
    ]
    assert trace["request"] == PURCHASE_REQUEST
    assert trace["merchant"] == MERCHANT_ORIGIN
    assert trace["merchant_name"] == MERCHANT_NAME
    assert trace["total"] == {"minor": 1_924_000_00, "currency": "COP"}
    selection = step(trace, "selection")
    assert selection["product"] == "Sony WH-1000XM6 negro"
    assert (selection["total_minor"], selection["currency"]) == (1_899_000_00, "COP")
    assert selection["duration_ms"] is not None
    policy = step(trace, "policy_decision")
    assert policy["state"] == "done" and policy["actor"] == "policy"
    decision = policy["decision"]
    assert (decision["mode"], decision["outcome"], decision["effect"]) == (
        "shadow",
        "allow",
        "observed",
    )
    assert decision["policy"] == {"set_id": "d1-demo-policy", "revision": 3}
    assert decision["rules"] == [
        {"id": "POL-01", "result": "pass", "observed": "SON-XM6-BLK", "expected": ELIGIBLE},
        {
            "id": "POL-02",
            "result": "pass",
            "observed": MERCHANT_ORIGIN,
            "expected": MERCHANT_ORIGIN,
        },
    ]
    assert decision["reason_codes"] == ["ALL_APPLICABLE_POLICIES_SATISFIED"]
    assert policy["duration_ms"] == decision["latency_ms"]
    assert step(trace, "checkout_created")["checkout_id"] == CHECKOUT_ID
    approval = step(trace, "approval")
    assert approval["state"] == "done" and approval["approved_at"] == approval["at"]
    assert approval["expires_at"] == "2099-09-20T13:00:00.000Z"
    payment = step(trace, "payment_authorized")
    assert (payment["max_minor"], payment["currency"]) == (1_924_000_00, "COP")
    order = step(trace, "order_confirmed")
    assert order["order_id"] == "ord_storybook_1" and order["receipts"] == []
    assert step(trace, "order_event")["event_type"] == "order_update"
    assert row == {
        "run_id": run_id,
        "started_at": trace["started_at"],
        "request": PURCHASE_REQUEST,
        "merchant": MERCHANT_ORIGIN,
        "merchant_name": MERCHANT_NAME,
        "total": trace["total"],
        "status": "completed",
        "policy": {"outcome": "allow", "failed_rules": []},
        "approved_at": approval["approved_at"],
        "order_id": "ord_storybook_1",
        "duration_ms": row["duration_ms"],
    }
    assert isinstance(row["duration_ms"], int)


def test_second_purchase_in_a_conversation_shows_its_own_selection(tmp_path: Path) -> None:
    # The chat keeps one LangGraph thread per conversation, so a new turn starts from a
    # checkpoint that still carries the previous turn's selection until the graph clears it.
    # Policy blocks keep the merchant double to one checkout id and leave no pending purchase.
    with shop(tmp_path, "enforce", agent_d1_allowed_merchant_origins=ALLIED_STORE) as current:
        for turn_id in ("turn-1", "turn-2"):
            denied = current.buy(turn_id, conversation_id="chat-1")
            assert denied.status_code == 409 and denied.json()["code"] == "d1_denied"
        newest = current.history()["purchases"][0]["run_id"]
        trace = current.trace(newest)

    selection = step(trace, "selection")
    assert selection["at"] > step(trace, "request")["at"]
    assert selection["duration_ms"] > 0
    assert_chronological(trace)


def test_trace_reads_a_relative_database_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default AGENT_DATABASE_PATH is relative; read-only URIs need an absolute path.
    monkeypatch.chdir(tmp_path)
    with shop(Path("."), "shadow") as current:
        prepared = current.prepare()
        trace = current.trace(prepared["purchase"]["run_id"])

    assert step(trace, "selection")["at"] > step(trace, "request")["at"]


def test_a_webhook_stored_before_the_completion_stays_in_time_order(tmp_path: Path) -> None:
    merchant = EarlyWebhookMerchant()
    with shop(tmp_path, "off", merchant=merchant) as current:
        merchant.before_answering = lambda: record_order_webhook(
            current, ORDER_ID, "order_create", "confirmed"
        )
        prepared = current.prepare()
        current.approve(prepared)
        trace = current.trace(prepared["purchase"]["run_id"])
        row = current.history()["purchases"][0]

    # Both can fall in the same millisecond; the order still follows the writes.
    assert kinds(trace)[-3:] == ["payment_authorized", "order_event", "order_confirmed"]
    assert step(trace, "order_event")["at"] <= step(trace, "order_confirmed")["at"]
    assert_chronological(trace)
    # The purchase still ends at its order, as with a late webhook.
    assert row["status"] == "completed"
    assert row["duration_ms"] == (
        datetime.fromisoformat(step(trace, "order_confirmed")["at"])
        - datetime.fromisoformat(trace["started_at"])
    ) // timedelta(milliseconds=1)


@pytest.mark.parametrize(
    ("webhook_us", "expected"),
    [(100, ["order_event", "order_confirmed"]), (900, ["order_confirmed", "order_event"])],
    ids=["webhook-first", "order-first"],
)
def test_a_webhook_in_the_same_millisecond_keeps_the_order_of_the_writes(
    tmp_path: Path, webhook_us: int, expected: list[str]
) -> None:
    with shop(tmp_path, "off") as current:
        prepared = current.prepare()
        run_id = prepared["purchase"]["run_id"]
        current.approve(prepared)
        record_order_webhook(current, ORDER_ID)
        # One millisecond, a second after the payment: the order at 500 µs, the webhook around it.
        with closing(sqlite3.connect(current.configuration.agent_database_path)) as db, db:
            [stored] = db.execute(
                "SELECT completed_at FROM completion_results WHERE run_id = ?", (run_id,)
            ).fetchone()
            second = datetime.fromisoformat(stored).replace(microsecond=0) + timedelta(seconds=1)
            order_at, webhook_at = (
                (second + timedelta(microseconds=us)).isoformat() for us in (500, webhook_us)
            )
            db.execute(
                "UPDATE completion_results SET completed_at = ? WHERE run_id = ?",
                (order_at, run_id),
            )
            db.execute("UPDATE webhook_events SET received_at = ?", (webhook_at,))
        trace = current.trace(run_id)

    # The trace shows milliseconds, so both read the same; the stored microseconds decide.
    assert step(trace, "order_event")["at"] == step(trace, "order_confirmed")["at"]
    assert kinds(trace)[-2:] == expected


def test_a_late_merchant_webhook_does_not_stretch_the_purchase_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with shop(tmp_path, "shadow") as current:
        prepared = current.prepare()
        run_id = prepared["purchase"]["run_id"]
        current.approve(prepared)
        late = "2099-01-01T00:00:00.000Z"
        monkeypatch.setattr("commerce_agent.persistence.sqlite._timestamp", lambda: late)
        record_order_webhook(current, "ord_storybook_1")
        trace = current.trace(run_id)
        row = current.history()["purchases"][0]

    assert trace["updated_at"] == step(trace, "order_event")["at"] == late
    confirmed = step(trace, "order_confirmed")["at"]
    assert row["duration_ms"] == (
        datetime.fromisoformat(confirmed) - datetime.fromisoformat(trace["started_at"])
    ) // timedelta(milliseconds=1)


def test_enforce_block_by_merchant_rule_shows_origins_not_pseudonyms(tmp_path: Path) -> None:
    with shop(tmp_path, "enforce", agent_d1_allowed_merchant_origins=ALLIED_STORE) as current:
        denied = current.buy()
        assert denied.status_code == 409 and denied.json()["code"] == "d1_denied"
        run_id = current.history()["purchases"][0]["run_id"]
        trace = current.trace(run_id)
        row = current.history()["purchases"][0]

    assert kinds(trace) == ["request", "selection", "policy_decision"]
    assert_chronological(trace)
    assert trace["status"] == "blocked" and trace["policy_mode"] == "enforce"
    assert trace["merchant"] == MERCHANT_ORIGIN and trace["total"] is None
    # Named from the decision's origin: a blocked purchase never had terms.
    assert trace["merchant_name"] == row["merchant_name"] == MERCHANT_NAME
    assert step(trace, "selection")["product"] == "Sony WH-1000XM6 negro"
    policy = step(trace, "policy_decision")
    assert policy["state"] == "blocked"
    decision = policy["decision"]
    assert (decision["mode"], decision["outcome"], decision["effect"]) == (
        "enforce",
        "deny",
        "blocked",
    )
    assert decision["rules"][1] == {
        "id": "POL-02",
        "result": "fail",
        "observed": MERCHANT_ORIGIN,
        "expected": ALLIED_STORE,
    }
    assert decision["reason_codes"] == ["ACTION_OR_MERCHANT_NOT_ALLOWED"]
    assert "merchant:" not in json.dumps(trace)
    assert row["status"] == "blocked"
    assert row["policy"] == {"outcome": "deny", "failed_rules": ["POL-02"]}
    assert row["approved_at"] is None and row["order_id"] is None


def test_policy_off_purchase_has_no_policy_step(tmp_path: Path) -> None:
    with shop(tmp_path, "off") as current:
        prepared = current.prepare()
        current.approve(prepared)
        trace = current.trace(prepared["purchase"]["run_id"])
        row = current.history()["purchases"][0]

    assert trace["policy_mode"] == "off" and trace["status"] == "completed"
    assert kinds(trace) == [
        "request",
        "selection",
        "checkout_created",
        "approval",
        "payment_authorized",
        "order_confirmed",
    ]
    assert_chronological(trace)
    assert row["policy"] is None


def test_purchase_awaiting_approval_normalizes_merchant_expiry(tmp_path: Path) -> None:
    with shop(tmp_path, "off", merchant=OffsetMerchant()) as current:
        prepared = current.prepare()
        trace = current.trace(prepared["purchase"]["run_id"])

    assert trace["status"] == "awaiting_approval"
    assert kinds(trace) == ["request", "selection", "checkout_created", "approval"]
    approval = step(trace, "approval")
    assert approval["state"] == "waiting" and approval["actor"] == "user"
    assert approval["approved_at"] is None and approval["duration_ms"] is None
    assert approval["fingerprint"] == prepared["approval_terms"]["fingerprint"]
    assert approval["expires_at"] == "2099-09-20T13:00:00.000Z"
    assert_chronological(trace)


def test_canceled_purchase_ends_with_the_buyer_cancellation(tmp_path: Path) -> None:
    with shop(tmp_path, "off") as current:
        prepared = current.prepare()
        run_id = prepared["purchase"]["run_id"]
        canceled = current.client.post(f"/purchases/{run_id}/cancel", headers=as_buyer())
        assert canceled.status_code == 200, canceled.text
        trace = current.trace(run_id)

    assert trace["status"] == "canceled"
    assert kinds(trace) == ["request", "selection", "checkout_created", "approval", "canceled"]
    assert step(trace, "approval")["state"] == "skipped"
    cancel = step(trace, "canceled")
    assert (cancel["by"], cancel["actor"], cancel["state"]) == ("user", "user", "done")
    assert_chronological(trace)


def test_payment_rejected_by_the_merchant_ends_in_a_failure(tmp_path: Path) -> None:
    with shop(tmp_path, "off", merchant=DecliningMerchant()) as current:
        prepared = current.prepare()
        current.approve(prepared, completes=False)
        trace = current.trace(prepared["purchase"]["run_id"])
        row = current.history()["purchases"][0]

    assert trace["status"] == "failed"
    assert kinds(trace)[-3:] == ["approval", "payment_authorized", "failed"]
    failure = step(trace, "failed")
    # The merchant's code, stored when the completion was released (purchases.failure_code).
    assert (failure["actor"], failure["state"], failure["code"]) == (
        "merchant",
        "error",
        "payment_declined",
    )
    # The UI fixture shows the failure this backend writes.
    shown = step(json.loads((TRACE_FIXTURES / "failed.json").read_text("utf-8")), "failed")
    assert {key: shown[key] for key in ("actor", "state", "code")} == {
        key: failure[key] for key in ("actor", "state", "code")
    }
    assert row["status"] == "failed" and row["order_id"] is None
    assert_chronological(trace)


def test_a_failure_stored_before_its_code_existed_stays_generic(tmp_path: Path) -> None:
    with shop(tmp_path, "off", merchant=DecliningMerchant()) as current:
        prepared = current.prepare()
        run_id = prepared["purchase"]["run_id"]
        current.approve(prepared, completes=False)
        with closing(sqlite3.connect(current.configuration.agent_database_path)) as db, db:
            db.execute("UPDATE purchases SET failure_code = NULL WHERE run_id = ?", (run_id,))
        trace = current.trace(run_id)

    assert step(trace, "failed")["code"] == "purchase_failed"


def test_a_merchant_4xx_after_delegating_keeps_the_payment_step(tmp_path: Path) -> None:
    with shop(tmp_path, "off", merchant=RetryableRejectionMerchant()) as current:
        prepared = current.prepare()
        run_id = prepared["purchase"]["run_id"]
        current.approve(prepared, completes=False)
        trace = current.trace(run_id)
        purchase = current.store.purchase(run_id)

    # The merchant refused after the provider issued the token: the buyer may retry.
    assert purchase is not None and purchase["stage"] == "approved"
    assert trace["status"] == "in_progress"
    assert kinds(trace)[-2:] == ["approval", "payment_authorized"]
    payment = step(trace, "payment_authorized")
    assert (payment["max_minor"], payment["currency"]) == (1_924_000_00, "COP")
    assert_chronological(trace)


def test_a_failed_delegation_has_no_payment_step(tmp_path: Path) -> None:
    with shop(tmp_path, "off", payment_status=503) as current:
        prepared = current.prepare()
        run_id = prepared["purchase"]["run_id"]
        current.approve(prepared, completes=False)
        failed = current.trace(run_id)
        # Bookkeeping written after the failure proves nothing about the provider.
        later = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        with closing(sqlite3.connect(current.configuration.agent_database_path)) as db, db:
            db.execute(
                "UPDATE purchases SET failure_code = 'delegation_unavailable', updated_at = ? "
                "WHERE run_id = ?",
                (later, run_id),
            )
        rewritten = current.trace(run_id)
        purchase = current.store.purchase(run_id)

    # The delegation key exists, but the provider never returned a token.
    assert purchase is not None and purchase["stage"] == "approved"
    for trace in (failed, rewritten):
        assert kinds(trace).count("payment_authorized") == 0
        assert kinds(trace)[-1] == "approval"


def test_a_merchant_key_older_than_the_delegation_proves_nothing(tmp_path: Path) -> None:
    """Completion created the merchant key before delegating until the trace read it as proof."""
    with shop(tmp_path, "off", merchant=RetryableRejectionMerchant()) as current:
        prepared = current.prepare()
        run_id = prepared["purchase"]["run_id"]
        current.approve(prepared, completes=False)
        with closing(sqlite3.connect(current.configuration.agent_database_path)) as db, db:
            db.execute(
                "UPDATE operation_keys SET created_at = '2026-01-01T00:00:00Z' "
                "WHERE run_id = ? AND operation = 'checkout.complete'",
                (run_id,),
            )
        trace = current.trace(run_id)

    assert "payment_authorized" not in kinds(trace)


def test_sandbox_approval_time_does_not_reorder_the_checks(tmp_path: Path) -> None:
    # AGENT_SANDBOX_CLOCK_AT fixes the approval clock in the past; the checks use the real one.
    sandbox = datetime.fromisoformat("2026-09-10T14:05:00+00:00")
    with shop(tmp_path, "shadow", agent_sandbox_clock_at=sandbox) as current:
        prepared = current.prepare()
        current.approve(prepared)
        trace = current.trace(prepared["purchase"]["run_id"])

    assert kinds(trace) == [
        "request",
        "selection",
        "policy_decision",
        "checkout_created",
        "freshness_check",
        "freshness_check",
        "approval",
        "payment_authorized",
        "order_confirmed",
    ]
    approval = step(trace, "approval")
    # The time shown is still the sandbox's: it is what the approval recorded.
    assert approval["approved_at"] == approval["at"] == "2026-09-10T14:05:00.000Z"
    assert approval["duration_ms"] is None
    real = [item["at"] for item in trace["steps"] if item["kind"] != "approval"]
    assert real == sorted(real)


def test_trace_of_another_buyer_or_unknown_run_is_not_found(tmp_path: Path) -> None:
    with shop(tmp_path, "off") as current:
        run_id = current.prepare()["purchase"]["run_id"]
        foreign = current.client.get(f"/purchases/{run_id}/trace", headers=as_buyer("b"))
        unknown = current.client.get("/purchases/run_missing/trace", headers=as_buyer())
        anonymous = current.client.get(f"/purchases/{run_id}/trace")

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()
    assert anonymous.status_code == 401


def test_history_reads_each_conversation_checkpoints_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []
    read = GraphCheckpoints._read

    def counted(checkpoints: GraphCheckpoints, thread_id: str):
        reads.append(thread_id)
        return read(checkpoints, thread_id)

    monkeypatch.setattr(GraphCheckpoints, "_read", counted)
    with shop(tmp_path, "enforce", agent_d1_allowed_merchant_origins=ALLIED_STORE) as current:
        for turn_id in ("turn-1", "turn-2"):
            denied = current.buy(turn_id, conversation_id="chat-1")
            assert denied.status_code == 409 and denied.json()["code"] == "d1_denied"
        reads.clear()
        rows = current.history()["purchases"]
        assert len(reads) == 1
        traces = [current.trace(row["run_id"]) for row in rows]

    # Each row still reads its own turn from the shared thread.
    assert [row["started_at"] for row in rows] == [trace["started_at"] for trace in traces]
    assert rows[0]["started_at"] > rows[1]["started_at"]


def seed(store: SqliteAgentStore, owner: str, count: int, prefix: str) -> list[str]:
    run_ids = [f"run_{prefix}{index}" for index in range(count)]
    for run_id in run_ids:
        store.create_purchase(run_id, f"Pedido {run_id}", owner)
    return run_ids


def test_history_is_paginated_newest_first_and_only_lists_own_purchases(tmp_path: Path) -> None:
    with shop(tmp_path, "off") as current:
        own = seed(current.store, BUYER, 3, "a")
        other = seed(current.store, OTHER, 1, "b")
        first = current.history(limit=2)
        second = current.history(limit=2, before=first["next_cursor"])
        theirs = current.history(buyer="b")
        foreign_cursor = current.client.get(
            "/me/purchases", params={"before": other[0]}, headers=as_buyer()
        )
        too_large = current.client.get("/me/purchases", params={"limit": 0}, headers=as_buyer())
        anonymous = current.client.get("/me/purchases")

    assert [row["run_id"] for row in first["purchases"]] == [own[2], own[1]]
    assert first["next_cursor"] == own[1]
    assert [row["run_id"] for row in second["purchases"]] == [own[0]]
    assert second["next_cursor"] is None
    assert [row["run_id"] for row in theirs["purchases"]] == other
    assert theirs["purchases"][0]["status"] == "in_progress"
    assert foreign_cursor.status_code == 400
    assert too_large.status_code == 422
    assert anonymous.status_code == 401


def seed_signed_purchase(store: SqliteAgentStore, terms: dict[str, Any]) -> str:
    """A completed AP2 purchase written through the store: mandates and receipts are JWTs."""
    run_id = "run_signed"
    jwt = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJzaWduZWQifQ.c2lnbmF0dXJl"
    store.create_purchase(run_id, PURCHASE_REQUEST, BUYER)
    store.update_purchase(run_id, stage="selected", selected_variant_id="SON-XM6-BLK")
    store.idempotency_key(run_id, "checkout.create")
    store.update_purchase(run_id, stage="checkout_prepared", checkout_id="cs_signed")
    store.save_approval_terms(run_id, {**terms, "checkout_id": "cs_signed"})
    store.update_purchase(run_id, stage="awaiting_confirmation")
    store.save_approval(
        ApprovalRecord(
            run_id=run_id,
            checkout_id="cs_signed",
            checkout_fingerprint=terms["fingerprint"],
            actor_id=BUYER,
            approved_at="2026-10-20T16:06:05.208Z",
            signature="f" * 64,
            consumed_at=None,
            checkout_jwt=jwt + "checkout",
            checkout_mandate=jwt + "mandate",
            payment_mandate=jwt + "payment",
        )
    )
    store.update_purchase(run_id, stage="approved")
    store.idempotency_key(run_id, "payment.delegate")
    assert store.claim_completion(run_id)
    store.record_completion(
        run_id,
        {"id": "cs_signed", "order": {"id": "ord_signed", "checkout_session_id": "cs_signed"}},
        checkout_receipt_jwt=jwt + "receipt",
        payment_receipt_jwt=jwt + "payment-receipt",
    )
    return run_id


def forbidden_values(shop: Shop) -> set[str]:
    configuration = shop.configuration
    secret_settings = (
        configuration.agent_approval_secret,
        configuration.merchant_bearer_token,
        configuration.merchant_webhook_secret,
        configuration.payment_agent_bearer_token,
        configuration.agent_d1_hmac_key_hex,
        configuration.agent_d1_aes_key_hex,
    )
    values = {secret.get_secret_value() for secret in secret_settings if secret is not None}
    values |= {BUYER, OTHER, PAYMENT_TOKEN, "ana@example.test", "+573000000000", "Calle 100"}
    with closing(sqlite3.connect(configuration.agent_database_path)) as connection:
        queries = (
            "SELECT checkout_jwt, checkout_mandate, payment_mandate, signature, actor_id "
            "FROM approvals",
            "SELECT checkout_receipt_jwt, payment_receipt_jwt FROM completion_results",
            "SELECT idempotency_key FROM operation_keys",
            "SELECT canonical_input, input_digest, body FROM governance_evidence",
            "SELECT owner_ref, trace_id FROM d1_provenance_episodes",
            "SELECT ciphertext, nonce FROM d1_provenance_sources",
        )
        for query in queries:
            for row in connection.execute(query):
                for value in row:
                    if isinstance(value, bytes):
                        values |= {value.hex(), base64.b64encode(value).decode()}
                    elif isinstance(value, str):
                        values.add(value)
    return {value for value in values if len(value) >= 8}


def strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def test_responses_never_expose_secrets_tokens_or_owner(tmp_path: Path) -> None:
    with shop(tmp_path, "shadow") as current:
        prepared = current.prepare()
        current.approve(prepared)
        shadow_run = prepared["purchase"]["run_id"]
        signed_run = seed_signed_purchase(current.store, prepared["approval_terms"])
        signed = current.trace(signed_run)
        responses = [current.trace(shadow_run), signed, current.history()]
        forbidden = forbidden_values(current)

    assert step(signed, "authorization_signed")["mandates"] == ["checkout", "payment"]
    assert step(signed, "order_confirmed")["receipts"] == ["checkout", "payment"]
    assert len(forbidden) > 20
    for text in (text for response in responses for text in strings(response)):
        assert not JWT.search(text), text
        leaked = [value for value in forbidden if value in text]
        assert not leaked, (text, leaked)


# Application controls of the governed HP flow (b2-governed-pilot): they come from hp_evidence,
# are attributed to the agent and never appear as policy rules.


def freshness(trace: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in trace["steps"] if item["kind"] == "freshness_check"]


def governed_trace(client: TestClient, run_id: str) -> dict[str, Any]:
    response = client.get(f"/purchases/{run_id}/trace")
    assert response.status_code == 200, response.text
    body = response.json()
    assert PurchaseTrace.model_validate_json(response.text).model_dump(mode="json") == body
    return body


# The demo's S04 rewrites the offer: a new price and a new validity window counted from that moment
# (commerce_lab.managed_catalog.changed_offer). Seconds apart, so both read the same to the minute.
REPRICED_EXPIRES_AT = "2099-09-20T13:00:42Z"


def reprice(merchant: StoreMerchant) -> None:
    """S04 stimulus: the merchant reprices the live offer after the terms were presented."""
    pricing = merchant.offer["pricing"]
    merchant.offer = {
        **merchant.offer,
        "revision": 2,
        "expires_at": REPRICED_EXPIRES_AT,
        "pricing": {**pricing, "items_total_minor": 80_000, "total_minor": 82_000},
    }


def test_s04_trace_shows_the_freshness_block_and_what_changed(tmp_path: Path) -> None:
    settings = governed_settings(tmp_path / "agent.db")
    merchant = StoreMerchant()
    with governed_client(settings, merchant) as client:
        turn = ask(client, "turn-s04")
        assert turn.status_code == 200, turn.text
        run_id = turn.json()["purchase"]["run_id"]
        fresh = governed_trace(client, run_id)
        reprice(merchant)
        approval = client.post(
            f"/purchases/{run_id}/approval",
            json={"expected_fingerprint": turn.json()["approval_terms"]["fingerprint"]},
        )
        assert approval.status_code == 409 and approval.json()["code"] == "checkout_terms_changed"
        trace = governed_trace(client, run_id)
        row = client.get("/me/purchases").json()["purchases"][0]

    assert fresh["status"] == "awaiting_approval"
    assert [item["stage"] for item in freshness(fresh)] == ["present"]
    assert step(fresh, "approval")["state"] == "waiting"

    assert trace["status"] == "stale_terms" and trace["policy_mode"] == "enforce"
    assert kinds(trace) == [
        "request",
        "selection",
        "policy_decision",
        "checkout_created",
        "freshness_check",
        "approval",
        "freshness_check",
    ]
    assert_chronological(trace)
    # Enforce keeps no `checkout.create` key: the creation envelope was prepared with the decision.
    assert step(trace, "checkout_created")["at"] == step(trace, "policy_decision")["at"]
    # The PDP allowed the purchase; the stop belongs to the application control.
    assert step(trace, "policy_decision")["decision"]["outcome"] == "allow"
    assert step(trace, "approval")["state"] == "skipped"
    presented, approved = freshness(trace)
    assert {key: presented[key] for key in ("stage", "mode", "outcome", "state", "actor")} == {
        "stage": "present",
        "mode": "enforce",
        "outcome": "fresh",
        "state": "done",
        "actor": "agent",
    }
    assert presented["changes"] == [] and presented["offer_revision"] == 1
    assert {key: approved[key] for key in ("stage", "outcome", "state", "actor")} == {
        "stage": "approve",
        "outcome": "changed",
        "state": "blocked",
        "actor": "agent",
    }
    assert approved["changes"] == [
        {"field": "subtotal_minor", "presented": 72_000, "current": 80_000},
        {"field": "total_minor", "presented": 74_000, "current": 82_000},
        {
            "field": "expires_at",
            "presented": "2099-09-20T13:00:00.000Z",
            "current": "2099-09-20T13:00:42.000Z",
        },
    ]
    assert (approved["currency"], approved["offer_revision"], approved["read_error"]) == (
        "COP",
        2,
        None,
    )
    assert approved["id"] == "freshness_check:2" and approved["duration_ms"] is None
    assert approved["evidence_digest"].startswith("sha256:")
    assert row["status"] == "stale_terms"
    assert row["policy"] == {"outcome": "allow", "failed_rules": []}


def test_s04_in_the_chat_stops_when_approving_rereads_the_terms(tmp_path: Path) -> None:
    """The UI's order: «Aprobar» first reads the terms again (GET), and only then would POST."""
    settings = governed_settings(tmp_path / "agent.db")
    merchant = StoreMerchant()
    with governed_client(settings, merchant) as client:
        turn = ask(client, "turn-s04-chat")
        assert turn.status_code == 200, turn.text
        run_id = turn.json()["purchase"]["run_id"]
        # Rereading unchanged terms checks again but adds no row.
        assert client.get(f"/purchases/{run_id}/approval").status_code == 200
        shown = governed_trace(client, run_id)
        reprice(merchant)
        reread = client.get(f"/purchases/{run_id}/approval")
        assert reread.status_code == 409 and reread.json()["code"] == "checkout_terms_changed"
        trace = governed_trace(client, run_id)
        row = client.get("/me/purchases").json()["purchases"][0]
        canceled = client.post(f"/purchases/{run_id}/cancel")
        assert canceled.status_code == 200, canceled.text
        after = governed_trace(client, run_id)

    assert [(item["stage"], item["outcome"]) for item in freshness(shown)] == [("present", "fresh")]
    assert trace["status"] == row["status"] == "stale_terms"
    assert kinds(trace) == [
        "request",
        "selection",
        "policy_decision",
        "checkout_created",
        "freshness_check",
        "approval",
        "freshness_check",
    ]
    assert_chronological(trace)
    # No POST: the stop is at `present`, after the terms were shown, and nothing was signed.
    assert step(trace, "approval")["state"] == "skipped"
    assert step(trace, "approval")["approved_at"] is None
    presented, reread_check = freshness(trace)
    assert (presented["stage"], presented["outcome"], presented["state"]) == (
        "present",
        "fresh",
        "done",
    )
    assert (reread_check["stage"], reread_check["outcome"], reread_check["state"]) == (
        "present",
        "changed",
        "blocked",
    )
    assert [change["field"] for change in reread_check["changes"]] == [
        "subtotal_minor",
        "total_minor",
        "expires_at",
    ]
    assert after["status"] == "canceled"
    assert kinds(after)[-4:] == ["freshness_check", "approval", "freshness_check", "canceled"]


def test_a_repeated_blocked_check_is_one_row_and_canceling_keeps_the_checks(
    tmp_path: Path,
) -> None:
    settings = governed_settings(tmp_path / "agent.db")
    merchant = StoreMerchant()
    with governed_client(settings, merchant) as client:
        turn = ask(client, "turn-retry")
        run_id = turn.json()["purchase"]["run_id"]
        fingerprint = turn.json()["approval_terms"]["fingerprint"]
        reprice(merchant)
        # Each attempt checks again and records it; the same blocked result is one row.
        for _ in range(3):
            approval = client.post(
                f"/purchases/{run_id}/approval", json={"expected_fingerprint": fingerprint}
            )
            assert approval.status_code == 409, approval.text
        stale = governed_trace(client, run_id)
        canceled = client.post(f"/purchases/{run_id}/cancel")
        assert canceled.status_code == 200, canceled.text
        trace = governed_trace(client, run_id)

    assert stale["status"] == "stale_terms"
    assert [(item["stage"], item["outcome"], item["state"]) for item in freshness(stale)] == [
        ("present", "fresh", "done"),
        ("approve", "changed", "blocked"),
    ]
    assert trace["status"] == "canceled"
    assert kinds(trace)[-4:] == ["freshness_check", "approval", "freshness_check", "canceled"]
    assert step(trace, "approval")["state"] == "skipped"
    assert_chronological(trace)


def test_s02_budget_recovery_appears_after_the_cancellation(tmp_path: Path) -> None:
    merchant = PhoneMerchant()
    router = S02Router(s02_intent(max_total=100_000_000), pick_pixel_8())
    with conversation(tmp_path, router, merchant, hp_recovery_evidence(tmp_path)) as service:
        response = service.execute(
            (ConversationMessage(role="user", content=S02_REQUEST),),
            CheckoutDetails.model_validate_json(json.dumps(checkout_details())),
            turn_id="turn-s02",
            conversation_id="conversation-s02",
        )
    assert response.action == "respond"
    traces = PurchaseTraces(governed(tmp_path / "agent.db", "off"))
    trace = traces.trace(S02_RUN_ID).model_dump(mode="json")

    assert trace["status"] == "canceled"
    assert kinds(trace) == [
        "request",
        "selection",
        "checkout_created",
        "canceled",
        "governed_recovery",
    ]
    # Canceled before any terms, with the policy off: no origin, so no name either.
    assert trace["merchant"] is None and trace["merchant_name"] is None
    assert step(trace, "canceled")["by"] == "budget"
    recovery = step(trace, "governed_recovery")
    assert recovery == {
        "id": "governed_recovery:1",
        "at": None,
        "duration_ms": None,
        "actor": "agent",
        "state": "done",
        "kind": "governed_recovery",
        "recovery": "budget_exceeded_after_checkout",
        "cause": "budget",
        "variant_id": PIXEL_8,
        "budget_minor": 100_000_000,
        "observed_minor": 154_000_000,
        "currency": "COP",
        "evidence_digest": recovery["evidence_digest"],
    }
    assert recovery["evidence_digest"].startswith("sha256:")
    # Neither the buyer's text nor the explanation, not even as the evidence's digests.
    recorded = recorded_recoveries(tmp_path)[0]
    leaked = {recorded["intent_sha256"], recorded["explanation_sha256"], response.assistant_message}
    assert not leaked & set(strings(trace))


def tamper_hp_evidence(database: Path, run_id: str) -> None:
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TRIGGER IF EXISTS hp_evidence_no_update")
        connection.execute(
            "UPDATE hp_evidence SET body = json_set(body, '$.outcome', 'changed') WHERE run_id = ?",
            (run_id,),
        )


def assert_unavailable(shop: Shop, run_id: str) -> None:
    response = shop.client.get(f"/purchases/{run_id}/trace", headers=as_buyer())
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "Purchase trace unavailable"}


def test_a_tampered_hp_evidence_chain_makes_the_trace_unavailable(tmp_path: Path) -> None:
    settings = governed_settings(tmp_path / "agent.db")
    with governed_client(settings, StoreMerchant()) as client:
        run_id = ask(client, "turn-tamper").json()["purchase"]["run_id"]
        tamper_hp_evidence(settings.agent_database_path, run_id)
        trace = client.get(f"/purchases/{run_id}/trace")
        history = client.get("/me/purchases")

    assert trace.status_code == 503
    assert history.status_code == 200
    assert [row["status"] for row in history.json()["purchases"]] == ["trace_unavailable"]


def test_a_broken_purchase_marks_only_its_own_history_row(tmp_path: Path) -> None:
    with shop(tmp_path, "shadow") as current:
        prepared = current.prepare()
        broken = prepared["purchase"]["run_id"]
        current.approve(prepared)
        [newer] = seed(current.store, BUYER, 1, "newer")
        tamper_hp_evidence(current.configuration.agent_database_path, broken)
        assert_unavailable(current, broken)
        rows = current.history()["purchases"]
        intact = current.trace(newer)
        purchase = current.store.purchase(broken)

    assert purchase is not None and purchase["created_at"] is not None
    assert [row["run_id"] for row in rows] == [newer, broken]
    assert rows[0]["status"] == intact["status"] == "in_progress"
    # Only what the purchase row stores: status, policy, terms and times need the evidence.
    assert rows[1] == {
        "run_id": broken,
        "started_at": utc(purchase["created_at"]),
        "request": PURCHASE_REQUEST,
        "merchant": None,
        "merchant_name": None,
        "total": None,
        "status": "trace_unavailable",
        "policy": None,
        "approved_at": None,
        "order_id": ORDER_ID,
        "duration_ms": None,
    }


def test_a_purchase_row_outside_the_contract_keeps_only_its_run_id(tmp_path: Path) -> None:
    with shop(tmp_path, "off") as current:
        broken = current.prepare()["purchase"]["run_id"]
        [newer] = seed(current.store, BUYER, 1, "newer")
        with closing(sqlite3.connect(current.configuration.agent_database_path)) as db, db:
            db.execute(
                "UPDATE purchases SET created_at = 'yesterday', request_text = '', order_id = '', "
                "state_version = state_version + 1 WHERE run_id = ?",
                (broken,),
            )
        assert_unavailable(current, broken)
        rows = current.history()["purchases"]

    assert {row["run_id"]: row["status"] for row in rows} == {
        broken: "trace_unavailable",
        newer: "in_progress",
    }
    assert next(row for row in rows if row["run_id"] == broken) == {
        "run_id": broken,
        "started_at": None,
        "request": None,
        "merchant": None,
        "merchant_name": None,
        "total": None,
        "status": "trace_unavailable",
        "policy": None,
        "approved_at": None,
        "order_id": None,
        "duration_ms": None,
    }


@pytest.mark.parametrize(
    ("code", "retry_after"),
    [(sqlite3.SQLITE_BUSY, "1"), (sqlite3.SQLITE_LOCKED, "1"), (sqlite3.SQLITE_IOERR, None)],
    ids=["busy", "locked", "io-error"],
)
def test_a_locked_database_marks_the_row_and_asks_to_retry_the_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    code: int,
    retry_after: str | None,
) -> None:
    def unreadable(*_: object) -> None:
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = code
        raise error

    settings = governed_settings(tmp_path / "agent.db")
    with governed_client(settings, StoreMerchant()) as client:
        run_id = ask(client, "turn-locked").json()["purchase"]["run_id"]
        monkeypatch.setattr(sources, "read_run", unreadable)
        caplog.set_level(logging.WARNING, logger="commerce_agent.trace.assembler")
        trace = client.get(f"/purchases/{run_id}/trace")
        history = client.get("/me/purchases")

    # Only a lock is worth retrying; any other read error is evidence the trace cannot use.
    assert (trace.status_code, trace.headers.get("retry-after")) == (503, retry_after)
    assert trace.json() == {"detail": "Purchase trace unavailable"}
    assert [row["status"] for row in history.json()["purchases"]] == ["trace_unavailable"]
    assert f"run_id={run_id}" in caplog.text


def test_a_policy_artifact_that_does_not_verify_makes_the_trace_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ledger checks its own episode; the trace also binds the artifact to the last decision.
    def mismatch(*_: object) -> None:
        raise GovernanceError("GOV_ARTIFACT_MISMATCH")

    with shop(tmp_path, "shadow") as current:
        run_id = current.prepare()["purchase"]["run_id"]
        monkeypatch.setattr("commerce_agent.trace.sources.verify_artifact", mismatch)
        assert_unavailable(current, run_id)
        row = current.history()["purchases"][0]

    assert (row["status"], row["policy"]) == ("trace_unavailable", None)


def rewrite_terms(database: Path, run_id: str, **changes: object) -> None:
    with closing(sqlite3.connect(database)) as connection, connection:
        (terms,) = connection.execute(
            "SELECT terms FROM approval_terms WHERE run_id = ?", (run_id,)
        ).fetchone()
        connection.execute(
            "UPDATE approval_terms SET terms = ? WHERE run_id = ?",
            (json.dumps({**json.loads(terms), **changes}), run_id),
        )


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"expires_at": "mañana"}, id="malformed-expiry"),
        pytest.param({"currency": "pesos"}, id="contract-rejects-the-currency"),
        pytest.param({"fingerprint": ""}, id="contract-rejects-an-empty-text"),
    ],
)
def test_stored_terms_outside_the_contract_make_the_trace_unavailable(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    with shop(tmp_path, "off") as current:
        run_id = current.prepare()["purchase"]["run_id"]
        rewrite_terms(current.configuration.agent_database_path, run_id, **changes)
        assert_unavailable(current, run_id)
        row = current.history()["purchases"][0]

    assert row["run_id"] == run_id and row["status"] == "trace_unavailable"


# A freshness_check body as OfferFreshnessGuard._body writes it.
FRESHNESS_BODY = {
    "checkout_id": CHECKOUT_ID,
    "stage": "approve",
    "observed_revision": None,
    "compared": {},
    "outcome": "unavailable",
    "changed_fields": [],
    "reason": "freshness_check_failed",
    "governance_mode": "shadow",
    "decision": "allow",
    "checked_at": "2026-10-20T16:00:00Z",
}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"stage": "approve", "outcome": "fresh"}, id="missing-fields"),
        pytest.param({**FRESHNESS_BODY, "changed_fields": "total_minor"}, id="text-not-list"),
        pytest.param({**FRESHNESS_BODY, "compared": {"total_minor": 1}}, id="pair-not-object"),
        pytest.param({**FRESHNESS_BODY, "stage": "complete"}, id="stage-outside-contract"),
        pytest.param({**FRESHNESS_BODY, "checked_at": "ayer"}, id="malformed-time"),
    ],
)
def test_an_hp_evidence_body_the_trace_cannot_read_makes_it_unavailable(
    tmp_path: Path, body: dict[str, object]
) -> None:
    with shop(tmp_path, "shadow") as current:
        run_id = current.prepare()["purchase"]["run_id"]
        # Appended through the store: the chain verifies, only the body has another shape.
        evidence = SqliteHpEvidenceStore(current.configuration.agent_database_path)
        evidence.append(run_id, "freshness_check", {**body, "run_id": run_id})
        assert evidence.verify_chain(run_id)
        assert_unavailable(current, run_id)
        row = current.history()["purchases"][0]

    assert row["status"] == "trace_unavailable"
