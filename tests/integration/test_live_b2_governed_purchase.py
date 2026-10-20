"""B2 governed Human Present purchases against the real Merchant, on the B0 lane.

Same lane, catalog and AP2 profile as the B0 baseline, with AGENT_GOVERNANCE_MODE=enforce
and AGENT_HP_FLOW=governed: the gate comes from the production bootstrap, D1 allows the
lane's Merchant and the checkout is created by the production dispatch ports. No model.
"""

import secrets
from pathlib import Path

from pydantic import SecretStr

from commerce_agent.acp import ACPClient, load_product_feed
from commerce_agent.application import PurchaseService
from commerce_agent.approval import ApprovalService
from commerce_agent.config import Settings
from commerce_agent.domain import PurchaseIntent
from commerce_agent.governance.bootstrap import build_enforce_gate
from commerce_agent.persistence import SqliteAgentStore
from tests.integration.live_purchase import PurchaseLane, buyer_details
from tests.integration.test_live_purchase import APPROVED_AT, PHONE


def governed_settings(lane: PurchaseLane, database: Path) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
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
