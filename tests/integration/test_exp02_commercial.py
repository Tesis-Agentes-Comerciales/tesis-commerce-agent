"""Frozen CT15 HP / CT14 HP against the existing synthetic live stack; no model or payment."""

import hashlib
import json
import sqlite3
import time
from contextlib import closing

import pytest

from commerce_agent.acp import load_product_feed
from commerce_agent.application import PurchaseServiceError
from commerce_agent.approval import ApprovalError
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.runtime import build_runtime
from tests.integration.exp01_support import ROOT, recorded_case, sqlite_snapshot
from tests.integration.live_purchase import buyer_details
from tests.integration.live_stack import CREATE_PATH

PROTOCOL = ROOT / "evaluation/scenarios/exp02-commercial.json"


def observe(lane, episode):
    return lane.rpc("exp01_observe", run_id=episode.run_id)


def offer(lane, episode, variant):
    return lane.rpc("observe_offer", run_id=episode.run_id, offer_id=variant)


def local_effects(path):
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {
            name: db.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0]
            for name in ("approvals", "completion_results", "grant_reservations", "dispatch_outbox")
            if name in tables
        }


def assert_no_payment(lane, before, psp):
    after = lane.rpc("snapshot")
    for name in ("orders", "checkout_completion_attempts"):
        assert after[name] == before["tables"][name]
    assert sqlite_snapshot(lane.root / "payments.sqlite") == psp
    assert not any("complete" in call.path for call in lane.proxy.calls)


def cleanup(lane, episode, variant, original, record):
    lane.proxy.faults.clear()
    lane.proxy.before_forward.clear()
    current = observe(lane, episode)
    for row in current["checkouts"]:
        if row[1] != "canceled":
            result = lane.rpc(
                "cancel_fixture",
                bearer=episode.bearer.get_secret_value(),
                checkout_id=row[0],
                key="exp02-cleanup-" + row[0],
            )
            assert result["status"] == "canceled"
    lane.rpc(
        "edit_offer", run_id=episode.run_id, offer_id=variant, change="restore", snapshot=original
    )
    final = observe(lane, episode)
    record["observations"]["after_cleanup"] = final
    record["observations"]["psp_after_cleanup"] = sqlite_snapshot(lane.root / "payments.sqlite")
    assert offer(lane, episode, variant) == original
    record["cleanup_verified"] = all(row[1] == "canceled" for row in final["checkouts"])
    assert record["cleanup_verified"]


def setup(lane, tmp_path, monkeypatch):
    from tests.integration.test_live_b2_d2_authorization import settings_for

    episode = lane.episode("exp02")
    feed = load_product_feed(episode.feed)
    candidates = sorted(feed.candidates, key=lambda c: (c.observed_unit_amount_minor, c.variant_id))
    selected = next(c for c in candidates if c.observed_unit_amount_minor > 0)
    original = offer(lane, episode, selected.variant_id)
    assert original["availability"] == "in_stock" and original["available_quantity"] > 0
    settings = settings_for(lane, episode, tmp_path, monkeypatch)
    return episode, candidates, selected, original, settings


@pytest.mark.parametrize("control", ["within", "above"])
def test_ct15_price_change(b0, tmp_path, monkeypatch, control):
    episode, candidates, selected, original, settings = setup(b0, tmp_path, monkeypatch)
    # The controlled replacement is another price actually observed in this Merchant episode.
    replacement = next(
        c
        for c in candidates
        if c.currency == selected.currency
        and c.observed_unit_amount_minor > original["pricing"]["items_total_minor"]
    )
    new_price = replacement.observed_unit_amount_minor
    shipping = original["pricing"]["shipping_total_minor"]
    maximum = new_price + shipping if control == "within" else original["pricing"]["total_minor"]
    intent = PurchaseIntent(
        hard=HardConstraints(currency=selected.currency, max_total_amount_minor=maximum)
    )
    feed_path = episode.feed / "products.jsonl"
    feed_hash = hashlib.sha256(feed_path.read_bytes()).hexdigest()
    with recorded_case(
        tmp_path,
        "CT15:HP:" + control,
        {
            "run_id": episode.run_id,
            "actor_id": episode.actor_id,
            "mode": "HP",
            "offer_before": original,
            "replacement_source": replacement.model_dump(mode="json"),
            "new_item_price_minor": new_price,
            "maximum_minor": maximum,
            "intent": intent.model_dump(mode="json"),
            "selected_input_sku": selected.variant_id,
            "feed_sha256": feed_hash,
            "clock": str(settings.agent_sandbox_clock_at),
            "mutation": "controlled synthetic item price and revision; shipping unchanged",
        },
        protocol_path=PROTOCOL,
    ) as record:
        runtime = None
        facts = record["observations"]
        try:
            before = observe(b0, episode)
            psp = sqlite_snapshot(b0.root / "payments.sqlite")
            b0.rpc(
                "reprice_offer",
                run_id=episode.run_id,
                variant=selected.variant_id,
                price_minor=new_price,
                feed_dir=str(tmp_path / "mutated-feed-not-given-to-agent"),
            )
            changed = offer(b0, episode, selected.variant_id)
            expected = json.loads(json.dumps(original))
            expected["revision"] += 1
            expected["pricing"]["items_total_minor"] = new_price
            expected["pricing"]["total_minor"] = new_price + shipping
            assert changed == expected
            mutation_at = time.time_ns()
            assert hashlib.sha256(feed_path.read_bytes()).hexdigest() == feed_hash
            facts.update(
                merchant_before=before,
                psp_before=psp,
                offer_before=original,
                offer_after=changed,
                feed_sha256=feed_hash,
                mutation_at_ns=mutation_at,
            )
            record["stimulus_verified"] = True
            runtime = build_runtime(settings)
            facts["agent_call_at_ns"] = time.time_ns()
            prepared = None
            try:
                prepared = runtime.purchases.start(
                    run_id="exp02-ct15-" + control,
                    owner_id=episode.actor_id,
                    request="Prepare the observed variant within the typed budget; no payment",
                    intent=intent,
                    selected_variant_id=selected.variant_id,
                    details=buyer_details("exp02"),
                )
                result = prepared.stage.value
                facts["checkout"] = prepared.checkout.model_dump(mode="json")
                facts["price_changed"] = prepared.price_changed
            except PurchaseServiceError as error:
                result = error.code
            facts.update(
                agent_result=result,
                merchant_after=observe(b0, episode),
                psp_after=sqlite_snapshot(b0.root / "payments.sqlite"),
                create_posts=b0.proxy.count("POST", CREATE_PATH),
                agent_effect_counts=local_effects(settings.agent_database_path),
            )
            assert facts["agent_call_at_ns"] > mutation_at
            assert facts["create_posts"] == 1
            assert all(value == 0 for value in facts["agent_effect_counts"].values())
            calls = [c for c in b0.proxy.calls if c.path == CREATE_PATH]
            assert len(calls) == 1 and calls[0].status == 201
            response = json.loads(calls[0].response_body)
            facts["merchant_creation"] = response
            # Total obtained from the actual Merchant HTTP response, never Agent scoring.
            from commerce_agent.acp.models import CheckoutSession

            authoritative = CheckoutSession.model_validate(response).total_minor
            assert authoritative == changed["pricing"]["total_minor"]
            if control == "within":
                assert prepared is not None
                assert result == "awaiting_confirmation"
                assert authoritative == maximum
                assert prepared.price_changed is True
                assert prepared.checkout.total_minor == maximum
            else:
                assert result == "budget_exceeded"
                assert authoritative > maximum
                assert all(c[1] == "canceled" for c in facts["merchant_after"]["checkouts"])
            assert_no_payment(b0, before, psp)
        finally:
            if runtime is not None:
                runtime.close()
            cleanup(b0, episode, selected.variant_id, original, record)


@pytest.mark.parametrize("control", ["stable", "depleted"])
def test_ct14_stock_after_checkout(b0, tmp_path, monkeypatch, control):
    episode, _, selected, original, settings = setup(b0, tmp_path, monkeypatch)
    maximum = original["pricing"]["total_minor"]
    intent = PurchaseIntent(
        hard=HardConstraints(currency=selected.currency, max_total_amount_minor=maximum)
    )
    feed_hash = hashlib.sha256((episode.feed / "products.jsonl").read_bytes()).hexdigest()
    with recorded_case(
        tmp_path,
        "CT14:HP:" + control,
        {
            "run_id": episode.run_id,
            "actor_id": episode.actor_id,
            "mode": "HP",
            "offer_before": original,
            "maximum_minor": maximum,
            "control": control,
            "intent": intent.model_dump(mode="json"),
            "selected_input_sku": selected.variant_id,
            "feed_sha256": feed_hash,
            "clock": str(settings.agent_sandbox_clock_at),
        },
        protocol_path=PROTOCOL,
    ) as record:
        runtime = None
        facts = record["observations"]
        try:
            before = observe(b0, episode)
            psp = sqlite_snapshot(b0.root / "payments.sqlite")
            facts.update(
                merchant_before=before, psp_before=psp, offer_before=original, feed_sha256=feed_hash
            )
            runtime = build_runtime(settings)
            local_run = "exp02-ct14-" + control
            prepared = runtime.purchases.start(
                run_id=local_run,
                owner_id=episode.actor_id,
                request="Prepare this observed variant; no approval or payment",
                intent=intent,
                selected_variant_id=selected.variant_id,
                details=buyer_details("exp02"),
            )
            terms = runtime.purchases.approval_terms(local_run)
            facts["terms_before"] = {
                "checkout_id": terms.checkout_id,
                "total_minor": terms.total_minor,
                "fingerprint": terms.fingerprint,
            }
            facts["prepared_at_ns"] = time.time_ns()
            facts["merchant_pre_mutation"] = observe(b0, episode)
            assert prepared.checkout.id == terms.checkout_id
            if control == "depleted":
                b0.rpc(
                    "edit_offer",
                    run_id=episode.run_id,
                    offer_id=selected.variant_id,
                    change="stock",
                )
            changed = offer(b0, episode, selected.variant_id)
            expected = json.loads(json.dumps(original))
            if control == "depleted":
                expected.update(
                    revision=original["revision"] + 1,
                    availability="out_of_stock",
                    available_quantity=0,
                )
            assert changed == expected
            facts.update(offer_after=changed, mutation_at_ns=time.time_ns())
            record["stimulus_verified"] = True
            facts["agent_call_at_ns"] = time.time_ns()
            try:
                runtime.purchases.approval_terms(local_run)
                result = "fresh_terms"
            except ApprovalError as error:
                result = error.code
            with closing(sqlite3.connect(settings.agent_database_path)) as db:
                freshness = [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT body FROM hp_evidence WHERE run_id=? ORDER BY sequence",
                        (local_run,),
                    )
                ]
            facts.update(
                agent_result=result,
                freshness=freshness,
                merchant_after=observe(b0, episode),
                psp_after=sqlite_snapshot(b0.root / "payments.sqlite"),
                create_posts=b0.proxy.count("POST", CREATE_PATH),
                agent_effect_counts=local_effects(settings.agent_database_path),
            )
            assert facts["prepared_at_ns"] < facts["mutation_at_ns"] < facts["agent_call_at_ns"]
            assert facts["create_posts"] == 1 and len(freshness) == 2
            assert all(value == 0 for value in facts["agent_effect_counts"].values())
            assert freshness[0]["outcome"] == "fresh"
            if control == "depleted":
                assert result == "checkout_terms_changed"
                assert freshness[-1]["changed_fields"] == ["stock"]
                assert freshness[-1]["decision"] == "block"
            else:
                assert result == "fresh_terms" and freshness[-1]["outcome"] == "fresh"
            assert_no_payment(b0, before, psp)
        finally:
            if runtime is not None:
                runtime.close()
            cleanup(b0, episode, selected.variant_id, original, record)
