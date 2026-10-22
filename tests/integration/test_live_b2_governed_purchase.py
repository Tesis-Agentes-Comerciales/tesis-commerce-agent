"""B2 governed Human Present purchases against the real Merchant, on the B0 lane.

Same lane, catalog and AP2 profile as the B0 baseline, with AGENT_GOVERNANCE_MODE=enforce
and AGENT_HP_FLOW=governed: the gate comes from the production bootstrap, D1 allows the
lane's Merchant and the checkout is created by the production dispatch ports. No model.
"""

import json
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from commerce_agent.acp import ACPClient, load_product_feed
from commerce_agent.application import PurchaseService, PurchaseServiceError
from commerce_agent.application.conversation import PENDING_STAGES
from commerce_agent.approval import ApprovalService
from commerce_agent.config import Settings
from commerce_agent.domain import PurchaseIntent
from commerce_agent.governance.bootstrap import build_enforce_gate
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.ap2_evidence_sqlite import SqliteAp2EvidenceStore
from commerce_agent.runtime import build_runtime
from commerce_agent.trace import PurchaseTraces
from commerce_agent.trace.models import OrderEventStep
from commerce_evaluation.verify_evidence import LINKS, verify_database
from tests.integration.live_purchase import (
    APPROVED_AT,
    OTHER_PHONE,
    PHONE,
    PurchaseLane,
    buyer_details,
)


def governed_settings(lane: PurchaseLane, database: Path, **changes: Any) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        **changes,
        merchant_base_url=lane.origin,
        agent_database_path=database,
        agent_governance_mode="enforce",
        agent_hp_flow="governed",
        agent_sandbox_clock_at=APPROVED_AT,
        agent_d1_key_id="live-b2",
        agent_d1_policy_set_id="live-b2-d1",
        agent_d1_policy_revision="1",
        agent_d1_hmac_key_hex=SecretStr(secrets.token_hex(32)),
        agent_d1_aes_key_hex=SecretStr(secrets.token_hex(32)),
        agent_d1_allowed_merchant_origins=lane.origin,
    )


def test_b2_governed_purchase_creates_once_and_awaits_approval(b0, tmp_path: Path):
    episode = b0.episode("b2-governed")
    settings = governed_settings(b0, tmp_path / "agent.sqlite")
    gate = build_enforce_gate(settings, None)
    store = SqliteAgentStore(settings.agent_database_path)
    store.initialize()
    approvals = ApprovalService(store, b0.approval_secret, merchant_origin=b0.origin)
    run_id = f"b2-{episode.run_id}"
    with ACPClient(b0.origin, episode.bearer, timeout_seconds=10) as gateway:
        purchases = PurchaseService(
            feed=load_product_feed(episode.feed),
            checkout=gateway,
            store=store,
            approvals=approvals,
            d1_gate=gate,
        )
        prepared = purchases.start(
            run_id=run_id,
            request="Quiero comprar este teléfono",
            intent=PurchaseIntent(),
            selected_variant_id=PHONE,
            details=buyer_details("b2"),
            owner_id=episode.actor_id,
        )
    assert prepared.stage == "awaiting_confirmation"
    assert prepared.checkout.status == "ready_for_payment"
    status = gate.recovery_status(run_id)
    assert status is not None and status[0:2] == ("confirmed", prepared.checkout.id)
    assert b0.proxy.count("POST", "/checkout_sessions") == 1
    b0.stack.record("b2:governed_creation", creations=1)


def test_b2_merchant_refusal_fails_the_purchase_and_frees_the_buyer(b0, tmp_path: Path):
    """The real Merchant has no stock left: its 409 to the one POST created nothing.

    The agent's feed still lists the phone, so D1 allows and dispatches. The purchase fails
    with `out_of_stock` instead of staying uncertain, and the buyer's next purchase goes on.
    """
    episode = b0.episode("b2-refused")
    b0.rpc("alter_offer_fixture", run_id=episode.run_id, variant=PHONE)
    settings = governed_settings(b0, tmp_path / "agent.sqlite")
    gate = build_enforce_gate(settings, None)
    store = SqliteAgentStore(settings.agent_database_path)
    store.initialize()
    approvals = ApprovalService(store, b0.approval_secret, merchant_origin=b0.origin)
    before = b0.rpc("effects")
    refused_run, next_run = f"b2-refused-{episode.run_id}", f"b2-next-{episode.run_id}"
    with ACPClient(b0.origin, episode.bearer, timeout_seconds=10) as gateway:
        purchases = PurchaseService(
            feed=load_product_feed(episode.feed),
            checkout=gateway,
            store=store,
            approvals=approvals,
            d1_gate=gate,
        )

        def buy(run_id: str, variant: str):
            return purchases.start(
                run_id=run_id,
                request="Quiero comprar este teléfono",
                intent=PurchaseIntent(),
                selected_variant_id=variant,
                details=buyer_details("b2-refused"),
                owner_id=episode.actor_id,
            )

        with pytest.raises(PurchaseServiceError) as refused:
            buy(refused_run, PHONE)
        assert refused.value.code == "out_of_stock"
        status = gate.recovery_status(refused_run)
        assert status is not None and status[0:2] == ("definitive_failure", None)
        assert b0.rpc("effects")["checkout_sessions"] == before["checkout_sessions"]
        assert store.pending_purchase(PENDING_STAGES, episode.actor_id) is None
        prepared = buy(next_run, OTHER_PHONE)
    assert prepared.stage == "awaiting_confirmation"
    assert (store.purchase(refused_run) or {}).get("stage") == "failed"
    assert b0.proxy.count("POST", "/checkout_sessions") == 2
    b0.stack.record("b2:governed_refusal", creations=1, refused="out_of_stock")


def test_b2_governed_purchase_completes_once_with_receipts_and_its_order_webhook(
    b0, tmp_path: Path
):
    """S01 under B2: the pilot's runtime wiring from approval to the Merchant's order event."""
    episode = b0.episode("b2-complete")
    settings = governed_settings(
        b0,
        tmp_path / "agent.sqlite",
        merchant_bearer_token=episode.bearer,
        merchant_webhook_secret=b0.webhook_secret,
        agent_approval_secret=b0.approval_secret,
        payment_provider_url=b0.payment.origin,
        payment_agent_bearer_token=b0.payment_token,
        agent_feed_source="file",
        agent_feed_path=episode.feed,
        agent_ap2_required=True,
        agent_ap2_private_key_pem=b0.agent_key,
        # build_runtime never authenticates buyers; jwt only satisfies the AP2 settings rule.
        agent_auth_mode="jwt",
        agent_auth_issuer="http://127.0.0.1:9",
    )
    before = b0.rpc("effects")
    runtime = build_runtime(settings)
    run_id = f"b2-complete-{episode.run_id}"
    try:
        prepared = runtime.purchases.start(
            run_id=run_id,
            request="Quiero comprar este teléfono",
            intent=PurchaseIntent(),
            selected_variant_id=PHONE,
            details=buyer_details("b2-complete"),
            owner_id=episode.actor_id,
        )
        assert prepared.stage == "awaiting_confirmation"
        terms = runtime.purchases.approval_terms(run_id)
        assert terms.checkout_jwt
        approved = runtime.purchases.approve(
            run_id=run_id,
            actor_id=episode.actor_id,
            expected_fingerprint=terms.fingerprint,
            checkout_jwt=terms.checkout_jwt,
        )
        assert approved.checkout_mandate and approved.payment_mandate
        completed = runtime.conversation.complete_purchase(run_id, episode.actor_id)
        assert completed.status == "completed" and completed.order.status == "confirmed"
        assert runtime.conversation.complete_purchase(run_id, episode.actor_id) == completed
    finally:
        runtime.close()
    with closing(sqlite3.connect(settings.agent_database_path)) as connection:
        stored = connection.execute(
            "SELECT checkout_receipt_jwt, payment_receipt_jwt FROM completion_results "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert stored is not None and all(stored)
    # Consent and effect link the mandates to what the real Merchant and provider signed.
    consent, effect = SqliteAp2EvidenceStore(settings.agent_database_path).for_run(run_id)
    receipts = effect.body["checkout_receipt"], effect.body["payment_receipt"]
    assert [receipt["status"] for receipt in receipts] == ["Success", "Success"]
    assert [receipt["reference"] for receipt in receipts] == [
        consent.body["checkout_mandate_sd_hash"],
        consent.body["payment_mandate_sd_hash"],
    ]
    assert receipts[0]["order_id"] == effect.body["order_id"] == completed.order.id
    assert receipts[1]["issuer"] == "tesis_payment_provider" and receipts[1]["payment_id"]
    assert b0.rpc("effects")["orders"] == before["orders"] + 1
    assert b0.proxy.count("POST", "/checkout_sessions") == 1
    assert b0.proxy.count("POST", f"/checkout_sessions/{completed.id}/ap2/complete") == 1

    # The Merchant queued `order_create` with the order; its dispatcher (what demo-stack and the
    # pilot runner run) signs it and posts it to this runtime's receiver.
    with b0.receiver.delivering_to(SqliteAgentStore(settings.agent_database_path)):
        delivered = b0.rpc("dispatch_webhooks", limit=50)
    assert delivered and {item["outcome"] for item in delivered} == {"delivered"}
    with closing(sqlite3.connect(settings.agent_database_path)) as connection:
        events = connection.execute(
            "SELECT event_type, payload FROM webhook_events WHERE order_id = ?",
            (completed.order.id,),
        ).fetchall()
    assert [event_type for event_type, _ in events] == ["order_create"]
    assert json.loads(events[0][1])["data"]["checkout_session_id"] == completed.id
    trace = PurchaseTraces(settings).trace(run_id)
    order_events = [step for step in trace.steps if isinstance(step, OrderEventStep)]
    assert [step.event_type for step in order_events] == ["order_create"]
    # And every link verifies, the payment included, as `verify-evidence` reports it. The
    # purchase has an order, so it is complete only with all nine ok: D2 is the one missing.
    [verified] = verify_database(
        settings.agent_database_path, run_id=run_id, psp_database=b0.root / "payments.sqlite"
    )
    assert {name: link["status"] for name, link in verified["links"].items()} == dict.fromkeys(
        LINKS, "ok"
    ) | {"d2_decision": "not_applicable"}
    assert verified["order_id"] == completed.order.id
    assert verified["complete"] is False
    b0.stack.record("b2:governed_purchase", orders_created=1, order_events=len(order_events))
