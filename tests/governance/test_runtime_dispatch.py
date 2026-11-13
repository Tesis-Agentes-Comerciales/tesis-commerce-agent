"""AGENT_HP_FLOW through build_runtime: the real ACP client against a mock Merchant.

The governed flow dispatches a committed D1 allow with one POST and continues to the
human approval; the blocked default keeps stopping at the durable reservation.
"""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest

from commerce_agent.application import PurchaseServiceError
from commerce_agent.config import Settings
from commerce_agent.domain import PurchaseIntent
from commerce_agent.runtime import AgentRuntime, build_runtime
from commerce_evaluation.stack.fake_checkout import details
from tests.governance.test_shadow_integration import OWNER, configuration, start
from tests.test_api import checkout
from tests.test_client import lab_offer

UNLISTED = "https://otro-comercio.example"
CREATE = ("POST", "/checkout_sessions")
OFFER_READ = ("POST", "/lab/offers/get")
# The live offer behind SON-01, unchanged since the checkout: the freshness guard reads it.
LIVE_OFFER = {
    **lab_offer(
        price=72_000,
        shipping=2_000,
        observed_at="2026-09-20T11:00:00Z",
        expires_at="2099-09-20T13:00:00Z",
    ),
    "id": "SON-01",
    "product_id": "SON",
    "name": "Sonora",
}


def acp_checkout(status: str, *, with_details: bool) -> dict[str, Any]:
    """test_api's checkout as the merchant projects it: itemized totals, delivery in whole days."""
    payload = checkout(status, with_details=with_details)
    payload["totals"] = [
        {"type": "subtotal", "display_text": "Producto", "amount": 72_000},
        {"type": "fulfillment", "display_text": "Envio", "amount": 2_000},
        *payload["totals"],
    ]
    for option in payload["fulfillment_options"]:
        option["earliest_delivery_time"] = option["latest_delivery_time"] = "2026-09-22T11:00:00Z"
    return payload


# Error answers to the creation POST, as the merchant's ACP layer shapes them (`detail.code`).
CREATE_ERRORS: dict[str, tuple[int, Any]] = {
    "server_error": (500, {"code": "internal"}),
    # The merchant's 409 on creation; a reused key with other content is a 422 there.
    "client_error": (409, {"code": "idempotency_in_flight"}),
    # A checkout exists for the key, but its creation response cannot be rebuilt.
    "unrecoverable": (409, {"code": "creation_unrecoverable"}),
    "key_conflict": (422, {"code": "idempotency_conflict"}),
    # A 404 that is not the merchant's typed refusal, e.g. from a proxy.
    "route_not_found": (404, "Not Found"),
    # Refusals the merchant answers before creating anything for the key.
    "out_of_stock": (409, {"code": "out_of_stock", "message": "Not available."}),
    "offer_expired": (409, {"code": "OFFER_EXPIRED", "message": "Refresh the offer."}),
    "offer_expired_lowercase": (409, {"code": "offer_expired"}),
    "invalid_item": (404, {"code": "invalid_item", "message": "The offer does not exist."}),
}


class Merchant:
    """ACP checkout double: one session, the same body on POST and on the next GET.

    It also serves the live offer, so the governed freshness check sees unchanged terms.
    """

    def __init__(self, fault: str | None = None) -> None:
        self.fault = fault
        self.requests: list[tuple[str, str]] = []
        self.create_keys: list[str | None] = []
        self.current = acp_checkout("not_ready_for_payment", with_details=False)
        self.offer = dict(LIVE_OFFER)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if (request.method, request.url.path) == CREATE:
            self.create_keys.append(request.headers.get("Idempotency-Key"))
            return self._created(request)
        if (request.method, request.url.path) == OFFER_READ:
            return httpx.Response(200, json={"ok": True, "data": self.offer})
        if request.url.path == "/checkout_sessions/cs_api_1":
            if request.method == "POST":
                self.current = acp_checkout("ready_for_payment", with_details=True)
            return httpx.Response(200, json=self.current)
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    def _created(self, request: httpx.Request) -> httpx.Response:
        if self.fault == "timeout":
            raise httpx.ReadTimeout("Merchant answered too late", request=request)
        if self.fault == "connect_error":
            raise httpx.ConnectError("Merchant connection refused", request=request)
        if self.fault in CREATE_ERRORS:
            status, detail = CREATE_ERRORS[self.fault]
            return httpx.Response(status, json={"detail": detail})
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


def dispatch_result(path: Path) -> tuple[str, str | None, str]:
    with closing(sqlite3.connect(path)) as connection:
        (row,) = connection.execute("SELECT state, checkout_id, code FROM d1_checkout_results")
    return row


# A refused connection or a 4xx outside the merchant's definitive refusals is no proof that
# nothing was created: never inferred, never retried. The ACP client rejects a malformed body
# before the gate reads it.
@pytest.mark.parametrize(
    "fault",
    [
        "timeout",
        "connect_error",
        "server_error",
        "client_error",
        "unrecoverable",
        "key_conflict",
        "route_not_found",
        "malformed",
    ],
)
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
        assert dispatch_result(settings.agent_database_path) == (
            "uncertain",
            None,
            "DISPATCH_UNCERTAIN",
        )
        purchase = built.store.purchase("run-real")
        assert purchase is not None and purchase["stage"] == "uncertain"
        with pytest.raises(PurchaseServiceError) as again:
            start(built.purchases)
        assert again.value.code == "d1_recovery_required"
    finally:
        built.close()
    assert merchant.creations() == 1


def start_run(built: AgentRuntime, run_id: str):
    return built.purchases.start(
        run_id=run_id,
        request="private original request",
        intent=PurchaseIntent(),
        selected_variant_id="SON-01",
        details=details(),
        owner_id=OWNER,
    )


def failure_of(path: Path, run_id: str) -> tuple[str, str | None]:
    with closing(sqlite3.connect(path)) as connection:
        (row,) = connection.execute(
            "SELECT stage, failure_code FROM purchases WHERE run_id=?", (run_id,)
        )
    return row


# The merchant answers these before creating anything for the key, and D1 sends one POST per
# key: the purchase fails as in B0 and nothing stays pending (hp-p5a-definitive-failures.md).
@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("out_of_stock", "out_of_stock"),
        ("offer_expired", "offer_expired"),
        ("offer_expired_lowercase", "offer_expired"),
        ("invalid_item", "invalid_item"),
    ],
)
def test_governed_definitive_refusal_fails_the_purchase_and_frees_the_buyer(
    tmp_path: Path, fault: str, code: str
) -> None:
    settings = governed(tmp_path / "agent.db")
    merchant = Merchant(fault)
    built = runtime(settings, merchant)
    try:
        with pytest.raises(PurchaseServiceError) as refused:
            start(built.purchases)
        assert refused.value.code == code
        assert built.d1_gate is not None
        status = built.d1_gate.recovery_status("run-real")
        assert status is not None and status[0:2] == ("definitive_failure", None)
        assert dispatch_result(settings.agent_database_path) == (
            "definitive_failure",
            None,
            "VERIFIED_NOT_CREATED",
        )
        assert failure_of(settings.agent_database_path, "run-real") == ("failed", code)
        assert built.conversation.pending_purchase_view(OWNER) is None
        # The refused run itself is never sent again.
        with pytest.raises(PurchaseServiceError) as again:
            start(built.purchases)
        assert again.value.code == "d1_recovery_required"
        assert merchant.creations() == 1
        # The buyer asks again: a new run, its own D1 decision and key, and one more POST.
        merchant.fault = None
        prepared = start_run(built, "run-next")
        assert prepared.stage == "awaiting_confirmation"
        status = built.d1_gate.recovery_status("run-next")
        assert status is not None and status[0:2] == ("confirmed", "cs_api_1")
    finally:
        built.close()
    assert merchant.creations() == 2
    assert len(set(merchant.create_keys)) == 2


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
