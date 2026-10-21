"""Controlled HTTP + real disposable SQLite. NOT real Merchant/session authentication."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from threading import Barrier
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from commerce_agent.acp.creation_lookup import BackendLookupSession, CreationLookupClient
from commerce_agent.acp.schema import validate_acp
from commerce_agent.canonical_json import canonical_original_response
from commerce_agent.governance.canonical import canonical_bytes, parse_model
from commerce_agent.governance.evidence import EvidenceError
from commerce_agent.governance.remote_creation import (
    LOOKUP_PATH,
    LOOKUP_SCHEMA,
    SessionScope,
    Verification,
    VerificationRequest,
    original_response_sha256,
)
from commerce_agent.persistence.remote_creation_sqlite import (
    RemoteCreationVerifier,
    SqliteRemoteCreationStore,
)
from tests.governance.test_dispatch_envelope import binding, recover
from tests.governance.test_enforce_integration import OWNER, connect, make_case, start
from tests.governance.test_reconciliation import NOW, episode, protected, request


@pytest.fixture
def case(tmp_path):
    return episode(tmp_path / "lookup.db", expected_binding=True)


def query(case, **changes):
    return VerificationRequest.model_validate(
        dict(
            query_id="lookup:one",
            operation_id=request(case).operation_id,
            run_id="run-a",
            owner_id=OWNER,
            expected_version=0,
            **changes,
        )
    )


def session(**changes):
    expected = binding()
    scope = SessionScope(
        assurance="backend_session_registry/1",
        remote_run_id=expected.remote_run_id,
        remote_actor_id=expected.remote_actor_id,
        source_ref=expected.source_ref.model_copy(
            update={"id": "backend:independent-session-registry"}
        ),
    )
    return replace(
        BackendLookupSession(
            "http://127.0.0.1:4120", scope, SecretStr("synthetic-backend-credential")
        ),
        **changes,
    )


def merchant_original_response() -> dict[str, Any]:
    """Synthetic historical response shaped from Merchant c5bd811, not a live capture.

    acp.ACPCheckoutAdapter._response + payment_sandbox.payment_handler. No import,
    token, external service, or claim that the model authenticated a real Merchant.
    """
    handler_base = "http://127.0.0.1:4120/payment-handlers/tesis-sandbox"
    return {
        "id": "cs_1",
        "protocol": {"version": "2026-04-17"},
        "status": "not_ready_for_payment",
        "currency": "cop",
        "capabilities": {
            "payment": {
                "handlers": [
                    {
                        "id": "tesis_sandbox",
                        "name": "co.edu.uniandes.tesis.sandbox",
                        "display_name": "Tesis Sandbox",
                        "version": "2026-09-19",
                        "spec": handler_base + "/spec",
                        "requires_delegate_payment": True,
                        "requires_pci_compliance": False,
                        "psp": "tesis_sandbox",
                        "config_schema": handler_base + "/config-schema",
                        "instrument_schemas": [handler_base + "/instrument-schema"],
                        "config": {"environment": "sandbox"},
                    }
                ]
            }
        },
        "line_items": [
            {
                "id": "li_cs_1",
                "item": {"id": "SON-01", "name": "Sonora", "unit_amount": 72000},
                "quantity": 1,
                "totals": [
                    {
                        "type": "subtotal",
                        "display_text": "Producto con impuestos incluidos",
                        "amount": 72000,
                    },
                    {"type": "total", "display_text": "Total del producto", "amount": 72000},
                ],
            }
        ],
        "totals": [
            {
                "type": "subtotal",
                "display_text": "Producto con impuestos incluidos",
                "amount": 72000,
            },
            {"type": "fulfillment", "display_text": "Envio simulado", "amount": 2000},
            {"type": "total", "display_text": "Total de la compra", "amount": 74000},
        ],
        "fulfillment_options": [
            {
                "type": "shipping",
                "id": "ship_cs_1",
                "title": "Envio simulado a Bogota",
                "earliest_delivery_time": "2026-09-23T11:00:00Z",
                "latest_delivery_time": "2026-09-23T11:00:00Z",
                "totals": [{"type": "total", "display_text": "Total del envio", "amount": 2000}],
            }
        ],
        "selected_fulfillment_options": [],
        "messages": [
            {
                "type": "info",
                "severity": "info",
                "content_type": "plain",
                "content": "Buyer information is missing.",
            }
        ],
        "links": [],
        "created_at": "2026-09-20T11:00:00Z",
        "updated_at": "2026-09-20T11:00:00Z",
        "expires_at": "2026-09-20T13:00:00Z",
    }


def found(case) -> dict[str, Any]:
    envelope = recover(case)
    original = merchant_original_response()
    return dict(
        schema_version=LOOKUP_SCHEMA,
        result="found",
        run_id=binding().remote_run_id,
        actor_id=binding().remote_actor_id,
        request_id="merchant:server-correlation",
        idempotency_key_sha256=hashlib.sha256(
            envelope.idempotency_key.get_secret_value().encode()
        ).hexdigest(),
        acp_api_version="2026-04-17",
        acp_create_fingerprint=envelope.body_digest[7:],
        checkout_id=original["id"],
        original_response=original,
        original_response_profile="merchant-acp-original-response/1",
        original_response_sha256=original_response_sha256(original),
    )


def response(payload, status=200, **headers):
    return httpx.Response(status, json=payload, headers={"Cache-Control": "no-store", **headers})


def harness(case, handler=None, *, backend=None, factory=None, **limits):
    calls = []
    payload = found(case)

    def handle(req):
        # Verify the write lock is free while the HTTP handler runs.
        with closing(connect(case.path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        assert req.method == "POST" and req.url.path == LOOKUP_PATH
        assert req.url.query == b"" and "idempotency-key" not in req.headers
        assert req.headers["api-version"] == "2026-04-17"
        assert req.headers["authorization"] == "Bearer synthetic-backend-credential"
        assert json.loads(req.content) == {
            "schema_version": LOOKUP_SCHEMA,
            "idempotency_key_sha256": payload["idempotency_key_sha256"],
        }
        calls.append(req)
        return handler(req, payload) if handler else response(payload)

    client = CreationLookupClient(
        backend or session(), transport=httpx.MockTransport(handle), **limits
    )
    verifier = RemoteCreationVerifier(
        factory or (lambda: connect(case.path)), case.keys, client, clock=lambda: NOW
    )
    return verifier, calls, client


def store(case, connection):
    return SqliteRemoteCreationStore(connection, case.keys, clock=lambda: NOW)


def test_merchant_handler_and_shipping_do_not_equal_agent_request_capabilities(case):
    payload = found(case)
    original = payload["original_response"]
    request_body = recover(case).body()
    handler = original["capabilities"]["payment"]["handlers"][0]
    shipping = original["fulfillment_options"][0]
    validate_acp("PaymentHandler", handler)
    validate_acp("FulfillmentOptionShipping", shipping)
    validate_acp("CheckoutSession", original)
    assert request_body["capabilities"]["payment"]["handlers"] == []
    assert handler["id"] == "tesis_sandbox" and handler["requires_delegate_payment"] is True
    assert shipping["type"] == "shipping" and shipping["id"] == "ship_" + original["id"]
    assert original["capabilities"] != request_body["capabilities"]
    before = protected(case)
    verifier, calls, client = harness(case)
    try:
        result = verifier.verify(query(case))
        assert result.durable and result.verification.outcome == "found_verified"
        assert not result.verification.replay_allowed
        assert not result.verification.continuation_allowed
        assert verifier.inspect(query(case)) == (result.event,)
        assert len(calls) == 1 and protected(case) == before and not case.gateway.calls
    finally:
        client.close()


def test_verified_historical_evidence_survives_reopen_without_commercial_effect(case):
    before = protected(case)
    verifier, calls, client = harness(case)
    try:
        result = verifier.verify(query(case))
        assert result.durable and result.verification.outcome == "found_verified"
        assert result.verification.basis == "controlled_transport"
        assert not result.verification.replay_allowed
        assert not result.verification.continuation_allowed
        assert result.event is not None
        assert result.event.local.recovered.run_id != binding().remote_run_id
        assert verifier.inspect(query(case)) == (result.event,)
        # A new coordinator/connection recovers the committed event, without HTTP retry.
        second = RemoteCreationVerifier(
            lambda: connect(case.path), case.keys, client, clock=lambda: NOW
        )
        assert second.verify(query(case)) == result and len(calls) == 1
        assert second.inspect(query(case)) == (result.event,)
        body = canonical_bytes(result.event)
        assert b"synthetic-backend-credential" not in body
        assert recover(case).idempotency_key.get_secret_value().encode() not in body
        assert OWNER.encode() not in body and b'"original_response":' not in body
    finally:
        client.close()
    assert protected(case) == before and not case.gateway.calls


@pytest.mark.parametrize(
    "field,value,outcome",
    [
        ("run_id", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "identity_conflict"),
        ("actor_id", "foreign", "identity_conflict"),
        ("idempotency_key_sha256", "0" * 64, "identity_conflict"),
        ("acp_create_fingerprint", "0" * 64, "fingerprint_conflict"),
        ("checkout_id", "foreign", "identity_conflict"),
        ("checkout_id", "invalid/id", "malformed_response"),
        ("original_response_sha256", "0" * 64, "response_invalid"),
        ("original_response_profile", "unknown/1", "malformed_response"),
        ("acp_api_version", "unknown", "malformed_response"),
        ("schema_version", "unknown/1", "malformed_response"),
        ("unexpected", "sensitive", "malformed_response"),
    ],
)
def test_remote_conflicts_are_durable_conservative_results(case, field, value, outcome):
    def handle(req, payload):
        return response({**payload, field: value})

    before = protected(case)
    verifier, calls, client = harness(case, handle)
    try:
        result = verifier.verify(query(case))
        assert result.durable and result.verification.outcome == outcome
        assert result.verification.original_response_digest is None
        assert len(verifier.inspect(query(case))) == 1 and len(calls) == 1
    finally:
        client.close()
    assert protected(case) == before and not case.gateway.calls


@pytest.mark.parametrize("damage", ["schema", "item", "quantity", "currency", "status", "secret"])
def test_original_response_schema_and_creation_identity(case, damage):
    def handle(req, payload):
        original = payload["original_response"]
        if damage == "schema":
            del original["totals"]
        elif damage == "item":
            original["line_items"][0]["item"]["id"] = "foreign"
        elif damage == "quantity":
            original["line_items"][0]["quantity"] = 2
        elif damage in {"currency", "status"}:
            original[damage] = "usd" if damage == "currency" else "completed"
        else:
            original["extension"] = {"Authorization": "synthetic-sensitive"}
        if damage != "secret":
            payload["original_response_sha256"] = original_response_sha256(original)
        return response(payload)

    verifier, _, client = harness(case, handle)
    try:
        assert verifier.verify(query(case)).verification.outcome == "response_invalid"
    finally:
        client.close()


@pytest.mark.parametrize(
    "damage",
    [
        "handlers_type",
        "handler_required",
        "handler_bool",
        "handler_extra",
        "handler_uri",
        "handler_version",
        "shipping_required",
        "shipping_totals",
    ],
)
def test_merchant_capabilities_and_fulfillment_still_require_frozen_schema(case, damage):
    def handle(req, payload):
        original = payload["original_response"]
        handler = original["capabilities"]["payment"]["handlers"][0]
        shipping = original["fulfillment_options"][0]
        if damage == "handlers_type":
            original["capabilities"]["payment"]["handlers"] = {}
        elif damage == "handler_required":
            del handler["psp"]
        elif damage == "handler_bool":
            handler["requires_delegate_payment"] = "true"
        elif damage == "handler_extra":
            handler["unexpected"] = True
        elif damage == "handler_uri":
            handler["spec"] = "invalid URI"
        elif damage == "handler_version":
            handler["version"] = "unknown"
        elif damage == "shipping_required":
            del shipping["title"]
        else:
            shipping["totals"] = "invalid"
        # A correct digest does not make structurally invalid historical data valid.
        payload["original_response_sha256"] = original_response_sha256(original)
        return response(payload)

    before = protected(case)
    verifier, calls, client = harness(case, handle)
    try:
        result = verifier.verify(query(case))
        assert result.durable and result.verification.outcome == "response_invalid"
        assert result.verification.reason == "ORIGINAL_RESPONSE_INVALID"
        assert result.verification.checkout_id is None
        assert result.verification.original_response_digest is None
        assert not result.verification.continuation_allowed
        assert len(calls) == 1 and protected(case) == before and not case.gateway.calls
    finally:
        client.close()


@pytest.mark.parametrize("kind", ["origin", "scope", "unverified", "missing_scope", "bearer"])
def test_backend_trust_failures_do_not_send_http(case, kind):
    backend = session()
    if kind == "origin":
        backend = replace(backend, merchant_base_url="http://127.0.0.1:4999")
    elif kind == "scope":
        assert backend.scope
        backend = replace(
            backend, scope=backend.scope.model_copy(update={"remote_actor_id": "other"})
        )
    elif kind == "unverified":
        assert backend.scope
        backend = replace(
            backend,
            scope=backend.scope.model_copy(
                update={"assurance": "backend_expected_not_authenticated"}
            ),
        )
    elif kind == "missing_scope":
        backend = replace(backend, scope=None)
    else:
        backend = replace(backend, bearer=None)
    verifier, calls, client = harness(case, backend=backend)
    try:
        result = verifier.verify(query(case))
        assert result.durable and result.verification.outcome in {
            "unattributable",
            "authentication_error",
        }
        assert not calls
    finally:
        client.close()


def test_expected_binding_alone_never_authenticates_and_absence_blocks(tmp_path):
    case = episode(tmp_path / "unbound.db")
    verifier, calls, client = harness(case)
    try:
        assert verifier.verify(query(case)).verification.reason == "EXPECTED_BINDING_UNAVAILABLE"
        assert not calls
    finally:
        client.close()


@pytest.mark.parametrize(
    "destination",
    [
        "http://merchant.example",
        "https://user:password@merchant.example",
        "https://merchant.example?token=hidden",
        "https://merchant.example#fragment",
        "https://merchant.example\\@other.example",
        "https://merchant.example\n",
    ],
)
def test_untrusted_or_unsafe_destination_rejected(destination):
    with pytest.raises(ValueError):
        CreationLookupClient(session(merchant_base_url=destination))


@pytest.mark.parametrize("field", ["url", "endpoint", "bearer", "remote_run_id", "actor_id"])
def test_query_cannot_choose_authority_or_endpoint(case, field):
    with pytest.raises(ValidationError):
        VerificationRequest.model_validate({**query(case).model_dump(), field: "untrusted"})


def test_absolute_lookup_does_not_inherit_acp_prefix(case):
    envelope = recover(case)
    envelope = envelope.model_copy(update={"merchant_origin": "http://127.0.0.1:4120/api/acp"})
    seen = []

    def handle(req):
        seen.append(req.url.path)
        return response(found(case))

    client = CreationLookupClient(
        session(merchant_base_url=envelope.merchant_origin), transport=httpx.MockTransport(handle)
    )
    try:
        assert client.lookup(envelope, None).outcome == "found_verified"
        assert seen == [LOOKUP_PATH]
    finally:
        client.close()


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("timeout_before", "timeout"),
        ("timeout_after", "timeout"),
        ("network", "transport_error"),
        ("503", "transport_error"),
        ("401", "authentication_error"),
        ("redirect", "unattributable"),
        ("json", "malformed_response"),
        ("duplicate", "malformed_response"),
        ("huge", "malformed_response"),
        ("no_store", "malformed_response"),
        ("undefined", "malformed_response"),
        ("not_found", "not_found"),
        ("unrecoverable", "unrecoverable"),
    ],
)
def test_readonly_distributed_failures_never_become_authority(case, fault, expected):
    observed = []

    def handle(req, payload):
        if fault == "timeout_before":
            raise httpx.ConnectTimeout("synthetic-sensitive", request=req)
        observed.append("lookup")
        if fault == "timeout_after":
            raise httpx.ReadTimeout("synthetic-sensitive", request=req)
        if fault == "network":
            raise httpx.ConnectError("synthetic-sensitive", request=req)
        if fault in {"401", "503"}:
            return response(
                dict(
                    schema_version=LOOKUP_SCHEMA,
                    result="error",
                    request_id="server:error",
                    code="unauthenticated" if fault == "401" else "lookup_unavailable",
                ),
                int(fault),
            )
        if fault == "redirect":
            return httpx.Response(307, headers={"Location": "https://foreign.invalid"})
        if fault in {"json", "duplicate", "huge"}:
            raw = {
                "json": b"{",
                "duplicate": b'{"result":"found","result":"not_found"}',
                "huge": b"x" * 300000,
            }[fault]
            return httpx.Response(
                200,
                content=raw,
                headers={"Cache-Control": "no-store", "Content-Type": "application/json"},
            )
        if fault == "no_store":
            return httpx.Response(200, json=payload, headers={"Cache-Control": "public"})
        if fault == "undefined":
            return response({})
        return response(
            {
                key: payload[key]
                for key in (
                    "schema_version",
                    "run_id",
                    "actor_id",
                    "idempotency_key_sha256",
                    "request_id",
                )
            }
            | {"result": fault}
        )

    verifier, calls, client = harness(case, handle)
    before = protected(case)
    try:
        result = verifier.verify(query(case))
        assert result.durable and result.verification.outcome == expected
        assert result.event and b"synthetic-sensitive" not in canonical_bytes(result.event)
        assert not result.verification.continuation_allowed and len(calls) == 1
        assert len(observed) == (0 if fault == "timeout_before" else 1)
    finally:
        client.close()
    assert protected(case) == before and not case.gateway.calls


@pytest.mark.parametrize("damage", ["envelope", "evidence", "old_history"])
def test_local_corruption_prevents_http(case, damage):
    from tests.governance.test_reconciliation import apply

    verifier, calls, client = harness(case)
    if damage == "old_history":
        apply(case)
    with closing(connect(case.path)) as connection, connection:
        if damage == "envelope":
            connection.execute("DROP TRIGGER d1_envelope_no_update")
            connection.execute("UPDATE d1_checkout_envelopes SET canonical_envelope=?", (b"{}",))
        elif damage == "evidence":
            # Deliberately remove the immutable ledger row in a disposable fixture DB.
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='governance_evidence'"
            ).fetchall():
                connection.execute(f'DROP TRIGGER "{name}"')
            connection.execute(
                "UPDATE governance_evidence SET body_digest=?", ("sha256:" + "0" * 64,)
            )
        else:
            connection.execute("DROP TRIGGER d1_reconciliation_event_no_update")
            connection.execute("UPDATE d1_reconciliation_events SET body=?", (b"{}",))
    try:
        result = verifier.verify(query(case))
        assert not result.durable and result.verification.outcome == "local_invalid"
        assert not calls
    finally:
        client.close()


@pytest.mark.parametrize("field", ["run_id", "owner_id", "operation_id"])
def test_local_scope_isolation(case, field):
    verifier, calls, client = harness(case)
    try:
        req = query(case).model_copy(update={field: "foreign"})
        assert not verifier.verify(req).durable
        with pytest.raises(EvidenceError):
            verifier.inspect(req)
        assert not calls
    finally:
        client.close()


def test_local_state_changed_while_http_rejects_stale_evidence(case):
    def handle(req, payload):
        with closing(connect(case.path)) as connection, connection:
            connection.execute(
                "UPDATE purchases SET stage='uncertain',state_version=state_version+1"
            )
        return response(payload)

    verifier, _, client = harness(case, handle)
    try:
        result = verifier.verify(query(case))
        assert not result.durable and result.verification.outcome == "cas_conflict"
        assert not verifier.inspect(query(case))
    finally:
        client.close()


@pytest.mark.parametrize("same_query", [False, True])
def test_two_concurrent_lookups_use_cas_without_overwriting(case, same_query):
    barrier = Barrier(2)

    def handle(req, payload):
        barrier.wait(timeout=30)
        return response(payload)

    verifier, calls, client = harness(case, handle)

    def attempt(index):
        req = query(case).model_copy(
            update={"query_id": "lookup:one" if same_query else f"lookup:{index}"}
        )
        return verifier.verify(req)

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(attempt, [1, 2]))
        assert len(calls) == 2 and len(verifier.inspect(query(case))) == 1
        assert sum(x.durable for x in outcomes) == (2 if same_query else 1)
        if same_query:
            assert outcomes[0] == outcomes[1]
        else:
            assert any(x.verification.outcome == "cas_conflict" for x in outcomes)
    finally:
        client.close()


def test_multiple_versions_and_retry_registration_without_http(case):
    verifier, calls, client = harness(case)
    try:
        first = verifier.verify(query(case))
        second_req = query(case).model_copy(
            update={"query_id": "lookup:two", "expected_version": 1}
        )
        second = verifier.verify(second_req)
        assert first.event and second.event
        assert [x.version for x in verifier.inspect(query(case))] == [1, 2]
        with closing(connect(case.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            assert (
                store(case, connection).append(second_req, second.event.local, second.verification)
                == second.event
            )
        assert len(calls) == 2
        assert verifier.verify(query(case)) == first
        assert not verifier.verify(
            query(case).model_copy(update={"query_id": "lookup:stale"})
        ).durable
        assert len(calls) == 2
    finally:
        client.close()


@pytest.mark.parametrize("damage", ["bytes", "digest", "noncanonical", "scope", "gap", "tail"])
def test_corrupt_first_event_blocks_inspection_and_advancement(case, damage):
    verifier, calls, client = harness(case)
    try:
        first = verifier.verify(query(case))
        assert first.event
        verifier.verify(
            query(case).model_copy(update={"query_id": "lookup:two", "expected_version": 1})
        )
        with closing(connect(case.path)) as connection, connection:
            connection.execute("DROP TRIGGER d1_remote_creation_no_update")
            if damage in {"gap", "tail"}:
                connection.execute("DROP TRIGGER d1_remote_creation_no_delete")
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute(
                    "DELETE FROM d1_remote_creation_events WHERE version=?",
                    (1 if damage == "gap" else 2,),
                )
            elif damage == "digest":
                connection.execute(
                    "UPDATE d1_remote_creation_events SET digest=? WHERE version=1",
                    ("sha256:" + "0" * 64,),
                )
            else:
                event = first.event
                if damage == "scope":
                    event = event.model_copy(
                        update={
                            "local": event.local.model_copy(
                                update={
                                    "recovered": event.local.recovered.model_copy(
                                        update={"run_id": "foreign"}
                                    )
                                }
                            )
                        }
                    )
                body = b"{}" if damage == "bytes" else canonical_bytes(event)
                if damage == "noncanonical":
                    body = b" " + body
                connection.execute(
                    "UPDATE d1_remote_creation_events SET body=?,digest=? WHERE version=1",
                    (body, store(case, connection).digest(event)),
                )
        with pytest.raises(EvidenceError):
            verifier.inspect(query(case))
        assert not verifier.verify(
            query(case).model_copy(update={"query_id": "lookup:three", "expected_version": 2})
        ).durable
        assert len(calls) == 2
    finally:
        client.close()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE d1_remote_creation_events SET version=version",
        "DELETE FROM d1_remote_creation_events",
        "INSERT OR REPLACE INTO d1_remote_creation_events SELECT * FROM d1_remote_creation_events",
        "INSERT OR REPLACE INTO d1_remote_creation_events "
        "SELECT operation_id,version,'other',body,digest FROM d1_remote_creation_events",
        "DELETE FROM d1_remote_creation_head",
        "INSERT OR REPLACE INTO d1_remote_creation_head SELECT * FROM d1_remote_creation_head",
        "UPDATE d1_remote_creation_head SET version=version",
    ],
)
def test_append_only_guards(case, sql):
    verifier, _, client = harness(case)
    try:
        result = verifier.verify(query(case))
        with (
            closing(connect(case.path)) as connection,
            connection,
            pytest.raises(sqlite3.IntegrityError),
        ):
            connection.execute(sql)
        assert verifier.inspect(query(case)) == (result.event,)
    finally:
        client.close()


@pytest.mark.parametrize(
    "failure", ["rollback", "insert", "commit_before", "commit_after", "commit_noop"]
)
def test_crash_and_persistence_failure_never_report_provisional_as_durable(case, failure):
    class FailedConnection(sqlite3.Connection):
        def commit(self):
            if failure == "commit_noop":
                return
            if failure == "commit_after":
                super().commit()
            raise sqlite3.OperationalError("synthetic commit uncertainty")

    count = 0

    def factory():
        nonlocal count
        count += 1
        if count == 2 and failure in {"commit_before", "commit_after", "commit_noop"}:
            connection = sqlite3.connect(case.path, factory=FailedConnection)
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        return connect(case.path)

    verifier, _, client = harness(case, factory=factory)
    try:
        if failure in {"insert", "rollback"}:
            with closing(connect(case.path)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                store(case, connection).initialize()
                if failure == "insert":
                    connection.execute(
                        "CREATE TRIGGER synthetic_insert BEFORE INSERT "
                        "ON d1_remote_creation_events BEGIN SELECT RAISE(ABORT,'fixture'); END"
                    )
                    connection.commit()
                else:
                    local, _ = store(case, connection).snapshot(query(case))
                    store(case, connection).append(
                        query(case), local, Verification(outcome="timeout", reason="LOOKUP_TIMEOUT")
                    )
                    connection.rollback()
            if failure == "rollback":
                assert not verifier.inspect(query(case))
                return
        result = verifier.verify(query(case))
        assert not result.durable and result.event is None
        assert result.verification.outcome == "persistence_error"
        recovered = verifier.inspect(query(case))
        assert len(recovered) == (1 if failure == "commit_after" else 0)
        if recovered:
            assert recovered[0].verification.outcome == "found_verified"
    finally:
        client.close()


def test_canonical_profile_matches_frozen_merchant_vector():
    assert (
        canonical_original_response({"z": [2, 1], "a": "café"}) == '{"a":"café","z":[2,1]}'.encode()
    )
    assert original_response_sha256({"z": [2, 1], "a": "café"}) == (
        "94e595e954f31d50753f13f634cae7761ce942aeabc9b37fefd39b5fe4da4348"
    )
    assert original_response_sha256({"a": [1, 2]}) != original_response_sha256({"a": [2, 1]})
    assert original_response_sha256("é") != original_response_sha256("e\u0301")


@pytest.mark.parametrize(
    "value",
    [
        1.0,
        float("nan"),
        9007199254740992,
        (1,),
        {1: "value"},
        {"nested": {"Token": "private"}},
        "\ud800",
    ],
)
def test_canonical_profile_rejects_nonportable_or_sensitive_values(value):
    with pytest.raises((ValueError, UnicodeError)):
        canonical_original_response(value)


def test_models_cannot_grant_commercial_permissions():
    for changes in ({"replay_allowed": True}, {"continuation_allowed": True}, {"allow": True}):
        with pytest.raises(ValidationError):
            Verification.model_validate(
                {"outcome": "timeout", "reason": "LOOKUP_TIMEOUT", **changes}
            )
    with pytest.raises(ValidationError):
        Verification(outcome="found_verified", reason="HISTORICAL_CREATION_ONLY")
    with pytest.raises(ValueError):
        parse_model(VerificationRequest, b'{"query_id":"a","query_id":"b"}')


@pytest.mark.parametrize("known", ["matching", "conflict", "post_corrupt"])
def test_confirmed_creation_and_post_create_history_are_preserved(tmp_path, known):
    case = make_case(tmp_path / "confirmed.db")
    # Explicit backend fixture setup before creating the original immutable envelope.
    case.gate._expected_remote_binding = binding()
    start(case)
    before = protected(case)
    commercial_calls = tuple(case.gateway.calls)

    def handle(req, payload):
        if known == "conflict":
            payload["checkout_id"] = "foreign"
            payload["original_response"]["id"] = "foreign"
            payload["original_response_sha256"] = original_response_sha256(
                payload["original_response"]
            )
        return response(payload)

    verifier, calls, client = harness(case, handle)
    if known == "post_corrupt":
        with closing(connect(case.path)) as connection, connection:
            connection.execute("DROP TRIGGER d1_post_create_events_no_update")
            connection.execute("UPDATE d1_post_create_events SET body=? WHERE version=1", (b"{}",))
    try:
        result = verifier.verify(query(case))
        assert (
            result.verification.outcome
            == {
                "matching": "found_verified",
                "conflict": "identity_conflict",
                "post_corrupt": "local_invalid",
            }[known]
        )
        assert len(calls) == (0 if known == "post_corrupt" else 1)
        assert protected(case) == before and tuple(case.gateway.calls) == commercial_calls
    finally:
        client.close()


def test_crash_after_lookup_before_persistence_and_restart_are_read_only(case):
    def handle(req, payload):
        raise SystemExit("synthetic abrupt caller termination")

    verifier, calls, client = harness(case, handle)
    before = protected(case)
    try:
        with pytest.raises(SystemExit):
            verifier.verify(query(case))
        restarted = RemoteCreationVerifier(
            lambda: connect(case.path), case.keys, client, clock=lambda: NOW
        )
        assert not restarted.inspect(query(case))
        assert len(calls) == 1 and protected(case) == before and not case.gateway.calls
    finally:
        client.close()


def test_client_security_configuration_and_closed_surface():
    client = CreationLookupClient(
        session(merchant_base_url="https://merchant.example/api/acp"), timeout_seconds=2.0
    )
    try:
        assert client._endpoint == "https://merchant.example" + LOOKUP_PATH
        assert not client._http.follow_redirects and not client._http.trust_env
        assert client._http.timeout.connect == 2.0
        assert client._basis == "https_backend_session"
        assert not any(
            hasattr(client, name)
            for name in (
                "create_checkout",
                "update_checkout",
                "cancel_checkout",
                "complete_checkout",
                "replay",
            )
        )
    finally:
        client.close()


def test_schema_rollback_and_unexpected_definition_fail_closed(case):
    with closing(connect(case.path)) as connection:
        core = store(case, connection)
        with pytest.raises(EvidenceError):
            core.initialize()
        connection.execute("BEGIN IMMEDIATE")
        core.initialize()
        core.initialize()
        connection.rollback()
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='d1_remote_creation_events'"
        ).fetchone()
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            core.initialize()
        connection.execute("DROP TRIGGER d1_remote_creation_no_update")
        connection.execute(
            "CREATE TRIGGER d1_remote_creation_no_update BEFORE UPDATE "
            "ON d1_remote_creation_events BEGIN SELECT 1; END"
        )
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(EvidenceError):
                core.initialize()


def test_head_failure_rolls_back_event_even_when_caller_catches_error(case):
    verifier, _, client = harness(case)
    try:
        assert verifier.verify(query(case)).durable
        before = verifier.inspect(query(case))
        with closing(connect(case.path)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER synthetic_head BEFORE UPDATE "
                "ON d1_remote_creation_head BEGIN SELECT RAISE(ABORT,'fixture'); END"
            )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            req = query(case).model_copy(update={"query_id": "lookup:two", "expected_version": 1})
            core = store(case, connection)
            local, _ = core.snapshot(req)
            with pytest.raises(sqlite3.IntegrityError):
                core.append(req, local, Verification(outcome="timeout", reason="LOOKUP_TIMEOUT"))
        assert verifier.inspect(query(case)) == before
    finally:
        client.close()
