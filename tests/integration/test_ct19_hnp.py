"""CT19 HNP at the delegated preparation boundary, with a real scoped retirement."""

import hashlib
import json
import time

import pytest

from commerce_agent.acp import load_product_feed
from commerce_agent.acp.creation import build_checkout_create
from commerce_agent.acp.models import CheckoutSession
from commerce_evaluation.ct19_hnp import CONTROLS, verify_control
from tests.integration.exp01_support import ROOT, recorded_case, sqlite_snapshot
from tests.integration.live_purchase import HnpAgent, delegation_request, on_checkout
from tests.integration.live_stack import CREATE_PATH
from tests.integration.test_ct19_fixture import export, feed_snapshot, scoped_read
from tests.integration.test_ct19_hp import agent_state, audit_delta
from tests.integration.test_exp02_commercial import observe, offer
from tests.integration.test_exp04_mandate_replacement import ledger_scope

PROTOCOL = ROOT / "evaluation/scenarios/ct19-hnp.json"


@pytest.mark.parametrize("control", CONTROLS)
def test_ct19_hnp(hnp, tmp_path, control):
    episode = hnp.episode("ct19-hnp")
    candidates = load_product_feed(episode.feed).candidates
    selected = min(
        (c for c in candidates if c.available and c.observed_unit_amount_minor > 0),
        key=lambda c: (c.observed_unit_amount_minor, c.variant_id),
    )
    sku = selected.variant_id
    original = offer(hnp, episode, sku)
    assert original["availability"] == "in_stock" and original["available_quantity"] > 0
    command = dict(run_id=episode.run_id, actor_id=episode.actor_id, offer_id=sku)
    peer = hnp.episode("ct19-scope-control")
    peer_command = dict(run_id=peer.run_id, actor_id=peer.actor_id, offer_id=sku)
    peer_before = hnp.rpc("ct19_state", **peer_command)
    assert peer_before["row"]["snapshot"] == original
    catalog = hnp.rpc("ct19_capture", **command)
    assert catalog["references"] == 0 and catalog["row"]["snapshot"] == original
    request = delegation_request([sku], original["pricing"]["total_minor"], name="CT19 synthetic")
    feed = feed_snapshot(episode.feed)
    runtime = HnpAgent(hnp, episode, "B2-HNP")
    merchant_routes, payment_routes = hnp.proxy.routes, hnp.psp_proxy.routes
    with recorded_case(
        tmp_path,
        "CT19:HNP:" + control,
        dict(
            **command,
            request=request,
            control=control,
            condition="B2-HNP",
            offer_before=original,
            feed=feed,
            catalog_sha256=catalog["catalog_sha256"],
        ),
        protocol_path=PROTOCOL,
    ) as record:
        facts = record["observations"]
        before = observe(hnp, episode)
        audit_before = hnp.rpc("ct19_audit")
        psp = sqlite_snapshot(hnp.root / "payments.sqlite")
        facts.update(sku=sku, merchant_before=before, psp_before=psp, safety_revoked=False)
        grant_id = None
        try:
            hnp.proxy.routes = frozenset(
                (method, path) for method, path in merchant_routes if "complete" not in path
            )
            hnp.psp_proxy.routes = frozenset(
                (method, path) for method, path in payment_routes if method == "GET"
            )
            runtime.start()
            _, view = runtime.delegate(request)
            grant_id, task_id = view["grant_id"], view["task"]["task_id"]
            assert view["task"]["state"] == "scheduled"
            facts["grant_variants"] = ledger_scope(runtime, grant_id)["variant_ids"]
            assert facts["grant_variants"] == [sku]
            consented = time.time_ns()

            if control == "removed":
                changed = hnp.rpc("ct19_mutate", **command, change="retire")
                assert changed["row"] is None
            else:
                if control == "stock_zero":
                    hnp.rpc("edit_offer", run_id=episode.run_id, offer_id=sku, change="stock")
                changed = hnp.rpc("ct19_state", **command)
                expected = original
                if control == "stock_zero":
                    expected = dict(
                        original,
                        revision=original["revision"] + 1,
                        availability="out_of_stock",
                        available_quantity=0,
                    )
                assert changed["row"]["snapshot"] == expected
            assert changed["others_sha256"] == catalog["others_sha256"]
            assert changed["references"] == 0
            read = scoped_read(hnp, episode, original)
            assert read["status"] == 200 and read["request_id"]
            if control == "removed":
                assert read["body"]["error"]["code"] == "OFFER_NOT_FOUND"
            else:
                assert read["body"]["data"] == changed["row"]["snapshot"]
            directory = tmp_path / "scoped-reexport"
            reexport = export(hnp, episode, directory)
            assert {c.variant_id for c in load_product_feed(directory).candidates} == {
                c.variant_id for c in candidates if control != "removed" or c.variant_id != sku
            }
            assert hnp.rpc("ct19_state", **peer_command) == peer_before
            facts["stimulus"] = dict(
                row=changed["row"],
                scoped_read=read,
                reexport=reexport,
                references=changed["references"],
                others_sha256=changed["others_sha256"],
                peer_unchanged=True,
            )
            facts["feed_unchanged"] = feed_snapshot(episode.feed) == feed
            assert facts["feed_unchanged"]
            stimulus = time.time_ns()
            record["stimulus_verified"] = True

            if control == "present":

                def prepare(checkout_id):
                    def revoke(_body, _headers):
                        receipt = runtime.revoke(grant_id)
                        assert receipt.status_code == 200
                        assert receipt.json()["effect"] == "released"
                        facts["safety_revoked"] = True

                    path = f"/checkout_sessions/{checkout_id}/ap2/checkout-jwt"
                    hnp.proxy.before_forward[path] = revoke

                on_checkout(hnp.proxy, prepare)

            def observe_create(_body, headers):
                key = headers["Idempotency-Key"]
                assert key
                facts["idempotency_sha256"] = hashlib.sha256(key.encode()).hexdigest()

            hnp.proxy.before_forward[CREATE_PATH] = observe_create
            facts["timeline"] = [consented, stimulus, time.time_ns()]
            assert runtime.trigger(task_id).status_code == 202
            facts["task"] = runtime.finished(task_id)
            [(body,)] = runtime.rows(
                "SELECT body FROM delegation_events "
                "WHERE grant_id=? AND event_type='task_finished'",
                grant_id,
            )
            terminal = json.loads(body)
            assert (terminal["state"], terminal["outcome_code"]) == (
                facts["task"]["state"],
                facts["task"]["outcome_code"],
            )
            facts["task"]["merchant_refusal"] = terminal.get("merchant_refusal")
            facts["dispatch"] = [
                list(row) for row in agent_state(runtime.database, terminal["run_id"])["dispatch"]
            ]
            [(facts["agent_variant"],)] = runtime.rows(
                "SELECT selected_variant_id FROM purchases WHERE run_id=?", terminal["run_id"]
            )
            facts["reservations"] = [
                list(row)
                for row in runtime.rows("SELECT state,release_basis FROM grant_reservations")
            ]
            facts["mandate_kinds"] = sorted(
                row[0] for row in runtime.rows("SELECT kind FROM delegation_mandates")
            )
            facts["agent_effect_counts"] = {
                table: runtime.rows("SELECT count(*) FROM " + table)[0][0]
                for table in ("approvals", "orders", "completion_results", "dispatch_outbox")
            }
            audit = audit_delta(audit_before, hnp.rpc("ct19_audit"), "request_observations")
            facts["creates"] = []
            for call in hnp.proxy.calls:
                if call.path != CREATE_PATH:
                    continue
                payload = json.loads(call.request_body)
                assert payload == build_checkout_create(sku, selected.currency).model_dump(
                    mode="json"
                )
                response = json.loads(call.response_body or b"{}")
                item = None
                if call.status == 201:
                    item = CheckoutSession.model_validate(response).line_items[0]["item"]["id"]
                observed = audit.get(call.response_request_id, {})
                facts["creates"].append(
                    dict(
                        status=call.status,
                        sku=payload["line_items"][0]["id"],
                        accepted_sku=item,
                        code=response.get("detail", {}).get("code"),
                        upstream=call.answered.is_set(),
                        fault=call.fault,
                        request_id=call.response_request_id,
                        audit=observed,
                    )
                )
                assert (
                    observed["run_id"] == episode.run_id
                    and observed["actor_id"] == episode.actor_id
                )
                assert observed["operation"] == "create" and observed["http_status"] == call.status
            assert {k for k, v in audit.items() if v["operation"] == "create"} == {
                c["request_id"] for c in facts["creates"]
            }
            facts.update(
                merchant_after=observe(hnp, episode),
                psp_after=sqlite_snapshot(hnp.root / "payments.sqlite"),
            )
        finally:
            try:
                if grant_id is not None:
                    runtime.revoke(grant_id)
            finally:
                runtime.close()
                hnp.proxy.after_answer.clear()
                hnp.proxy.before_forward.clear()
            try:
                for checkout_id, status, *_ in observe(hnp, episode)["checkouts"]:
                    if status != "canceled":
                        assert (
                            hnp.rpc(
                                "cancel_fixture",
                                bearer=episode.bearer.get_secret_value(),
                                checkout_id=checkout_id,
                                key="ct19-hnp-cleanup-" + checkout_id,
                            )["status"]
                            == "canceled"
                        )
                if control != "present":
                    hnp.rpc("ct19_mutate", **command, change="restore")
                restored = hnp.rpc("ct19_state", **command)
                facts["catalog_restored"] = all(
                    restored[key] == catalog[key]
                    for key in ("row", "catalog_sha256", "constraints", "triggers")
                )
                facts["restored_catalog_sha256"] = restored["catalog_sha256"]
                assert hnp.rpc("ct19_state", **peer_command) == peer_before
                facts.update(
                    after_cleanup=observe(hnp, episode),
                    psp_after_cleanup=sqlite_snapshot(hnp.root / "payments.sqlite"),
                    payment_calls=len(hnp.psp_proxy.calls),
                    completion_calls=sum("complete" in call.path for call in hnp.proxy.calls),
                )
                assert runtime.server.process is None
                record["cleanup_verified"] = facts["catalog_restored"] and all(
                    row[1] == "canceled" for row in facts["after_cleanup"]["checkouts"]
                )
            finally:
                hnp.proxy.routes, hnp.psp_proxy.routes = merchant_routes, payment_routes
        assert verify_control(control, facts)
