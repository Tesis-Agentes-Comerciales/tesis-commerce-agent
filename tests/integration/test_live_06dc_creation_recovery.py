"""06D-C live: read-only recovery of a creation the Agent never saw answered.

Real PostgreSQL (Docker), the Merchant process over loopback HTTP and the real Agent
verifier. Faults come from FaultProxy, Docker and killed Agent processes only.
"""

import signal
import sqlite3
import subprocess
import time
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from threading import Barrier

import pytest

from commerce_agent.acp.creation_lookup import CreationLookupClient
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.evidence import EvidenceError
from commerce_agent.governance.remote_creation import LOOKUP_PATH, original_response_sha256
from commerce_agent.persistence.remote_creation_sqlite import (
    RemoteCreationVerifier,
    SqliteRemoteCreationStore,
)
from commerce_evaluation.stack.environment import clean_environment, connect, uv_run
from commerce_evaluation.stack.fault_proxy import CREATE_PATH
from commerce_evaluation.stack.joint_case import JointCase
from commerce_evaluation.stack.postgres import POSTGRES_DATA

# The Merchant's own creation/lookup regressions, run on the cluster that served the Agent.
MERCHANT_REGRESSIONS = (
    "tests/test_acp_creation.py",
    "tests/integration/test_acp_creation_recovery.py",
    "tests/test_creation_lookup.py",
    "tests/integration/test_acp_creation_lookup.py",
)


def test_06dc_real_found_committed_envelope_and_durable_history(stack, proxy):
    case = JointCase.prepare(stack)
    original = case.setup_create()
    assert original["capabilities"]["payment"]["handlers"][0]["id"] == "tesis_sandbox"
    assert original["fulfillment_options"][0]["type"] == "shipping"
    before = stack.rpc("snapshot")
    started = time.perf_counter()
    response = case.raw_lookup()
    http_ms = (time.perf_counter() - started) * 1000
    assert response.status_code == 200
    remote = response.json()
    assert remote["original_response"] == original
    assert "sha256:" + remote["acp_create_fingerprint"] == case.envelope.body_digest
    assert remote["original_response_sha256"] == original_response_sha256(original)
    assert remote["idempotency_key_sha256"] == case.key_sha256()
    assert remote["request_id"] == response.headers["Request-Id"]
    assert remote["request_id"] != "client-is-not-authority"
    assert stack.rpc("snapshot") == before
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
    # The repeated query is answered from durable history, without HTTP.
    assert proxy.requests() == [("POST", CREATE_PATH)] + [("POST", LOOKUP_PATH)] * 2
    stack.record(
        "06dc:real_positive",
        http_lookup_ms=http_ms,
        original_response_digest=remote["original_response_sha256"],
        fingerprint=remote["acp_create_fingerprint"],
    )


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
def test_06dc_real_authentication_and_binding_boundaries(stack, proxy, mode):
    case = JointCase.prepare(stack, binding=mode != "expected_absent")
    case.setup_create()
    credential = dict(case.credential)
    expected = "found_verified"
    request = case.request()
    if mode == "renewed":
        credential = stack.rpc(
            "session", run_id=credential["run_id"], actor_id=credential["actor_id"]
        )
        assert credential["session_sha256"] != case.credential["session_sha256"]
    elif mode == "nonexistent":
        credential["bearer"] = "live-deliberately-unissued"
        expected = "authentication_error"
    elif mode in {"expired", "revoked"}:
        stack.rpc("session_state", state=mode, session_sha256=credential["session_sha256"])
        expected = "authentication_error"
    elif mode in {"other_run", "other_actor", "misprovisioned"}:
        episode = stack.rpc(
            "episode",
            actor=(credential["actor_id"] if mode == "other_run" else "foreign:" + case.index),
        )
        foreign = stack.rpc("session", run_id=episode["run_id"], actor_id=episode["actor_id"])
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
        request = request.model_copy(update={"run_id": "not-the-local-run"})
        expected = "local_invalid"
    if expected == "authentication_error":
        denied = case.raw_lookup(credential=credential)
        assert denied.status_code == 401 and denied.json()["code"] == "unauthenticated"
        assert denied.headers["Cache-Control"] == "no-store"
    case.verify_zero(credential=credential, request=request, expected=expected)


def test_06dc_not_found_before_deliberate_creation_then_found_without_replay(stack, proxy):
    case = JointCase.prepare(stack)
    first = case.verify_zero(expected="not_found")
    assert first.verification.reason == "NOT_FOUND_NOT_ABSENCE"
    case.setup_create()  # Exactly one fixture POST; not sent by recovery.
    second = case.verify_zero(request=case.request("query:after-create", 1))
    assert first.event and second.event and second.event.version == 2
    assert proxy.count("POST", CREATE_PATH) == 1


@pytest.mark.parametrize("mode", ["unrecoverable", "pending"])
def test_06dc_real_internal_and_uncommitted_creations(stack, proxy, mode):
    case = JointCase.prepare(stack)
    stack.rpc(
        "internal_create",
        bearer=case.credential["bearer"],
        variant=case.envelope.body()["line_items"][0]["id"],
        key=case.envelope.idempotency_key.get_secret_value(),
        handle=case.index,
        body=case.envelope.body(),
        pending=mode == "pending",
    )
    case.setup_creations = 1
    case.verify_zero(expected="unrecoverable" if mode == "unrecoverable" else "not_found")
    if mode == "pending":
        stack.rpc("commit_pending", handle=case.index)
        result = case.verify_zero(request=case.request("query:after-commit", 1))
        assert result.verification.outcome == "found_verified"
    assert proxy.count("POST", CREATE_PATH) == 0


@pytest.mark.parametrize("change", ["catalog_changed", "canceled"])
def test_06dc_historical_creation_is_independent_of_catalog_and_current_status(
    stack, proxy, change
):
    case = JointCase.prepare(stack)
    original = case.setup_create()
    if change == "catalog_changed":
        stack.rpc(
            "alter_offer_fixture",
            run_id=case.episode["run_id"],
            variant=case.envelope.body()["line_items"][0]["id"],
        )
    else:
        result = stack.rpc(
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
    stack.record("06dc:historical_" + change, verified_current_authority=False)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("scope", "identity_conflict"),
        ("digest", "response_invalid"),
        ("profile", "malformed_response"),
        ("malformed", "malformed_response"),
        ("delay", "timeout"),
        ("drop", "transport_error"),
        ("reset", "transport_error"),
        ("status_503", "malformed_response"),
    ],
)
def test_06dc_real_socket_proxy_faults_are_labelled_and_fail_closed(stack, proxy, mode, expected):
    case = JointCase.prepare(stack)
    case.setup_create()
    proxy.faults[LOOKUP_PATH] = mode
    proxy.delay_seconds = 0.6
    result = case.verify_zero(expected=expected, timeout=0.15 if mode == "delay" else 5)
    assert result.durable
    assert proxy.requests() == [("POST", CREATE_PATH), ("POST", LOOKUP_PATH)]
    # The genuine lookup ran before every fault except the proxy's own 503.
    assert proxy.calls[-1].answered.wait(5) is (mode != "status_503")
    stack.record("06dc:proxy_" + mode, genuine_lookup=mode != "status_503")


def test_06dc_merchant_unavailable_restart_and_explicit_readonly_recovery(stack, proxy):
    case = JointCase.prepare(stack)
    case.setup_create()
    old_pid = stack.server.process.pid
    stack.server.stop()
    try:
        first = case.verify_zero(expected="transport_error")
    finally:
        stack.server.start()
    assert stack.server.process.pid != old_pid
    # Cached observation, not automatic HTTP.
    assert case.verify_zero(expected="transport_error") == first
    case.verify_zero(request=case.request("query:after-restart", 1))
    assert [call.fault for call in proxy.calls] == ["pass", "merchant_unreachable", "pass"]
    stack.record("06dc:merchant_restart", old_pid=old_pid, new_pid=stack.server.process.pid)


def test_06dc_real_503_when_isolated_postgres_is_stopped(stack, proxy):
    case = JointCase.prepare(stack)
    case.setup_create()
    before = stack.rpc("snapshot")
    client = case.client()
    stack.postgres.stop()
    try:
        raw = case.raw_lookup()
        assert raw.status_code == 503 and raw.json()["code"] == "lookup_unavailable"
        result = case.verifier(client).verify(case.request())
        assert result.durable and result.verification.reason == "LOOKUP_UNAVAILABLE"
    finally:
        client.close()
        stack.postgres.restart()
    assert stack.rpc("snapshot") == before
    case.verify_zero(request=case.request("query:pg-restarted", 1))
    stack.record("06dc:real_503_pg_restart", observed_http=503, public_tables_unchanged=True)


@pytest.mark.parametrize("same_query", [False, True])
def test_06dc_real_concurrent_verifiers_use_cas_and_idempotent_inspection(stack, proxy, same_query):
    clients: list[CreationLookupClient] = []
    try:
        case = JointCase.prepare(stack)
        case.setup_create()
        barrier = Barrier(2)
        proxy.after_answer[LOOKUP_PATH] = lambda: barrier.wait(timeout=20)
        before = stack.rpc("snapshot")

        def verify(index):
            client = case.client()
            clients.append(client)
            request = case.request("query:same" if same_query else f"query:{index}")
            return case.verifier(client).verify(request)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(verify, [1, 2]))
        assert sorted(x.verification.outcome for x in results) == ["cas_conflict", "found_verified"]
        winner = next(x for x in results if x.durable)
        assert winner.event
        proxy.after_answer.clear()
        verifier = case.verifier(clients[0])
        assert verifier.inspect(case.request()) == (winner.event,)
        assert verifier.verify(case.request(winner.event.query_id)) == winner
        assert len(proxy.calls) == 3
        assert stack.rpc("snapshot") == before
        stack.record(
            "06dc:concurrency_same" if same_query else "06dc:concurrency_distinct",
            winners=1,
            cas_conflicts=1,
            genuine_http_lookups=2,
        )
    finally:
        for client in clients:
            client.close()


def test_06dc_two_real_sessions_and_revocation_between_requests(stack, proxy):
    case = JointCase.prepare(stack)
    case.setup_create()
    renewed = stack.rpc(
        "session", run_id=case.credential["run_id"], actor_id=case.credential["actor_id"]
    )
    before = stack.rpc("snapshot")
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(lambda c: case.raw_lookup(credential=c), [case.credential, renewed])
        )
    assert all(
        r.status_code == 200 and r.json()["original_response"] == case.original for r in responses
    )
    assert len({r.json()["request_id"] for r in responses}) == 2
    assert stack.rpc("snapshot") == before
    case.verify_zero()
    stack.rpc("session_state", state="revoked", session_sha256=case.credential["session_sha256"])
    case.verify_zero(expected="authentication_error", request=case.request("query:revoked", 1))
    case.verify_zero(credential=renewed, request=case.request("query:renewed", 2))


@pytest.mark.parametrize("damage", ["envelope", "old_event"])
def test_06dc_sqlite_corruption_blocks_http_without_touching_merchant(stack, proxy, damage):
    case = JointCase.prepare(stack)
    case.setup_create()
    if damage == "old_event":
        case.verify_zero()
    with closing(connect(case.path)) as connection, connection:
        if damage == "envelope":
            connection.execute("DROP TRIGGER d1_envelope_no_update")
            connection.execute("UPDATE d1_checkout_envelopes SET canonical_envelope=?", (b"{}",))
        else:
            connection.execute("DROP TRIGGER d1_remote_creation_no_update")
            connection.execute("UPDATE d1_remote_creation_events SET body=?", (b"{}",))
    count = len(proxy.calls)
    case.verify_zero(
        expected="local_invalid",
        request=case.request("query:corrupt", 1 if damage == "old_event" else 0),
    )
    assert len(proxy.calls) == count
    client = case.client()
    try:
        with pytest.raises(EvidenceError):
            case.verifier(client).inspect(case.request())
    finally:
        client.close()


def test_06dc_local_purchase_changed_during_real_lookup_rejects_stale_result(stack, proxy):
    case = JointCase.prepare(stack)
    case.setup_create()

    def change():
        with closing(connect(case.path)) as connection, connection:
            connection.execute(
                "UPDATE purchases SET stage='uncertain',state_version=state_version+1"
            )

    proxy.after_answer[LOOKUP_PATH] = change
    result = case.verify_zero(expected="cas_conflict")
    assert not result.durable and len(proxy.calls) == 2


def test_06dc_real_agent_process_crash_commit_failure_and_restart(stack, proxy):
    case = JointCase.prepare(stack)
    case.setup_create()
    before = stack.rpc("snapshot")
    # Killed at the verifier's SQLite commit, after a real lookup was validated.
    case.probe(crash=True)
    assert len(proxy.calls) == 2
    assert case.probe() == {"count": 0, "digests": []}
    assert len(proxy.calls) == 2

    class FailedCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("controlled commit failure")

    def factory():
        connection = sqlite3.connect(case.path, factory=FailedCommit)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    failed = case.verify_zero(expected="persistence_error", factory=factory)
    assert not failed.durable
    inspected = case.probe()
    assert inspected is not None and inspected["count"] == 0
    recovered = case.verify_zero()
    assert recovered.event
    durable = case.probe()
    assert durable == {"count": 1, "digests": [sha256_digest(canonical_bytes(recovered.event))]}
    assert len(proxy.calls) == 4
    assert stack.rpc("snapshot") == before
    stack.record(
        "06dc:real_agent_crash_restart",
        crash_signal=signal.SIGKILL.name,
        uncommitted_events=0,
        recovered_events=1,
        new_checkouts=0,
    )


def test_06dc_measured_real_lookup_append_and_commit_exclude_bootstrap(stack, proxy):
    case = JointCase.prepare(stack)
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
    before = stack.rpc("snapshot")
    try:
        start = time.perf_counter()
        result = TimedVerifier(factory, case.keys, client, clock=lambda: datetime.now(UTC)).verify(
            case.request()
        )
        phases["verifier_total_ms"] = (time.perf_counter() - start) * 1000
        assert result.durable and result.verification.outcome == "found_verified"
        assert stack.rpc("snapshot") == before
        assert all(value >= 0 for value in phases.values())
        stack.record("06dc:measured_real_phases", phases_ms=phases, bootstrap_excluded=True)
    finally:
        client.close()


@pytest.mark.slow
def test_06dc_merchant_regressions_run_sequentially_on_the_live_cluster(stack, tmp_path):
    # Nothing else touches this PostgreSQL while the Merchant's own suites run.
    report = tmp_path / "merchant-regressions.xml"
    env = clean_environment()
    env.update(
        DATABASE_URL=stack.postgres.dsn,
        RUN_DB_INTEGRATION="1",
        RECOVERY_TEST_CLUSTER=POSTGRES_DATA,
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    with (tmp_path / "merchant-regressions.log").open("wb") as log:
        code = subprocess.run(
            [
                *uv_run(stack.merchant),
                "pytest",
                *MERCHANT_REGRESSIONS,
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp=" + str(tmp_path / "merchant-pytest"),
                "--junitxml=" + str(report),
            ],
            cwd=stack.merchant,
            env=env,
            stdout=log,
            stderr=log,
            timeout=600,
        ).returncode
    suite = ElementTree.parse(report).getroot().find("testsuite")
    assert suite is not None
    summary = {key: int(suite.get(key, "0")) for key in ("tests", "failures", "errors", "skipped")}
    assert code == 0, summary
    assert summary["tests"] > 0
    assert summary["failures"] == summary["errors"] == summary["skipped"] == 0
    stack.record("06dc:merchant_regressions", **summary)
