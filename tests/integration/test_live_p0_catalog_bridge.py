"""First Merchant P0 -> Agent bridge. No conditional UX03 claim or real LLM."""

import hashlib
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from commerce_agent.acp import load_product_feed
from commerce_agent.api import create_app
from commerce_agent.config import Settings
from commerce_agent.domain import PurchaseIntent
from commerce_agent.domain.selection import rank_candidates
from tests.integration.live_purchase import (
    APPROVED_AT,
    PurchaseLane,
    StaticConversationRouter,
    buyer_details,
    payment_requirements,
)
from tests.integration.live_stack import AGENT, LiveStack, missing_requirements

REGISTRY = AGENT / "evaluation/scenarios/p0/merchant-provenance.json"


def source() -> dict[str, Any]:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def p2_lane(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PurchaseLane]:
    problems = missing_requirements() + payment_requirements()
    if problems:
        pytest.fail("; ".join(problems), pytrace=False)
    pinned = source()
    for variable, key in (("MERCHANT_REPO", "merchant"), ("PAYMENT_REPO", "psp")):
        repository = os.environ[variable]
        actual = subprocess.check_output(
            ["git", "-C", repository, "rev-parse", "HEAD"], text=True
        ).strip()
        assert actual == pinned[key]["sha"], "Use the audited SHA in nightly workflow_dispatch"
        assert not subprocess.check_output(
            ["git", "-C", repository, "status", "--porcelain=v1"]
        ).strip(), "Sibling checkout must be clean"
    stack = LiveStack(tmp_path_factory.mktemp("p2-p0"))
    lane = None
    try:
        stack.start()
        lane = PurchaseLane(stack, "acp")
        lane.start()
        stack.record(
            "P2-source-pins",
            merchant_sha=pinned["merchant"]["sha"],
            psp_sha=pinned["psp"]["sha"],
            mode="HP ACP preparation; AP2 not exercised",
        )
        yield lane
    finally:
        if lane is not None:
            lane.close()
        stack.close()


def catalog(lane: PurchaseLane, name: str):
    episode = lane.episode(name)
    observed = lane.rpc("p0_provenance", run_id=episode.run_id, feed_dir=str(episode.feed))
    assert observed["sources_sha256"] == source()["sources_sha256"]
    return episode, observed


def pair(lane: PurchaseLane, offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = []
    for rule in source()["pair_rules"]:
        matches = [
            offer
            for offer in offers
            if offer["brand"] == rule["brand"]
            and offer["product_title"] == rule["product_title"]
            and offer["attributes"].get("storage") == rule["storage"]
            and offer["condition"] == rule["condition"]
            and offer["available_quantity"] > 0
        ]
        if len(matches) != 1:
            lane.stack.record(
                "P2-pair",
                status="skipped",
                reason="insufficient_fixture_data",
                role=rule["role"],
                matching_variants=len(matches),
            )
            pytest.skip("insufficient_fixture_data: no unique in-stock variant for documented rule")
        selected.append(matches[0])
    return selected


def feed_hash(path: Path) -> str:
    return hashlib.sha256((path / "products.jsonl").read_bytes()).hexdigest()


def test_p0_managed_and_b0_export_reach_the_agent(p2_lane: PurchaseLane):
    episode, observed = catalog(p2_lane, "provenance")
    # Different runs and clocks; equal fixture identities do not make them one catalog.
    assert observed["managed_run_id"] != episode.run_id
    assert observed["episode_matches_fixture"]

    def comparable(offers):
        return [
            {k: v for k, v in offer.items() if k not in {"observed_at", "expires_at"}}
            for offer in offers
        ]

    assert comparable(observed["managed_offers"]) == comparable(observed["episode_offers"])
    products = observed["products"]
    assert len(products) == source()["static_admissible_product_count"]
    exported = load_product_feed(episode.feed)
    assert {c.variant_id for c in exported.candidates} == {
        offer["id"] for offer in observed["episode_offers"]
    }
    available = sum(c.available for c in exported.candidates)
    a, b = pair(p2_lane, observed["episode_offers"])
    p2_lane.stack.record(
        "P2-PROV-01",
        status="passed",
        sources_sha256=observed["sources_sha256"],
        fixture_sha256=observed["fixture_sha256"],
        managed_run_id=observed["managed_run_id"],
        episode_run_id=episode.run_id,
        snapshot_products=observed["snapshot_products"],
        exported_products=len(products),
        variants=len(exported.candidates),
        available_variants=available,
        feed_sha256=feed_hash(episode.feed),
        managed_metadata=observed["metadata"],
        episode_metadata=json.loads((episode.feed / "metadata.json").read_text(encoding="utf-8")),
        pair=[
            {k: offer[k] for k in ("id", "brand", "product_title", "attributes", "condition")}
            for offer in (a, b)
        ],
    )


def test_managed_price_and_stock_revision_are_visible_to_the_agent(
    p2_lane: PurchaseLane, tmp_path: Path
):
    episode, observed = catalog(p2_lane, "mutation")
    a, _ = pair(p2_lane, observed["managed_offers"])
    # Adverse condition fixed before mutation: +one minor unit and zero stock.
    price = a["pricing"]["items_total_minor"] + 1
    changed = p2_lane.rpc("p0_managed_update", offer_id=a["id"], price_minor=price, stock=0)
    try:
        response = httpx.get(p2_lane.origin + "/feeds/current")
        response.raise_for_status()
        payload = response.json()
        (tmp_path / "metadata.json").write_text(json.dumps(payload["metadata"]), encoding="utf-8")
        (tmp_path / "products.jsonl").write_text(
            "".join(json.dumps(p) + "\n" for p in payload["products"]), encoding="utf-8"
        )
        feed = load_product_feed(tmp_path)
        candidate = next(c for c in feed.candidates if c.variant_id == a["id"])
        assert not candidate.available and candidate.observed_unit_amount_minor == price
        result = rank_candidates(feed, PurchaseIntent())
        assert a["id"] not in {r.candidate.variant_id for r in result.ranked}
        assert changed["revision"] == a["revision"] + 1
        # Same IDs, different scope: the B0 episode must retain its original offers.
        assert (
            p2_lane.rpc("p0_episode_state", run_id=episode.run_id)["offers"]
            == observed["episode_offers"]
        )
        p2_lane.stack.record(
            "P2-PROV-02",
            status="passed",
            offer_id=a["id"],
            source="managed catalog",
            initial_revision=a["revision"],
            final_revision=changed["revision"],
            initial_price_minor=a["pricing"]["items_total_minor"],
            final_price_minor=price,
            initial_stock=a["available_quantity"],
            final_stock=0,
            feed_sha256=feed_hash(tmp_path),
            rejected_as="unavailable",
        )
    finally:
        restored = p2_lane.rpc(
            "p0_managed_update",
            offer_id=a["id"],
            price_minor=a["pricing"]["items_total_minor"],
            stock=a["available_quantity"],
        )
        assert restored["revision"] == changed["revision"] + 1
        p2_lane.stack.record(
            "P2-managed-restored", offer_id=a["id"], restored_revision=restored["revision"]
        )


def test_agent_prepares_one_disposable_checkout_and_requires_hp_approval(
    p2_lane: PurchaseLane, tmp_path: Path
):
    episode, observed = catalog(p2_lane, "agent")
    a, b = pair(p2_lane, observed["episode_offers"])
    currency = a["pricing"]["currency"].upper()
    budget = a["pricing"]["total_minor"]  # Planning input, not yet an observed checkout total.
    intent = PurchaseIntent.model_validate(
        {
            "hard": {
                "currency": currency,
                "max_total_amount_minor": budget,
                "categories": [a["category"]],
                "conditions": [a["condition"]],
                "required_terms": [a["product_title"]],
                "required_options": [
                    {"name": "almacenamiento", "value": a["attributes"]["storage"]}
                ],
            }
        }
    )
    request = (
        f"Compra {a['product_title']} {a['attributes']['storage']}, {a['condition']}, "
        f"con presupuesto total de {budget} centavos de {currency}."
    )
    settings = Settings(
        merchant_base_url=p2_lane.origin,
        merchant_bearer_token=episode.bearer,
        merchant_webhook_secret=p2_lane.webhook_secret,
        agent_approval_secret=p2_lane.approval_secret,
        payment_provider_url=p2_lane.payment.origin,
        payment_agent_bearer_token=p2_lane.payment_token,
        agent_database_path=tmp_path / "agent.sqlite",
        agent_feed_source="file",
        agent_feed_path=episode.feed,
        agent_principal_id="p2-buyer",
    )
    before = p2_lane.rpc("p0_episode_state", run_id=episode.run_id)
    tokens = p2_lane.payment_tokens()
    calls_before = p2_lane.proxy.count("POST", "/checkout_sessions")
    ranked = rank_candidates(load_product_feed(episode.feed), intent)
    app = create_app(
        settings=settings,
        clock=lambda: APPROVED_AT,
        conversation_router=StaticConversationRouter(request, intent),
    )
    with TestClient(app) as client:
        response = client.post(
            "/conversation/turn",
            json={
                "turn_id": "p2-bridge",
                "messages": [{"role": "user", "content": request}],
                "details": buyer_details("p2").model_dump(mode="json", exclude_none=True),
            },
        )
        turn = response.json()
        if response.status_code != 200 or turn.get("action") != "purchase":
            p2_lane.stack.record(
                "P2-PROV-03",
                status="failed",
                phase="conversation",
                interpretation="simulated",
                http_status=response.status_code,
                action=turn.get("action"),
                expected_selection=a["id"],
                feed_sha256=feed_hash(episode.feed),
            )
        assert response.status_code == 200 and turn["action"] == "purchase"
        purchase = turn["purchase"]
        run_id = purchase["run_id"]
        blocked = client.post(f"/purchases/{run_id}/complete")
        prepared = p2_lane.rpc("p0_episode_state", run_id=episode.run_id)
        total = next(
            row["amount"] for row in purchase["checkout"]["totals"] if row["type"] == "total"
        )
        facts = {
            "status": "passed",
            "interpretation": "simulated; existing StaticConversationRouter",
            "user_input": request,
            "intent": intent.model_dump(mode="json"),
            "episode_run_id": episode.run_id,
            "feed_sha256": feed_hash(episode.feed),
            "eligible": [r.candidate.variant_id for r in ranked.ranked],
            "rejection_counts": ranked.rejection_counts,
            "pair": [a["id"], b["id"]],
            "expected_selection": a["id"],
            "observed_selection": purchase["selected_candidate"]["variant_id"],
            "action": turn["action"],
            "checkout_total_minor": total,
            "checkout_posts": p2_lane.proxy.count("POST", "/checkout_sessions") - calls_before,
            "orders": prepared["orders"],
            "payment_tokens": p2_lane.payment_tokens() - tokens,
            "approval_gate_http": blocked.status_code,
            "ux03_validated": False,
        }
        try:
            assert facts["observed_selection"] == a["id"]
            assert facts["checkout_posts"] == 1 and len(prepared["checkouts"]) == 1
            assert blocked.status_code == 409 and facts["orders"] == 0
            assert facts["payment_tokens"] == 0 and total <= budget
            assert before["offers"] == prepared["offers"]
        except AssertionError:
            facts["status"] = "failed"
            raise
        finally:
            p2_lane.stack.record("P2-PROV-03", **facts)
            canceled = client.post(f"/purchases/{run_id}/cancel")
            assert canceled.status_code == 200
    after = p2_lane.rpc("p0_episode_state", run_id=episode.run_id)
    assert len(after["checkouts"]) == 1 and after["checkouts"][0]["status"] == "canceled"
    assert after["offers"] == before["offers"] and after["orders"] == 0
    p2_lane.stack.record(
        "P2-cleanup",
        status="passed",
        checkouts_canceled=1,
        orders=0,
        payment_tokens=p2_lane.payment_tokens() - tokens,
    )
