"""Live-offer freshness before Human Present approval (b2-governed-pilot, S04).

The merchant double mirrors the real one: the checkout keeps the offer snapshot it was created
from, so its GET (and the approval fingerprint) never moves; only `/lab/offers/get` does.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr

from commerce_agent.acp import ACPClient, CheckoutSession, load_product_feed
from commerce_agent.ap2 import AgentMandateSigner
from commerce_agent.api import error_response
from commerce_agent.application import PurchaseService, PurchaseStage
from commerce_agent.application.offer_freshness import (
    FreshnessMode,
    OfferFreshnessGuard,
    build_offer_freshness,
)
from commerce_agent.approval import ApprovalError, ApprovalService
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.hp_evidence_sqlite import SqliteHpEvidenceStore
from commerce_agent.runtime import build_runtime
from tests.governance.test_shadow_integration import COMMERCIAL_NOW, configuration
from tests.test_client import lab_offer

FEED = Path(__file__).parent / "fixtures" / "feed"
MERCHANT = "http://127.0.0.1:4120"
RUN_ID = "run_s04"
CHECKOUT_ID = "chk_s04"
OFFER_ID = "BBGooglePixel8-128GB"
SIGNED_CHECKOUT = "merchant.checkout.jwt"
NOW = COMMERCIAL_NOW  # The runtime tests approve with this sandbox clock too.


def checkout_snapshot(offer: dict[str, Any]) -> dict[str, Any]:
    """ACP projection of a merchant checkout whose pricing and expiry were frozen at creation."""
    pricing = offer["pricing"]
    line_id, option_id = f"li_{CHECKOUT_ID}", f"ship_{CHECKOUT_ID}"
    return {
        "id": CHECKOUT_ID,
        "protocol": {"version": "2026-04-17"},
        "status": "ready_for_payment",
        "currency": "cop",
        "capabilities": {"payment": {"handlers": []}},
        "line_items": [
            {
                "id": line_id,
                "item": {
                    "id": offer["id"],
                    "name": offer["name"],
                    "unit_amount": pricing["items_total_minor"],
                },
                "quantity": 1,
                "totals": [
                    {
                        "type": "total",
                        "display_text": "Producto",
                        "amount": pricing["items_total_minor"],
                    },
                ],
            }
        ],
        "totals": [
            {
                "type": "subtotal",
                "display_text": "Producto",
                "amount": pricing["items_total_minor"],
            },
            {
                "type": "fulfillment",
                "display_text": "Envio simulado",
                "amount": pricing["shipping_total_minor"],
            },
            {"type": "total", "display_text": "Total", "amount": pricing["total_minor"]},
        ],
        "fulfillment_options": [
            {
                "type": "shipping",
                "id": option_id,
                "title": "Envio simulado a Bogota",
                "earliest_delivery_time": "2026-09-22T11:30:00Z",
                "latest_delivery_time": "2026-09-22T11:30:00Z",
                "totals": [
                    {
                        "type": "total",
                        "display_text": "Envio",
                        "amount": pricing["shipping_total_minor"],
                    },
                ],
            }
        ],
        "selected_fulfillment_options": [
            {"type": "shipping", "option_id": option_id, "item_ids": [line_id]}
        ],
        "buyer": {"email": "buyer@example.test", "first_name": "Buyer", "last_name": "Test"},
        "messages": [],
        "links": [],
        "created_at": "2026-09-20T11:30:00Z",
        "updated_at": "2026-09-20T11:31:00Z",
        "expires_at": offer["expires_at"],
    }


class LabMerchant:
    def __init__(self) -> None:
        self.offer = lab_offer()
        self.checkout = checkout_snapshot(self.offer)
        self.offer_reads = 0
        self.offer_failure: httpx.Response | Exception | None = None

    def mutate(self, **changes: Any) -> None:
        """Like evaluation_mutate_offer.py: a new revision; the checkout keeps its snapshot."""
        self.offer = lab_offer(revision=self.offer["revision"] + 1, **changes)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/lab/offers/get":
            self.offer_reads += 1
            if isinstance(self.offer_failure, Exception):
                raise self.offer_failure
            return self.offer_failure or httpx.Response(200, json={"ok": True, "data": self.offer})
        if request.method == "GET" and request.url.path == f"/checkout_sessions/{CHECKOUT_ID}":
            return httpx.Response(200, json=self.checkout)
        raise AssertionError(f"Unexpected merchant request {request.method} {request.url.path}")


class SignedCheckoutClient(ACPClient):
    """Real HTTP for checkout and offer reads; AP2 checkout JWT crypto is covered elsewhere."""

    def signed_checkout(self, checkout: CheckoutSession) -> str:
        return SIGNED_CHECKOUT

    def verify_signed_checkout(self, token: str, checkout: CheckoutSession) -> None:
        assert token == SIGNED_CHECKOUT


class RecordingSigner(AgentMandateSigner):
    def __init__(self) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        super().__init__(pem, merchant_id="tesis_merchant", merchant_name="Tesis")
        self.signed: list[str] = []

    def sign(self, checkout_jwt: str, checkout: CheckoutSession) -> tuple[str, str]:
        self.signed.append(checkout.id)
        return "checkout.mandate", "payment.mandate"


class Harness:
    """PurchaseService with the guard injected directly, as runtime does once PR-3 lands."""

    def __init__(self, tmp_path: Path, mode: FreshnessMode) -> None:
        self.merchant = LabMerchant()
        self.store = SqliteAgentStore(tmp_path / "agent.db")
        self.store.initialize()
        seed_awaiting_purchase(self.store)
        self.evidence = SqliteHpEvidenceStore(tmp_path / "agent.db")
        self.evidence.initialize()
        self.signer = RecordingSigner()
        client = SignedCheckoutClient(
            MERCHANT, SecretStr("merchant-token"), transport=httpx.MockTransport(self.merchant)
        )
        guard = OfferFreshnessGuard(client, self.evidence, mode=mode, clock=lambda: NOW)
        self.service = PurchaseService(
            feed=load_product_feed(FEED),
            checkout=client,
            store=self.store,
            approvals=ApprovalService(
                self.store,
                SecretStr("approval-secret-with-at-least-32-characters"),
                merchant_origin=MERCHANT,
                clock=lambda: NOW,
                mandate_signer=self.signer,
            ),
            offer_freshness=guard,
        )

    def approve(self, fingerprint: str) -> None:
        self.service.approve(
            run_id=RUN_ID,
            actor_id="human-1",
            expected_fingerprint=fingerprint,
            checkout_jwt=SIGNED_CHECKOUT,
        )

    def assert_not_approved(self) -> None:
        assert self.signer.signed == []
        assert self.store.approval(RUN_ID) is None
        assert self.store.purchase(RUN_ID)["stage"] == PurchaseStage.AWAITING_CONFIRMATION  # type: ignore[index]

    def checks(self) -> list[dict[str, Any]]:
        assert self.evidence.verify_chain(RUN_ID)
        return [item.body for item in self.evidence.for_run(RUN_ID)]


def seed_awaiting_purchase(store: SqliteAgentStore) -> None:
    store.create_purchase(RUN_ID, "Quiero un Google Pixel 8 de 128 GB negro")
    store.update_purchase(
        RUN_ID,
        stage=PurchaseStage.AWAITING_CONFIRMATION,
        selected_variant_id=OFFER_ID,
        checkout_id=CHECKOUT_ID,
    )


def rejection(error: ApprovalError) -> tuple[str, int]:
    return error.code, error_response(error).status_code


def test_s04_price_change_after_presentation_is_rejected(tmp_path: Path) -> None:
    lab = Harness(tmp_path, "enforce")
    terms = lab.service.approval_terms(RUN_ID)
    lab.merchant.mutate(
        price=180_000_000, observed_at="2026-09-20T11:45:00Z", expires_at="2026-09-20T12:45:00Z"
    )

    with pytest.raises(ApprovalError) as captured:
        lab.approve(terms.fingerprint)

    assert rejection(captured.value) == ("checkout_terms_changed", 409)
    lab.assert_not_approved()
    presented, approval = lab.checks()
    assert (presented["stage"], presented["outcome"], presented["observed_revision"]) == (
        "present",
        "fresh",
        1,
    )
    assert approval["outcome"] == "changed"
    assert approval["observed_revision"] == 2
    assert approval["changed_fields"] == ["subtotal_minor", "total_minor", "expires_at"]
    assert approval["compared"]["subtotal_minor"] == {
        "checkout": 154_000_000,
        "offer": 180_000_000,
    }
    assert (approval["governance_mode"], approval["decision"]) == ("enforce", "block")
    assert (approval["run_id"], approval["checkout_id"], approval["stage"]) == (
        RUN_ID,
        CHECKOUT_ID,
        "approve",
    )
    assert approval["checked_at"] == "2026-09-20T12:00:00Z"


@pytest.mark.parametrize(
    ("change", "fields"),
    [
        ({"price": 150_000_000}, ["subtotal_minor", "total_minor"]),
        ({"shipping": 3_000_000}, ["shipping_minor", "total_minor"]),
        ({"expires_at": "2026-09-20T12:20:00Z"}, ["expires_at"]),
        ({"stock": 0}, ["stock"]),
    ],
)
def test_material_change_blocks_approval_before_mandates(
    tmp_path: Path, change: dict[str, Any], fields: list[str]
) -> None:
    lab = Harness(tmp_path, "enforce")
    terms = lab.service.approval_terms(RUN_ID)
    lab.merchant.mutate(**change)

    with pytest.raises(ApprovalError) as captured:
        lab.approve(terms.fingerprint)

    assert rejection(captured.value) == ("checkout_terms_changed", 409)
    lab.assert_not_approved()
    assert lab.checks()[-1]["changed_fields"] == fields


def test_changed_offer_terms_are_not_presented(tmp_path: Path) -> None:
    lab = Harness(tmp_path, "enforce")
    lab.merchant.mutate(price=180_000_000)

    with pytest.raises(ApprovalError) as captured:
        lab.service.approval_terms(RUN_ID)

    assert rejection(captured.value) == ("checkout_terms_changed", 409)
    assert lab.checks()[-1]["stage"] == "present"


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("merchant is slow"),
        httpx.Response(503, json={"detail": "unavailable"}),
        httpx.Response(200, json={"ok": False, "error": {"code": "OFFER_NOT_FOUND"}}),
    ],
)
def test_unreadable_offer_fails_closed_after_one_attempt(
    tmp_path: Path, failure: httpx.Response | Exception
) -> None:
    lab = Harness(tmp_path, "enforce")
    terms = lab.service.approval_terms(RUN_ID)
    lab.merchant.offer_failure = failure

    with pytest.raises(ApprovalError) as captured:
        lab.approve(terms.fingerprint)

    assert rejection(captured.value) == ("offer_freshness_unavailable", 503)
    assert lab.merchant.offer_reads == 2
    lab.assert_not_approved()
    last = lab.checks()[-1]
    assert (last["outcome"], last["decision"], last["observed_revision"]) == (
        "unavailable",
        "block",
        None,
    )


def test_revision_alone_does_not_block_and_same_terms_are_approved(tmp_path: Path) -> None:
    lab = Harness(tmp_path, "enforce")
    terms = lab.service.approval_terms(RUN_ID)
    lab.merchant.mutate()

    lab.approve(terms.fingerprint)

    assert lab.signer.signed == [CHECKOUT_ID]
    assert lab.store.approval(RUN_ID) is not None
    assert lab.store.purchase(RUN_ID)["stage"] == PurchaseStage.APPROVED  # type: ignore[index]
    last = lab.checks()[-1]
    assert (last["outcome"], last["observed_revision"], last["decision"]) == ("fresh", 2, "allow")
    assert last["changed_fields"] == []


def test_mismatched_fingerprint_is_rejected_without_reading_the_offer(tmp_path: Path) -> None:
    lab = Harness(tmp_path, "enforce")
    lab.service.approval_terms(RUN_ID)

    with pytest.raises(ApprovalError, match="no longer match"):
        lab.approve("0" * 64)

    assert lab.merchant.offer_reads == 1
    lab.assert_not_approved()


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [(None, "changed"), (httpx.Response(503, json={"detail": "unavailable"}), "unavailable")],
)
def test_shadow_records_without_blocking(
    tmp_path: Path, failure: httpx.Response | None, outcome: str
) -> None:
    lab = Harness(tmp_path, "shadow")
    terms = lab.service.approval_terms(RUN_ID)
    lab.merchant.mutate(price=180_000_000)
    lab.merchant.offer_failure = failure

    lab.approve(terms.fingerprint)

    assert lab.signer.signed == [CHECKOUT_ID]
    assert lab.store.purchase(RUN_ID)["stage"] == PurchaseStage.APPROVED  # type: ignore[index]
    last = lab.checks()[-1]
    assert (last["outcome"], last["governance_mode"], last["decision"]) == (
        outcome,
        "shadow",
        "allow",
    )


def test_shadow_survives_unavailable_evidence(tmp_path: Path) -> None:
    lab = Harness(tmp_path, "shadow")
    terms = lab.service.approval_terms(RUN_ID)
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection:
        connection.execute("DROP TABLE hp_evidence")

    lab.approve(terms.fingerprint)

    assert lab.store.purchase(RUN_ID)["stage"] == PurchaseStage.APPROVED  # type: ignore[index]


def test_enforce_fails_closed_without_evidence(tmp_path: Path) -> None:
    lab = Harness(tmp_path, "enforce")
    terms = lab.service.approval_terms(RUN_ID)
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection:
        connection.execute("DROP TABLE hp_evidence")

    with pytest.raises(ApprovalError) as captured:
        lab.approve(terms.fingerprint)

    assert rejection(captured.value) == ("offer_freshness_unavailable", 503)
    lab.assert_not_approved()


@pytest.mark.parametrize(
    ("mode", "flow", "expected"),
    [
        ("off", "blocked", None),
        ("off", "governed", None),
        ("shadow", "blocked", "shadow"),
        ("shadow", "governed", "shadow"),
        ("enforce", "blocked", None),
        ("enforce", "governed", "enforce"),
    ],
)
def test_guard_exists_only_for_shadow_or_governed_enforce(
    tmp_path: Path, mode: str, flow: str, expected: str | None
) -> None:
    settings = configuration(tmp_path / "agent.db", mode=mode, agent_hp_flow=flow)
    client = ACPClient(MERCHANT, SecretStr("merchant-token"))

    guard = build_offer_freshness(settings, client)

    assert (guard.mode if guard is not None else None) == expected
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert ("hp_evidence" in tables) == (expected is not None)


def runtime_approval(tmp_path: Path, mode: str, merchant: LabMerchant) -> SqliteAgentStore:
    """Approval clock is the sandbox clock of `configuration`, before the offer expires."""
    settings = configuration(tmp_path / "agent.db", mode=mode)
    runtime = build_runtime(
        settings,
        transport=httpx.MockTransport(merchant),
        payment_transport=httpx.MockTransport(merchant),
    )
    try:
        seed_awaiting_purchase(runtime.store)
        terms = runtime.purchases.approval_terms(RUN_ID)
        merchant.mutate(price=180_000_000)
        runtime.purchases.approve(
            run_id=RUN_ID, actor_id="human-1", expected_fingerprint=terms.fingerprint
        )
    finally:
        runtime.close()
    return runtime.store


def test_off_runtime_never_reads_the_offer(tmp_path: Path) -> None:
    merchant = LabMerchant()
    merchant.offer_failure = AssertionError("B0 must not read /lab/offers/get")

    store = runtime_approval(tmp_path, "off", merchant)

    assert merchant.offer_reads == 0
    assert store.purchase(RUN_ID)["stage"] == PurchaseStage.APPROVED  # type: ignore[index]
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection:
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'hp_evidence'").fetchone()
            is None
        )


def test_shadow_runtime_records_the_stale_approval(tmp_path: Path) -> None:
    merchant = LabMerchant()

    store = runtime_approval(tmp_path, "shadow", merchant)

    assert merchant.offer_reads == 2
    assert store.purchase(RUN_ID)["stage"] == PurchaseStage.APPROVED  # type: ignore[index]
    evidence = SqliteHpEvidenceStore(tmp_path / "agent.db")
    assert [item.body["outcome"] for item in evidence.for_run(RUN_ID)] == ["fresh", "changed"]
    assert evidence.verify_chain(RUN_ID)
    recorded = json.dumps([item.body for item in evidence.for_run(RUN_ID)])
    assert "buyer@example.test" not in recorded and "Bearer" not in recorded
