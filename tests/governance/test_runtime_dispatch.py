"""AGENT_HP_FLOW through build_runtime: the real ACP client against a mock Merchant.

The governed flow dispatches a committed D1 allow with one POST and continues to the
human approval; the blocked default keeps stopping at the durable reservation.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from commerce_agent.application import PurchaseServiceError
from commerce_agent.config import Settings
from commerce_agent.runtime import AgentRuntime, build_runtime
from tests.governance.test_shadow_integration import configuration, start
from tests.test_api import checkout

UNLISTED = "https://otro-comercio.example"
CREATE = ("POST", "/checkout_sessions")


class Merchant:
    """ACP checkout double: one session, the same body on POST and on the next GET."""

    def __init__(self, fault: str | None = None) -> None:
        self.fault = fault
        self.requests: list[tuple[str, str]] = []
        self.create_keys: list[str | None] = []
        self.current = checkout("not_ready_for_payment", with_details=False)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if (request.method, request.url.path) == CREATE:
            self.create_keys.append(request.headers.get("Idempotency-Key"))
            return self._created(request)
        if request.url.path == "/checkout_sessions/cs_api_1":
            if request.method == "POST":
                self.current = checkout("ready_for_payment", with_details=True)
            return httpx.Response(200, json=self.current)
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    def _created(self, request: httpx.Request) -> httpx.Response:
        if self.fault == "timeout":
            raise httpx.ReadTimeout("Merchant answered too late", request=request)
        if self.fault == "server_error":
            return httpx.Response(500, json={"detail": {"code": "internal"}})
        if self.fault == "malformed":
            return httpx.Response(201, json={"id": "cs_api_1"})
        return httpx.Response(201, json=self.current)

    def creations(self) -> int:
        return self.requests.count(CREATE)


def governed(path: Path, **changes) -> Settings:
    return configuration(path, mode="enforce", agent_hp_flow="governed", **changes)


def runtime(settings: Settings, merchant: Merchant) -> AgentRuntime:
    return build_runtime(
        settings,
        transport=httpx.MockTransport(merchant),
        payment_transport=httpx.MockTransport(merchant),
    )


def operation_key(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        (key,) = connection.execute("SELECT idempotency_key FROM d1_checkout_operations").fetchone()
    return key


def test_governed_allow_creates_the_checkout_once_and_awaits_confirmation(tmp_path: Path) -> None:
    settings = governed(tmp_path / "agent.db")
    merchant = Merchant()
    built = runtime(settings, merchant)
    try:
        prepared = start(built.purchases)
        assert prepared.stage == "awaiting_confirmation"
        assert prepared.checkout.status == "ready_for_payment"
        assert built.d1_gate is not None
        status = built.d1_gate.recovery_status("run-real")
        assert status is not None and status[0:2] == ("confirmed", "cs_api_1")
        dispatched = list(merchant.requests)
        # The governed continuation presents the terms of the checkout D1 created.
        assert built.purchases.approval_terms("run-real").checkout_id == "cs_api_1"
    finally:
        built.close()
    assert merchant.creations() == 1
    # The POST carries the idempotency key sealed in the D1 envelope, not a B0 key.
    assert merchant.create_keys == [operation_key(settings.agent_database_path)]
    assert dispatched == [
        CREATE,
        ("GET", "/checkout_sessions/cs_api_1"),
        ("POST", "/checkout_sessions/cs_api_1"),
        ("GET", "/checkout_sessions/cs_api_1"),
    ]


@pytest.mark.parametrize("fault", ["timeout", "server_error", "malformed"])
def test_governed_uncertain_creation_is_never_retried(tmp_path: Path, fault: str) -> None:
    settings = governed(tmp_path / "agent.db")
    merchant = Merchant(fault)
    built = runtime(settings, merchant)
    try:
        with pytest.raises(PurchaseServiceError) as uncertain:
            start(built.purchases)
        assert uncertain.value.code == "d1_dispatch_uncertain"
        assert built.d1_gate is not None
        status = built.d1_gate.recovery_status("run-real")
        assert status is not None and status[0:2] == ("uncertain", None)
        with pytest.raises(PurchaseServiceError) as again:
            start(built.purchases)
        assert again.value.code == "d1_recovery_required"
    finally:
        built.close()
    assert merchant.creations() == 1


def test_blocked_flow_still_stops_at_the_reservation(tmp_path: Path) -> None:
    settings = configuration(tmp_path / "agent.db", mode="enforce")
    assert settings.agent_hp_flow == "blocked"
    merchant = Merchant()
    built = runtime(settings, merchant)
    try:
        with pytest.raises(PurchaseServiceError) as blocked:
            start(built.purchases)
        assert blocked.value.code == "d1_dispatch_blocked"
    finally:
        built.close()
    assert merchant.requests == []


def test_governed_deny_never_creates_a_checkout(tmp_path: Path) -> None:
    settings = governed(tmp_path / "agent.db", agent_d1_allowed_merchant_origins=UNLISTED)
    merchant = Merchant()
    built = runtime(settings, merchant)
    try:
        with pytest.raises(PurchaseServiceError) as denied:
            start(built.purchases)
        assert denied.value.code == "d1_denied"
    finally:
        built.close()
    assert merchant.requests == []
