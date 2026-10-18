"""New applied D1 + real SQLite, controlled D17 wire; not remote authentication."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.authority_observation import AuthoritySession, ObservationBlocked
from commerce_agent.acp.guarded_creation import HttpGuardedCheckout
from commerce_agent.application import PurchaseServiceError
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.guarded_creation import (
    CREATE_PATH,
    LOOKUP_PATH,
    LOOKUP_SCHEMA,
    GuardedEnvelope,
    GuardedProof,
    Rejection,
)
from commerce_agent.governance.models import SCHEMA, D1PolicyArtifact, PolicyBinding
from commerce_agent.governance.provenance import ClockReader, D1EnforceConfiguration
from commerce_agent.governance.remote_creation import CreationFound, original_response_sha256
from commerce_agent.persistence.d1_envelope_sqlite import SqliteD1EnvelopeStore
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.guarded_d1_sqlite import SqliteGuardedD1
from tests.governance.test_preselection_d1 import BEARER, open_link, setup_case
from tests.governance.test_remote_creation import merchant_original_response


def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.execute("PRAGMA foreign_keys=ON")
    return db


class Wire:
    case: SqliteGuardedD1

    def __init__(self):
        self.calls = []
        self.proof: GuardedProof | None = None
        self.mode = "found"
        self.fault: str | None = None
        self.lookup_fault: str | None = None

    def handle(self, request):
        body: dict[str, Any]
        self.calls.append((request.method, request.url.path))
        with closing(connect(self.case.path)) as db:
            row = db.execute("SELECT state FROM d1_checkout_operations").fetchone()
            assert row == ("claimed",)
            guard = parse_model(
                GuardedEnvelope, db.execute("SELECT body FROM b_d17_envelopes").fetchone()[0]
            )
            legacy = SqliteD1EnvelopeStore(db, self.case.keys).recover(
                guard.operation_id,
                run_id=guard.run_id,
                owner_id=guard.owner_id,
            )
            assert legacy is not None
        assert request.headers["API-Version"] == "2026-04-17"
        assert request.headers["Authorization"] == "Bearer synthetic-session"
        scope = guard.shadow_binding.merchant.scope
        key_hash = hashlib.sha256(legacy.idempotency_key.get_secret_value().encode()).hexdigest()
        if request.url.path == CREATE_PATH:
            assert request.content == canonical_bytes(guard.request)
            assert request.headers["Idempotency-Key"] == legacy.idempotency_key.get_secret_value()
            original = merchant_original_response()
            original["line_items"][0]["item"]["id"] = guard.request.offer_id
            original["line_items"][0]["item"]["unit_amount"] = (
                guard.selected_offer.pricing.items_total_minor
            )
            for row in original["totals"]:
                row["amount"] = {
                    "subtotal": guard.selected_offer.pricing.items_total_minor,
                    "fulfillment": guard.selected_offer.pricing.shipping_total_minor,
                    "total": guard.selected_offer.pricing.total_minor,
                }[row["type"]]
            self.proof = GuardedProof(
                offer_id=guard.request.offer_id,
                accepted_offer_revision=guard.request.expected_offer_revision,
                offer_snapshot_profile="merchant-offer-snapshot/1",
                accepted_offer_snapshot_sha256=guard.request.offer_snapshot_sha256,
                guard_fingerprint=guard.guard_fingerprint,
                checked_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                creation=CreationFound(
                    schema_version="merchant-acp-creation-lookup/1",
                    result="found",
                    run_id=scope.remote_run_id,
                    actor_id=scope.remote_actor_id,
                    idempotency_key_sha256=key_hash,
                    request_id="synthetic-request",
                    acp_api_version="2026-04-17",
                    acp_create_fingerprint=legacy.body_digest[7:],
                    checkout_id=original["id"],
                    original_response=original,
                    original_response_profile="merchant-acp-original-response/1",
                    original_response_sha256=original_response_sha256(original),
                ),
            )
            if self.fault == "lost":
                raise httpx.ReadTimeout("synthetic response loss")
            if self.fault == "crash":
                raise SystemExit(77)
            proof = self.proof.model_dump(mode="json")
            if self.fault in {"revision", "digest", "fingerprint", "actor", "key", "response"}:
                if self.fault == "revision":
                    proof["accepted_offer_revision"] += 1
                if self.fault == "digest":
                    proof["accepted_offer_snapshot_sha256"] = "a" * 64
                if self.fault == "fingerprint":
                    proof["guard_fingerprint"] = "b" * 64
                if self.fault == "actor":
                    proof["creation"]["actor_id"] = "foreign"
                if self.fault == "key":
                    proof["creation"]["idempotency_key_sha256"] = "c" * 64
                if self.fault == "response":
                    proof["creation"]["original_response"]["id"] = "foreign"
            body = {
                "schema_version": "merchant-guarded-acp-create/1",
                "result": "created",
                "request_id": "synthetic-request",
                "proof": proof,
            }
        else:
            assert request.url.path == LOOKUP_PATH
            assert json.loads(request.content)["idempotency_key_sha256"] == key_hash
            body = dict(
                schema_version=LOOKUP_SCHEMA,
                result=self.mode,
                run_id=scope.remote_run_id,
                actor_id=scope.remote_actor_id,
                idempotency_key_sha256=key_hash,
                request_id="lookup",
            )
            if self.mode == "found":
                assert self.proof is not None
                body.update(
                    proof=self.proof.model_dump(mode="json"), proof_sha256=self.proof.digest()
                )
            if self.mode == "rejected":
                rejection = Rejection(
                    schema_version="merchant-guarded-acp-rejection/1",
                    run_id=scope.remote_run_id,
                    actor_id=scope.remote_actor_id,
                    idempotency_sha256=key_hash,
                    guard_fingerprint=guard.guard_fingerprint,
                    code="offer_revision_mismatch",
                )
                body.update(
                    rejection=rejection.model_dump(mode="json"), rejection_sha256=rejection.digest()
                )
        if request.url.path == LOOKUP_PATH:
            if self.lookup_fault == "scope":
                body["actor_id"] = "foreign"
            if self.lookup_fault == "digest":
                body["proof_sha256"] = "d" * 64
            if self.lookup_fault == "timeout":
                raise httpx.ReadTimeout("controlled failure")
            if self.lookup_fault == "redirect":
                return httpx.Response(307, headers={"Location": "https://foreign.example"})
            if self.lookup_fault == "large":
                return httpx.Response(
                    200,
                    content=b"x" * 262145,
                    headers={"Cache-Control": "no-store", "Content-Type": "application/json"},
                )
        return httpx.Response(
            201 if request.url.path == CREATE_PATH else 200,
            json=body,
            headers={"Cache-Control": "no-store"},
        )


def open_case(root, link, seal, wire, **options):
    configuration = D1EnforceConfiguration.model_validate(
        {
            **link.configuration.model_dump(),
            "governance_mode": "enforce",
            "policy_set_id": "b-policy",
        }
    )
    port = HttpGuardedCheckout(
        AuthoritySession(seal.episode.binding, BEARER), transport=httpx.MockTransport(wire.handle)
    )
    case = SqliteGuardedD1(
        root / "applied.sqlite",
        shadow=link,
        custody_key=SecretBytes(b"b-link-key-only-for-tests-32xxxxxx"),
        keys=link.keys,
        configuration=configuration,
        clocks=ClockReader(configuration.clock),
        transport=port,
        **options,
    )
    wire.case = case
    return case


def make_case(root, **options):
    link, request, seal, _registry, _grant = setup_case(root)
    shadow = link.evaluate(request)
    wire = Wire()
    case = open_case(root, link, seal, wire, **options)
    case.initialize()
    return case, request, wire, shadow


def test_fresh_applied_allow_is_not_promoted_shadow_and_cannot_continue(tmp_path):
    case, request, wire, shadow = make_case(tmp_path)
    with pytest.raises(ObservationBlocked):
        case.dispatch(request)
    assert wire.calls == []
    guard = case.prepare(request)
    assert guard.shadow_binding == shadow and shadow.operation_id is None
    assert wire.calls == []
    with closing(connect(case.path)) as db:
        record = SqliteGovernanceEvidenceStore(db).for_run(request.run_id)[0]
        assert isinstance(record, DecisionRecord)
        assert record.application.disposition == "transition_committed"
        assert record.application.transition_ref == guard.operation_id
        assert (
            record.decision.governance_mode == "enforce"
            and record.evidence_id != shadow.evidence_id
        )
        legacy = SqliteD1EnvelopeStore(db, case.keys).recover(
            guard.operation_id, run_id=request.run_id, owner_id=request.owner_id
        )
        assert legacy is not None and legacy.path == "/checkout_sessions"
    result = case.dispatch(request)
    assert (
        result.result == "found" and not result.continuation_allowed and not result.replay_allowed
    )
    assert case.recover(request) == result == case.recover(request)
    assert wire.calls.count(("POST", CREATE_PATH)) == 1
    assert wire.calls.count(("POST", LOOKUP_PATH)) == 2
    with pytest.raises(ObservationBlocked):
        case.dispatch(request)
    with pytest.raises(ObservationBlocked):
        case.prepare(request)
    assert case.shadow.recover(request) == shadow
    with closing(connect(case.path)) as db:
        assert db.execute(
            "SELECT stage,state_version,checkout_id,order_id FROM purchases"
        ).fetchone() == ("selected", 2, None, None)
        assert db.execute("SELECT count(*) FROM orders").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM completion_results").fetchone() == (0,)
    case.transport.close()


@pytest.mark.parametrize(
    "field",
    ["owner_id", "run_id", "seal_digest", "feed_digest", "intent_digest", "selected_variant_id"],
)
def test_foreign_selected_facts_fail_before_new_evaluation(tmp_path, field):
    case, request, wire, _ = make_case(tmp_path)
    with pytest.raises(ObservationBlocked):
        case.prepare(request.model_copy(update={field: "foreign"}))
    assert wire.calls == []
    with closing(connect(case.path)) as db:
        assert db.execute("SELECT count(*) FROM b_d17_attempts").fetchone() == (0,)
    case.transport.close()


def test_applied_deny_is_real_and_never_creates_a_guard(tmp_path):
    raw = canonical_bytes(
        D1PolicyArtifact(
            schema_version=SCHEMA,
            policy_set_id="b-policy",
            version=1,
            policy_ids=("POL-01", "POL-02"),
            allowed_actions=("prepare_checkout",),
            allowed_merchant_refs=("merchant:forbidden",),
            currency="COP",
            quantity=1,
        )
    )
    case, request, wire, _shadow = make_case(
        tmp_path,
        policy_bytes=raw,
        policy_binding=PolicyBinding(
            schema_version=SCHEMA,
            policy_set_id="b-policy",
            version=1,
            artifact_digest=sha256_digest(raw),
        ),
    )
    with pytest.raises(PurchaseServiceError) as error:
        case.prepare(request)
    assert error.value.code == "d1_denied" and wire.calls == []
    with closing(connect(case.path)) as db:
        record = SqliteGovernanceEvidenceStore(db).for_run(request.run_id)[0]
        assert isinstance(record, DecisionRecord) and record.decision.outcome == "deny"
        assert record.application.disposition == "blocked"
        assert db.execute("SELECT count(*) FROM b_d17_envelopes").fetchone() == (0,)
    case.transport.close()


@pytest.mark.parametrize(
    "fault", ["revision", "digest", "fingerprint", "actor", "key", "response", "lost"]
)
def test_unverified_or_lost_initial_response_only_allows_readonly_recovery(tmp_path, fault):
    case, request, wire, _ = make_case(tmp_path)
    case.prepare(request)
    wire.fault = fault
    assert case.dispatch(request).result == "error"
    assert case.recover(request).result == "found"
    with pytest.raises(ObservationBlocked):
        case.dispatch(request)
    assert wire.calls.count(("POST", CREATE_PATH)) == 1
    case.transport.close()


@pytest.mark.parametrize("state", ["found", "rejected", "not_found", "unrecoverable", "error"])
def test_all_lookup_states_are_observations_not_retry_permission(tmp_path, state):
    case, request, wire, _ = make_case(tmp_path)
    case.prepare(request)
    wire.fault = "lost"
    case.dispatch(request)
    wire.mode = state
    first = case.recover(request)
    assert first.result == state and not first.replay_allowed and not first.continuation_allowed
    assert case.recover(request) == first
    with pytest.raises(ObservationBlocked):
        case.dispatch(request)
    assert wire.calls.count(("POST", CREATE_PATH)) == 1
    case.transport.close()


def test_concurrent_workers_have_only_one_commercial_call(tmp_path):
    case, request, wire, _ = make_case(tmp_path)
    case.prepare(request)

    def run():
        try:
            return case.dispatch(request).result
        except Exception:
            return "blocked"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(results) == ["blocked", "found"]
    assert wire.calls.count(("POST", CREATE_PATH)) == 1
    case.transport.close()


def test_crash_after_claim_before_send_never_releases_claim(tmp_path, monkeypatch):
    case, request, wire, _ = make_case(tmp_path)
    case.prepare(request)

    def crash(*args):
        raise SystemExit(77)

    monkeypatch.setattr(case.transport, "create_once", crash)
    with pytest.raises(SystemExit):
        case.dispatch(request)
    assert wire.calls == []
    wire.mode = "not_found"
    assert case.recover(request).result == "not_found"
    with pytest.raises(ObservationBlocked):
        case.dispatch(request)
    assert wire.calls == [("POST", LOOKUP_PATH)]
    case.transport.close()


def test_crash_after_merchant_commit_and_reopened_sqlite(tmp_path):
    case, request, wire, _shadow = make_case(tmp_path)
    guard = case.prepare(request)
    wire.fault = "crash"
    with pytest.raises(SystemExit):
        case.dispatch(request)
    case.transport.close()
    seal = case.shadow.source.recover(request.episode_id, request.owner_id)
    reopened = open_case(tmp_path, open_link(tmp_path), seal, wire)
    reopened.initialize()
    assert reopened.recover_preparation(request) == guard
    assert reopened.recover(request).result == "found"
    with pytest.raises(ObservationBlocked):
        reopened.dispatch(request)
    assert wire.calls.count(("POST", CREATE_PATH)) == 1
    reopened.transport.close()


def test_missing_guard_and_legacy_envelope_cannot_be_promoted(tmp_path, monkeypatch):
    case, request, wire, _ = make_case(tmp_path)

    def crash(*args):
        raise SystemExit(79)

    monkeypatch.setattr(case, "recover_preparation", crash)
    with pytest.raises(SystemExit):
        case.prepare(request)
    with pytest.raises(ObservationBlocked):
        case.dispatch(request)
    assert wire.calls == []
    monkeypatch.undo()
    assert case.recover_preparation(request).operation_id
    assert wire.calls == []
    case.transport.close()


@pytest.mark.parametrize(
    "failure", ["credential", "actor", "revoked", "context", "seal", "operation", "key"]
)
def test_backend_context_and_durable_identity_tamper_fail_closed(tmp_path, failure):
    case, request, wire, shadow = make_case(tmp_path)
    case.prepare(request)
    if failure == "credential":
        case.transport.session = replace(case.transport.session, bearer=SecretStr("foreign"))
    if failure == "actor":
        binding = case.transport.session.binding
        case.transport.session = replace(
            case.transport.session,
            binding=binding.model_copy(
                update={"scope": binding.scope.model_copy(update={"remote_actor_id": "foreign"})}
            ),
        )
    if failure == "revoked":
        case.shadow.registry.revoke_test_credential(shadow.provisioning.credential_ref)
    if failure == "context":
        case.configuration = case.configuration.model_copy(update={"policy_set_id": "foreign"})
    if failure == "seal":
        with closing(connect(case.shadow.source._path)) as db, db:
            db.execute("DROP TRIGGER pa_selections_UPDATE")
            db.execute("UPDATE pa_selections SET seal='corrupt'")
    if failure in {"operation", "key"}:
        with closing(connect(case.path)) as db, pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE d1_checkout_operations SET "
                + (
                    "operation_id='foreign'"
                    if failure == "operation"
                    else "idempotency_key='foreign'"
                )
            )
        case.transport.close()
        return
    with pytest.raises(ObservationBlocked):
        case.dispatch(request)
    assert wire.calls == []
    case.transport.close()


@pytest.mark.parametrize("fault", ["scope", "digest", "timeout", "redirect", "large"])
def test_lookup_corruption_and_transport_errors_never_authorize_retry(tmp_path, fault):
    case, request, wire, _ = make_case(tmp_path)
    case.prepare(request)
    wire.fault = "lost"
    case.dispatch(request)
    wire.lookup_fault = fault
    assert case.recover(request).result == "error"
    with pytest.raises(ObservationBlocked):
        case.dispatch(request)
    assert wire.calls.count(("POST", CREATE_PATH)) == 1
    case.transport.close()


def test_crash_before_claim_rolls_back_and_does_not_send(tmp_path, monkeypatch):
    from commerce_agent.persistence.d1_uow_sqlite import SqliteD1UnitOfWork

    case, request, wire, _ = make_case(tmp_path)
    case.prepare(request)

    def crash(*args, **kwargs):
        raise SystemExit(78)

    monkeypatch.setattr(SqliteD1UnitOfWork, "claim", crash)
    with pytest.raises(SystemExit):
        case.dispatch(request)
    with closing(connect(case.path)) as db:
        assert db.execute("SELECT state,claimant FROM d1_checkout_operations").fetchone() == (
            "reserved",
            None,
        )
    assert wire.calls == []
    with pytest.raises(ObservationBlocked):
        case.recover(request)
    monkeypatch.undo()
    assert case.dispatch(request).result == "found"
    assert wire.calls.count(("POST", CREATE_PATH)) == 1
    case.transport.close()


def test_auxiliary_rows_are_immutable_even_with_sqlite_replace(tmp_path):
    case, request, _wire, _ = make_case(tmp_path)
    case.prepare(request)
    case.dispatch(request)
    with closing(connect(case.path)) as db:
        assert db.execute("PRAGMA recursive_triggers").fetchone() == (0,)
        for table in ("b_d17_meta", "b_d17_attempts", "b_d17_envelopes", "b_d17_events"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}")
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(f"DELETE FROM {table}")
    case.transport.close()


def test_controlled_asgi_d17_route_with_independent_sqlite_claim(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.responses import Response
    from fastapi.testclient import TestClient

    case, request, wire, _ = make_case(tmp_path)
    case.prepare(request)
    app = FastAPI()

    @app.post(CREATE_PATH)
    async def route(value: Request):
        response = wire.handle(
            httpx.Request(
                "POST", str(value.url), headers=dict(value.headers), content=await value.body()
            )
        )
        return Response(
            response.content, status_code=response.status_code, headers=dict(response.headers)
        )

    with TestClient(app) as client:

        def exchange(value):
            response = client.request(
                value.method, value.url.path, headers=dict(value.headers), content=value.content
            )
            return httpx.Response(
                response.status_code, headers=dict(response.headers), content=response.content
            )

        case.transport.close()
        case.transport = HttpGuardedCheckout(
            case.transport.session, transport=httpx.MockTransport(exchange)
        )
        assert case.dispatch(request).result == "found"
    assert wire.calls == [("POST", CREATE_PATH)]
    case.transport.close()
