"""CT19 HP contract: identity retired after durable D1, before the real Merchant POST."""

import hashlib
import json
import sqlite3
import time
from contextlib import closing

import pytest

from commerce_agent.application import PurchaseServiceError
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.runtime import build_runtime
from tests.integration.exp01_support import ROOT, recorded_case, sqlite_snapshot
from tests.integration.live_purchase import buyer_details
from tests.integration.live_stack import CREATE_PATH
from tests.integration.test_ct19_fixture import export, feed_snapshot, scoped_read
from tests.integration.test_exp02_commercial import local_effects, observe, setup

PROTOCOL = ROOT / "evaluation/scenarios/ct19-hp.json"


def agent_state(path, run_id):
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        return {
            "purchase": db.execute(
                "SELECT stage,selected_variant_id,checkout_id,order_id,failure_code "
                "FROM purchases WHERE run_id=?",
                (run_id,),
            ).fetchall(),
            "d1": db.execute(
                "SELECT record_kind,outcome FROM governance_evidence "
                "WHERE run_id=? AND phase='D1' ORDER BY sequence",
                (run_id,),
            ).fetchall(),
            "dispatch": db.execute(
                "SELECT r.state,r.checkout_id,r.code FROM d1_checkout_results r "
                "JOIN d1_checkout_operations o USING(operation_id) WHERE o.run_id=?",
                (run_id,),
            ).fetchall(),
        }


@pytest.mark.parametrize("control", ["removed", "stock_zero"])
def test_ct19_hp(b0, tmp_path, monkeypatch, control):
    lane = b0
    episode, candidates, selected, original, settings = setup(lane, tmp_path, monkeypatch)
    command = dict(run_id=episode.run_id, actor_id=episode.actor_id, offer_id=selected.variant_id)
    catalog = lane.rpc("ct19_capture", **command)
    before = observe(lane, episode)
    psp = sqlite_snapshot(lane.root / "payments.sqlite")
    feed = feed_snapshot(episode.feed)
    intent = PurchaseIntent(
        hard=HardConstraints(
            currency=original["pricing"]["currency"],
            max_total_amount_minor=original["pricing"]["total_minor"],
        )
    )
    local_run = "ct19-hp-" + control
    with recorded_case(
        tmp_path,
        "CT19:HP:" + control,
        dict(
            **command,
            control=control,
            offer=original,
            catalog=catalog,
            feed=feed,
            merchant_state=before,
            psp_state=psp,
            intent=intent.model_dump(mode="json"),
            selected_input_sku=selected.variant_id,
            quantity=1,
            clock=str(settings.agent_sandbox_clock_at),
            mutation_phase="after durable D1 allow and before forwarding sole creation POST",
            fixture_gate_run=41967635493,
            fixture_gate_zip_sha256="237b04b72973fe106003b41766029311845e0fd5007a12aeed20d648467a8a28",
        ),
        protocol_path=PROTOCOL,
    ) as record:
        runtime = None
        facts = record["observations"]
        facts.update(
            merchant_before=before,
            psp_before=psp,
            catalog_before=catalog,
            feed_before=feed,
            commercial_result="unknown",
        )
        try:
            runtime = build_runtime(settings)

            def mutate(body, headers):
                from commerce_agent.acp.creation import build_checkout_create

                facts["pre_mutation_agent"] = agent_state(settings.agent_database_path, local_run)
                state = facts["pre_mutation_agent"]
                assert state["purchase"] == [("selected", selected.variant_id, None, None, None)]
                assert state["d1"] == [("decision_record", "allow")]
                assert state["dispatch"] == []
                facts["d1_observed_at_ns"] = time.time_ns()
                assert json.loads(body) == build_checkout_create(
                    selected.variant_id, selected.currency
                ).model_dump(mode="json")
                facts["idempotency_sha256"] = hashlib.sha256(
                    headers["Idempotency-Key"].encode()
                ).hexdigest()
                facts["read_before"] = scoped_read(lane, episode, original)
                assert facts["read_before"]["body"]["data"] == original
                if control == "removed":
                    changed = lane.rpc("ct19_mutate", **command, change="retire")
                    assert changed["row"] is None
                else:
                    lane.rpc(
                        "edit_offer",
                        run_id=episode.run_id,
                        offer_id=selected.variant_id,
                        change="stock",
                    )
                    changed = lane.rpc("ct19_state", **command)
                    expected = json.loads(json.dumps(original))
                    expected.update(
                        revision=original["revision"] + 1,
                        availability="out_of_stock",
                        available_quantity=0,
                    )
                    assert changed["row"]["snapshot"] == expected
                facts["catalog_changed"] = changed
                assert changed["references"] == 0
                assert changed["others_sha256"] == catalog["others_sha256"]
                facts["read_changed"] = scoped_read(lane, episode, original)
                read = facts["read_changed"]
                assert read["status"] == 200
                if control == "removed":
                    assert read["body"]["error"]["code"] == "OFFER_NOT_FOUND"
                else:
                    assert read["body"]["data"] == changed["row"]["snapshot"]
                directory = tmp_path / "mutated-feed-not-given-to-agent"
                facts["feed_changed"] = export(lane, episode, directory)
                from commerce_agent.acp import load_product_feed

                ids = {c.variant_id for c in load_product_feed(directory).candidates}
                assert ids == {
                    c.variant_id
                    for c in candidates
                    if control != "removed" or c.variant_id != selected.variant_id
                }
                assert feed_snapshot(episode.feed) == feed
                facts["mutation_verified_at_ns"] = time.time_ns()
                assert facts["mutation_verified_at_ns"] > facts["d1_observed_at_ns"]
                record["stimulus_verified"] = True

            lane.proxy.before_forward[CREATE_PATH] = mutate
            try:
                runtime.purchases.start(
                    run_id=local_run,
                    owner_id=episode.actor_id,
                    request="Prepare only this observed identity within its budget; no payment",
                    intent=intent,
                    selected_variant_id=selected.variant_id,
                    details=buyer_details("CT19 synthetic buyer"),
                )
                facts["agent_result"] = "unexpected_success"
            except PurchaseServiceError as error:
                facts["agent_result"] = error.code
            calls = [c for c in lane.proxy.calls if c.path == CREATE_PATH]
            facts["acp_calls"] = [
                dict(
                    method=c.method,
                    path=c.path,
                    fault=c.fault,
                    status=c.status,
                    request=json.loads(c.request_body) if c.request_body else None,
                    response=json.loads(c.response_body) if c.response_body else None,
                )
                for c in lane.proxy.calls
            ]
            facts.update(
                create_posts=len(calls),
                agent_after=agent_state(settings.agent_database_path, local_run),
                agent_effect_counts=local_effects(settings.agent_database_path),
                agent_tables=sqlite_snapshot(settings.agent_database_path),
                merchant_after=observe(lane, episode),
                psp_after=sqlite_snapshot(lane.root / "payments.sqlite"),
            )
            assert record.get("stimulus_verified") is True
            code, status = ("invalid_item", 404) if control == "removed" else ("out_of_stock", 409)
            assert len(calls) == 1 and calls[0].fault == "pass" and calls[0].status == status
            assert json.loads(calls[0].response_body)["detail"]["code"] == code
            assert facts["agent_result"] == code
            facts["commercial_result"] = "false"
            assert facts["agent_after"]["purchase"] == [
                ("failed", selected.variant_id, None, None, code)
            ]
            assert facts["agent_after"]["dispatch"] == [
                ("definitive_failure", None, "VERIFIED_NOT_CREATED")
            ]
            assert all(n == 0 for n in facts["agent_effect_counts"].values())
            assert not facts["merchant_after"]["checkouts"]
            assert facts["psp_after"] == psp and lane.payment_tokens() == 0
            for table, value in before["tables"].items():
                if table not in {"catalog_offers", "run_events"}:
                    assert facts["merchant_after"]["tables"][table] == value, table
            assert not any("complete" in c.path for c in lane.proxy.calls)
        finally:
            lane.proxy.before_forward.clear()
            if runtime is not None:
                runtime.close()
            restored = lane.rpc("ct19_mutate", **command, change="restore")
            facts["catalog_restored"] = restored
            for key in ("row", "catalog_sha256", "others_sha256", "constraints", "triggers"):
                assert restored[key] == catalog[key]
            facts["after_cleanup"] = observe(lane, episode)
            facts["psp_after_cleanup"] = sqlite_snapshot(lane.root / "payments.sqlite")
            assert feed_snapshot(episode.feed) == feed
            assert not facts["after_cleanup"]["checkouts"]
            assert facts["psp_after_cleanup"] == psp
            for table, value in before["tables"].items():
                if table != "run_events":
                    assert facts["after_cleanup"]["tables"][table] == value, table
            record["cleanup_verified"] = True
