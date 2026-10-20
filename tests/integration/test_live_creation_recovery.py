"""06D-C opt-in: real PostgreSQL, frozen Merchant process, loopback HTTP, real Agent."""

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from commerce_agent.acp.creation_lookup import CreationLookupClient
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.evidence import EvidenceError
from commerce_agent.governance.remote_creation import original_response_sha256
from commerce_agent.persistence.remote_creation_sqlite import (
    RemoteCreationVerifier,
    SqliteRemoteCreationStore,
)
from tests.integration.gov06dc_harness import (
    JointCase,
    LiveEnvironment,
    LookupProxy,
    clean_environment,
    connect,
    live_requirements,
)

pytestmark = live_requirements(
    "RUN_GOV06DC_REAL",
    "requires RUN_GOV06DC_REAL=1 and a new verified disposable PostgreSQL/Merchant cluster",
    windows_listener=True,
)


@pytest.fixture(scope="module")
def live():
    environment = LiveEnvironment()
    try:
        environment.start()
        yield environment
    finally:
        environment.close()


def test_real_found_committed_envelope_and_durable_history(live):
    case = JointCase.prepare(live)
    original = case.setup_create()
    assert original["capabilities"]["payment"]["handlers"][0]["id"] == "tesis_sandbox"
    assert original["fulfillment_options"][0]["type"] == "shipping"
    before = live.rpc("snapshot")
    started = time.perf_counter()
    response = case.raw_lookup()
    http_ms = (time.perf_counter() - started) * 1000
    assert response.status_code == 200
    remote = response.json()
    assert remote["original_response"] == original
    assert "sha256:" + remote["acp_create_fingerprint"] == case.envelope.body_digest
    assert remote["original_response_sha256"] == original_response_sha256(original)
    assert (
        remote["idempotency_key_sha256"]
        == hashlib.sha256(case.envelope.idempotency_key.get_secret_value().encode()).hexdigest()
    )
    assert remote["request_id"] == response.headers["Request-Id"]
    assert remote["request_id"] != "client-is-not-authority"
    assert live.rpc("snapshot") == before
    first = case.verify_zero()
    assert first.durable and first.event is not None
    assert first.verification.basis == "loopback_backend_session"
    assert first.verification.checkout_id == original["id"]
    assert (
        first.verification.original_response_digest
        == "sha256:" + remote["original_response_sha256"]
    )
    second = case.verify_zero()
    assert second == first
    live.record(
        "real_positive",
        http_lookup_ms=http_ms,
        original_response_digest=remote["original_response_sha256"],
        fingerprint=remote["acp_create_fingerprint"],
        remote_request_id=remote["request_id"],
        merchant_run=case.credential["run_id"],
        local_run=case.local_run,
    )


def child_probe(case, mode):
    client = case.client()
    try:
        scope = client._session.scope
        assert scope is not None
        private = dict(
            mode=mode,
            database=str(case.path),
            key_id=case.keys.key_id,
            hmac=case.keys.pseudonym_key.get_secret_value().hex(),
            aes=case.keys.encryption_key.get_secret_value().hex(),
            request=case.request().model_dump(mode="json"),
            origin=case.origin,
            scope=scope.model_dump(mode="json"),
            bearer=case.credential["bearer"],
        )
        result = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).with_name("gov06dc_agent_worker.py"))],
            input=json.dumps(private),
            text=True,
            capture_output=True,
            timeout=30,
            env=clean_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert not result.stderr, "Independent Agent probe failed (private diagnostic withheld)"
        if mode == "crash":
            assert result.returncode == 81
            return None
        assert result.returncode == 0
        return json.loads(result.stdout)
    finally:
        client.close()


@pytest.mark.parametrize(
    "mode",
    [
        "renewed",
        "nonexistent",
        "expired",
        "revoked",
        "other_run",
        "other_actor",
        "expected_absent",
        "misprovisioned",
        "wrong_local",
    ],
)
def test_real_authentication_and_binding_boundaries(live, mode):
    case = JointCase.prepare(live, binding=mode != "expected_absent")
    case.setup_create()
    credential = dict(case.credential)
    expected = "found_verified"
    req = case.request()
    if mode == "renewed":
        credential = live.rpc("issue", run_id=credential["run_id"], actor_id=credential["actor_id"])
        assert credential["session_sha256"] != case.credential["session_sha256"]
    elif mode == "nonexistent":
        credential["bearer"] = "gov06dc-deliberately-unissued"
        expected = "authentication_error"
    elif mode in {"expired", "revoked"}:
        live.rpc("session_state", state=mode, session_sha256=credential["session_sha256"])
        expected = "authentication_error"
    elif mode in {"other_run", "other_actor", "misprovisioned"}:
        episode = live.rpc(
            "episode",
            actor=(credential["actor_id"] if mode == "other_run" else "foreign:" + case.index),
        )
        foreign = live.rpc("issue", run_id=episode["run_id"], actor_id=episode["actor_id"])
        # Direct HTTP establishes the other legitimate session cannot retrieve this checkout.
        denied = case.raw_lookup(credential=foreign)
        assert denied.status_code == 200 and denied.json()["result"] == "not_found"
        if mode == "misprovisioned":
            credential["bearer"] = foreign["bearer"]  # Deliberately lying provisioning assertion.
            expected = "identity_conflict"
        else:
            credential = foreign
            expected = "unattributable"
    elif mode == "expected_absent":
        expected = "unattributable"
    elif mode == "wrong_local":
        req = req.model_copy(update={"run_id": "not-the-local-run"})
        expected = "local_invalid"
    if expected == "authentication_error":
        denied = case.raw_lookup(credential=credential)
        assert denied.status_code == 401 and denied.json()["code"] == "unauthenticated"
        assert denied.headers["Cache-Control"] == "no-store"
    case.verify_zero(credential=credential, request=req, expected=expected)


def test_not_found_before_deliberate_creation_then_found_without_replay(live):
    case = JointCase.prepare(live)
    first = case.verify_zero(expected="not_found")
    assert first.verification.reason == "NOT_FOUND_NOT_ABSENCE"
    case.setup_create()  # Exactly one fixture POST; not sent by recovery.
    second = case.verify_zero(request=case.request("query:after-create", 1))
    assert first.event and second.event and second.event.version == 2


@pytest.mark.parametrize("mode", ["unrecoverable", "pending"])
def test_real_internal_and_uncommitted_creations(live, mode):
    case = JointCase.prepare(live)
    values = dict(
        bearer=case.credential["bearer"],
        variant=case.envelope.body()["line_items"][0]["id"],
        key=case.envelope.idempotency_key.get_secret_value(),
        handle=case.index,
        body=case.envelope.body(),
    )
    live.rpc("internal_create" if mode == "unrecoverable" else "pending_create", **values)
    case.setup_creations = 1
    case.verify_zero(expected="unrecoverable" if mode == "unrecoverable" else "not_found")
    if mode == "pending":
        live.rpc("commit_pending", handle=case.index)
        result = case.verify_zero(request=case.request("query:after-commit", 1))
        assert result.verification.outcome == "found_verified"
    live.record(
        "setup_internal_" + mode,
        setup_transport="real Merchant internal transaction API; lookup uses independent HTTP",
    )


@pytest.mark.parametrize("change", ["catalog_changed", "canceled"])
def test_historical_creation_is_independent_of_catalog_and_current_status(live, change):
    case = JointCase.prepare(live)
    original = case.setup_create()
    if change == "catalog_changed":
        live.rpc(
            "alter_offer_fixture",
            run_id=case.episode["run_id"],
            variant=case.envelope.body()["line_items"][0]["id"],
        )
    else:
        result = live.rpc(
            "cancel_fixture",
            bearer=case.credential["bearer"],
            checkout_id=original["id"],
            key="fixture-cancel-" + case.index,
        )
        assert result["status"] == "canceled"
    raw = case.raw_lookup()
    assert raw.json()["original_response"] == original
    assert raw.json()["original_response"]["status"] == "not_ready_for_payment"
    case.verify_zero()
    live.record("historical_" + change, setup_change=change, verified_current_authority=False)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("scope", "identity_conflict"),
        ("digest", "response_invalid"),
        ("profile", "malformed_response"),
        ("malformed", "malformed_response"),
        ("delay", "timeout"),
        ("drop", "transport_error"),
    ],
)
def test_real_socket_proxy_faults_are_labelled_and_fail_closed(live, mode, expected):
    proxy = LookupProxy(live)
    try:
        case = JointCase.prepare(live, origin=proxy.origin)
        case.setup_create()
        proxy.allow_setup = False
        proxy.mode = mode
        result = case.verify_zero(expected=expected, timeout=0.15 if mode == "delay" else 5)
        assert result.durable
        assert proxy.requests == [
            ("POST", "/checkout_sessions"),
            ("POST", "/lab/recovery/checkout-creation/lookup"),
        ]
        assert len(proxy.upstream_ms) == 2  # The genuine lookup executed before the injected fault.
        live.record(
            "proxy_" + mode,
            method="real sockets + explicit proxy injection",
            genuine_upstream_lookup_executed=True,
            upstream_http_ms=proxy.upstream_ms[-1],
        )
    finally:
        proxy.close()


def test_merchant_unavailable_restart_and_explicit_readonly_recovery(live):
    case = JointCase.prepare(live)
    case.setup_create()
    old_pid = live.server_pid
    live.stop_server()
    try:
        first = case.verify_zero(expected="transport_error")
    finally:
        live.start_server()
    assert live.server_pid != old_pid
    assert (
        case.verify_zero(expected="transport_error") == first
    )  # Cached observation, not automatic HTTP.
    case.verify_zero(request=case.request("query:after-restart", 1))
    live.record("merchant_restart", old_pid=old_pid, new_pid=live.server_pid)


def test_real_503_when_isolated_postgres_is_stopped(live):
    case = JointCase.prepare(live)
    case.setup_create()
    before = live.rpc("snapshot")
    client = case.client()
    live.stop_pg()
    try:
        raw = case.raw_lookup()
        assert raw.status_code == 503 and raw.json()["code"] == "lookup_unavailable"
        result = case.verifier(client).verify(case.request())
        assert result.durable and result.verification.reason == "LOOKUP_UNAVAILABLE"
    finally:
        client.close()
        live.start_pg()
    assert live.rpc("snapshot") == before
    case.verify_zero(request=case.request("query:pg-restarted", 1))
    live.record("real_503_pg_restart", observed_http=503, public_tables_unchanged=True)


@pytest.mark.parametrize("same_query", [False, True])
def test_real_concurrent_verifiers_use_cas_and_idempotent_inspection(live, same_query):
    proxy = LookupProxy(live)
    clients = []
    try:
        case = JointCase.prepare(live, origin=proxy.origin)
        case.setup_create()
        proxy.allow_setup = False
        barrier = Barrier(2)
        proxy.callback = lambda: barrier.wait(timeout=20)
        before = live.rpc("snapshot")

        def verify(index):
            client = case.client()
            clients.append(client)
            req = case.request("query:same" if same_query else f"query:{index}")
            return case.verifier(client).verify(req)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(verify, [1, 2]))
        assert sorted(x.verification.outcome for x in results) == ["cas_conflict", "found_verified"]
        winner = next(x for x in results if x.durable)
        assert winner.event
        proxy.callback = None
        verifier = case.verifier(clients[0])
        assert verifier.inspect(case.request()) == (winner.event,)
        assert verifier.verify(case.request(winner.event.query_id)) == winner
        assert len(proxy.requests) == 3
        assert live.rpc("snapshot") == before
        live.record(
            "concurrency_same" if same_query else "concurrency_distinct",
            winners=1,
            cas_conflicts=1,
            genuine_http_lookups=2,
            stored_events=1,
            commercial_mutations=0,
        )
    finally:
        for client in clients:
            client.close()
        proxy.close()


def test_two_real_sessions_and_revocation_between_requests(live):
    case = JointCase.prepare(live)
    case.setup_create()
    renewed = live.rpc(
        "issue", run_id=case.credential["run_id"], actor_id=case.credential["actor_id"]
    )
    before = live.rpc("snapshot")
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(lambda c: case.raw_lookup(credential=c), [case.credential, renewed])
        )
    assert all(
        r.status_code == 200 and r.json()["original_response"] == case.original for r in responses
    )
    assert len({r.json()["request_id"] for r in responses}) == 2
    assert live.rpc("snapshot") == before
    case.verify_zero()
    live.rpc("session_state", state="revoked", session_sha256=case.credential["session_sha256"])
    case.verify_zero(expected="authentication_error", request=case.request("query:revoked", 1))
    case.verify_zero(credential=renewed, request=case.request("query:renewed", 2))


@pytest.mark.parametrize("damage", ["envelope", "old_event"])
def test_sqlite_corruption_blocks_http_without_touching_merchant(live, damage):
    proxy = LookupProxy(live)
    try:
        case = JointCase.prepare(live, origin=proxy.origin)
        case.setup_create()
        proxy.allow_setup = False
        if damage == "old_event":
            case.verify_zero()
        with closing(connect(case.path)) as connection, connection:
            if damage == "envelope":
                connection.execute("DROP TRIGGER d1_envelope_no_update")
                connection.execute(
                    "UPDATE d1_checkout_envelopes SET canonical_envelope=?", (b"{}",)
                )
            else:
                connection.execute("DROP TRIGGER d1_remote_creation_no_update")
                connection.execute("UPDATE d1_remote_creation_events SET body=?", (b"{}",))
        count = len(proxy.requests)
        case.verify_zero(
            expected="local_invalid",
            request=case.request("query:corrupt", 1 if damage == "old_event" else 0),
        )
        assert len(proxy.requests) == count
        client = case.client()
        try:
            with pytest.raises(EvidenceError):
                case.verifier(client).inspect(case.request())
        finally:
            client.close()
    finally:
        proxy.close()


def test_local_purchase_changed_during_real_lookup_rejects_stale_result(live):
    proxy = LookupProxy(live)
    try:
        case = JointCase.prepare(live, origin=proxy.origin)
        case.setup_create()
        proxy.allow_setup = False

        def change():
            with closing(connect(case.path)) as connection, connection:
                connection.execute(
                    "UPDATE purchases SET stage='uncertain',state_version=state_version+1"
                )

        proxy.callback = change
        result = case.verify_zero(expected="cas_conflict")
        assert not result.durable and len(proxy.requests) == 2
    finally:
        proxy.close()


def test_real_agent_process_crash_commit_failure_and_restart(live):
    proxy = LookupProxy(live)
    try:
        case = JointCase.prepare(live, origin=proxy.origin)
        case.setup_create()
        proxy.allow_setup = False
        before = live.rpc("snapshot")
        child_probe(case, "crash")
        assert len(proxy.requests) == 2
        assert child_probe(case, "inspect") == {"count": 0, "digests": []}
        assert len(proxy.requests) == 2

        class FailedCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("controlled commit failure")

        def factory():
            connection = sqlite3.connect(case.path, factory=FailedCommit)
            connection.execute("PRAGMA foreign_keys=ON")
            return connection

        failed = case.verify_zero(expected="persistence_error", factory=factory)
        assert not failed.durable
        inspected = child_probe(case, "inspect")
        assert inspected is not None and inspected["count"] == 0
        recovered = case.verify_zero()
        assert recovered.event
        durable = child_probe(case, "inspect")
        assert durable == {"count": 1, "digests": [sha256_digest(canonical_bytes(recovered.event))]}
        assert len(proxy.requests) == 4
        assert live.rpc("snapshot") == before
        live.record(
            "real_agent_crash_restart",
            crash_exit=81,
            uncommitted_events=0,
            recovered_events=1,
            commercial_mutations=0,
            new_checkouts=0,
        )
    finally:
        proxy.close()


def test_measured_real_lookup_append_and_commit_exclude_bootstrap(live):
    case = JointCase.prepare(live)
    case.setup_create()
    phases = {}

    class TimedClient(CreationLookupClient):
        def lookup(self, envelope, known_checkout_id):
            start = time.perf_counter()
            try:
                return super().lookup(envelope, known_checkout_id)
            finally:
                phases["lookup_http_and_wire_verification_ms"] = (
                    time.perf_counter() - start
                ) * 1000

    class TimedConnection(sqlite3.Connection):
        def commit(self):
            start = time.perf_counter()
            super().commit()
            phases["sqlite_commit_ms"] = (time.perf_counter() - start) * 1000

    class TimedStore(SqliteRemoteCreationStore):
        def append(self, request, local, verification):
            start = time.perf_counter()
            try:
                return super().append(request, local, verification)
            finally:
                phases["sqlite_revalidation_append_ms"] = (time.perf_counter() - start) * 1000

    class TimedVerifier(RemoteCreationVerifier):
        def _store(self, connection):
            return TimedStore(connection, self.keys, clock=self.clock)

    def factory():
        connection = sqlite3.connect(case.path, factory=TimedConnection)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    plain = case.client()
    client = TimedClient(plain._session)
    plain.close()
    before = live.rpc("snapshot")
    try:
        start = time.perf_counter()
        result = TimedVerifier(
            factory,
            case.keys,
            client,
            clock=lambda: datetime.now(UTC),
        ).verify(case.request())
        phases["verifier_total_ms"] = (time.perf_counter() - start) * 1000
        assert result.durable and result.verification.outcome == "found_verified"
        assert live.rpc("snapshot") == before
        assert all(value >= 0 for value in phases.values())
        live.record(
            "measured_real_phases",
            phases_ms=phases,
            bootstrap_and_seed_excluded=True,
            new_checkouts=0,
            commercial_mutations=0,
        )
    finally:
        client.close()


def test_merchant_regressions_run_sequentially_on_the_verified_cluster(live):
    # No other suite or fixture is mutating this PostgreSQL while this child runs.
    child = live.spawn("regressions")
    assert child.wait(timeout=300) == 0, "See sanitized merchant-validation.json counts/node IDs"
    summary = json.loads((live.root / "merchant-validation.json").read_text(encoding="utf-8"))
    assert summary["failed"] == 0 and summary["skipped"] == 0
    assert summary["passed"] == 105
    live.record("merchant_regressions", **summary)
