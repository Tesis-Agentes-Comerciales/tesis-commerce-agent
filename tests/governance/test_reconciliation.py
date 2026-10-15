"""Local transactional reconciliation; remote observations are explicitly fixtures."""

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

from commerce_agent.application import PurchaseServiceError
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.evidence import DecisionRecord, EvidenceError
from commerce_agent.governance.models import ObjectRef
from commerce_agent.governance.reconciliation import (
    CheckoutStatus,
    ReconciliationRequest,
    ReplayAssessment,
    SnapshotObservation,
    UnverifiedMerchantObservation,
)
from commerce_agent.persistence.d1_reconciliation_sqlite import SqliteD1Reconciler
from commerce_agent.persistence.d1_uow_sqlite import SqliteD1UnitOfWork
from tests.governance.test_dispatch_envelope import binding, recover
from tests.governance.test_enforce_integration import OWNER, connect, make_case, records, start

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def request(case, **changes):
    with closing(connect(case.path)) as connection:
        op = connection.execute("SELECT operation_id FROM d1_checkout_operations").fetchone()[0]
    return ReconciliationRequest.model_validate(
        {
            "schema_version": "commerce-d1-reconciliation/1",
            "request_id": "inspection:a",
            "operation_id": op,
            "run_id": "run-a",
            "owner_id": OWNER,
            "expected_projection_version": 0,
            "observation": None,
            **changes,
        }
    )


def core(case, connection, clock=lambda: NOW):
    return SqliteD1Reconciler(connection, case.keys, clock=clock)


def initialize(case):
    with closing(connect(case.path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        core(case, connection).initialize()


def apply(case, req=None):
    req = req or request(case)
    with closing(connect(case.path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        return core(case, connection).reconcile(req)


def history(case):
    with closing(connect(case.path)) as connection:
        result = core(case, connection).history(request(case))
        assert not connection.in_transaction
        return result


def protected(case):
    with closing(connect(case.path)) as connection:
        return tuple(
            tuple(connection.execute(f"SELECT * FROM {table} ORDER BY 1"))
            for table in (
                "purchases",
                "d1_checkout_operations",
                "d1_checkout_results",
                "governance_evidence",
                "d1_checkout_envelopes",
                "operation_keys",
            )
        )


def episode(path, state="reserved", expected_binding=False):
    case = make_case(
        path,
        blocked=state in ("reserved", "claimed"),
        fault="timeout" if state == "uncertain" else None,
    )
    if expected_binding:
        case.gate = SqliteD1EnforceGate(
            path,
            case.gate._configuration,
            case.keys,
            case.gate._clocks,
            expected_remote_binding=binding(),
        )
        case.service._d1_gate = case.gate
    if state == "confirmed":
        start(case)
    else:
        with pytest.raises(PurchaseServiceError):
            start(case)
    if state == "claimed":
        with closing(connect(path)) as connection:
            policy = connection.execute("SELECT policy_bytes FROM governance_episodes").fetchone()[
                0
            ]
            record = records(case)[0]
            assert isinstance(record, DecisionRecord)
            uow = SqliteD1UnitOfWork(
                connection,
                keys=case.keys,
                policy_bytes=policy,
                policy_binding=record.decision.policy_binding,
            )
            op = request(case).operation_id
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                assert uow.claim(op, claimant="fixture:worker", owner_id=OWNER)
    initialize(case)
    return case


@pytest.fixture
def reserved(tmp_path):
    return episode(tmp_path / "reserved.db")


def ref(name, digit="1"):
    return ObjectRef(
        schema_version="commerce-policy/1.0",
        id="fixture:" + name,
        version=1,
        digest="sha256:" + digit * 64,
    )


def observation(case, status: CheckoutStatus = "canceled", **changes):
    envelope = recover(case)
    original = SnapshotObservation(
        checkout_id="cs_1",
        status="not_ready_for_payment",
        snapshot_digest="sha256:" + "1" * 64,
        source_ref=ref("historical"),
        observed_at="2026-10-15T11:00:00.000Z",
    )
    current = SnapshotObservation(
        checkout_id="cs_1",
        status=status,
        snapshot_digest="sha256:" + "2" * 64,
        source_ref=ref("current", "2"),
        observed_at="2026-10-15T12:00:00.000Z",
    )
    return UnverifiedMerchantObservation.model_validate(
        {
            "provenance": "offline_fixture",
            "source_ref": ref("transport"),
            "merchant_origin": envelope.merchant_origin,
            "remote_run_id": binding().remote_run_id,
            "remote_actor_id": binding().remote_actor_id,
            "idempotency_key_digest": sha256_digest(
                envelope.idempotency_key.get_secret_value().encode()
            ),
            "acp_fingerprint": envelope.body_digest,
            "original_creation": original,
            "current_checkout": current,
            **changes,
        }
    )


@pytest.mark.parametrize("state", ["reserved", "claimed", "uncertain", "confirmed"])
def test_local_states_never_grant_replay_or_continuation(tmp_path, state):
    case = episode(tmp_path / f"{state}.db", state)
    before = protected(case)
    calls = tuple(case.gateway.calls)
    event = apply(case)
    assert event.recovered.local_outcome == {"claimed": "claimed_without_observation"}.get(
        state, state
    )
    assert event.recovered.authorization == "historical_allow_only"
    assert event.recovered.envelope_status == "available"
    assert not event.replay.eligible and not event.continuation_allowed
    assert "REMOTE_PROOF_UNAVAILABLE" in event.reasons
    assert "HISTORICAL_AUTHORIZATION_NOT_REPLAY_AUTHORITY" in event.reasons
    assert "RETENTION_UNPROVEN" in event.reasons
    if state == "claimed":
        assert "CLAIM_NOT_DELIVERY_PROOF" in event.reasons
    assert event.outcome == ("historical_confirmation_only" if state == "confirmed" else "blocked")
    assert protected(case) == before and tuple(case.gateway.calls) == calls


def test_inspection_is_read_only_and_reexecution_returns_original_event(reserved):
    before = protected(reserved)
    req = request(reserved)
    first = apply(reserved, req)
    with closing(connect(reserved.path)) as connection:
        store = core(reserved, connection, clock=lambda: datetime(2027, 1, 1, tzinfo=UTC))
        assert store.inspect(req).projection_version == 1
        assert not connection.in_transaction
        with connection:
            connection.execute("BEGIN")
            assert canonical_bytes(store.reconcile(req)) == canonical_bytes(first)
    assert history(reserved) == (first,) and protected(reserved) == before


def test_actual_new_process_recovers_the_same_durable_event(reserved):
    original = apply(reserved)
    script = """
import sys,json,sqlite3
from datetime import UTC,datetime
from pydantic import SecretBytes
from commerce_agent.governance.provenance import ProvenanceKeys
from commerce_agent.governance.reconciliation import ReconciliationRequest
from commerce_agent.persistence.d1_reconciliation_sqlite import SqliteD1Reconciler
from commerce_agent.governance.canonical import canonical_bytes,sha256_digest
data=json.load(sys.stdin)
keys=ProvenanceKeys(key_id=data['key_id'],pseudonym_key=SecretBytes(bytes.fromhex(data['hmac'])),encryption_key=SecretBytes(bytes.fromhex(data['aes'])))
with sqlite3.connect(data['database']) as connection:
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('BEGIN IMMEDIATE')
    event=SqliteD1Reconciler(connection,keys,clock=lambda:datetime(2027,1,1,tzinfo=UTC)).reconcile(ReconciliationRequest.model_validate(data['request']))
    print(sha256_digest(canonical_bytes(event)))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        input=json.dumps(
            {
                "database": str(reserved.path),
                "key_id": reserved.keys.key_id,
                "hmac": reserved.keys.pseudonym_key.get_secret_value().hex(),
                "aes": reserved.keys.encryption_key.get_secret_value().hex(),
                "request": request(reserved).model_dump(mode="json"),
            }
        ),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == sha256_digest(canonical_bytes(original))
    assert history(reserved) == (original,)


@pytest.mark.parametrize("damage", ["missing_table", "missing_row", "corrupt"])
def test_missing_or_corrupt_envelope_blocks_without_reconstruction(reserved, damage):
    with closing(connect(reserved.path)) as connection, connection:
        if damage == "missing_table":
            connection.execute("DROP TABLE d1_checkout_envelopes")
        elif damage == "missing_row":
            connection.execute("DROP TRIGGER d1_envelope_no_delete")
            connection.execute("DELETE FROM d1_checkout_envelopes")
        else:
            connection.execute("DROP TRIGGER d1_envelope_no_update")
            connection.execute("UPDATE d1_checkout_envelopes SET canonical_envelope=?", (b"{}",))
    event = apply(reserved)
    assert event.outcome == "blocked" and event.recovered.envelope_digest is None
    assert ("ENVELOPE_UNVERIFIABLE" if damage == "corrupt" else "ENVELOPE_MISSING") in event.reasons
    assert not reserved.gateway.calls


@pytest.mark.parametrize(
    "scope", [{"owner_id": "foreign"}, {"run_id": "foreign"}, {"operation_id": "foreign"}]
)
def test_wrong_scope_never_reads_or_records_other_operation(reserved, scope):
    req = request(reserved, **scope)
    with closing(connect(reserved.path)) as connection:
        store = core(reserved, connection)
        for read in (store.inspect, store.history):
            with pytest.raises(EvidenceError):
                read(req)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(EvidenceError):
                store.reconcile(req)
    assert not history(reserved)


def test_expected_binding_and_matching_fixture_do_not_authenticate(tmp_path):
    case = episode(tmp_path / "bound.db", expected_binding=True)
    event = apply(case, request(case, observation=observation(case)))
    assert event.recovered.binding_status == "backend_expected_unverified"
    assert "EXPECTED_BINDING_NOT_AUTHENTICATED" in event.reasons
    assert event.observation is not None
    assert event.observation.provenance == "offline_fixture"
    assert event.replay.required == ReplayAssessment().required
    assert not event.replay.eligible and not case.gateway.calls


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("merchant_origin", "http://127.0.0.1:4999", "REMOTE_SCOPE_CONFLICT"),
        ("remote_run_id", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "REMOTE_SCOPE_CONFLICT"),
        ("remote_actor_id", "foreign", "REMOTE_SCOPE_CONFLICT"),
        ("idempotency_key_digest", "sha256:" + "0" * 64, "REQUEST_KEY_CONFLICT"),
        ("acp_fingerprint", "sha256:" + "0" * 64, "ACP_FINGERPRINT_CONFLICT"),
    ],
)
def test_untrusted_key_fingerprint_and_scope_conflicts_are_recorded(tmp_path, field, value, reason):
    case = episode(tmp_path / "conflict.db", expected_binding=True)
    event = apply(case, request(case, observation=observation(case, **{field: value})))
    assert reason in event.reasons and event.outcome == "blocked"
    assert not event.continuation_allowed and not case.gateway.calls


@pytest.mark.parametrize("status", ["canceled", "expired", "completed", "ready_for_payment"])
def test_historical_confirmation_and_changed_current_checkout_are_separate(tmp_path, status):
    case = episode(tmp_path / "confirmed.db", "confirmed")
    before = protected(case)
    event = apply(case, request(case, observation=observation(case, status=status)))
    assert event.outcome == "historical_confirmation_only"
    assert event.observation is not None
    assert event.observation.original_creation is not None
    assert event.observation.current_checkout is not None
    assert event.observation.original_creation.status == "not_ready_for_payment"
    assert event.observation.current_checkout.status == status
    assert "HISTORICAL_CURRENT_DIFFER" in event.reasons
    assert not event.continuation_allowed and not event.replay.eligible
    assert protected(case) == before


def test_arbitrary_response_cannot_confirm_uncertain_operation(tmp_path):
    case = episode(tmp_path / "uncertain.db", "uncertain")
    before = protected(case)
    supplied = observation(case)
    assert supplied.current_checkout is not None
    supplied = supplied.model_copy(
        update={
            "current_checkout": supplied.current_checkout.model_copy(
                update={"checkout_id": "foreign"}
            )
        }
    )
    event = apply(case, request(case, observation=supplied))
    assert event.recovered.local_outcome == "uncertain" and event.outcome == "blocked"
    assert "CHECKOUT_ID_CONFLICT" in event.reasons and protected(case) == before
    assert sum(name == "create" for name, _ in case.gateway.calls) == 1


@pytest.mark.parametrize("same_request", [False, True])
def test_two_reconcilers_use_cas_without_duplicate_events(reserved, same_request):
    barrier = Barrier(2)

    def attempt(index):
        req = request(
            reserved, request_id="inspection:same" if same_request else f"inspection:{index}"
        )
        with closing(connect(reserved.path)) as connection:
            barrier.wait(timeout=10)
            try:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    return core(reserved, connection).reconcile(req)
            except EvidenceError:
                return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(attempt, (1, 2)))
    assert len(history(reserved)) == 1
    assert sum(value is not None for value in outcomes) == (2 if same_request else 1)
    if same_request:
        assert outcomes[0] == outcomes[1]
    assert not reserved.gateway.calls


def test_crash_before_commit_rolls_back_and_retry_keeps_event_identity(reserved):
    req = request(reserved)
    with closing(connect(reserved.path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        provisional = core(reserved, connection).reconcile(req)
        connection.rollback()
    assert not history(reserved)
    assert apply(reserved, req) == provisional and len(history(reserved)) == 1


def test_commit_that_persisted_then_raised_is_recovered_without_rewrite(reserved):
    class Connection(sqlite3.Connection):
        def commit(self):
            super().commit()
            raise sqlite3.OperationalError("synthetic loss after durable commit")

    req = request(reserved)
    with closing(sqlite3.connect(reserved.path, factory=Connection)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        event = core(reserved, connection).reconcile(req)
        with pytest.raises(sqlite3.OperationalError):
            connection.commit()
        connection.rollback()
    assert apply(reserved, req) == event and history(reserved) == (event,)
    assert not reserved.gateway.calls


@pytest.mark.parametrize("phase", ["INSERT", "UPDATE"])
def test_event_and_projection_rollback_even_if_caller_catches_error(reserved, phase):
    original = apply(reserved) if phase == "UPDATE" else None
    before = history(reserved)
    with closing(connect(reserved.path)) as connection, connection:
        connection.execute(
            f"CREATE TRIGGER projection_fault BEFORE {phase} "
            "ON d1_reconciliation_projection BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
        )
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(EvidenceError):
            core(reserved, connection).reconcile(
                request(
                    reserved,
                    request_id="inspection:next",
                    expected_projection_version=1 if original else 0,
                )
            )
    assert history(reserved) == before


@pytest.mark.parametrize(
    "command",
    [
        "UPDATE d1_reconciliation_events SET outcome=outcome",
        "DELETE FROM d1_reconciliation_events",
        "INSERT OR REPLACE INTO d1_reconciliation_events SELECT * FROM d1_reconciliation_events",
        "INSERT OR REPLACE INTO d1_reconciliation_events "
        "SELECT 'different',operation_id,request_id,"
        "version,request_digest,outcome,body,body_digest FROM d1_reconciliation_events",
        "DELETE FROM d1_reconciliation_projection",
        "INSERT OR REPLACE INTO d1_reconciliation_projection "
        "SELECT * FROM d1_reconciliation_projection",
        "UPDATE d1_reconciliation_projection SET version=version",
    ],
)
def test_historical_event_and_projection_guards(reserved, command):
    first = apply(reserved)
    with closing(connect(reserved.path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(command)
    assert history(reserved) == (first,)


def test_new_observation_appends_without_rewriting_original_uncertain(tmp_path):
    case = episode(tmp_path / "history.db", "uncertain")
    before = protected(case)
    first = apply(case)
    second = apply(
        case,
        request(
            case,
            request_id="inspection:new",
            expected_projection_version=1,
            observation=observation(case),
        ),
    )
    assert first.version == 1 and second.version == 2
    assert history(case) == (first, second) and protected(case) == before
    assert second.outcome == "blocked" and second.recovered.local_outcome == "uncertain"
    assert sum(name == "create" for name, _ in case.gateway.calls) == 1


def test_same_request_id_with_different_content_is_conflict(reserved):
    first = apply(reserved)
    with pytest.raises(EvidenceError):
        apply(reserved, request(reserved, observation=observation(reserved)))
    assert history(reserved) == (first,)


def test_missing_proof_cannot_be_bypassed_with_boolean_or_removed_requirements(reserved):
    req = request(reserved)
    for change in ({"trusted": True}, {"authenticated": True}, {"replay_allowed": True}):
        with pytest.raises(ValidationError):
            ReconciliationRequest.model_validate({**req.model_dump(), **change})
    with pytest.raises(ValidationError):
        ReplayAssessment.model_validate({"eligible": True})
    with pytest.raises(ValidationError):
        ReplayAssessment(required=())


def test_schema_is_explicit_reversible_and_rejects_weakened_definition(reserved):
    with closing(connect(reserved.path)) as connection:
        store = core(reserved, connection)
        with pytest.raises(EvidenceError):
            store.initialize()
        connection.execute("BEGIN IMMEDIATE")
        store.initialize()
        store.initialize()
        connection.rollback()
        connection.execute("DROP TRIGGER d1_reconciliation_event_no_update")
        connection.execute(
            "CREATE TRIGGER d1_reconciliation_event_no_update "
            "BEFORE UPDATE ON d1_reconciliation_events BEGIN SELECT 1; END"
        )
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(EvidenceError):
                store.initialize()


@pytest.mark.parametrize("damage", ["body", "missing_projection"])
def test_event_corruption_is_not_silently_returned(reserved, damage):
    apply(reserved)
    with closing(connect(reserved.path)) as connection, connection:
        if damage == "missing_projection":
            connection.execute("DROP TABLE d1_reconciliation_projection")
        else:
            connection.execute("DROP TRIGGER d1_reconciliation_event_no_update")
            connection.execute("UPDATE d1_reconciliation_events SET body=?", (b"{}",))
    with pytest.raises(EvidenceError):
        history(reserved)


@pytest.mark.parametrize("damage", ["body", "digest", "noncanonical", "operation"])
def test_older_event_corruption_blocks_all_projection_users(reserved, damage):
    first = apply(reserved)
    second_request = request(
        reserved, request_id="inspection:second", expected_projection_version=1
    )
    second = apply(reserved, second_request)
    commercial_before = protected(reserved)
    with closing(connect(reserved.path)) as connection, connection:
        connection.execute("DROP TRIGGER d1_reconciliation_event_no_update")
        if damage == "digest":
            connection.execute(
                "UPDATE d1_reconciliation_events SET body_digest=? WHERE event_id=?",
                ("sha256:" + "0" * 64, first.event_id),
            )
        else:
            body = b"{}" if damage == "body" else b" " + canonical_bytes(first)
            digest = core(reserved, connection)._event_digest(first)
            if damage == "operation":
                altered = first.model_copy(
                    update={
                        "recovered": first.recovered.model_copy(update={"operation_id": "foreign"})
                    }
                )
                body = canonical_bytes(altered)
                digest = core(reserved, connection)._event_digest(altered)
            connection.execute(
                "UPDATE d1_reconciliation_events SET body=?,body_digest=? WHERE event_id=?",
                (body, digest, first.event_id),
            )

    def journal():
        with closing(connect(reserved.path)) as connection:
            return tuple(
                tuple(connection.execute(f"SELECT * FROM {table} ORDER BY 1"))
                for table in ("d1_reconciliation_events", "d1_reconciliation_projection")
            )

    before = journal()
    with closing(connect(reserved.path)) as connection:
        store = core(reserved, connection)
        assert store._event(second.event_id) == second
        for read in (store.inspect, store.history):
            with pytest.raises(EvidenceError):
                read(request(reserved))
    for req in (
        request(reserved, request_id="inspection:third", expected_projection_version=2),
        second_request,
    ):
        with pytest.raises(EvidenceError):
            apply(reserved, req)
        assert journal() == before
    assert protected(reserved) == commercial_before and not reserved.gateway.calls


def test_valid_multiple_versions_are_readable_and_extendable(reserved):
    events = [apply(reserved)]
    for version in (1, 2):
        with closing(connect(reserved.path)) as connection:
            assert (
                core(reserved, connection).inspect(request(reserved)).projection_version == version
            )
        assert history(reserved) == tuple(events)
        req = request(
            reserved, request_id=f"inspection:{version + 1}", expected_projection_version=version
        )
        events.append(apply(reserved, req))
        assert apply(reserved, req) == events[-1]
    assert [event.version for event in history(reserved)] == [1, 2, 3]
    assert history(reserved) == tuple(events) and not reserved.gateway.calls


def test_history_with_a_missing_middle_version_is_rejected(reserved):
    apply(reserved)
    middle = apply(
        reserved, request(reserved, request_id="inspection:second", expected_projection_version=1)
    )
    last = apply(
        reserved, request(reserved, request_id="inspection:third", expected_projection_version=2)
    )
    with closing(connect(reserved.path)) as connection, connection:
        connection.execute("DROP TRIGGER d1_reconciliation_event_no_delete")
        connection.execute(
            "DELETE FROM d1_reconciliation_events WHERE event_id=?", (middle.event_id,)
        )
        assert core(reserved, connection)._event(last.event_id) == last
    with pytest.raises(EvidenceError):
        history(reserved)
    with pytest.raises(EvidenceError):
        apply(
            reserved,
            request(reserved, request_id="inspection:fourth", expected_projection_version=3),
        )
