"""Complete purchases across the Agent, the real Merchant and the real payment provider.

B0 is the baseline the thesis compares against: ACP plus AP2 Human Present, explicit human
approval, signed mandates and receipts, idempotent completion and signed order webhooks. The
`acp` lane keeps the plain ACP completion and the conversational API covered. Replaces the
September joint E2E; the mapping is in tests/integration/README.md.
"""

import os
import sqlite3
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from commerce_agent.acp import ACPClientError, load_product_feed
from commerce_agent.api import create_app
from commerce_agent.completion import CompletionError
from commerce_agent.config import Settings
from commerce_agent.domain import PurchaseIntent
from commerce_evaluation.stack.purchase_agent import (
    APPROVED_AT,
    OTHER_PHONE,
    PHONE,
    PHONE_INTENT,
    PHONE_REQUEST,
    StaticConversationRouter,
    agent,
    buyer_details,
    payments,
)
from commerce_evaluation.stack.purchase_lane import DECLINED_CARD, PurchaseLane


def receipts(lane: PurchaseLane, run_id: str) -> tuple[str | None, str | None] | None:
    with closing(sqlite3.connect(lane.database)) as connection:
        return connection.execute(
            "SELECT checkout_receipt_jwt, payment_receipt_jwt "
            "FROM completion_results WHERE run_id = ?",
            (run_id,),
        ).fetchone()


def test_b0_ap2_purchase_completes_once_with_signed_receipts(b0):
    episode = b0.episode("ap2")
    before = b0.rpc("effects")
    with agent(b0, episode) as buyer:
        run_id = f"b0-{episode.run_id}"
        prepared = buyer.purchases.start(
            run_id=run_id,
            request="Quiero comprar este teléfono",
            intent=PurchaseIntent(),
            selected_variant_id=PHONE,
            details=buyer_details("ap2"),
        )
        assert prepared.checkout.status == "ready_for_payment"
        assert buyer.purchases.approval_terms(run_id).checkout_jwt
        with payments(b0) as provider:
            service = buyer.completion(provider)
            with pytest.raises(CompletionError, match="not approved"):
                service.complete(run_id=run_id)
            approved = buyer.approve(run_id)
            assert approved.checkout_mandate and approved.payment_mandate
            result = service.complete(run_id=run_id)
            assert result.status == "completed"
            assert result.order.status == "confirmed"
            assert service.complete(run_id=run_id) == result
    stored = receipts(b0, run_id)
    assert stored is not None and all(isinstance(value, str) and value for value in stored)
    assert b0.store.purchase(run_id)["stage"] == "completed"  # type: ignore[index]
    after = b0.rpc("effects")
    assert after["orders"] == before["orders"] + 1
    # Only the AP2 route completes under B0; plain ACP completion is refused by profile.
    assert b0.proxy.count("POST", f"/checkout_sessions/{result.id}/ap2/complete") >= 1
    assert b0.proxy.count("POST", f"/checkout_sessions/{result.id}/complete") == 0
    b0.stack.record("b0:ap2_purchase", orders_created=after["orders"] - before["orders"])


def test_b0_declined_payment_leaves_the_checkout_open_and_no_order(b0):
    episode = b0.episode("declined")
    before = b0.rpc("effects")
    with agent(b0, episode) as buyer:
        run_id = f"b0-declined-{episode.run_id}"
        prepared = buyer.purchases.start(
            run_id=run_id,
            request="Buy another available product",
            intent=PurchaseIntent(),
            selected_variant_id=OTHER_PHONE,
            details=buyer_details("declined"),
        )
        buyer.approve(run_id)
        with (
            payments(b0, DECLINED_CARD) as provider,
            pytest.raises(ACPClientError) as declined,
        ):
            buyer.completion(provider).complete(run_id=run_id)
        assert declined.value.code == "payment_declined"
        assert declined.value.request_id is not None
        assert buyer.gateway.get_checkout(prepared.checkout.id).status == "ready_for_payment"
    purchase = b0.store.purchase(run_id)
    assert purchase is not None
    assert purchase["stage"] == "failed" and purchase["order_id"] is None
    assert b0.rpc("effects")["orders"] == before["orders"]


def test_b0_order_webhooks_reach_the_agent_and_the_permalink_hides_the_buyer(b0):
    episode = b0.episode("webhooks")
    with agent(b0, episode) as buyer:
        run_id = f"b0-webhooks-{episode.run_id}"
        buyer.purchases.start(
            run_id=run_id,
            request="Buy one available product",
            intent=PurchaseIntent(),
            selected_variant_id=PHONE,
            details=buyer_details("webhooks"),
        )
        buyer.approve(run_id)
        with payments(b0) as provider:
            order = buyer.completion(provider).complete(run_id=run_id).order

    permalink = httpx.get(order.permalink_url, timeout=5, trust_env=False)
    assert permalink.status_code == 200
    for private_value in ("buyer.webhooks@example.test", "+573000000000", "Calle 100", "vt_"):
        assert private_value not in permalink.text

    delivered = b0.rpc("dispatch_webhooks", limit=10)
    assert delivered and {item["outcome"] for item in delivered} == {"delivered"}
    projected = b0.store.order(order.id)
    assert projected is not None and projected["data"]["status"] == "confirmed"

    updated = b0.rpc(
        "order_update", run_id=episode.run_id, actor_id=episode.actor_id, order_id=order.id
    )
    assert updated["status"] == "processing"
    delivered = b0.rpc("dispatch_webhooks", limit=10)
    assert delivered and {item["outcome"] for item in delivered} == {"delivered"}
    projected = b0.store.order(order.id)
    assert projected is not None and projected["data"]["status"] == "processing"


def test_acp_purchase_replays_locally_and_at_the_merchant(acp):
    episode = acp.episode("replay")
    with agent(acp, episode) as buyer:
        assert buyer.gateway.discover().protocol.version == "2026-04-17"
        run_id = f"acp-{episode.run_id}"
        prepared = buyer.purchases.start(
            run_id=run_id,
            request="Buy one available product",
            intent=PurchaseIntent(),
            selected_variant_id=PHONE,
            details=buyer_details("acp"),
        )
        assert prepared.stage == "awaiting_confirmation"
        assert prepared.checkout.status == "ready_for_payment"
        buyer.approve(run_id)
        with payments(acp) as provider:
            completed = buyer.completion(provider).complete(run_id=run_id)
            assert buyer.completion(provider).complete(run_id=run_id) == completed
            assert completed.status == "completed"
            assert completed.order.status == "confirmed"
            merchant_replay = buyer.gateway.complete_checkout(
                completed.id,
                provider.payment_data(
                    checkout=prepared.checkout,
                    idempotency_key=acp.store.idempotency_key(run_id, "payment.delegate"),
                ),
                idempotency_key=acp.store.idempotency_key(run_id, "checkout.complete"),
            )
        assert merchant_replay == completed
    assert acp.store.purchase(run_id)["stage"] == "completed"  # type: ignore[index]


def test_acp_natural_request_crosses_the_model_boundary_and_completes(acp, tmp_path):
    episode = acp.episode("conversation")
    settings = Settings(
        merchant_base_url=acp.origin,
        merchant_bearer_token=episode.bearer,
        merchant_webhook_secret=acp.webhook_secret,
        agent_approval_secret=acp.approval_secret,
        payment_provider_url=acp.payment.origin,
        payment_agent_bearer_token=acp.payment_token,
        agent_database_path=tmp_path / "agent.sqlite",
        agent_feed_source="file",
        agent_feed_path=episode.feed,
        agent_principal_id="human-conversational-live",
    )
    # RUN_LIVE_LLM=1 puts the configured Claude model behind the boundary (manual runs only).
    router = None
    if os.getenv("RUN_LIVE_LLM") != "1":
        router = StaticConversationRouter(PHONE_REQUEST, PHONE_INTENT)
    app = create_app(settings=settings, clock=lambda: APPROVED_AT, conversation_router=router)
    with TestClient(app) as client:
        prepared = client.post(
            "/conversation/turn",
            json={
                "turn_id": "turn-conversational-live",
                "messages": [{"role": "user", "content": PHONE_REQUEST}],
                "details": buyer_details("conversation").model_dump(mode="json", exclude_none=True),
            },
        )
        assert prepared.status_code == 200, prepared.text
        turn = prepared.json()
        assert turn["action"] == "purchase"
        preparation = turn["purchase"]
        # Whichever variant the model picks, it meets the hard constraints of the request.
        selected = next(
            candidate
            for candidate in load_product_feed(episode.feed).candidates
            if candidate.variant_id == preparation["selected_candidate"]["variant_id"]
        )
        assert "smartphones" in selected.categories and "new" in selected.conditions
        assert ("Almacenamiento", "256 GB") in selected.options
        assert preparation["checkout"]["status"] == "ready_for_payment"
        assert preparation["checkout"]["totals"][-1]["amount"] <= 2_000_000_00
        run_id = preparation["run_id"]

        assert client.post(f"/purchases/{run_id}/complete").status_code == 409
        terms = turn["approval_terms"]
        assert terms["total_minor"] <= 2_000_000_00
        approved = client.post(
            f"/purchases/{run_id}/approval",
            json={"expected_fingerprint": terms["fingerprint"]},
        )
        assert approved.status_code == 200
        assert approved.json()["principal_id"] == "human-conversational-live"

        completed = client.post(f"/purchases/{run_id}/complete")
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "completed"
        assert completed.json()["order"]["checkout_session_id"] == preparation["checkout"]["id"]
