"""Synthetic PurchaseService setups for the offline D1 measurement; never a deployment path.

The checkout is an in-memory spy over the versioned test feed. Each mode mirrors the setup used by
tests/governance/test_enforce_integration.py without importing test modules; pyright checks this
copy and tests/governance/test_d1_bench.py runs every mode once.
"""

import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import SecretBytes, SecretStr

from commerce_agent.acp import load_product_feed
from commerce_agent.acp.models import (
    Address,
    Buyer,
    CheckoutSession,
    CheckoutUpdateRequest,
    FulfillmentDetails,
)
from commerce_agent.application import CheckoutDetails, PurchaseService
from commerce_agent.approval import ApprovalService
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.provenance import (
    ClockProfile,
    ClockReader,
    D1Configuration,
    D1EnforceConfiguration,
    ProvenanceKeys,
)
from commerce_agent.governance.shadow import ShadowD1Observer
from commerce_agent.persistence import SqliteAgentStore

FEED = Path(__file__).resolve().parents[2] / "tests/fixtures/feed"
OWNER = "private-test-owner@example.test"
MARKER = "private-secret-must-not-be-logged"
NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)
PROFILE = ClockProfile(
    authority_source="system_utc",
    commercial_source="fixed_sandbox",
    authority_provider=None,
    commercial_provider=None,
    fixed_commercial_at="2026-09-20T12:00:00.000Z",
)
SETTINGS: dict[str, Any] = dict(
    merchant_base_url="http://127.0.0.1:4120",
    policy_set_id="backend-cop-d1",
    policy_revision=1,
    currency="COP",
    quantity=1,
    clock=PROFILE,
)


def checkout(status: str) -> CheckoutSession:
    return CheckoutSession.model_validate(
        {
            "id": "cs_1",
            "protocol": {"version": "2026-04-17"},
            "status": status,
            "currency": "cop",
            "capabilities": {"payment": {"handlers": []}},
            "line_items": [
                {
                    "id": "li_1",
                    "item": {"id": "SON-01", "name": "Sonora", "unit_amount": 72_000},
                    "quantity": 1,
                    "totals": [{"type": "total", "display_text": "Item", "amount": 72_000}],
                }
            ],
            "totals": [{"type": "total", "display_text": "Total", "amount": 74_000}],
            "fulfillment_options": [{"type": "shipping", "id": "ship_1"}],
            "selected_fulfillment_options": [],
            "messages": [],
            "links": [],
            "created_at": "2026-09-20T11:00:00Z",
            "updated_at": "2026-09-20T11:30:00Z",
            "expires_at": "2026-09-20T13:00:00Z",
        }
    )


def details() -> CheckoutDetails:
    return CheckoutDetails(
        buyer=Buyer(email="buyer@example.test", first_name="Buyer", last_name="Test"),
        fulfillment=FulfillmentDetails(
            name="Buyer Test",
            email="buyer@example.test",
            phone_number="+573000000000",
            address=Address(
                name="Buyer Test",
                line_one="Calle 100 # 10-20",
                city="Bogota",
                state="DC",
                country="CO",
                postal_code="110111",
            ),
        ),
    )


class SpyGateway:
    """In-memory checkout that proves no SQLite write lock is held while it is called."""

    def __init__(self, path: Path):
        self.path = path
        self.calls: list[tuple[str, str]] = []
        self.current = checkout("not_ready_for_payment")

    def _free(self) -> None:
        with closing(sqlite3.connect(self.path, timeout=0.05)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()

    def create_checkout(
        self, variant_id: str, *, currency: str, idempotency_key: str
    ) -> CheckoutSession:
        self._free()
        self.calls.append(("create", idempotency_key))
        return self.current

    def get_checkout(self, checkout_id: str) -> CheckoutSession:
        self._free()
        self.calls.append(("get", checkout_id))
        return self.current

    def update_checkout(
        self, checkout_id: str, update: CheckoutUpdateRequest, *, idempotency_key: str
    ) -> CheckoutSession:
        self._free()
        self.calls.append(("update", idempotency_key))
        self.current = checkout("ready_for_payment")
        return self.current

    # The measured path never signs (no AP2) and never cancels (the total fits the budget).
    def signed_checkout(self, checkout: CheckoutSession) -> str:
        raise AssertionError("The D1 bench runs without AP2")

    def verify_signed_checkout(self, token: str, checkout: CheckoutSession) -> None:
        raise AssertionError("The D1 bench runs without AP2")

    def cancel_checkout(
        self, checkout_id: str, *, idempotency_key: str, reason_code: str | None = None
    ) -> CheckoutSession:
        raise AssertionError("The D1 bench never cancels")


class SingleAttemptSpy:
    """Offline dispatch port that calls once; no assertion about remote idempotence."""

    def create_once(self, gateway, *, variant_id: str, currency: str, idempotency_key: str):
        return gateway.create_checkout(
            variant_id, currency=currency, idempotency_key=idempotency_key
        )

    def perform_once(self, effect, call):
        return call()


def keys() -> ProvenanceKeys:
    return ProvenanceKeys(
        key_id="test-ephemeral",
        pseudonym_key=SecretBytes(secrets.token_bytes(32)),
        encryption_key=SecretBytes(secrets.token_bytes(32)),
    )


def initialized_store(path: Path) -> SqliteAgentStore:
    store = SqliteAgentStore(path)
    store.initialize()
    return store


def service(store: SqliteAgentStore, gateway: SpyGateway, **governance: Any) -> PurchaseService:
    return PurchaseService(
        feed=load_product_feed(FEED),
        checkout=gateway,
        store=store,
        approvals=ApprovalService(store, SecretStr(secrets.token_urlsafe(40)), clock=lambda: NOW),
        **governance,
    )


def enforce(path: Path) -> tuple[PurchaseService, SpyGateway, SqliteD1EnforceGate]:
    store = initialized_store(path)
    gate = SqliteD1EnforceGate(
        path,
        D1EnforceConfiguration(governance_mode="enforce", **SETTINGS),
        keys(),
        ClockReader(PROFILE),
        dispatch=SingleAttemptSpy(),
        post_create_dispatch=SingleAttemptSpy(),
    )
    gateway = SpyGateway(path)
    return service(store, gateway, d1_gate=gate), gateway, gate


def baseline(path: Path, *, shadow: bool):
    store = initialized_store(path)
    gateway = SpyGateway(path)
    observer = (
        ShadowD1Observer(
            path,
            D1Configuration(governance_mode="shadow", **SETTINGS),
            keys(),
            ClockReader(PROFILE),
        )
        if shadow
        else None
    )
    return service(store, gateway, d1_observer=observer), gateway, observer


def start(purchases: PurchaseService):
    return purchases.start(
        run_id="run-a",
        owner_id=OWNER,
        request=MARKER,
        intent=PurchaseIntent(hard=HardConstraints(currency="COP")),
        selected_variant_id="SON-01",
        details=details(),
    )
