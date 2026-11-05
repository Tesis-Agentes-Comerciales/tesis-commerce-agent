"""Phase-one gate only: real scoped identity removal, full feed and exact restoration."""

import hashlib

import httpx

from commerce_agent.acp import load_product_feed
from tests.integration.exp01_support import ROOT, recorded_case, sqlite_snapshot
from tests.integration.live_stack import CREATE_PATH
from tests.integration.test_exp02_commercial import observe, offer

PROTOCOL = ROOT / "evaluation/scenarios/ct19-fixture.json"


def feed_snapshot(directory):
    return {
        name: {
            "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest(),
            "value": (directory / name).read_text(),
        }
        for name in ("products.jsonl", "metadata.json")
    }


def scoped_read(lane, episode, original):
    response = httpx.post(
        lane.origin + "/lab/offers/get",
        headers={"Authorization": "Bearer " + episode.bearer.get_secret_value()},
        json={"offer_id": original["id"], "delivery_context": original["delivery_context"]},
        timeout=10,
    )
    return {"status": response.status_code, "body": response.json()}


def export(lane, episode, directory):
    lane.rpc("exp01_export", run_id=episode.run_id, feed_dir=str(directory))
    return feed_snapshot(directory)


def test_identity_retirement_fixture(b0, tmp_path):
    episode = b0.episode("ct19-fixture")
    candidates = sorted(
        load_product_feed(episode.feed).candidates,
        key=lambda c: (c.observed_unit_amount_minor, c.variant_id),
    )
    selected = next(c for c in candidates if c.observed_unit_amount_minor > 0)
    original = offer(b0, episode, selected.variant_id)
    assert original["availability"] == "in_stock" and original["available_quantity"] > 0
    command = dict(run_id=episode.run_id, actor_id=episode.actor_id, offer_id=selected.variant_id)
    before = b0.rpc("ct19_capture", **command)
    merchant = observe(b0, episode)
    psp = sqlite_snapshot(b0.root / "payments.sqlite")
    feed = feed_snapshot(episode.feed)
    with recorded_case(
        tmp_path,
        "CT19:fixture",
        dict(
            **command,
            catalog_before=before,
            offer=original,
            feed=feed,
            merchant_state=merchant,
            psp_state=psp,
            quantity=1,
            clock="2026-09-10T14:05:00Z",
        ),
        protocol_path=PROTOCOL,
    ) as record:
        facts = record["observations"]
        facts.update(merchant_before=merchant, psp_before=psp, before=before, feed_before=feed)
        try:
            reads = [scoped_read(b0, episode, original)]
            assert reads[0]["status"] == 200 and reads[0]["body"]["data"] == original
            retired = b0.rpc("ct19_mutate", **command, change="retire")
            facts["retired"] = retired
            assert retired["row"] is None and retired["references"] == 0
            assert len(retired["catalog"]) == len(before["catalog"]) - 1
            assert retired["others_sha256"] == before["others_sha256"]
            reads.append(scoped_read(b0, episode, original))
            facts["reads"] = reads
            assert reads[-1]["status"] == 200
            assert reads[-1]["body"]["error"]["code"] == "OFFER_NOT_FOUND"
            removed_dir = tmp_path / "removed-feed-not-given-to-agent"
            facts["feed_removed"] = export(b0, episode, removed_dir)
            removed_candidates = load_product_feed(removed_dir).candidates
            assert {c.variant_id for c in removed_candidates} == {
                c.variant_id for c in candidates if c.variant_id != selected.variant_id
            }
            assert feed_snapshot(episode.feed) == feed
            assert retired["constraints"] == before["constraints"]
            assert all(row[1] is True for row in before["constraints"])
            assert any(row[0] == "checkout_sessions_offer_fk" for row in before["constraints"])
            assert all(row[2] == "O" for row in retired["triggers"])
            facts["constraint_probes"] = b0.rpc("ct19_constraints", **command)
            assert facts["constraint_probes"] == {
                "identity": "23514",
                "revision": "23514",
                "positive_revision": "23514",
                "run_fk": "23503",
            }
            record["stimulus_verified"] = True
        finally:
            restored = b0.rpc("ct19_mutate", **command, change="restore")
            facts["restored"] = restored
            assert restored["row"] == before["row"]
            for key in ("catalog_sha256", "others_sha256", "constraints", "triggers", "references"):
                assert restored[key] == before[key]
            facts["feed_restored"] = export(b0, episode, tmp_path / "restored-feed")
            assert facts["feed_restored"] == feed
            final_read = scoped_read(b0, episode, original)
            facts.setdefault("reads", []).append(final_read)
            assert final_read["status"] == 200 and final_read["body"]["data"] == original
            facts["after_cleanup"] = b0.rpc("ct19_state", **command)
            facts["merchant_after"] = observe(b0, episode)
            facts["psp_after"] = sqlite_snapshot(b0.root / "payments.sqlite")
            assert facts["psp_after"] == psp
            assert not facts["merchant_after"]["checkouts"]
            for table, value in merchant["tables"].items():
                if table != "run_events":
                    assert facts["merchant_after"]["tables"][table] == value, table
            events = facts["after_cleanup"]["offer_read_events"]
            assert [row[1]["result"] for row in events] == ["ok", "OFFER_NOT_FOUND", "ok"]
            assert all(row[1]["actor_id"] == episode.actor_id for row in events)
            assert not any(c.path == CREATE_PATH for c in b0.proxy.calls)
            assert not b0.psp_proxy.calls
            record["cleanup_verified"] = True
