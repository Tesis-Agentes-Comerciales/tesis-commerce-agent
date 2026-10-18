"""07A offline contracts. Transport responses below are synthetic, not Merchant evidence."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from commerce_agent.acp.creation_lookup import BackendLookupSession
from commerce_agent.acp.initial_dispatch import HttpInitialCheckoutDispatch
from commerce_agent.application import PurchaseServiceError
from commerce_agent.governance.dispatch_envelope import ExpectedRemoteBinding
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.models import ObjectRef
from commerce_agent.governance.provenance import ClockReader
from commerce_agent.governance.remote_creation import SessionScope
from commerce_agent.persistence.d1_envelope_sqlite import SqliteD1EnvelopeStore
from commerce_agent.persistence.post_create_sqlite import SqlitePostCreateStore
from tests.governance.test_enforce_integration import OWNER, connect, make_case, start
from tests.governance.test_remote_creation import merchant_original_response

SOURCE = ObjectRef(
    schema_version="commerce-policy/1.0",
    id="provision:test",
    version=1,
    digest="sha256:" + "1" * 64,
)
SCOPE = SessionScope(
    assurance="backend_session_registry/1",
    remote_run_id="12345678-1234-4234-8234-123456789012",
    remote_actor_id="actor-test",
    source_ref=SOURCE,
)
EXPECTED = ExpectedRemoteBinding(
    assurance="backend_expected_not_authenticated",
    remote_run_id=SCOPE.remote_run_id,
    remote_actor_id=SCOPE.remote_actor_id,
    source_ref=SOURCE,
)
SESSION = BackendLookupSession("http://127.0.0.1:4120", SCOPE, SecretStr("synthetic-token"))


def stage(case):
    row = case.store.purchase("run-a")
    assert row is not None
    return row["stage"]


def initial_case(tmp_path, *, fault=None, session=SESSION, deny=False):
    case = make_case(tmp_path / "agent.sqlite", blocked=True, deny=deny)
    calls = []

    def handler(request):
        # Independent connection proves durable claim/envelope before HTTP.
        with closing(connect(case.path)) as db:
            row = db.execute("SELECT operation_id,state FROM d1_checkout_operations").fetchone()
            assert row[1] == "claimed"
            envelope = SqliteD1EnvelopeStore(db, case.keys).recover(
                row[0], run_id="run-a", owner_id=OWNER
            )
            assert envelope is not None
            assert request.content == envelope.body_canonical.encode()
            assert request.headers["Idempotency-Key"] == envelope.idempotency_key.get_secret_value()
        calls.append((request.method, request.url.path))
        if fault == "timeout":
            raise httpx.ReadTimeout("synthetic fault")
        if fault == "crash":
            raise SystemExit(79)
        payload = merchant_original_response()
        if fault == "identity":
            payload["line_items"][0]["item"]["id"] = "foreign"
        if fault == "status":
            payload["status"] = "ready_for_payment"
        if fault == "quantity":
            payload["line_items"][0]["quantity"] = 2
        if fault == "currency":
            payload["currency"] = "usd"
        headers = {
            "Request-Id": "server-request",
            "Idempotency-Key": request.headers["Idempotency-Key"],
        }
        if fault == "key":
            headers["Idempotency-Key"] = "wrong"
        if fault == "redirect":
            return httpx.Response(307, headers={"location": "http://127.0.0.1:4120/other"})
        if fault == "malformed":
            return httpx.Response(201, headers=headers, text="not-json")
        if fault == "duplicate":
            return httpx.Response(
                201,
                headers={**headers, "Content-Type": "application/json"},
                content=b'{"id":"x","id":"y"}',
            )
        return httpx.Response(201, headers=headers, json=payload)

    port = HttpInitialCheckoutDispatch(session, transport=httpx.MockTransport(handler))
    old = case.gate
    gate = SqliteD1EnforceGate(
        case.path,
        old._configuration,
        case.keys,
        ClockReader(old._configuration.clock),
        initial_dispatch=port,
        expected_remote_binding=EXPECTED,
        policy_bytes=old._policy,
        policy_binding=old._binding,
    )
    case = replace(case, gate=gate)
    case.service._d1_gate = gate
    return case, port, calls


def test_initial_creation_stops_before_every_post_create_effect(tmp_path):
    case, port, calls = initial_case(tmp_path)
    try:
        with pytest.raises(PurchaseServiceError) as error:
            start(case)
        assert error.value.code == "d1_initial_creation_observed"
        assert calls == [("POST", "/checkout_sessions")]
        assert case.gateway.calls == []
        history = case.gate.post_create_flow("run-a", OWNER).inspect()
        assert [e.phase for e in history] == ["creation_observed"]
        assert history[0].checkout_id == "cs_1"
        assert not history[0].replay_allowed and not history[0].continuation_allowed
        assert stage(case) == "selected"
        with pytest.raises(PurchaseServiceError) as repeated:
            start(case)
        assert repeated.value.code == "d1_recovery_required"
        assert len(calls) == 1
    finally:
        port.close()


@pytest.mark.parametrize(
    "fault",
    [
        "timeout",
        "identity",
        "status",
        "quantity",
        "currency",
        "key",
        "redirect",
        "malformed",
        "duplicate",
    ],
)
def test_uncertainty_never_triggers_second_post(tmp_path, fault):
    case, port, calls = initial_case(tmp_path, fault=fault)
    try:
        with pytest.raises(PurchaseServiceError) as error:
            start(case)
        assert error.value.code == "d1_dispatch_uncertain"
        assert stage(case) == "uncertain"
        with pytest.raises(PurchaseServiceError):
            start(case)
        assert len(calls) == 1 and case.gateway.calls == []
    finally:
        port.close()


@pytest.mark.parametrize(
    "session",
    [
        replace(SESSION, bearer=None),
        replace(SESSION, scope=None),
        replace(SESSION, merchant_base_url="http://127.0.0.1:4121"),
        replace(SESSION, scope=SCOPE.model_copy(update={"remote_actor_id": "other"})),
    ],
)
def test_missing_or_contradictory_session_blocks_before_claim(tmp_path, session):
    case, port, calls = initial_case(tmp_path, session=session)
    try:
        with pytest.raises(PurchaseServiceError) as error:
            start(case)
        assert error.value.code == "d1_unavailable"
        assert not calls
        status = case.gate.recovery_status("run-a")
        assert status is not None and status[0] == "reserved"
    finally:
        port.close()


def test_deny_never_reaches_transport(tmp_path):
    case, port, calls = initial_case(tmp_path, deny=True)
    try:
        with pytest.raises(PurchaseServiceError) as error:
            start(case)
        assert error.value.code == "d1_denied" and not calls
    finally:
        port.close()


def test_crash_after_claim_is_not_permission_to_resend(tmp_path):
    case, port, calls = initial_case(tmp_path, fault="crash")
    try:
        with pytest.raises(SystemExit):
            start(case)
        status = case.gate.recovery_status("run-a")
        assert status is not None and status[0] == "uncertain"
        with pytest.raises(PurchaseServiceError):
            start(case)
        assert len(calls) == 1
    finally:
        port.close()


def test_failed_creation_observation_preserves_uncertainty(tmp_path, monkeypatch):
    case, port, calls = initial_case(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("synthetic commit failure")

    monkeypatch.setattr(SqlitePostCreateStore, "append", fail)
    try:
        with pytest.raises(PurchaseServiceError) as error:
            start(case)
        assert error.value.code == "d1_dispatch_uncertain"
        assert stage(case) == "uncertain"
        assert len(calls) == 1
        with pytest.raises(PurchaseServiceError):
            start(case)
        assert len(calls) == 1
    finally:
        port.close()


def test_concurrent_start_admits_only_one_initial_post(tmp_path):
    case, port, calls = initial_case(tmp_path)

    def attempt():
        try:
            start(case)
        except PurchaseServiceError as error:
            return error.code
        return "unexpected"

    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        assert "d1_initial_creation_observed" in results
        assert len(calls) == 1 and case.gateway.calls == []
    finally:
        port.close()


def test_model_bypass_body_or_digest_rejected_before_network(tmp_path):
    case, port, calls = initial_case(tmp_path)
    try:
        # A blocked reservation supplies a real envelope with valid historical D1.
        case.gate._initial_dispatch = None
        with pytest.raises(PurchaseServiceError):
            start(case)
        with closing(connect(case.path)) as db:
            op = db.execute("SELECT operation_id FROM d1_checkout_operations").fetchone()[0]
            envelope = SqliteD1EnvelopeStore(db, case.keys).recover(
                op, run_id="run-a", owner_id=OWNER
            )
        assert envelope is not None
        for change in [
            {"body_digest": "sha256:" + "0" * 64},
            {"body_canonical": json.dumps(json.loads(envelope.body_canonical))},
            {"path": "/other"},
            {"binding_status": "unavailable"},
        ]:
            with pytest.raises((ValidationError, ValueError)):
                port.create_once(envelope.model_copy(update=change))
        assert not calls
    finally:
        port.close()
