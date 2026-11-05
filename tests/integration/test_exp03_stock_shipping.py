"""CT13 HNP and CT16 HP; existing product, independent Merchant/PSP, no complete or LLM."""

import hashlib
import json
import time

import pytest

from commerce_agent.acp import load_product_feed
from tests.integration.exp01_support import ROOT, recorded_case, sqlite_snapshot
from tests.integration.live_purchase import HnpAgent, delegation_request
from tests.integration.live_stack import CREATE_PATH
from tests.integration.test_exp02_commercial import (
    cleanup,
    hp_revalidation,
    observe,
    offer,
)
from tests.integration.test_exp02_commercial import (
    hp_lane as hp_lane,
)

PROTOCOL = ROOT / "evaluation/scenarios/exp03-stock-shipping.json"


@pytest.mark.parametrize("control", ["stable", "increased"])
def test_ct16_shipping_after_checkout(hp_lane, tmp_path, monkeypatch, control):
    hp_revalidation(hp_lane, tmp_path, monkeypatch, control, scenario="CT16", protocol=PROTOCOL)


def test_ct13_stock_after_consent(hnp, tmp_path):
    episode = hnp.episode("exp03-ct13")
    feed = load_product_feed(episode.feed)
    selected = sorted(feed.candidates, key=lambda c: (c.observed_unit_amount_minor, c.variant_id))[
        0
    ]
    original = offer(hnp, episode, selected.variant_id)
    assert original["availability"] == "in_stock" and original["available_quantity"] > 0
    request = delegation_request(
        [selected.variant_id], original["pricing"]["total_minor"], name="EXP03 synthetic buyer"
    )
    runtime = HnpAgent(hnp, episode, "B2-HNP")
    merchant_routes, payment_routes = hnp.proxy.routes, hnp.psp_proxy.routes
    # Effect ceiling: any unexpected attempt is recorded/failed, never paid or completed.
    hnp.proxy.routes = frozenset(
        (m, p) for m, p in merchant_routes if "complete" not in p and "checkout-jwt" not in p
    )
    hnp.psp_proxy.routes = frozenset((m, p) for m, p in payment_routes if m == "GET")
    with recorded_case(
        tmp_path,
        "CT13:HNP:depleted",
        {
            "run_id": episode.run_id,
            "actor_id": episode.actor_id,
            "mode": "HNP",
            "condition": "B2-HNP",
            "request": request,
            "offer_before": original,
            "feed_sha256": hashlib.sha256(
                (episode.feed / "products.jsonl").read_bytes()
            ).hexdigest(),
            "mutation": "only stock/revision after consent, before trigger",
        },
        protocol_path=PROTOCOL,
    ) as record:
        facts = record["observations"]
        grant_id = None
        try:
            before = observe(hnp, episode)
            psp = sqlite_snapshot(hnp.root / "payments.sqlite")
            facts.update(
                merchant_before=before, psp_before=psp, offer_before=original, request=request
            )
            runtime.start()
            proposal, view = runtime.delegate(request)
            grant_id, task_id = view["grant_id"], view["task"]["task_id"]
            assert view["task"]["state"] == "scheduled"
            facts.update(
                grant_id=grant_id,
                task_id=task_id,
                task_before=view["task"],
                grant_scope=proposal["scope"],
                consented_at_ns=time.time_ns(),
            )
            hnp.rpc(
                "edit_offer", run_id=episode.run_id, offer_id=selected.variant_id, change="stock"
            )
            changed = offer(hnp, episode, selected.variant_id)
            expected = json.loads(json.dumps(original))
            expected.update(
                revision=original["revision"] + 1, availability="out_of_stock", available_quantity=0
            )
            assert changed == expected
            facts.update(offer_after=changed, mutation_at_ns=time.time_ns())
            record["stimulus_verified"] = True
            facts["trigger_at_ns"] = time.time_ns()
            response = runtime.trigger(task_id)
            facts["trigger_status"] = response.status_code
            assert response.status_code == 202
            task = runtime.finished(task_id)
            calls = [call for call in hnp.proxy.calls if call.path == CREATE_PATH]
            facts.update(
                task_after=task,
                merchant_after=observe(hnp, episode),
                psp_after=sqlite_snapshot(hnp.root / "payments.sqlite"),
                create_posts=len(calls),
                merchant_http_status=[c.status for c in calls],
            )
            counts = {
                name: runtime.rows("SELECT count(*) FROM " + name)[0][0]
                for name in ("grant_reservations", "dispatch_outbox", "completion_results")
            }
            facts["agent_effect_counts"] = counts
            facts["mandate_kinds"] = runtime.rows("SELECT kind FROM delegation_mandates")
            assert facts["consented_at_ns"] < facts["mutation_at_ns"] < facts["trigger_at_ns"]
            assert len(calls) == 1 and calls[0].status == 409
            facts["merchant_refusal"] = json.loads(calls[0].response_body)
            assert "out_of_stock" in json.dumps(facts["merchant_refusal"])
            assert task["state"] == "failed" and task["outcome_code"] == "checkout_failed"
            assert not facts["merchant_after"]["checkouts"]
            assert all(value == 0 for value in counts.values())
            assert sorted(row[0] for row in facts["mandate_kinds"]) == [
                "open_checkout",
                "open_payment",
            ]
            assert facts["psp_after"] == psp
            for table in ("orders", "checkout_completion_attempts"):
                assert facts["merchant_after"]["tables"][table] == before["tables"][table]
            assert not any("complete" in call.path for call in hnp.proxy.calls)
            assert not hnp.psp_proxy.calls
        finally:
            if grant_id is not None:
                runtime.revoke(grant_id)
            runtime.close()
            cleanup(hnp, episode, selected.variant_id, original, record)
            hnp.proxy.routes, hnp.psp_proxy.routes = merchant_routes, payment_routes
            assert runtime.server.process is None
