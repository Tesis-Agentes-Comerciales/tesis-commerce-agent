"""Preregistered MA24/INF30/INF31, real product services and real synthetic Merchant/PSP.

No model, completed purchase, new product behavior or fixture commercial response. The HP
entry point is PurchaseService after an explicit SKU input; conversational selection is out
of scope. Scenario attribution is performed by the EXP-01 reporter, not the P0 CI summary.
"""

import hashlib
import json
import threading
import time
from contextlib import closing
from uuid import UUID

import pytest
from pydantic import SecretStr

from commerce_agent.acp import ACPClient, load_product_feed
from commerce_agent.application import PurchaseService, PurchaseServiceError
from commerce_agent.approval import ApprovalService
from commerce_agent.governance.canonical import canonical_bytes
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.provenance import ClockReader
from commerce_agent.governance.remote_creation import LOOKUP_PATH, original_response_sha256
from commerce_agent.governance.runtime_dispatch import (
    SingleAttemptCheckoutDispatch,
    SingleAttemptPostCreateDispatch,
)
from commerce_agent.persistence import SqliteAgentStore
from tests.integration.exp01_support import (
    INF31_REASSESSMENT,
    PROTOCOL,
    recorded_case,
    sqlite_snapshot,
)
from tests.integration.live_purchase import PHONE, HnpAgent, delegation_request
from tests.integration.live_stack import (
    CREATE_PATH,
    FIXED_CLOCK,
    JointCase,
    closed,
    connect,
    wait_until,
)
from tests.integration.test_live_hnp import catalog

CONTROLS = (
    "proxy_503",
    "timeout_before_forward",
    "merchant_503",
    "timeout_after_accept",
    "definitive_stock_refusal",
    "positive",
)


def test_ma24_revocation_before_trigger(hnp, tmp_path):
    episode = hnp.episode("exp01-ma24")
    title, price = catalog(episode.feed, PHONE)
    runtime = HnpAgent(hnp, episode, "B2-HNP")
    request = delegation_request([PHONE], price + 500_000_00, name="EXP01 synthetic buyer")
    with recorded_case(
        tmp_path,
        "MA24",
        {
            "request": request,
            "offer_title": title,
            "feed_sha256": hashlib.sha256(
                (episode.feed / "products.jsonl").read_bytes()
            ).hexdigest(),
            "run_id": episode.run_id,
            "actor_id": episode.actor_id,
        },
    ) as record:
        grant_id = None
        try:
            runtime.start()
            proposal, view = runtime.delegate(request)
            grant_id, task_id = view["grant_id"], view["task"]["task_id"]
            assert view["task"]["state"] == "scheduled"
            record["observations"].update(
                grant_id=grant_id,
                task_id=task_id,
                scope_digest=proposal["scope_digest"],
                task_before=view["task"]["state"],
            )
            # Barrier: the only scheduler trigger remains unsent until revocation is durable.
            revoked = runtime.revoke(grant_id)
            assert revoked.status_code == 200
            assert revoked.json()["effect"] == "no_effect"
            assert runtime.rows("SELECT state,revoked_reason FROM delegation_grants") == [
                ("revoked", "owner")
            ]
            revoked_ns = time.time_ns()
            record["observations"]["revoked_grant"] = runtime.rows(
                "SELECT grant_id,state,revoked_reason,authority_epoch FROM delegation_grants"
            )
            before = hnp.rpc("exp01_observe", run_id=episode.run_id)
            psp = sqlite_snapshot(hnp.root / "payments.sqlite")
            posts = hnp.proxy.requests()
            payment_calls = hnp.psp_proxy.requests()
            triggered_ns = time.time_ns()
            reply = runtime.trigger(task_id)
            after = hnp.rpc("exp01_observe", run_id=episode.run_id)
            record["observations"].update(
                revoked_at_ns=revoked_ns,
                triggered_at_ns=triggered_ns,
                trigger_status=reply.status_code,
                merchant_before=before,
                merchant_after=after,
                psp_before=psp,
                psp_after=sqlite_snapshot(hnp.root / "payments.sqlite"),
            )
            record["stimulus_verified"] = triggered_ns > revoked_ns
            assert triggered_ns > revoked_ns and reply.status_code == 409
            assert reply.json()["code"] == "hnp_task_not_runnable"
            task_after = runtime.task(task_id)
            record["observations"]["task_after"] = {
                key: task_after.get(key) for key in ("state", "outcome_code")
            }
            assert task_after["state"] == "canceled"
            counts = {}
            for table in (
                "dispatch_outbox",
                "grant_reservations",
                "completion_results",
                "purchases",
            ):
                counts[table] = runtime.rows("SELECT count(*) FROM " + table)[0][0]
                assert counts[table] == 0
            record["observations"]["agent_effect_counts"] = counts
            assert before == after
            assert sqlite_snapshot(hnp.root / "payments.sqlite") == psp
            assert hnp.proxy.requests() == posts
            assert hnp.psp_proxy.requests() == payment_calls
            assert not after["checkouts"]
        finally:
            if grant_id is not None:
                runtime.revoke(grant_id)
            runtime.close()
            record["cleanup_verified"] = runtime.server.process is None


def test_inf30_lost_creation_response(b0, tmp_path):
    hp_control(b0, tmp_path, "INF30", "drop")


@pytest.mark.parametrize("control", CONTROLS)
def test_inf31_preparation_faults(b0, tmp_path, control):
    hp_control(b0, tmp_path, "INF31:" + control, control)


def hp_control(lane, path, case_id, control, *, protocol_path=PROTOCOL):
    reassessment = protocol_path == INF31_REASSESSMENT
    case = JointCase.open(lane.stack, "exp01")
    run_id, offer = case.episode["run_id"], case.episode["offer"]
    case.origin = lane.origin
    feed_dir = path / "feed"
    lane.rpc("exp01_export", run_id=case.episode["run_id"], feed_dir=str(feed_dir))
    feed = load_product_feed(feed_dir)
    assert any(c.variant_id == case.episode["offer"]["id"] for c in feed.candidates)
    old_routes = lane.proxy.routes
    lane.proxy.routes |= {("POST", LOOKUP_PATH), ("POST", "/checkout_sessions/*/cancel")}
    release, entered = threading.Event(), threading.Event()
    postgres_stopped = False
    postgres = lane.stack.postgres

    def postgres_running():
        state = postgres.docker("inspect", "-f", "{{.State.Running}}", postgres.name)
        assert state in {"true", "false"}
        return state == "true"

    with recorded_case(
        path,
        case_id,
        {
            "run_id": case.episode["run_id"],
            "offer": case.episode["offer"],
            "selected_input_sku": case.episode["offer"]["id"],
            "quantity": 1,
            "feed_sha256": hashlib.sha256((feed_dir / "products.jsonl").read_bytes()).hexdigest(),
            "clock": FIXED_CLOCK.model_dump(mode="json"),
            "fault": control,
        },
        protocol_path=protocol_path,
    ) as record:
        gateway = ACPClient(
            lane.origin,
            SecretStr(case.credential["bearer"]),
            timeout_seconds=15
            if control == "merchant_503"
            else 0.3
            if control in {"timeout_before_forward", "timeout_after_accept"}
            else 5,
        )
        original_stock = psp_before = None
        try:
            if reassessment:
                original_stock = lane.rpc("exp01_observe", run_id=case.episode["run_id"])["stock"]
            if control == "definitive_stock_refusal":
                if reassessment:
                    lane.rpc("edit_offer", run_id=run_id, offer_id=offer["id"], change="stock")
                    changed = lane.rpc("observe_offer", run_id=run_id, offer_id=offer["id"])
                    record["observations"]["offer_at_dispatch"] = changed
                    assert changed == {
                        **offer,
                        "revision": offer["revision"] + 1,
                        "available_quantity": 0,
                        "availability": "out_of_stock",
                    }
                else:
                    lane.rpc("alter_offer_fixture", run_id=run_id, variant=offer["id"])
            before = lane.rpc("exp01_observe", run_id=case.episode["run_id"])
            psp_before = sqlite_snapshot(lane.root / "payments.sqlite")
            record["observations"].update(merchant_before=before, psp_before=psp_before)

            def before_forward(body, headers):
                nonlocal postgres_stopped
                case.recover_envelope()
                assert canonical_bytes(json.loads(body)) == case.envelope.body_canonical.encode()
                assert (
                    headers["Idempotency-Key"] == case.envelope.idempotency_key.get_secret_value()
                )
                if control == "timeout_before_forward":
                    entered.set()
                    assert release.wait(10)
                if reassessment and control == "merchant_503":
                    record["observations"]["postgres_before"] = postgres_running()
                    assert record["observations"]["postgres_before"] is True
                    postgres_stopped = True
                    postgres.stop()
                    record["observations"]["postgres_before_forward"] = postgres_running()
                    assert record["observations"]["postgres_before_forward"] is False
                    assert closed(postgres.port)

            lane.proxy.before_forward[CREATE_PATH] = before_forward
            if control in {"drop", "timeout_after_accept"}:
                lane.proxy.faults[CREATE_PATH] = "drop" if control == "drop" else "delay"
                lane.proxy.delay_seconds = 1.0
            if control in {"proxy_503", "timeout_before_forward"}:
                lane.proxy.faults[CREATE_PATH] = "status_503"
            if control == "merchant_503" and not reassessment:
                postgres.stop()
                postgres_stopped = True
            observed = start_product(case, gateway, feed)
            release.set()
            if postgres_stopped:
                if reassessment:
                    record["observations"]["postgres_during_response"] = postgres_running()
                    assert record["observations"]["postgres_during_response"] is False
                postgres.restart()
                postgres_stopped = False
                if reassessment:
                    record["observations"]["postgres_restored"] = postgres_running()
                    assert record["observations"]["postgres_restored"] is True
            case.recover_envelope()
            create_calls = [c for c in lane.proxy.calls if c.path == CREATE_PATH]
            assert len(create_calls) == 1
            call = create_calls[0]
            accepted = control in {"drop", "timeout_after_accept", "positive"}
            if control not in {"proxy_503", "timeout_before_forward"}:
                assert call.answered.wait(10)
            if control == "timeout_before_forward":
                assert entered.is_set()
            after = lane.rpc("exp01_observe", run_id=case.episode["run_id"])
            with closing(connect(case.path)) as db:
                state = db.execute(
                    "SELECT stage,checkout_id,failure_code FROM purchases"
                ).fetchall()
                operation = db.execute("SELECT state FROM d1_checkout_results").fetchall()
            record["observations"].update(
                agent_result=observed,
                agent_state=state,
                operation_state=operation,
                merchant_after=after,
                upstream_answered=call.answered.is_set(),
                merchant_http_status=call.status,
                idempotency_sha256=case.key_sha256(),
            )
            # Establish the injected stimulus before attributing an assertion to the Agent.
            if accepted:
                assert call.status == 201
            elif control == "merchant_503":
                assert call.status == 503
                if reassessment:
                    assert call.fault == "pass"
                    assert call.response_body is not None and call.response_request_id is not None
                    code = json.loads(call.response_body)["detail"]["code"]
                    assert code == "lab_session_store_unavailable"
                    assert UUID(call.response_request_id).version == 4
                    record["observations"].update(
                        merchant_error_code=code, merchant_request_id=call.response_request_id
                    )
            elif control in {"proxy_503", "timeout_before_forward"}:

                def local_response():
                    assert call.status == 503

                wait_until(local_response, "local non-forwarded response")
                assert not call.answered.is_set()
            else:
                assert call.status == 409
                if reassessment:
                    assert call.response_body is not None
                    assert json.loads(call.response_body)["detail"]["code"] == "out_of_stock"
            assert not lane.proxy.errors
            record["stimulus_verified"] = True
            if reassessment:
                record["observations"].update(
                    proxy_fault=call.fault, merchant_http_status=call.status
                )
                if control == "merchant_503":
                    assert state[0][1] is None
                    assert after == before
            if control == "positive":
                assert observed == "awaiting_confirmation"
                assert state[0][0] == "awaiting_confirmation"
            elif control == "definitive_stock_refusal":
                assert (observed, state[0][0]) == ("out_of_stock", "failed")
            else:
                assert (observed, state[0][0]) == ("d1_dispatch_uncertain", "uncertain")
                assert operation == [("uncertain",)]
                # Reopen product services, not a fixture gateway: a caller's retry must not resend.
                assert start_product(case, gateway, feed) == "d1_recovery_required"
                recovered = case.verify_zero(expected="found_verified" if accepted else "not_found")
                record["observations"]["recovery"] = recovered.verification.model_dump(mode="json")
                if accepted:
                    original = json.loads(call.response_body)
                    assert (
                        recovered.verification.checkout_id
                        == original["id"]
                        == after["checkouts"][0][0]
                    )
                    assert (
                        recovered.verification.original_response_digest
                        == "sha256:" + original_response_sha256(original)
                    )
                else:
                    assert recovered.verification.reason == "NOT_FOUND_NOT_ABSENCE"
                network_before = len(lane.proxy.calls)
                restarted = case.probe()
                assert restarted is not None and restarted["count"] == 1
                assert len(lane.proxy.calls) == network_before
            assert len(after["checkouts"]) == int(accepted)
            if control == "merchant_503":
                assert call.status == 503
            if accepted:
                assert call.status == 201
                assert after["checkouts"][0][4] == case.key_sha256()
            assert lane.proxy.count("POST", CREATE_PATH) == 1
            record["observations"].update(
                create_posts=lane.proxy.count("POST", CREATE_PATH),
                psp_after=sqlite_snapshot(lane.root / "payments.sqlite"),
            )
            for table in ("orders", "checkout_completion_attempts"):
                assert after["tables"][table] == before["tables"][table]
            assert sqlite_snapshot(lane.root / "payments.sqlite") == psp_before
            assert after["stock"] == before["stock"]
            assert not any("complete" in c.path for c in lane.proxy.calls)
            if reassessment:
                with closing(connect(case.path)) as db:
                    effects = {
                        table: db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                        for table in ("approvals", "orders", "completion_results")
                    }
                record["observations"]["agent_effect_counts"] = effects
                assert all(count == 0 for count in effects.values())
        finally:
            release.set()
            if postgres_stopped:
                postgres.restart()
            lane.proxy.faults.clear()
            lane.proxy.before_forward.clear()
            # Observe Merchant directly: uncertain creation may have no local checkout id.
            cleanup_before = lane.rpc("exp01_observe", run_id=case.episode["run_id"])
            for checkout in cleanup_before["checkouts"]:
                canceled = lane.rpc(
                    "cancel_fixture",
                    bearer=case.credential["bearer"],
                    checkout_id=checkout[0],
                    key="exp01-cleanup-" + case.index,
                )
                assert canceled["status"] == "canceled"
            if control == "definitive_stock_refusal":
                lane.rpc(
                    "edit_offer",
                    run_id=case.episode["run_id"],
                    offer_id=case.episode["offer"]["id"],
                    change="restore",
                    snapshot=case.episode["offer"],
                )
                if reassessment:
                    restored = lane.rpc("observe_offer", run_id=run_id, offer_id=offer["id"])
                    record["observations"]["offer_restored"] = restored
                    assert restored == offer
            final = lane.rpc("exp01_observe", run_id=case.episode["run_id"])
            record["observations"]["after_cleanup"] = final
            record["observations"]["psp_after_cleanup"] = sqlite_snapshot(
                lane.root / "payments.sqlite"
            )
            if reassessment and original_stock is not None:
                assert final["stock"] == original_stock
            if reassessment and psp_before is not None:
                assert record["observations"]["psp_after_cleanup"] == psp_before
            record["cleanup_verified"] = all(c[1] == "canceled" for c in final["checkouts"])
            assert record["cleanup_verified"]
            gateway.close()
            lane.proxy.routes = old_routes


def start_product(case, gateway, feed):
    store = SqliteAgentStore(case.path)
    store.initialize()
    gate = SqliteD1EnforceGate(
        case.path,
        case.configuration("exp01-hp-enforce"),
        case.keys,
        ClockReader(FIXED_CLOCK),
        dispatch=SingleAttemptCheckoutDispatch(),
        post_create_dispatch=SingleAttemptPostCreateDispatch(),
        expected_remote_binding=case.expected_binding(),
    )
    service = PurchaseService(
        feed=feed,
        checkout=gateway,
        store=store,
        approvals=ApprovalService(store, SecretStr("exp01-synthetic-approval-not-used")),
        d1_gate=gate,
    )
    try:
        case.start(service, "Prepare this observed SKU; do not approve or pay")
    except PurchaseServiceError as error:
        return error.code
    return "awaiting_confirmation"
