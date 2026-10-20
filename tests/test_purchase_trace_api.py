"""`GET /purchases/{run_id}/trace` and `GET /me/purchases` on real SQLite, built through the app.

Purchases go through the real conversation, purchase, approval, completion and D1 services; only
the merchant, the payment provider and the model are doubles (tests/assistant_support.py).
"""

import base64
import json
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from assistant_support import (
    CHECKOUT_ID,
    MERCHANT_ORIGIN,
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
from commerce_agent.buyer_auth import BuyerPrincipal
from commerce_agent.config import Settings
from commerce_agent.persistence import ApprovalRecord, SqliteAgentStore
from commerce_agent.trace.models import PurchaseHistory, PurchaseTrace

BUYER = "buyer-a-owner@example.test"
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
    tmp_path: Path, mode: str = "off", merchant: Merchant | None = None, **extra: Any
) -> Iterator[Shop]:
    configuration = governed(tmp_path / "agent.db", mode, **extra)
    app = create_app(
        settings=configuration,
        transport=(merchant or Merchant()).transport(),
        payment_transport=httpx.MockTransport(
            lambda _: httpx.Response(201, json={"id": PAYMENT_TOKEN})
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


def record_order_webhook(shop: Shop, order_id: str) -> None:
    payload = {
        "type": "order_update",
        "data": {
            "type": "order",
            "id": order_id,
            "checkout_session_id": CHECKOUT_ID,
            "status": "shipped",
        },
    }
    shop.store.record_webhook(
        request_id="req_1",
        event_type="order_update",
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

    assert kinds(trace) == [
        "request",
        "selection",
        "policy_decision",
        "checkout_created",
        "approval",
        "payment_authorized",
        "order_confirmed",
        "order_event",
    ]
    assert_chronological(trace)
    assert trace["status"] == "completed" and trace["policy_mode"] == "shadow"
    assert trace["request"] == PURCHASE_REQUEST
    assert trace["merchant"] == MERCHANT_ORIGIN
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
    assert (failure["actor"], failure["state"], failure["code"]) == (
        "merchant",
        "error",
        "purchase_failed",
    )
    assert row["status"] == "failed" and row["order_id"] is None
    assert_chronological(trace)


def test_trace_of_another_buyer_or_unknown_run_is_not_found(tmp_path: Path) -> None:
    with shop(tmp_path, "off") as current:
        run_id = current.prepare()["purchase"]["run_id"]
        foreign = current.client.get(f"/purchases/{run_id}/trace", headers=as_buyer("b"))
        unknown = current.client.get("/purchases/run_missing/trace", headers=as_buyer())
        anonymous = current.client.get(f"/purchases/{run_id}/trace")

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()
    assert anonymous.status_code == 401


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
