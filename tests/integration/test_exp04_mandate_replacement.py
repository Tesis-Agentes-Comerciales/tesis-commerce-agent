"""Real HNP revoke + new consent; immutable original grants, zero commercial effects."""

import hashlib
import json
import time

import pytest

from commerce_agent.acp import load_product_feed
from commerce_evaluation.stack.fault_proxy import CREATE_PATH
from commerce_evaluation.stack.hnp_agent import HnpAgent, delegation_request
from commerce_evaluation.verify_evidence import verify_database
from tests.integration.exp01_support import ROOT, recorded_case, sqlite_snapshot
from tests.integration.test_exp02_commercial import cleanup, observe, offer

PROTOCOL = ROOT / "evaluation/scenarios/exp04-mandate-replacement.json"


def ledger_scope(runtime, grant_id):
    [(value,)] = runtime.rows("SELECT scope_json FROM delegation_grants WHERE grant_id=?", grant_id)
    return json.loads(value)["scope"]


def verified(runtime, lane, grant_id, *, reached_d1):
    [document] = verify_database(
        runtime.database, psp_database=lane.root / "payments.sqlite", grant_id=grant_id
    )
    expected_intent = "ok" if reached_d1 else "not_applicable"
    assert document["links"]["intent"]["status"] == expected_intent
    for name in ("delegation_consent", "open_mandates"):
        assert document["links"][name]["status"] == "ok", document["links"][name]
    return document


@pytest.mark.parametrize("scenario", ["MA22", "MA23"])
def test_replace_grant_before_trigger(hnp, tmp_path, scenario):
    episode = hnp.episode("exp04-" + scenario.lower())
    candidates = sorted(
        load_product_feed(episode.feed).candidates,
        key=lambda c: (c.observed_unit_amount_minor, c.variant_id),
    )
    selected = candidates[0]
    x = offer(hnp, episode, selected.variant_id)
    assert x["availability"] == "in_stock" and x["available_quantity"] > 0
    old_ids = [selected.variant_id]
    new_ids = list(old_ids)
    old_budget = x["pricing"]["total_minor"]
    new_budget = x["pricing"]["items_total_minor"] - 1
    y = None
    if scenario == "MA23":
        # Fixture prerequisite is actual availability, never an observed Agent outcome.
        for candidate in candidates:
            if candidate.observed_unit_amount_minor <= old_budget:
                continue
            observed = offer(hnp, episode, candidate.variant_id)
            if (
                observed["product_status"] == "active"
                and observed["availability"] == "in_stock"
                and observed["available_quantity"] > 0
            ):
                y = observed
                break
        assert y is not None, "No available Merchant alternative above the fixed budget"
        assert y["pricing"]["items_total_minor"] > old_budget
        old_ids.append(y["id"])
        new_ids = [y["id"]]
        new_budget = old_budget
    old_request = delegation_request(old_ids, old_budget, name="EXP04 synthetic buyer")
    new_request = delegation_request(new_ids, new_budget, name="EXP04 synthetic buyer")
    runtime = HnpAgent(hnp, episode, "B2-HNP")
    routes, psp_routes = hnp.proxy.routes, hnp.psp_proxy.routes
    # No attempt at creating or paying is allowed for these unsatisfiable new scopes.
    hnp.proxy.routes = frozenset(
        (m, p)
        for m, p in routes
        if p != CREATE_PATH and "complete" not in p and "checkout-jwt" not in p
    )
    hnp.psp_proxy.routes = frozenset((m, p) for m, p in psp_routes if m == "GET")
    with recorded_case(
        tmp_path,
        scenario + ":HNP:replacement",
        {
            "run_id": episode.run_id,
            "actor_id": episode.actor_id,
            "mode": "HNP",
            "condition": "B2-HNP",
            "old_request": old_request,
            "new_request": new_request,
            "offer_x": x,
            "offer_y": y,
            "feed_sha256": hashlib.sha256(
                (episode.feed / "products.jsonl").read_bytes()
            ).hexdigest(),
            "budget_rule": "MA22 item price minus one minor unit; MA23 same maximum, exclude X",
        },
        protocol_path=PROTOCOL,
    ) as record:
        facts = record["observations"]
        grants = []
        try:
            before = observe(hnp, episode)
            psp = sqlite_snapshot(hnp.root / "payments.sqlite")
            facts.update(
                merchant_before=before,
                psp_before=psp,
                old_request=old_request,
                new_request=new_request,
            )
            runtime.start()
            _, old = runtime.delegate(old_request)
            old_id, old_task = old["grant_id"], old["task"]["task_id"]
            grants.append(old_id)
            assert old["task"]["state"] == "scheduled"
            old_scope = ledger_scope(runtime, old_id)
            assert runtime.revoke(old_id).status_code == 200
            revoked_at = time.time_ns()
            old_state = runtime.rows(
                "SELECT state,revoked_reason,authority_epoch FROM delegation_grants "
                "WHERE grant_id=?",
                old_id,
            )
            assert old_state == [("revoked", "owner", 2)]
            _, new = runtime.delegate(new_request)
            new_id, new_task = new["grant_id"], new["task"]["task_id"]
            grants.append(new_id)
            new_consented = time.time_ns()
            new_scope = ledger_scope(runtime, new_id)
            assert new["task"]["state"] == "scheduled" and new_id != old_id and new_task != old_task
            assert ledger_scope(runtime, old_id) == old_scope
            assert old_scope["total_max_minor"] == old_budget
            assert new_scope["total_max_minor"] == new_budget
            assert new_scope["variant_ids"] == new_ids
            if scenario == "MA22":
                assert old_scope["variant_ids"] == new_scope["variant_ids"]
                assert new_budget < x["pricing"]["items_total_minor"] <= old_budget
            else:
                assert (
                    new_budget == old_budget and selected.variant_id not in new_scope["variant_ids"]
                )
                assert set(old_scope["variant_ids"]) - set(new_scope["variant_ids"]) == {
                    selected.variant_id
                }
            facts.update(
                old_grant_id=old_id,
                new_grant_id=new_id,
                old_task_id=old_task,
                new_task_id=new_task,
                old_scope=old_scope,
                new_scope=new_scope,
                old_grant_after_revoke=old_state,
                revoked_at_ns=revoked_at,
                new_consented_at_ns=new_consented,
            )
            record["stimulus_verified"] = True
            facts["trigger_at_ns"] = time.time_ns()
            old_response = runtime.trigger(old_task)
            facts["old_trigger_status"] = old_response.status_code
            assert (
                old_response.status_code == 409
                and old_response.json()["code"] == "hnp_task_not_runnable"
            )
            assert runtime.trigger(new_task).status_code == 202
            task = runtime.finished(new_task)
            facts.update(
                new_task_after=task,
                merchant_after=observe(hnp, episode),
                psp_after=sqlite_snapshot(hnp.root / "payments.sqlite"),
                create_posts=hnp.proxy.count("POST", CREATE_PATH),
            )
            counts = {
                name: runtime.rows("SELECT count(*) FROM " + name)[0][0]
                for name in ("grant_reservations", "dispatch_outbox", "completion_results")
            }
            facts["agent_effect_counts"] = counts
            facts["verified_old"] = verified(runtime, hnp, old_id, reached_d1=False)
            facts["verified_new"] = verified(runtime, hnp, new_id, reached_d1=True)
            assert (task["state"], task["outcome_code"]) == ("escalated", "budget_not_satisfiable")
            assert facts["verified_old"]["links"]["revocation"]["status"] == "ok"
            assert facts["verified_new"]["links"]["escalation"]["status"] == "ok"
            assert facts["revoked_at_ns"] < facts["new_consented_at_ns"] < facts["trigger_at_ns"]
            assert facts["merchant_after"] == before and facts["psp_after"] == psp
            assert facts["create_posts"] == 0 and all(v == 0 for v in counts.values())
            assert runtime.rows(
                "SELECT count(*) FROM delegation_mandates WHERE reservation_id IS NOT NULL"
            ) == [(0,)]
            assert not hnp.psp_proxy.calls
            assert not any("complete" in c.path for c in hnp.proxy.calls)
        finally:
            for grant_id in grants:
                runtime.revoke(grant_id)
            runtime.close()
            cleanup(hnp, episode, selected.variant_id, x, record)
            hnp.proxy.routes, hnp.psp_proxy.routes = routes, psp_routes
            assert runtime.server.process is None
