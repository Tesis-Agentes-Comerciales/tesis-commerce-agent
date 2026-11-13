"""P3 point 5: composition of real checkout witnesses, no UX03 episodes."""

import hashlib
import os
import secrets
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.models import ACP_VERSION, CheckoutSession
from commerce_agent.api import create_app
from commerce_agent.config import Settings
from commerce_agent.domain import PurchaseIntent
from commerce_evaluation.agent_condition import D1RunKeys
from commerce_evaluation.auth_client import AuthClient
from commerce_evaluation.demo_stack import MODES, mode_environment
from commerce_evaluation.p3_protocol import (
    CheckoutWitness,
    Creation,
    ProtocolBlocked,
    ProtocolGate,
    digest,
    equivalent_clones,
    verified_cleanup,
)
from tests.integration.live_browser import AUTH, NodeServer
from tests.integration.live_purchase import (
    SANDBOX_CLOCK,
    StaticConversationRouter,
    buyer_details,
)
from tests.integration.live_stack import FaultProxy, clean_environment, free_port
from tests.integration.test_live_p3_offer_checkout import (
    CONTEXT,
    CREATE,
    checkout_update,
    cleanup,
    client,
    exercise,
    prepare,
    psp_state,
    state,
    summary,
)
from tests.integration.test_live_p3_offer_checkout import p3_lane as contract_lane

ORACLE = Path("evaluation/scenarios/p0/ux03-oracle.json")
ROLES = ("A-cal", "B-cal", "V", "E")
p3_lane = contract_lane


def scoped(snapshot):
    return {k: value for k, value in snapshot.items() if not k.startswith("global_")}


def feed_digest(path):
    return hashlib.sha256((path / "products.jsonl").read_bytes()).hexdigest()


def candidates(snapshot):
    eligible = [
        offer
        for offer in snapshot["offers"]
        if offer["product_status"] == "active"
        and offer["availability"] == "in_stock"
        and offer["available_quantity"] > 0
    ]
    return sorted(eligible, key=lambda offer: offer["id"])


def headers(episode):
    return {
        "Authorization": "Bearer " + episode.bearer.get_secret_value(),
        "API-Version": ACP_VERSION,
        "Request-Id": str(uuid4()),
        "Idempotency-Key": str(uuid4()),
    }


@contextmanager
def protocol_case(lane, case_id):
    assert not any(name.startswith("OPENAI_") for name in clean_environment())
    assert not os.environ.get("OPENAI_API_KEY")
    lane.proxy.reset()
    episodes = []
    facts: dict[str, Any] = {"cleanup_verified": False, "commercial": "unknown"}
    initial_psp = psp_state(lane)
    initial_effects = lane.rpc("snapshot")
    status = "failed"
    try:
        yield episodes, facts
        status = "passed"
    except BaseException as exc:
        facts["execution_error"] = type(exc).__name__
        raise
    finally:
        lane.proxy.faults.clear()
        try:
            facts["after"] = {ep.run_id: summary(cleanup(lane, ep)) for ep in episodes}
            assert episodes
            last = state(lane, episodes[-1])
            assert all(row["status"] == "canceled" for row in last["global_checkouts"])
            assert psp_state(lane) == initial_psp
            final_effects = lane.rpc("snapshot")
            assert final_effects["checkout_sessions"]["count"] == (
                initial_effects["checkout_sessions"]["count"]
                + sum(len(s["checkouts"]) for s in facts["after"].values())
            )
            allowed = {
                "experiment_runs",
                "catalog_offers",
                "run_events",
                "lab_sessions",
                "request_observations",
                "checkout_sessions",
                "checkout_mutations",
            }
            assert initial_effects.keys() == final_effects.keys()
            for table in initial_effects.keys() - allowed:
                assert initial_effects[table] == final_effects[table], table
            assert not lane.proxy.errors
            assert not any(call.fault == "blocked" for call in lane.proxy.calls)
            assert not any("complete" in call.path for call in lane.proxy.calls)
            facts.update(psp_before=initial_psp, psp_after=psp_state(lane), cleanup_verified=True)
        except BaseException as exc:
            facts["cleanup_error"] = type(exc).__name__
            status = "failed"
            raise
        finally:
            facts["trace"] = [
                dict(
                    method=c.method,
                    path=c.path,
                    status=c.status,
                    fault=c.fault,
                    request_sha256=hashlib.sha256(c.request_body).hexdigest(),
                    response_sha256=hashlib.sha256(c.response_body or b"").hexdigest(),
                )
                for c in lane.proxy.calls
            ]
            lane.stack.record("P3-PV-case", case_id=case_id, status=status, **facts)


def create_witness(lane, episode, raw, attempts, facts):
    with client(lane, episode) as acp:
        offer = acp.get_offer(raw["id"], CONTEXT)
    assert offer.model_dump(mode="json") == raw
    key = str(uuid4())
    checkout = prepare(lane, episode, offer, facts, creation_key=key)
    saved = next(c for c in state(lane, episode)["checkouts"] if c["id"] == checkout.id)
    witness = CheckoutWitness(
        checkout.model_dump(mode="json"),
        saved | {"run_id": episode.run_id, "actor_id": episode.actor_id},
        raw,
        episode.run_id,
        episode.actor_id,
    )
    witness.total()
    attempts.append(
        Creation(
            episode.run_id,
            hashlib.sha256(key.encode()).hexdigest(),
            "accepted",
            checkout.id,
            "response",
        )
    )
    return witness


def cleanup_ledger(lane, episodes):
    readbacks, persisted = {}, {}
    for ep in episodes:
        final = cleanup(lane, ep)
        with client(lane, ep) as acp:
            for checkout in final["checkouts"]:
                cid = checkout["id"]
                readbacks[cid] = acp.get_checkout(cid).status
                persisted[cid] = (ep.run_id, checkout["status"])
    return readbacks, persisted


def clone_inputs(episodes, snapshots, directory) -> dict[str, dict[str, Any]]:
    return {
        role: dict(
            run_id=ep.run_id,
            actor_id=ep.actor_id,
            session_alias=hashlib.sha256(ep.bearer.get_secret_value().encode()).hexdigest(),
            agent_store=str(directory / f"{role}.sqlite"),
            thread=f"{role}:{ep.actor_id}",
            operation_scope=f"{role}:{ep.run_id}",
            offers=snapshots[role]["offers"],
            feed_sha256=feed_digest(ep.feed),
            clock=snapshots[role]["clock_at"],
            configuration={
                "profile": "acp",
                "quantity": 1,
                "context": CONTEXT.model_dump(mode="json"),
            },
        )
        for role, ep in episodes.items()
    }


def test_protocol_contract_pv05_06_08(p3_lane, tmp_path):
    lane = p3_lane
    with protocol_case(lane, "PV-05-06-08") as (episodes, facts):
        by_role = {role: lane.episode(role) for role in ROLES}
        episodes.extend(by_role.values())
        before = {role: state(lane, ep) for role, ep in by_role.items()}
        assert all(s["matches_full_fixture"] and not s["checkouts"] for s in before.values())
        clones = clone_inputs(by_role, before, tmp_path)
        equivalent_clones(clones)
        eligible = candidates(before["A-cal"])
        a = next(o for o in eligible if o["pricing"]["shipping_total_minor"] == 0)
        b = next(o for o in eligible if o["pricing"]["shipping_total_minor"] > 0)
        attempts: list[Creation] = []
        pair = {"A": a, "B": b}
        calibration = {
            branch: create_witness(lane, by_role[f"{branch}-cal"], raw, attempts, {})
            for branch, raw in pair.items()
        }
        reads, persisted = cleanup_ledger(lane, [by_role["A-cal"], by_role["B-cal"]])
        seal = {
            "totals": {branch: w.total() for branch, w in calibration.items()},
            "witnesses": {branch: digest(vars(w)) for branch, w in calibration.items()},
            "ledger": verified_cleanup(attempts, reads, persisted),
        }
        lane.stack.record("P3-PV-calibration", evidence=seal, sha256=digest(seal))
        assert scoped(state(lane, by_role["V"])) == scoped(before["V"])
        captured = {
            branch: (calibration[branch], create_witness(lane, by_role["V"], raw, attempts, {}))
            for branch, raw in pair.items()
        }
        gate = ProtocolGate(ORACLE.read_bytes())
        gate.calibrate(captured)
        facts["totals"] = {branch: [w.total() for w in pair] for branch, pair in captured.items()}
        facts["checkout_witnesses"] = {
            branch: [
                {
                    "run_id": w.run_id,
                    "actor_id": w.actor_id,
                    "offer": w.offer,
                    "checkout": w.wire["id"],
                    "wire_sha256": digest(w.wire),
                }
                for w in pair
            ]
            for branch, pair in captured.items()
        }

        # The unrelated actor may not read or mutate the real canceled calibration session.
        cal, v = by_role["A-cal"], by_role["V"]
        with pytest.raises(AssertionError, match="ValueError"):
            lane.rpc("session", run_id=cal.run_id, actor_id=v.actor_id)
        original = state(lane, cal)["checkouts"]
        path = f"/checkout_sessions/{captured['A'][0].wire['id']}"
        refusals = []
        for method, suffix in (("GET", ""), ("POST", ""), ("POST", "/cancel")):
            response = httpx.request(
                method,
                lane.origin + path + suffix,
                headers=headers(v),
                json={} if method == "POST" else None,
                trust_env=False,
            )
            refusals.append(response.status_code)
            assert response.status_code in {403, 404}
        assert state(lane, cal)["checkouts"] == original
        facts["foreign_checkout_http"] = refusals
        facts["foreign_actor_session_rejected"] = True
        reads, persisted = cleanup_ledger(lane, list(by_role.values()))
        gate.cleanup(attempts, reads, persisted)
        e_before = scoped(before["E"])
        e_after = scoped(state(lane, by_role["E"]))
        gate.compare(clones, e_before, e_after)
        assert not any(Path(clone["agent_store"]).exists() for clone in clones.values())
        facts.update(
            attempts=[asdict(row) for row in attempts],
            canceled_readbacks=reads,
            persisted=persisted,
            initial={role: summary(s) for role, s in before.items()},
            clone_inputs={
                role: {key: value for key, value in clone.items() if key != "offers"}
                for role, clone in clones.items()
            },
            e_unchanged=digest(e_before) == digest(e_after),
            variants=len(before["E"]["offers"]),
        )

        # Each negative changes only a separate validation clone. E never receives a turn.
        changed = {}
        for kind in ("shipping", "revision", "stock", "clock"):
            negative = lane.episode(f"drift-{kind}")
            episodes.append(negative)
            replacement = by_role | {"V": negative}
            snapshots = before | {"V": state(lane, negative)}
            equivalent_clones(clone_inputs(replacement, snapshots, tmp_path))
            reexport = tmp_path / f"feed-{kind}"
            lane.rpc(
                "p3_mutate",
                run_id=negative.run_id,
                offer_id=a["id"],
                kind=kind,
                offset=0,
                feed_dir=str(reexport),
            )
            snapshots["V"] = state(lane, negative)
            stimulus = clone_inputs(replacement, snapshots, tmp_path)
            if kind == "shipping":
                assert feed_digest(reexport) == stimulus["V"]["feed_sha256"]
            with pytest.raises(ProtocolBlocked, match="Non-equivalent"):
                equivalent_clones(stimulus)
            assert not snapshots["V"]["checkouts"]
            changed[kind] = summary(snapshots["V"])
        facts["blocked_drift"] = changed
        # Reuse the existing P2 worker and keep the administrative catalog outside the clones.
        admin_feed = str(tmp_path / "managed-control-export")
        provenance = lane.rpc("p0_provenance", run_id=by_role["E"].run_id, feed_dir=admin_feed)
        assert provenance["managed_run_id"] not in {ep.run_id for ep in episodes}
        managed = next(o for o in provenance["managed_offers"] if o["id"] == a["id"])
        assert managed["available_quantity"] > 0
        try:
            edited = lane.rpc("p0_managed_update", offer_id=a["id"], stock=0)
            assert edited["available_quantity"] == 0 and edited["revision"] > managed["revision"]
            assert scoped(state(lane, by_role["E"])) == e_before
        finally:
            restored = lane.rpc(
                "p0_managed_update", offer_id=a["id"], stock=managed["available_quantity"]
            )
        assert restored["available_quantity"] == managed["available_quantity"]
        assert restored["pricing"] == managed["pricing"]
        assert restored["revision"] > managed["revision"]
        facts["managed_control"] = {
            "run_id": provenance["managed_run_id"],
            "offer_id": a["id"],
            "stock": [
                managed["available_quantity"],
                edited["available_quantity"],
                restored["available_quantity"],
            ],
            "revisions": [managed["revision"], edited["revision"], restored["revision"]],
        }
        assert scoped(state(lane, by_role["E"])) == e_before
        assert feed_digest(by_role["E"].feed) == clones["E"]["feed_sha256"]
        for role in ("A-cal", "B-cal", "V"):
            assert state(lane, by_role[role])["offers"] == before[role]["offers"]


def test_protocol_contract_pv07(p3_lane):
    lane = p3_lane
    with protocol_case(lane, "PV-07") as (episodes, facts):
        episode = lane.episode("protocol-ledger")
        episodes.append(episode)
        raw = candidates(state(lane, episode))[0]
        # Reuse #160's verified lookup (run, actor, key digest, request/response fingerprints).
        reused: dict[str, Any] = {}
        exercise(lane, episode, episode, raw, "lost-create", reused)
        proof = reused["reconciliation"]
        cid = proof["checkout_id"]
        attempts = [
            Creation(episode.run_id, proof["idempotency_key_sha256"], "lost", cid, "lookup")
        ]
        update_key = str(uuid4())
        update = checkout_update(CheckoutSession.model_validate(proof["original_response"]))
        with client(lane, episode) as acp:
            first = acp.update_checkout(cid, update, idempotency_key=update_key)
            after_update = state(lane, episode)
            assert acp.update_checkout(cid, update, idempotency_key=update_key) == first
            conflict = update.model_copy(update={"buyer": buyer_details("other-buyer").buyer})
            with pytest.raises(ACPClientError) as refused:
                acp.update_checkout(cid, conflict, idempotency_key=update_key)
            assert refused.value.status_code == 422
            assert acp.get_checkout(cid) == first
        after_replay = state(lane, episode)
        assert after_replay["checkouts"] == after_update["checkouts"]
        assert (
            after_replay["tables"]["checkout_mutations"]
            == after_update["tables"]["checkout_mutations"]
        )
        path = f"/checkout_sessions/{cid}/cancel"
        key = str(uuid4())
        lane.proxy.faults[path] = "drop"
        with client(lane, episode) as acp, pytest.raises(ACPClientError):
            acp.cancel_checkout(cid, idempotency_key=key)
        assert lane.proxy.calls[-1].answered.is_set()
        lane.proxy.faults.clear()
        with client(lane, episode) as acp:
            assert acp.cancel_checkout(cid, idempotency_key=key).status == "canceled"
        reads, persisted = cleanup_ledger(lane, episodes)
        assert lane.proxy.count("POST", CREATE) == 1
        assert lane.proxy.count("POST", path) == 2
        facts.update(
            ledger_sha256=verified_cleanup(attempts, reads, persisted),
            attempts=[asdict(row) for row in attempts],
            reconciliation_sha256=digest(proof),
            canceled_readbacks=reads,
            persisted=persisted,
            cancel_key_sha256=hashlib.sha256(key.encode()).hexdigest(),
            update_key_sha256=hashlib.sha256(update_key.encode()).hexdigest(),
            update_replay_same_checkout=True,
            update_conflict_http=refused.value.status_code,
        )


def test_protocol_contract_pv10(p3_lane, tmp_path, monkeypatch):
    lane = p3_lane
    with protocol_case(lane, "PV-10") as (episodes, facts):
        episode = lane.episode("protocol-hp-auth")
        episodes.append(episode)
        raw = candidates(state(lane, episode))[0]
        port = free_port()
        auth = NodeServer(
            lane.root,
            "p3-protocol-auth",
            AUTH,
            ("server.js",),
            clean_environment()
            | {
                "BETTER_AUTH_URL": f"http://127.0.0.1:{port}",
                "BETTER_AUTH_SECRET": secrets.token_urlsafe(32),
                "AUTH_DATABASE_PATH": str(tmp_path / "auth.sqlite"),
                "PORT": str(port),
            },
            port,
            "/api/auth/jwks",
        )
        psp_guard = FaultProxy(frozenset())
        psp_guard.upstream = lane.payment_origin
        original_routes = lane.proxy.routes
        # The Agent uses discovery before preparing. The direct V01 clients did not need it.
        lane.proxy.routes |= {("GET", "/.well-known/acp.json")}
        try:
            auth.start()
            buyers = [AuthClient(auth.origin), AuthClient(auth.origin)]
            owners = [buyer.signup() for buyer in buyers]
            assert owners[0] != owners[1]
            bearer = [{"Authorization": "Bearer " + buyer.token()} for buyer in buyers]
            for name, value in mode_environment(
                MODES["b2"], lane.origin, D1RunKeys.generate()
            ).items():
                monkeypatch.setenv(name, value)
            settings = Settings(
                merchant_base_url=lane.origin,
                merchant_bearer_token=episode.bearer,
                merchant_webhook_secret=lane.webhook_secret,
                agent_approval_secret=lane.approval_secret,
                payment_provider_url=psp_guard.origin,
                payment_agent_bearer_token=lane.payment_token,
                agent_database_path=tmp_path / "hp.sqlite",
                agent_feed_source="file",
                agent_feed_path=episode.feed,
                agent_sandbox_clock_at=datetime.fromisoformat(SANDBOX_CLOCK),
                agent_auth_mode="jwt",
                agent_auth_issuer=auth.origin,
                agent_ap2_required=False,
                agent_hnp_enabled=False,
                openai_api_key=None,
                openai_model=None,
            )
            assert settings.agent_governance_mode == "enforce"
            intent = PurchaseIntent.model_validate(
                {
                    "hard": {
                        "currency": "COP",
                        "categories": [raw["category"]],
                        "max_total_amount_minor": raw["pricing"]["total_minor"],
                        "required_terms": [raw["product_title"]],
                    }
                }
            )
            request = "Preparar una compra simple para comprobar la autorización HP."
            app = create_app(
                settings=settings, conversation_router=StaticConversationRouter(request, intent)
            )
            with TestClient(app) as agent:
                turn = agent.post(
                    "/conversation/turn",
                    headers=bearer[0],
                    json={
                        "turn_id": "protocol-hp",
                        "messages": [{"role": "user", "content": request}],
                        "details": buyer_details("protocol-hp").model_dump(
                            mode="json", exclude_none=True
                        ),
                    },
                )
                facts["turn_http"] = turn.status_code
                assert turn.status_code == 200 and turn.json()["action"] == "purchase"
                purchase = turn.json()["purchase"]
                complete = f"/purchases/{purchase['run_id']}/complete"
                refusals = [agent.post(complete, headers=h).status_code for h in [*bearer, {}]]
                facts["complete_http"] = refusals
                assert refusals == [409, 404, 401]
                assert lane.proxy.count("POST", CREATE) == 1
                assert not psp_guard.calls
                assert not any("complete" in call.path for call in lane.proxy.calls)
                facts.update(
                    auth="JWT/JWKS",
                    governance="enforce",
                    scripted_router=True,
                    checkout_id=purchase["checkout"]["id"],
                    psp_attempts=0,
                )
            with closing(sqlite3.connect(settings.agent_database_path)) as db:
                assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
            restarted = settings.model_copy(
                update={"agent_database_path": tmp_path / "hp-independent.sqlite"}
            )
            with TestClient(create_app(settings=restarted)) as isolated:
                assert isolated.post(complete, headers=bearer[0]).status_code == 404
            facts["fresh_agent_store_denied_previous_run"] = True
        finally:
            lane.proxy.routes = original_routes
            try:
                auth.stop()
            finally:
                psp_guard.close()
