"""B2 governed Human Present purchases against the real Merchant, on the B0 lane.

Same lane, catalog and AP2 profile as the B0 baseline, with AGENT_GOVERNANCE_MODE=enforce
and AGENT_HP_FLOW=governed: the gate comes from the production bootstrap, D1 allows the
lane's Merchant and the checkout is created by the production dispatch ports. No model.
"""

import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from commerce_agent.acp import ACPClient, load_product_feed
from commerce_agent.application import PurchaseService
from commerce_agent.approval import ApprovalService
from commerce_agent.config import Settings
from commerce_agent.domain import PurchaseIntent
from commerce_agent.governance.bootstrap import build_enforce_gate
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.runtime import build_runtime
from tests.integration.live_purchase import PurchaseLane, buyer_details
from tests.integration.test_live_purchase import APPROVED_AT, PHONE


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


def test_b2_governed_purchase_completes_once_with_signed_receipts(b0, tmp_path: Path):
    """S01 under B2: the pilot's runtime wiring from approval to a confirmed order."""
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
    assert b0.rpc("effects")["orders"] == before["orders"] + 1
    assert b0.proxy.count("POST", "/checkout_sessions") == 1
    assert b0.proxy.count("POST", f"/checkout_sessions/{completed.id}/ap2/complete") == 1
    b0.stack.record("b2:governed_purchase", orders_created=1)
