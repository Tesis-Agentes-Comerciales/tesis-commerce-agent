"""P3-V01: pinned real Offer -> ACP contract, never a UX03 or LLM experiment."""

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from commerce_agent.acp import ACPClient
from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.creation import build_checkout_create
from commerce_agent.acp.models import (
    ACP_VERSION,
    CheckoutSession,
    CheckoutUpdateRequest,
    SelectedFulfillmentOption,
)
from commerce_agent.acp.offer_snapshot import DeliveryContext, MerchantOffer
from commerce_agent.governance.dispatch_envelope import merchant_creation_digest
from commerce_agent.governance.remote_creation import (
    LOOKUP_PATH,
    LOOKUP_SCHEMA,
    CreationFound,
    original_response_sha256,
)
from tests.integration.live_purchase import (
    Episode,
    PurchaseLane,
    buyer_details,
    payment_requirements,
)
from tests.integration.live_stack import AGENT, LiveStack, missing_requirements
from tests.support.p3_contract import AGENT_BASE, CASES, MERCHANT, PSP, condition

READ = "/lab/offers/get"
CREATE = "/checkout_sessions"
CONTEXT = DeliveryContext(country="CO", city="Bogota", postal_code="110111")


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture(scope="module")
def p3_lane(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PurchaseLane]:
    if problems := missing_requirements() + payment_requirements():
        pytest.fail("; ".join(problems), pytrace=False)
    pins = {}
    for variable, sha in (("MERCHANT_REPO", MERCHANT), ("PAYMENT_REPO", PSP)):
        root = Path(os.environ[variable])
        assert git(root, "rev-parse", "HEAD") == sha, "Exact sibling pin required"
        assert not git(root, "status", "--porcelain=v1"), "Clean sibling required"
        pins[variable] = {
            "sha": sha,
            "lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
        }
    assert not git(AGENT, "diff", AGENT_BASE, "--", "src/commerce_agent"), "Product must stay fixed"
    stack = LiveStack(tmp_path_factory.mktemp("p3-v01"))
    lane = None
    try:
        stack.start()
        lane = PurchaseLane(stack, "acp")
        # This validation cannot send completion/payment requests, even accidentally.
        lane.proxy.routes = frozenset(
            {
                ("POST", READ),
                ("POST", CREATE),
                ("GET", "/checkout_sessions/*"),
                ("POST", "/checkout_sessions/*"),
                ("POST", "/checkout_sessions/*/cancel"),
                ("POST", LOOKUP_PATH),
            }
        )
        lane.start()
        stack.record(
            "P3-V01-manifest",
            pins=pins,
            agent_base=AGENT_BASE,
            agent_sha=git(AGENT, "rev-parse", "HEAD"),
            product_tree=git(AGENT, "rev-parse", "HEAD:src/commerce_agent"),
            base_product_tree=git(AGENT, "rev-parse", AGENT_BASE + ":src/commerce_agent"),
            required_cases=CASES,
            routes=sorted(lane.proxy.routes),
            llm=False,
            payments=False,
        )
        yield lane
    finally:
        if lane is not None:
            lane.close()
        stack.close()


def state(lane: PurchaseLane, ep: Episode) -> dict[str, Any]:
    return lane.rpc("p3_state", run_id=ep.run_id)


def client(
    lane: PurchaseLane, ep: Episode, *, token: SecretStr | None = None, timeout: float = 5
) -> ACPClient:
    return ACPClient(lane.origin, token or ep.bearer, timeout_seconds=timeout)


def psp_state(lane: PurchaseLane) -> dict[str, Any]:
    with closing(sqlite3.connect(f"file:{lane.root / 'payments.sqlite'}?mode=ro", uri=True)) as db:
        names = [
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        return {
            name: {"count": len(rows), "sha256": digest(rows)}
            for name in names
            for rows in [
                db.execute(
                    'SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid'
                ).fetchall()
            ]
        }


def summary(s: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in s.items() if k != "offers"} | {
        "catalog_sha256": digest(s["offers"]),
        "variants": len(s["offers"]),
    }


def read_events(s: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in s["events"] if e["type"] == "offer.read"]


def verify_checkout(lane: PurchaseLane, ep: Episode, offer: MerchantOffer, checkout) -> None:
    assert checkout.currency == offer.pricing.currency
    assert checkout.total_minor == offer.pricing.total_minor
    totals = {row["type"]: row["amount"] for row in checkout.totals}
    assert totals["fulfillment"] == offer.pricing.shipping_total_minor
    assert checkout.expires_at == offer.expires_at
    assert len(checkout.line_items) == 1 and checkout.line_items[0]["quantity"] == 1
    internal = next(c for c in state(lane, ep)["checkouts"] if c["id"] == checkout.id)
    assert internal["offer_id"] == offer.id and internal["quantity"] == 1
    assert internal["offer_revision"] == offer.revision
    assert internal["pricing"] == offer.pricing.model_dump(mode="json")
    assert internal["delivery_context"] == offer.delivery_context.model_dump(mode="json")


def prepare(
    lane: PurchaseLane,
    ep: Episode,
    offer: MerchantOffer,
    facts: dict[str, Any],
    *,
    creation_key: str | None = None,
):
    with client(lane, ep) as gateway:
        checkout = gateway.create_checkout(
            offer.id, currency="cop", idempotency_key=creation_key or str(uuid4())
        )
        verify_checkout(lane, ep, offer, checkout)
        details = buyer_details("p3")
        options = checkout.model_extra["fulfillment_options"] if checkout.model_extra else []
        assert len(options) == 1
        update = CheckoutUpdateRequest(
            buyer=details.buyer,
            fulfillment_details=details.fulfillment,
            selected_fulfillment_options=[
                SelectedFulfillmentOption(
                    type="shipping",
                    option_id=options[0]["id"],
                    item_ids=[checkout.line_items[0]["id"]],
                )
            ],
        )
        updated = gateway.update_checkout(checkout.id, update, idempotency_key=str(uuid4()))
        current = gateway.get_checkout(checkout.id)
        assert updated == current and current.status == "ready_for_payment"
        verify_checkout(lane, ep, offer, current)
        facts["checkout"] = current.model_dump(mode="json")
        return current


def cleanup(lane: PurchaseLane, ep: Episode) -> dict[str, Any]:
    # Discover from persistence too: includes creations whose response never arrived.
    with client(lane, ep) as gateway:
        for checkout in state(lane, ep)["checkouts"]:
            if checkout["status"] != "canceled":
                canceled = gateway.cancel_checkout(
                    checkout["id"], idempotency_key="cleanup-" + checkout["id"]
                )
                assert canceled.status == "canceled"
            assert gateway.get_checkout(checkout["id"]).status == "canceled"
    result = state(lane, ep)
    assert all(c["status"] == "canceled" for c in result["checkouts"])
    for name in (
        "orders",
        "checkout_completion_attempts",
        "guarded_checkout_creations",
        "rejected_guarded_checkout_attempts",
    ):
        assert result["tables"][name]["count"] == 0, name
    return result


@pytest.mark.parametrize("case_id", CASES)
def test_offer_checkout_contract(p3_lane: PurchaseLane, case_id: str):
    lane = p3_lane
    lane.proxy.reset()
    facts: dict[str, Any] = {"case_id": case_id, "cleanup_verified": False, "commercial": "unknown"}
    episodes: list[Episode] = []
    status = "failed"
    try:
        c, v = lane.episode("calibration"), lane.episode("validation")
        episodes.extend((c, v))
        cs, vs = state(lane, c), state(lane, v)
        assert c.run_id != v.run_id and c.actor_id != v.actor_id
        assert (cs["actor_id"], vs["actor_id"]) == (c.actor_id, v.actor_id)
        assert cs["matches_full_fixture"] and vs["matches_full_fixture"]
        assert cs["offers"] == vs["offers"] and cs["clock_at"] == vs["clock_at"]
        assert not cs["checkouts"] and not vs["checkouts"]
        facts["initial"] = {"calibration": summary(cs), "validation": summary(vs)}
        psp_before = psp_state(lane)
        facts["psp_before"] = psp_before
        positive = case_id != "stable-zero"
        eligible = [
            o
            for o in cs["offers"]
            if o["product_status"] == "active"
            and o["available_quantity"] > 0
            and (o["pricing"]["shipping_total_minor"] > 0) == positive
        ]
        assert len(eligible) >= 2, "BLOCKED: insufficient full catalog attributes"
        raw = sorted(eligible, key=lambda o: (o["brand"], o["name"], o["id"]))[0]
        with client(lane, c) as gateway:
            observed = gateway.get_offer(raw["id"], CONTEXT)
        assert observed.model_dump(mode="json") == raw
        calibrated = prepare(lane, c, observed, facts)
        facts["calibration"] = {
            "offer": raw,
            "total_minor": calibrated.total_minor,
            "frozen_before_validation": True,
        }
        cal_done = cleanup(lane, c)
        # No shared run effects, and no payment preparation, even in calibration.
        after_calibration = state(lane, v)
        assert {k: val for k, val in after_calibration.items() if not k.startswith("global_")} == {
            k: val for k, val in vs.items() if not k.startswith("global_")
        }
        assert psp_state(lane) == psp_before
        facts["calibration_clean"] = summary(cal_done)
        frozen = {
            "case_id": case_id,
            "calibration_run": c.run_id,
            "validation_run": v.run_id,
            "offer": raw,
            "checkout_total_minor": calibrated.total_minor,
            "context": CONTEXT.model_dump(mode="json"),
            "quantity": 1,
        }
        facts["calibration_sha256"] = digest(frozen)
        lane.stack.record("P3-V01-frozen-calibration", **frozen, sha256=facts["calibration_sha256"])
        lane.proxy.reset()
        exercise(lane, c, v, raw, case_id, facts)
        cal_after = state(lane, c)
        assert cal_after["offers"] == cal_done["offers"]
        assert cal_after["checkouts"] == cal_done["checkouts"]
        for name in cal_done["tables"]:
            if name != "run_events" or case_id not in {"foreign-run", "foreign-actor"}:
                assert cal_after["tables"][name] == cal_done["tables"][name], name
        assert psp_state(lane) == psp_before
        assert not lane.proxy.errors, lane.proxy.errors
        assert not [call for call in lane.proxy.calls if call.fault == "blocked"]
        status = "passed"
    except BaseException as error:
        facts["error_type"] = type(error).__name__
        raise
    finally:
        lane.proxy.faults.clear()
        lane.proxy.response_transform.clear()
        lane.proxy.before_forward.clear()
        lane.proxy.after_answer.clear()
        try:
            facts["final"] = {ep.run_id: summary(cleanup(lane, ep)) for ep in episodes}
            if episodes:
                facts["psp_after"] = psp_state(lane)
                assert facts["psp_after"] == facts["psp_before"]
                for ep in episodes:
                    initial = facts["initial"][
                        "calibration" if ep == episodes[0] else "validation"
                    ]["tables"]
                    final = facts["final"][ep.run_id]["tables"]
                    allowed = {
                        "run_events",
                        "request_observations",
                        "checkout_sessions",
                        "checkout_mutations",
                        "experiment_runs",
                        "catalog_offers",
                    }
                    for name in initial.keys() - allowed:
                        assert final[name] == initial[name], "Unexpected persistent effect: " + name
                last = state(lane, episodes[-1])
                assert all(row["status"] == "canceled" for row in last["global_checkouts"])
                existing = {row["id"] for row in facts["initial"]["validation"]["global_checkouts"]}
                new = [row for row in last["global_checkouts"] if row["id"] not in existing]
                assert all(row["run_id"] in {ep.run_id for ep in episodes} for row in new)
                globals_before = facts["initial"]["validation"]["global_effects"]
                for name in globals_before.keys() - {
                    "checkout_sessions",
                    "checkout_mutations",
                    "request_observations",
                }:
                    assert last["global_effects"][name] == globals_before[name], name
            facts["cleanup_verified"] = bool(episodes)
        except BaseException as error:
            status = "failed"
            facts["cleanup_error_type"] = type(error).__name__
            raise
        finally:
            facts["transport_trace"] = [
                {
                    "method": call.method,
                    "path": call.path,
                    "status": call.status,
                    "fault": call.fault,
                    "upstream_answered": call.answered.is_set(),
                    "request_sha256": hashlib.sha256(call.request_body).hexdigest(),
                    "response_sha256": hashlib.sha256(call.response_body or b"").hexdigest(),
                }
                for call in lane.proxy.calls
            ]
            lane.stack.record("P3-V01-case", status=status, **facts)


def exercise(
    lane: PurchaseLane,
    c: Episode,
    v: Episode,
    raw: dict[str, Any],
    case: str,
    facts: dict[str, Any],
) -> None:
    offer_id = raw["id"]
    baseline = state(lane, v)
    mutation = {
        "revision-only": "revision",
        "shipping-feed": "shipping",
        "price-change": "price",
        "out-of-stock": "stock",
        "inactive": "inactive",
    }.get(case)
    # Explicit negative stimuli; label separately from Merchant observations.
    before = None
    if mutation in {"revision", "shipping", "price"}:
        with client(lane, v) as gateway:
            before = gateway.get_offer(offer_id, CONTEXT)
    if mutation:
        feed_before = (v.feed / "products.jsonl").read_bytes()
        facts["mutation"] = lane.rpc(
            "p3_mutate", run_id=v.run_id, offer_id=offer_id, kind=mutation, feed_dir=str(v.feed)
        )
        if case == "shipping-feed":
            assert (v.feed / "products.jsonl").read_bytes() == feed_before
            facts["unchanged_feed_sha256"] = hashlib.sha256(feed_before).hexdigest()
    if case.startswith("expiry-"):
        offset = {"expiry-before": -1, "expiry-at": 0, "expiry-after": 1}[case]
        facts["mutation"] = lane.rpc(
            "p3_mutate", run_id=v.run_id, offer_id=offer_id, kind="clock", offset=offset
        )
    if case == "timeout-before":
        lane.proxy.delay_seconds = 2.5
        lane.proxy.faults[READ] = "timeout_before"
    if case == "lost-read-after":
        lane.proxy.faults[READ] = "drop"
    if case == "invalid-response":
        lane.proxy.faults[READ] = "malformed"
    if case == "different-offer":
        other = next(o for o in baseline["offers"] if o["id"] != offer_id)
        lane.proxy.response_transform[READ] = lambda _: json.dumps(
            {"ok": True, "data": other}
        ).encode()
    actor = v
    if case in {"foreign-run", "foreign-actor"}:
        actor = c
    if case == "foreign-actor":
        # The pinned contract binds one actor per run: a second actor cannot be issued in V.
        with pytest.raises(AssertionError, match="ValueError"):
            lane.rpc("session", run_id=v.run_id, actor_id=c.actor_id)
        facts["foreign_actor_in_validation_run_rejected"] = True
    upstream = lane.proxy.upstream
    blocker = None
    if case == "network-error":
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        lane.proxy.upstream = f"http://127.0.0.1:{blocker.getsockname()[1]}"
    observed = None
    code = None
    read_before = state(lane, v)
    events_before = len(read_events(read_before))
    foreign_before = state(lane, c)
    try:
        with client(lane, actor, timeout=1 if case == "timeout-before" else 5) as gateway:
            if case == "missing-auth":
                unauthenticated = httpx.post(
                    lane.origin + READ,
                    json={
                        "offer_id": offer_id,
                        "delivery_context": CONTEXT.model_dump(mode="json"),
                    },
                    trust_env=False,
                )
                assert unauthenticated.status_code == 401
                with ACPClient(lane.origin, None) as missing:
                    observed = missing.get_offer(offer_id, CONTEXT)
            else:
                observed = gateway.get_offer(offer_id, CONTEXT)
    except ACPClientError as error:
        code = error.code
    finally:
        lane.proxy.upstream = upstream
        if blocker:
            blocker.close()
    after = state(lane, v)
    assert after["global_effects"] == read_before["global_effects"]
    assert after["global_checkouts"] == read_before["global_checkouts"]
    # Offer reads are commercially read-only but can write audit. Verify that exact boundary.
    for name in read_before["tables"].keys() - {"run_events"}:
        assert after["tables"][name] == read_before["tables"][name], name
    events = read_events(after)[events_before:]
    facts.update(
        offer_read=observed.model_dump(mode="json") if observed else None,
        read_error=code,
        read_events=events,
    )
    if case in {"timeout-before", "network-error", "missing-auth", "foreign-run", "foreign-actor"}:
        assert not events
    else:
        assert len(events) == 1
        assert events[0]["payload"]["offer_id"] == offer_id
        assert events[0]["payload"]["actor_id"] == actor.actor_id
        assert events[0]["payload"]["request_id"]
    at = after["clock_at"].replace("+00:00", "Z")
    budget = raw["pricing"]["total_minor"] + {"budget-minus": -1, "budget-plus": 1}.get(case, 0)
    facts["budget_minor"] = budget
    commercial = condition(
        observed,
        expected_id=offer_id,
        at=at,
        budget=budget,
        scope_verified=case not in {"foreign-run", "foreign-actor"},
    )
    if case in {"expiry-at", "expiry-after"}:
        assert code == "offer_expired" and events[0]["payload"]["result"] == "OFFER_EXPIRED"
        commercial = "false"  # Typed, attributable rejection; never inferred from transport error.
    facts["commercial"] = commercial
    unknown = {
        "timeout-before",
        "lost-read-after",
        "invalid-response",
        "different-offer",
        "network-error",
        "foreign-run",
        "foreign-actor",
        "missing-auth",
    }
    if case in unknown:
        assert commercial == "unknown"
        assert not after["checkouts"]
        if case == "lost-read-after":
            assert lane.proxy.calls[-1].answered.is_set() and code is not None
        if case == "different-offer":
            assert observed is not None and observed.id != offer_id
            facts["product_limitation"] = (
                "ACPClient accepts a schema-valid different offer; observer rejects binding"
            )
        if case in {"foreign-run", "foreign-actor"}:
            foreign_events = read_events(state(lane, c))[len(read_events(foreign_before)) :]
            assert len(foreign_events) == 1
            assert foreign_events[0]["payload"]["actor_id"] == c.actor_id
            facts["foreign_run_read_events"] = foreign_events
            with client(lane, v) as gateway, pytest.raises(ACPClientError):
                gateway.get_checkout(state(lane, c)["checkouts"][0]["id"])
        return
    if case in {"out-of-stock", "inactive", "expiry-at", "expiry-after"}:
        assert commercial == "false"
        lane.proxy.faults.clear()
        with client(lane, v) as gateway, pytest.raises(ACPClientError):
            gateway.create_checkout(offer_id, currency="cop", idempotency_key=str(uuid4()))
        assert not state(lane, v)["checkouts"]
        return
    assert observed is not None
    if case.startswith("budget-"):
        assert commercial == ("false" if case == "budget-minus" else "true")
    if case in {"revision-only", "shipping-feed", "price-change"}:
        assert before is not None and before != observed
        facts["pre_mutation_offer"] = before.model_dump(mode="json")
        facts["stale_snapshot_correspondence"] = "unknown"
        if case == "revision-only":
            assert before.pricing == observed.pricing and before.revision != observed.revision
        if case == "shipping-feed":
            assert before.revision == observed.revision and before.pricing != observed.pricing
    if case == "lost-create":
        key = str(uuid4())
        lane.proxy.faults[CREATE] = "drop"
        with client(lane, v) as gateway, pytest.raises(ACPClientError):
            gateway.create_checkout(offer_id, currency="cop", idempotency_key=key)
        assert lane.proxy.calls[-1].answered.is_set()
        lane.proxy.faults.clear()
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        response = httpx.post(
            lane.origin + LOOKUP_PATH,
            headers={
                "Authorization": "Bearer " + v.bearer.get_secret_value(),
                "API-Version": ACP_VERSION,
                "Request-Id": str(uuid4()),
            },
            json={"schema_version": LOOKUP_SCHEMA, "idempotency_key_sha256": key_hash},
            trust_env=False,
        )
        response.raise_for_status()
        found = CreationFound.model_validate(response.json())
        assert (found.run_id, found.actor_id, found.idempotency_key_sha256) == (
            v.run_id,
            v.actor_id,
            key_hash,
        )
        assert found.original_response_sha256 == original_response_sha256(found.original_response)
        expected_body = build_checkout_create(offer_id, "cop").model_dump_json().encode()
        assert found.acp_create_fingerprint == merchant_creation_digest(expected_body).removeprefix(
            "sha256:"
        )
        recovered = CheckoutSession.model_validate(found.original_response)
        assert recovered.id == found.checkout_id
        verify_checkout(lane, v, observed, recovered)
        assert len(state(lane, v)["checkouts"]) == 1
        facts["reconciliation"] = found.model_dump(mode="json")
        return
    checkout = prepare(lane, v, observed, facts)
    if case == "create-idempotency":
        # A separate creation key: twice the same request yields exactly one additional session.
        key = str(uuid4())
        with client(lane, v) as gateway:
            first = gateway.create_checkout(offer_id, currency="cop", idempotency_key=key)
            second = gateway.create_checkout(offer_id, currency="cop", idempotency_key=key)
            assert first == second
            other_id = next(o["id"] for o in baseline["offers"] if o["id"] != offer_id)
            with pytest.raises(ACPClientError):
                gateway.create_checkout(other_id, currency="cop", idempotency_key=key)
        assert len(state(lane, v)["checkouts"]) == 2
    if case == "lost-cancel":
        path = f"/checkout_sessions/{checkout.id}/cancel"
        key = str(uuid4())
        lane.proxy.faults[path] = "drop"
        with client(lane, v) as gateway, pytest.raises(ACPClientError):
            gateway.cancel_checkout(checkout.id, idempotency_key=key)
        assert lane.proxy.calls[-1].answered.is_set()
        lane.proxy.faults.clear()
        with client(lane, v) as gateway:
            assert gateway.cancel_checkout(checkout.id, idempotency_key=key).status == "canceled"
        assert len(state(lane, v)["checkouts"]) == 1
