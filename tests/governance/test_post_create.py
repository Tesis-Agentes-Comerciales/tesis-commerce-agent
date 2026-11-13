"""Synthetic transport, isolated SQLite; no Merchant authentication or integration."""

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from threading import Barrier

import pytest
from pydantic import ValidationError

from commerce_agent.application import PurchaseService, PurchaseServiceError
from commerce_agent.completion import CompletionError, CompletionService
from commerce_agent.domain import HardConstraints, PurchaseIntent, rank_candidates
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.evidence import EvidenceError
from commerce_agent.governance.post_create import PostCreateEvent
from commerce_agent.persistence.post_create_sqlite import SqlitePostCreateStore
from commerce_evaluation.stack.fake_checkout import checkout
from tests.governance.test_enforce_integration import (
    MARKER,
    OWNER,
    connect,
    make_case,
    start,
)
from tests.test_completion import FakeGateway, FakePayments


def flow(case, **scope):
    return case.gate.post_create_flow(scope.get("run_id", "run-a"), scope.get("owner_id", OWNER))


def prepared(tmp_path):
    case = make_case(tmp_path / "agent.db")
    case.store.create_purchase("run-a", MARKER, OWNER)
    feed = case.service._feed
    intent = PurchaseIntent(hard=HardConstraints(currency="COP"))
    selection = rank_candidates(feed, intent)
    assert selection.selected is not None
    case.gate.create_checkout(
        case.gate.capture(feed, intent, rank_candidates),
        run_id="run-a",
        owner_id=OWNER,
        selection=selection,
        candidate=selection.selected,
        gateway=case.gateway,
    )
    assert flow(case).inspect()[-1].phase == "creation_confirmed"
    return case


def creation(case):
    with closing(connect(case.path)) as connection:
        return tuple(
            tuple(connection.execute(f"SELECT * FROM {table} ORDER BY 1"))
            for table in (
                "governance_evidence",
                "d1_checkout_results",
                "d1_checkout_envelopes",
            )
        )


def local(case):
    with closing(connect(case.path)) as connection:
        return tuple(
            tuple(connection.execute(f"SELECT * FROM {table} ORDER BY 1"))
            for table in (
                "purchases",
                "d1_post_create_events",
                "d1_post_create_checkpoint",
            )
        )


class AuditedPort:
    def __init__(self, case):
        self.case, self.creation = case, None

    def perform_once(self, effect, call):
        events = flow(self.case).inspect()
        assert events[-1].phase == "intent" and events[-1].effect == effect
        assert not events[-1].continuation_allowed
        if effect != "verify_creation_get" and self.creation is None:
            self.creation = creation(self.case)
        return call()


@pytest.mark.parametrize(
    "fault",
    [
        "update_timeout",
        "get_error",
        "update_invalid",
        "get_invalid",
        "budget_error",
        "cancel_timeout",
        "cancel_invalid",
    ],
)
def test_post_create_failure_preserves_confirmation_and_uncertainty(tmp_path, monkeypatch, fault):
    case = make_case(tmp_path / "agent.db")
    port = AuditedPort(case)
    case.gate._post_create_dispatch = port
    original_update = case.gateway.update_checkout
    original_get = case.gateway.get_checkout
    original_cancel = case.gateway.cancel_checkout

    def update(*args, **kwargs):
        result = original_update(*args, **kwargs)
        if fault == "update_timeout":
            raise TimeoutError("synthetic response loss after effect")
        return result.model_copy(update={"id": "foreign"}) if fault == "update_invalid" else result

    def get(*args, **kwargs):
        result = original_get(*args, **kwargs)
        if any(name == "update" for name, _ in case.gateway.calls):
            if fault == "get_error":
                raise RuntimeError("synthetic GET failure")
            if fault == "get_invalid":
                return result.model_copy(update={"status": "expired"})
        return result

    def cancel(*args, **kwargs):
        result = original_cancel(*args, **kwargs)
        if fault == "cancel_timeout":
            raise TimeoutError("synthetic cancellation response loss")
        return result.model_copy(update={"status": "ready_for_payment"})

    monkeypatch.setattr(case.gateway, "update_checkout", update)
    monkeypatch.setattr(case.gateway, "get_checkout", get)
    monkeypatch.setattr(case.gateway, "cancel_checkout", cancel)
    if fault == "budget_error":

        def budget(*args, **kwargs):
            port.creation = creation(case)
            raise ValueError("synthetic local budget failure")

        monkeypatch.setattr(case.service, "_enforce_authoritative_budget", budget)
    args = {}
    if fault.startswith("cancel"):
        case.gateway.current = checkout(
            status="not_ready_for_payment", total=90_000, unit_amount=72_000
        )
        args["intent"] = PurchaseIntent(
            hard=HardConstraints(currency="COP", max_total_amount_minor=80_000)
        )
    with pytest.raises((RuntimeError, ValueError)):
        start(case, **args)
    purchase = case.store.purchase("run-a")
    assert (
        purchase is not None
        and purchase["stage"] == "uncertain"
        and purchase["checkout_id"] == "cs_1"
    )
    status = case.gate.recovery_status("run-a")
    assert status is not None and status[:2] == ("confirmed", "cs_1")
    events = flow(case).inspect()
    assert events[-1].phase == "uncertain"
    assert any(event.phase == "creation_confirmed" for event in events)
    assert port.creation == creation(case)
    before = tuple(case.gateway.calls)
    with pytest.raises(PurchaseServiceError, match="explicit recovery"):
        start(case)
    assert tuple(case.gateway.calls) == before


def test_successful_experiment_has_committed_intents_and_no_continuation_authority(tmp_path):
    case = make_case(tmp_path / "agent.db")
    port = AuditedPort(case)
    case.gate._post_create_dispatch = port
    result = start(case)
    assert (
        result.stage == "awaiting_confirmation" and case.store.purchase_state_version("run-a") == 4
    )
    events = flow(case).inspect()
    assert [e.effect for e in events if e.phase == "intent"] == [
        "verify_creation_get",
        "update_details",
        "get_current",
    ]
    assert events[-1].phase == "awaiting_confirmation" and len(events) == 9
    assert all(not e.replay_allowed and not e.continuation_allowed for e in events)
    assert all(e.binding_status == "unavailable" for e in events)
    assert port.creation == creation(case)
    serialized = canonical_bytes([event.model_dump(mode="json") for event in events])
    assert b"buyer@example.test" not in serialized
    assert MARKER.encode() not in serialized
    key = case.store.idempotency_key("run-a", "checkout.update.details")
    assert events[4].key_digest == sha256_digest(key.encode())
    assert key.encode() not in serialized


def test_verified_budget_cancellation_is_recorded_without_authorizing_continuation(tmp_path):
    case = make_case(tmp_path / "agent.db")
    case.gateway.current = checkout(
        status="not_ready_for_payment", total=90_000, unit_amount=72_000
    )
    with pytest.raises(PurchaseServiceError) as error:
        start(
            case,
            intent=PurchaseIntent(
                hard=HardConstraints(currency="COP", max_total_amount_minor=80_000)
            ),
        )
    assert error.value.code == "budget_exceeded"
    purchase = case.store.purchase("run-a")
    assert purchase is not None and purchase["stage"] == "canceled"
    assert flow(case).inspect()[-1].phase == "canceled"
    status = case.gate.recovery_status("run-a")
    assert status is not None and status[:2] == ("confirmed", "cs_1")


@pytest.mark.parametrize("method", ["approval_terms", "approve", "cancel", "_current_checkout"])
def test_later_user_routes_are_blocked_for_d1_without_http(tmp_path, method):
    case = make_case(tmp_path / "agent.db")
    start(case)
    before = tuple(case.gateway.calls)
    args = dict(run_id="run-a")
    if method == "approve":
        args.update(actor_id=OWNER, expected_fingerprint="unused")
    with pytest.raises(PurchaseServiceError) as error:
        getattr(case.service, method)(**args)
    assert error.value.code == "d1_continuation_blocked" and tuple(case.gateway.calls) == before


@pytest.mark.parametrize("after_effect", [False, True])
def test_abrupt_process_exit_keeps_intent_and_restart_never_reexecutes(tmp_path, after_effect):
    case = prepared(tmp_path)
    before = creation(case)
    marker = tmp_path / "synthetic-effect.txt"
    script = """
import json,sys,sqlite3,os
from pathlib import Path
from pydantic import SecretBytes
from commerce_agent.governance.provenance import ProvenanceKeys
from commerce_agent.governance.post_create import PostCreateFlow
from commerce_agent.acp.models import CheckoutSession
d=json.load(sys.stdin)
keys=ProvenanceKeys(key_id=d['key_id'],pseudonym_key=SecretBytes(bytes.fromhex(d['hmac'])),encryption_key=SecretBytes(bytes.fromhex(d['aes'])))
def connect():
    c=sqlite3.connect(d['db']);c.execute('PRAGMA foreign_keys=ON');return c
def effect():
    Path(d['marker']).write_text('one synthetic effect',encoding='utf-8')
    return CheckoutSession.model_validate(d['checkout'])
class Port:
    def perform_once(self,name,call):
        if d['after']: call()
        os._exit(67)
flow=PostCreateFlow(connect,keys,'run-a',d['owner'],Port())
flow.effect('update_details',b'synthetic request','synthetic-key',effect)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        input=json.dumps(
            {
                "db": str(case.path),
                "key_id": case.keys.key_id,
                "hmac": case.keys.pseudonym_key.get_secret_value().hex(),
                "aes": case.keys.encryption_key.get_secret_value().hex(),
                "owner": OWNER,
                "checkout": case.gateway.current.model_dump(mode="json"),
                "marker": str(marker),
                "after": after_effect,
            }
        ),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 67 and marker.exists() == after_effect
    recovered = flow(case).inspect()
    assert recovered[-1].phase == "intent" and recovered[-1].effect == "update_details"
    purchase = case.store.purchase("run-a")
    assert purchase is not None and purchase["stage"] == "checkout_prepared"
    calls = tuple(case.gateway.calls)
    with pytest.raises(PurchaseServiceError):
        start(case)
    with pytest.raises(EvidenceError):
        flow(case).effect(
            "update_details", b"synthetic request", "synthetic-key", lambda: case.gateway.current
        )
    assert flow(case).inspect() == recovered and creation(case) == before
    assert tuple(case.gateway.calls) == calls


def test_concurrent_intents_have_one_cas_winner(tmp_path):
    case = prepared(tmp_path)
    barrier = Barrier(2)
    version = len(flow(case).inspect())

    def attempt(_):
        with closing(connect(case.path)) as connection:
            barrier.wait(timeout=10)
            try:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    SqlitePostCreateStore(connection, case.keys).append(
                        "run-a",
                        OWNER,
                        expected_version=version,
                        phase="intent",
                        effect="update_details",
                        recorded_at=datetime.now(UTC),
                        request_digest="sha256:" + "1" * 64,
                        key_digest="sha256:" + "2" * 64,
                    )
                return True
            except EvidenceError:
                return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(attempt, (1, 2))) == [False, True]
    assert len(flow(case).inspect()) == version + 1


def test_stale_purchase_blocks_effect_before_transport(tmp_path):
    case = prepared(tmp_path)
    case.store.update_purchase("run-a", stage="uncertain")
    with pytest.raises(EvidenceError):
        flow(case).effect(
            "update_details", b"request", "key", lambda: pytest.fail("transport called")
        )


def test_checkpoint_failure_rolls_back_event_and_purchase_even_if_caught(tmp_path):
    case = prepared(tmp_path)
    before = local(case)
    with closing(connect(case.path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER synthetic_checkpoint_fault BEFORE UPDATE "
            "ON d1_post_create_checkpoint BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.Error):
            SqlitePostCreateStore(connection, case.keys).append(
                "run-a",
                OWNER,
                expected_version=4,
                phase="uncertain",
                effect=None,
                recorded_at=datetime.now(UTC),
            )
    assert local(case) == before


@pytest.mark.parametrize("damage", ["checkpoint", "old_event", "missing_checkpoint", "envelope"])
def test_corruption_blocks_inspection_and_effects(tmp_path, damage):
    case = prepared(tmp_path)
    with closing(connect(case.path)) as connection, connection:
        if damage == "checkpoint":
            connection.execute("DROP TRIGGER d1_post_checkpoint_update")
            connection.execute(
                "UPDATE d1_post_create_checkpoint SET digest=?", ("sha256:" + "0" * 64,)
            )
        elif damage == "missing_checkpoint":
            connection.execute("DROP TABLE d1_post_create_checkpoint")
        elif damage == "old_event":
            connection.execute("DROP TRIGGER d1_post_create_events_no_update")
            connection.execute("UPDATE d1_post_create_events SET body=? WHERE version=1", (b"{}",))
        else:
            connection.execute("DROP TRIGGER d1_envelope_no_update")
            connection.execute("UPDATE d1_checkout_envelopes SET canonical_envelope=?", (b"{}",))
    with pytest.raises(EvidenceError):
        flow(case).inspect()
    with pytest.raises(EvidenceError):
        flow(case).effect(
            "update_details", b"request", "key", lambda: pytest.fail("transport called")
        )


@pytest.mark.parametrize("scope", [{"owner_id": "foreign"}, {"run_id": "foreign"}])
def test_scope_isolation_without_http(tmp_path, scope):
    case = prepared(tmp_path)
    with pytest.raises(EvidenceError):
        flow(case, **scope).inspect()
    with pytest.raises(EvidenceError):
        flow(case, **scope).effect(
            "update_details", b"request", "key", lambda: pytest.fail("transport called")
        )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE d1_post_create_events SET digest=digest",
        "DELETE FROM d1_post_create_events",
        "INSERT OR REPLACE INTO d1_post_create_events SELECT * FROM d1_post_create_events",
    ],
)
def test_event_immutability(tmp_path, sql):
    case = prepared(tmp_path)
    before = flow(case).inspect()
    with (
        closing(connect(case.path)) as connection,
        connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(sql)
    assert flow(case).inspect() == before


def test_no_default_post_create_transport_or_claim_of_permission(tmp_path):
    case = prepared(tmp_path)
    case.gate._post_create_dispatch = None
    with pytest.raises(EvidenceError):
        flow(case).effect(
            "update_details", b"request", "key", lambda: pytest.fail("transport called")
        )
    event = flow(case).inspect()[-1]
    for change in ({"replay_allowed": True}, {"continuation_allowed": True}, {"trusted": True}):
        with pytest.raises(ValidationError):
            PostCreateEvent.model_validate({**event.model_dump(), **change})


@pytest.mark.parametrize("restart_without_gate", [False, True])
@pytest.mark.parametrize("method", ["start", "approval_terms", "approve", "cancel", "complete"])
def test_d1_uncertainty_never_enters_legacy_continuation(tmp_path, restart_without_gate, method):
    case = prepared(tmp_path)
    case.store.update_purchase("run-a", stage="uncertain")
    if restart_without_gate:
        case.service = PurchaseService(
            feed=case.service._feed,
            checkout=case.gateway,
            store=case.store,
            approvals=case.service._approvals,
        )
    gateway = FakeGateway(case.gateway.current)
    completion = CompletionService(gateway, case.service._approvals, FakePayments(), case.store)
    before = tuple(case.gateway.calls)
    if method == "complete":
        with pytest.raises(CompletionError) as error:
            completion.complete(run_id="run-a")
        assert error.value.code == "d1_continuation_blocked"
    elif method == "start":
        with pytest.raises(PurchaseServiceError) as error:
            start(case)
        assert error.value.code == "d1_recovery_required"
    else:
        args = dict(run_id="run-a")
        if method == "approve":
            args.update(actor_id=OWNER, expected_fingerprint="unused")
        with pytest.raises(PurchaseServiceError) as error:
            getattr(case.service, method)(**args)
        assert error.value.code == "d1_continuation_blocked"
    assert tuple(case.gateway.calls) == before
    assert gateway.get_calls == 0 and gateway.complete_calls == []
