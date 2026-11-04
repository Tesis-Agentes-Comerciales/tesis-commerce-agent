"""New P0 CT15 HNP; historical HN03 refresh/restart is deliberately not rerun."""

import hashlib
import json
import time

import pytest

from commerce_agent.acp import load_product_feed
from commerce_agent.acp.models import CheckoutSession
from tests.integration.exp01_support import ROOT, recorded_case, sqlite_snapshot
from tests.integration.live_purchase import HnpAgent, delegation_request, on_checkout
from tests.integration.live_stack import CREATE_PATH
from tests.integration.test_exp02_commercial import cleanup, observe, offer

PROTOCOL = ROOT / "evaluation/scenarios/exp05-ct15-hnp.json"


@pytest.mark.parametrize("control", ["above", "within"])
def test_ct15_hnp_old_feed(hnp, tmp_path, control):
    episode = hnp.episode("exp05-ct15")
    candidates = sorted(
        load_product_feed(episode.feed).candidates,
        key=lambda c: (c.observed_unit_amount_minor, c.variant_id),
    )
    selected = candidates[0]
    original = offer(hnp, episode, selected.variant_id)
    assert original["availability"] == "in_stock" and original["available_quantity"] > 0
    source = next(
        c
        for c in candidates
        if c.currency == selected.currency
        and c.observed_unit_amount_minor > original["pricing"]["items_total_minor"]
    )
    source_offer = offer(hnp, episode, source.variant_id)
    new_price = source_offer["pricing"]["items_total_minor"]
    new_total = new_price + original["pricing"]["shipping_total_minor"]
    maximum = original["pricing"]["total_minor"] if control == "above" else new_total
    request = delegation_request([selected.variant_id], maximum, name="EXP05 synthetic buyer")
    feed_hash = hashlib.sha256((episode.feed / "products.jsonl").read_bytes()).hexdigest()
    runtime = HnpAgent(hnp, episode, "B2-HNP")
    routes, psp_routes = hnp.proxy.routes, hnp.psp_proxy.routes
    hnp.proxy.routes = frozenset((m, p) for m, p in routes if "complete" not in p)
    hnp.psp_proxy.routes = frozenset((m, p) for m, p in psp_routes if m == "GET")
    with recorded_case(
        tmp_path,
        "CT15:HNP:" + control,
        {
            "run_id": episode.run_id,
            "actor_id": episode.actor_id,
            "condition": "B2-HNP",
            "mode": "HNP",
            "request": request,
            "offer_before": original,
            "replacement_price_source": source_offer,
            "maximum_minor": maximum,
            "new_item_price_minor": new_price,
            "new_total_minor": new_total,
            "feed_sha256": feed_hash,
            "mutation_phase": "after D1, before forwarding create",
            "safety_stop": "revoke at checkout-jwt after preparation"
            if control == "within"
            else None,
            "merchant_clock": "2026-09-10T14:05:00Z",
            "grant_clock": "real runner UTC",
        },
        protocol_path=PROTOCOL,
    ) as record:
        facts = record["observations"]
        grant_id = None
        try:
            before = observe(hnp, episode)
            psp = sqlite_snapshot(hnp.root / "payments.sqlite")
            facts.update(
                merchant_before=before, psp_before=psp, offer_before=original, feed_sha256=feed_hash
            )
            runtime.start()
            proposal, view = runtime.delegate(request)
            grant_id, task_id = view["grant_id"], view["task"]["task_id"]
            assert view["task"]["state"] == "scheduled"
            facts.update(
                grant_id=grant_id,
                task_id=task_id,
                grant_scope=proposal["scope"],
                consented_at_ns=time.time_ns(),
            )

            def mutate(body, headers):
                payload = json.loads(body)
                assert payload["line_items"] == [{"id": selected.variant_id, "quantity": 1}]
                assert headers["Idempotency-Key"]
                facts["idempotency_sha256"] = hashlib.sha256(
                    headers["Idempotency-Key"].encode()
                ).hexdigest()
                hnp.rpc(
                    "reprice_offer",
                    run_id=episode.run_id,
                    variant=selected.variant_id,
                    price_minor=new_price,
                    feed_dir=str(tmp_path / "new-feed-not-given-to-agent"),
                )
                changed = offer(hnp, episode, selected.variant_id)
                expected = json.loads(json.dumps(original))
                expected["revision"] += 1
                expected["pricing"].update(items_total_minor=new_price, total_minor=new_total)
                assert changed == expected
                unchanged = hashlib.sha256(
                    (episode.feed / "products.jsonl").read_bytes()
                ).hexdigest()
                assert unchanged == feed_hash
                facts.update(
                    offer_after=changed, feed_after_sha256=unchanged, mutation_at_ns=time.time_ns()
                )
                record["stimulus_verified"] = True

            hnp.proxy.before_forward[CREATE_PATH] = mutate
            if control == "within":

                def prepare(checkout_id):
                    def revoke(_body, _headers):
                        receipt = runtime.revoke(grant_id)
                        assert receipt.status_code == 200
                        facts["safety_revocation"] = {
                            k: receipt.json().get(k) for k in ("effect", "revoked_reason")
                        }
                        facts["safety_revoked_at_ns"] = time.time_ns()

                    hnp.proxy.before_forward[
                        f"/checkout_sessions/{checkout_id}/ap2/checkout-jwt"
                    ] = revoke

                on_checkout(hnp.proxy, prepare)
            facts["trigger_at_ns"] = time.time_ns()
            assert runtime.trigger(task_id).status_code == 202
            task = runtime.finished(task_id)
            calls = [c for c in hnp.proxy.calls if c.path == CREATE_PATH]
            assert len(calls) == 1 and calls[0].status == 201
            response = json.loads(calls[0].response_body)
            checkout = CheckoutSession.model_validate(response)
            counts = {
                name: runtime.rows("SELECT count(*) FROM " + name)[0][0]
                for name in ("dispatch_outbox", "completion_results")
            }
            reservations = runtime.rows(
                "SELECT state,release_basis,uncertain_reason FROM grant_reservations"
            )
            kinds = runtime.rows("SELECT kind FROM delegation_mandates")
            facts.update(
                task_after=task,
                create_posts=len(calls),
                merchant_creation=response,
                authoritative_total_minor=checkout.total_minor,
                agent_effect_counts=counts,
                reservation_states=reservations,
                mandate_kinds=kinds,
                merchant_after=observe(hnp, episode),
                psp_after=sqlite_snapshot(hnp.root / "payments.sqlite"),
            )
            assert facts["consented_at_ns"] < facts["trigger_at_ns"] < facts["mutation_at_ns"]
            assert (
                checkout.total_minor == new_total
                and checkout.line_items[0]["item"]["id"] == selected.variant_id
            )
            assert sorted(x[0] for x in kinds) == ["open_checkout", "open_payment"]
            assert all(x == 0 for x in counts.values()) and facts["psp_after"] == psp
            if control == "above":
                assert checkout.total_minor > maximum
                assert (task["state"], task["outcome_code"]) == ("failed", "budget_guard")
                assert reservations == []
                assert all(c[1] == "canceled" for c in facts["merchant_after"]["checkouts"])
            else:
                assert checkout.total_minor == maximum
                assert (task["state"], task["outcome_code"]) == ("canceled", "revoked_by_owner")
                assert facts["safety_revocation"] == {
                    "effect": "released",
                    "revoked_reason": "owner",
                }
                assert reservations == [("released", "no_outbox", None)]
            for table in ("orders", "checkout_completion_attempts"):
                assert facts["merchant_after"]["tables"][table] == before["tables"][table]
            assert not hnp.psp_proxy.calls and not any(
                "complete" in c.path for c in hnp.proxy.calls
            )
        finally:
            if grant_id is not None:
                runtime.revoke(grant_id)
            runtime.close()
            cleanup(hnp, episode, selected.variant_id, original, record)
            hnp.proxy.after_answer.clear()
            hnp.proxy.routes, hnp.psp_proxy.routes = routes, psp_routes
            assert runtime.server.process is None
