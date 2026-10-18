"""Durable local intent; all transports are test doubles, never a live Merchant."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from commerce_agent.acp.client import ACPClient, ACPClientError
from commerce_agent.acp.creation import build_checkout_create
from commerce_agent.application import PurchaseServiceError
from commerce_agent.governance.canonical import canonical_bytes, parse_model
from commerce_agent.governance.dispatch_envelope import (
    CheckoutCreationEnvelope,
    ExpectedRemoteBinding,
    creation_body,
    merchant_creation_digest,
)
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.evidence import DecisionRecord, EvidenceError
from commerce_agent.governance.models import ObjectRef
from commerce_agent.persistence.d1_envelope_sqlite import SqliteD1EnvelopeStore
from tests.governance.test_enforce_integration import (
    OWNER,
    SingleAttemptSpy,
    connect,
    make_case,
    records,
    start,
)


def recover(case):
    with closing(connect(case.path)) as connection:
        operation = connection.execute(
            "SELECT operation_id FROM d1_checkout_operations"
        ).fetchone()[0]
        value = SqliteD1EnvelopeStore(connection, case.keys).recover(
            operation, run_id="run-a", owner_id=OWNER
        )
        assert value is not None and not connection.in_transaction
        return value


@pytest.fixture
def reserved(tmp_path: Path):
    case = make_case(tmp_path / "reserved.db", blocked=True)
    with pytest.raises(PurchaseServiceError) as error:
        start(case)
    assert error.value.code == "d1_dispatch_blocked" and not case.gateway.calls
    return case


def changed(envelope, **values):
    return parse_model(
        CheckoutCreationEnvelope,
        canonical_bytes({**json.loads(envelope.storage_bytes()), **values}),
    )


def binding(actor="remote-expected-actor"):
    return ExpectedRemoteBinding(
        assurance="backend_expected_not_authenticated",
        remote_run_id="96c06621-151f-4246-8c59-263c07062b50",
        remote_actor_id=actor,
        source_ref=ObjectRef(
            schema_version="commerce-policy/1.0",
            id="backend:synthetic-session-issuance",
            version=1,
            digest="sha256:" + "1" * 64,
        ),
    )


def test_envelope_is_committed_before_dispatch_and_identical_after_reopen(tmp_path: Path):
    observed = []

    class Dispatch(SingleAttemptSpy):
        def create_once(self, gateway, **kwargs):
            with closing(connect(case.path)) as connection:
                operation, state = connection.execute(
                    "SELECT operation_id,state FROM d1_checkout_operations"
                ).fetchone()
                assert state == "claimed"
                envelope = SqliteD1EnvelopeStore(connection, case.keys).recover(
                    operation, run_id="run-a", owner_id=OWNER
                )
                assert envelope is not None and not connection.in_transaction
                assert len(records(case)) == 1
                observed.append(envelope.storage_bytes())
            return super().create_once(gateway, **kwargs)

    case = make_case(tmp_path / "agent.db", dispatch=Dispatch())
    assert start(case).stage == "awaiting_confirmation"
    first = recover(case)
    reopened = recover(case)
    assert first.storage_bytes() == reopened.storage_bytes() == observed[0]
    assert first.digest() == reopened.digest()
    assert first.body_digest != first.d1_input_digest
    assert first.body_canonical.encode() == canonical_bytes(build_checkout_create("SON-01", "COP"))
    assert first.expected_remote_binding is None and first.binding_status == "unavailable"


def test_wire_constructor_and_merchant_fingerprint_match_offline(reserved):
    envelope = recover(reserved)
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("synthetic lost response", request=request)

    with (
        ACPClient(
            envelope.merchant_origin,
            SecretStr("synthetic-authentication-marker"),
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ACPClientError),
    ):
        client.create_checkout("SON-01", currency="COP", idempotency_key="synthetic-key")
    raw = json.loads(requests[0].content)
    assert canonical_bytes(raw).decode() == envelope.body_canonical
    # Independent expression taken from Merchant 71f9dba; no Merchant import/network.
    expected = hashlib.sha256(
        b"merchant-acp-create/v1\0"
        + json.dumps(
            {"api_version": "2026-04-17", "profile": "merchant-p0-create/1", "request": raw},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    assert envelope.body_digest == "sha256:" + expected
    assert "synthetic-authentication-marker" not in envelope.storage_bytes().decode()


def test_omitted_and_explicit_optional_fields_have_different_identity(reserved):
    original = recover(reserved)
    omitted = {"line_items": [{"id": "SON-01"}], "currency": "cop", "capabilities": {}}
    body = canonical_bytes(omitted)
    other = changed(
        original, body_canonical=body.decode(), body_digest=merchant_creation_digest(body)
    )
    assert original.body_digest != other.body_digest and original.digest() != other.digest()
    assert other.body()["capabilities"] == {}
    with closing(connect(reserved.path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(EvidenceError):
            SqliteD1EnvelopeStore(connection, reserved.keys).persist(other)


@pytest.mark.parametrize(
    "field,value",
    [
        ("idempotency_key", "another-key"),
        ("merchant_origin", "http://127.0.0.1:4999"),
        ("merchant_ref", "merchant:other"),
        ("owner_id", "other-owner"),
        ("run_id", "other-run"),
        ("decision_id", "decision:other"),
        ("evidence_id", "evidence:other"),
        ("d1_input_digest", "sha256:" + "0" * 64),
        ("prepared_at", "2026-01-01T00:00:00.000Z"),
    ],
)
def test_existing_operation_cannot_change_identity(reserved, field, value):
    original = recover(reserved)
    other = changed(original, **{field: value})
    with closing(connect(reserved.path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises((EvidenceError, ValueError)):
            SqliteD1EnvelopeStore(connection, reserved.keys).persist(other)
    assert recover(reserved).storage_bytes() == original.storage_bytes()


@pytest.mark.parametrize(
    "field,value",
    [
        ("method", "GET"),
        ("path", "/complete"),
        ("api_version", "2099-01-01"),
        ("creation_profile", "unknown/1"),
        ("schema_version", "unknown/1"),
        ("body_digest", "sha256:" + "0" * 64),
    ],
)
def test_invalid_contract_or_digest_rejected(reserved, field, value):
    with pytest.raises(ValueError):
        changed(recover(reserved), **{field: value})


@pytest.mark.parametrize("change", ["variant", "currency", "capabilities", "secret", "coercion"])
def test_changed_body_is_never_adopted(reserved, change):
    original = recover(reserved)
    body = original.body()
    if change == "variant":
        body["line_items"][0]["id"] = "different-variant"
    elif change == "currency":
        body["currency"] = "usd"
    elif change == "capabilities":
        body["capabilities"]["payment"]["handlers"] = [{"token": "unexpected"}]
    elif change == "secret":
        body["bearer_token"] = "unexpected"
    else:
        body["line_items"][0]["id"] = 123
    with pytest.raises((ValueError, EvidenceError)):
        encoded = canonical_bytes(body)
        other = changed(
            original, body_canonical=encoded.decode(), body_digest=merchant_creation_digest(encoded)
        )
        with closing(connect(reserved.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            SqliteD1EnvelopeStore(connection, reserved.keys).persist(other)
    assert recover(reserved) == original


def test_expected_binding_is_explicitly_not_authenticated_and_immutable(reserved):
    original = recover(reserved)
    expected = changed(
        original,
        binding_status="backend_expected_unverified",
        expected_remote_binding=binding().model_dump(mode="json"),
    )
    assert expected.expected_remote_binding is not None
    assert expected.expected_remote_binding.assurance == "backend_expected_not_authenticated"
    for other in (
        expected,
        changed(expected, expected_remote_binding=binding("other").model_dump(mode="json")),
    ):
        with closing(connect(reserved.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(EvidenceError):
                SqliteD1EnvelopeStore(connection, reserved.keys).persist(other)
    for payload in (
        {**binding().model_dump(mode="json"), "assurance": "authenticated"},
        {**binding().model_dump(mode="json"), "remote_run_id": "not-a-uuid"},
        {**binding().model_dump(mode="json"), "bearer_token": "unexpected"},
    ):
        with pytest.raises(ValidationError):
            ExpectedRemoteBinding.model_validate(payload)


def test_key_redaction_and_unknown_auth_fields(reserved):
    envelope = recover(reserved)
    key = envelope.idempotency_key.get_secret_value()
    assert key not in repr(envelope) and key not in envelope.model_dump_json()
    for secret in ("authorization", "bearer_token", "credentials", "authenticated_actor"):
        with pytest.raises(ValidationError):
            changed(envelope, **{secret: "synthetic-unexpected-secret"})


@pytest.mark.parametrize(
    "command",
    [
        "UPDATE d1_checkout_envelopes SET envelope_digest=envelope_digest",
        "DELETE FROM d1_checkout_envelopes",
        "INSERT OR REPLACE INTO d1_checkout_envelopes SELECT * FROM d1_checkout_envelopes",
    ],
)
def test_database_rejects_update_delete_replace(reserved, command):
    before = recover(reserved).storage_bytes()
    with closing(connect(reserved.path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(command)
    assert recover(reserved).storage_bytes() == before


def test_partial_envelope_write_rolls_back_cas_key_reservation_and_allow(tmp_path, monkeypatch):
    case = make_case(tmp_path / "rollback.db")
    original = SqliteD1EnvelopeStore.persist

    def fail(store, envelope):
        original(store, envelope)
        raise sqlite3.OperationalError("synthetic failure after envelope INSERT")

    monkeypatch.setattr(SqliteD1EnvelopeStore, "persist", fail)
    with pytest.raises(PurchaseServiceError):
        start(case)
    with closing(connect(case.path)) as connection:
        for table in ("d1_checkout_envelopes", "d1_checkout_operations", "operation_keys"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute("SELECT stage,state_version FROM purchases").fetchone() == (
            "received",
            1,
        )
    assert not case.gateway.calls
    assert all(
        r.application.disposition == "blocked"
        for r in records(case)
        if isinstance(r, DecisionRecord)
    )


def test_concurrent_identical_preparations_are_idempotent(reserved):
    envelope = recover(reserved)
    barrier = Barrier(2)

    def prepare(_):
        with closing(connect(reserved.path)) as connection, connection:
            barrier.wait(timeout=10)
            connection.execute("BEGIN IMMEDIATE")
            SqliteD1EnvelopeStore(connection, reserved.keys).persist(envelope)
        return True

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert all(workers.map(prepare, (1, 2)))
    with closing(connect(reserved.path)) as connection:
        assert connection.execute("SELECT count(*) FROM d1_checkout_envelopes").fetchone()[0] == 1


def test_recovery_is_scoped_and_fails_closed_on_corrupt_storage(reserved):
    envelope = recover(reserved)
    with closing(connect(reserved.path)) as connection:
        store = SqliteD1EnvelopeStore(connection, reserved.keys)
        assert store.recover(envelope.operation_id, run_id="other", owner_id=OWNER) is None
        assert store.recover(envelope.operation_id, run_id="run-a", owner_id="other") is None
        # Synthetic corruption after removing one guard: verify digest on reads too.
        connection.execute("DROP TRIGGER d1_envelope_no_update")
        connection.execute(
            "UPDATE d1_checkout_envelopes SET canonical_envelope=?",
            (envelope.storage_bytes() + b" ",),
        )
        connection.commit()
        with pytest.raises(EvidenceError):
            store.recover(envelope.operation_id, run_id="run-a", owner_id=OWNER)


def test_duplicate_keys_and_noncanonical_body_fail_closed(reserved):
    with pytest.raises(ValueError):
        creation_body(b'{"currency":"cop","currency":"usd"}')
    with pytest.raises(ValueError):
        changed(recover(reserved), body_canonical=recover(reserved).body_canonical + " ")


def test_backend_expected_binding_can_be_stored_without_authorizing_dispatch(tmp_path):
    case = make_case(tmp_path / "expected.db", blocked=True)
    gate = SqliteD1EnforceGate(
        case.path,
        case.gate._configuration,
        case.keys,
        case.gate._clocks,
        expected_remote_binding=binding(),
    )
    case.gate = gate
    case.service._d1_gate = gate
    with pytest.raises(PurchaseServiceError) as error:
        start(case)
    assert error.value.code == "d1_dispatch_blocked" and not case.gateway.calls
    envelope = recover(case)
    assert envelope.expected_remote_binding == binding()
    assert envelope.binding_status == "backend_expected_unverified"


def test_two_initial_gate_preparations_create_one_envelope(tmp_path, monkeypatch):
    case = make_case(tmp_path / "race.db")
    barrier = Barrier(2)
    original = case.gate.capture

    def capture(*args, **kwargs):
        value = original(*args, **kwargs)
        barrier.wait(timeout=15)
        return value

    monkeypatch.setattr(case.gate, "capture", capture)

    def attempt(_):
        try:
            return start(case).stage
        except PurchaseServiceError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(attempt, (1, 2))) == ["awaiting_confirmation", "blocked"]
    envelope = recover(case)
    with closing(connect(case.path)) as connection:
        assert connection.execute("SELECT operation_id FROM d1_checkout_envelopes").fetchall() == [
            (envelope.operation_id,)
        ]
    assert sum(name == "create" for name, _ in case.gateway.calls) == 1


def test_schema_initialization_is_idempotent_and_caller_can_rollback(tmp_path):
    from pydantic import SecretBytes

    from commerce_agent.governance.provenance import ProvenanceKeys

    keys = ProvenanceKeys(
        key_id="synthetic",
        pseudonym_key=SecretBytes(b"1" * 32),
        encryption_key=SecretBytes(b"2" * 32),
    )
    with closing(connect(tmp_path / "schema.db")) as connection:
        store = SqliteD1EnvelopeStore(connection, keys)
        with pytest.raises(EvidenceError):
            store.initialize()
        connection.execute("BEGIN IMMEDIATE")
        store.initialize()
        store.initialize()
        connection.rollback()
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='d1_checkout_envelopes'"
            ).fetchone()
            is None
        )
