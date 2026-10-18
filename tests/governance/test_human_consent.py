"""Synthetic authenticated humans + controlled D17 wire, never real UI evidence."""

import asyncio
import hashlib
import json
import secrets
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Barrier

import httpx
import pytest
from pydantic import SecretBytes, SecretStr, ValidationError

from commerce_agent.d2_consent_api import create_consent_app
from commerce_agent.governance.canonical import canonical_bytes, parse_model
from commerce_agent.governance.human_consent import (
    ACTION,
    ConsentBlocked,
    ConsentCommand,
    HumanIdentity,
    SyntheticHumanIssuer,
    timestamp,
)
from commerce_agent.persistence.d2_d17_source import VerifiedD17Source
from commerce_agent.persistence.human_consent_sqlite import SqliteHumanConsent
from tests.governance import test_guarded_d1 as previous
from tests.governance.test_preselection_d1 import open_link


@pytest.fixture(scope="module")
def historical(tmp_path_factory):
    """Create one synthetic D17 history BEFORE D2; clone it per adversarial test."""
    root = tmp_path_factory.mktemp("d2-history")
    original = previous.merchant_original_response

    def response():
        raw = original()
        now = datetime.now(UTC)
        raw["created_at"] = raw["updated_at"] = timestamp(now - timedelta(seconds=1))
        raw["expires_at"] = timestamp(now + timedelta(hours=1))
        return raw

    # Deliberately controlled fixture, not alteration of an existing checkout.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(previous, "merchant_original_response", response)
        case, request, wire, _shadow = previous.make_case(root)
        case.prepare(request)
        assert case.dispatch(request).result == "found"
        assert case.recover(request).result == "found"
        assert len(wire.calls) == 2  # setup only: one create, one historical lookup
        case.transport.close()
    return root, request


@pytest.fixture
def flow(tmp_path, historical, monkeypatch):
    root, request = historical
    clone = tmp_path / "history"
    shutil.copytree(root, clone)
    link = open_link(clone)
    selection = link.source.recover(request.episode_id, request.owner_id)
    wire = previous.Wire()
    case = previous.open_case(clone, link, selection, wire)
    source = VerifiedD17Source(case, request)
    key = SecretBytes(secrets.token_bytes(32))
    issuer = SyntheticHumanIssuer(SecretBytes(secrets.token_bytes(32)))
    clock = [datetime.now(UTC)]
    token = issuer.issue(
        principal_id="synthetic-human-a",
        owner_id=request.owner_id,
        run_id=request.run_id,
        authenticated_at=timestamp(clock[0] - timedelta(seconds=1)),
        expires_at=timestamp(clock[0] + timedelta(hours=2)),
    )
    consent = SqliteHumanConsent(
        tmp_path / "d2.sqlite",
        source=source,
        custody_key=key,
        authenticator=issuer,
        clock=lambda: clock[0],
    )
    consent.initialize()

    def forbidden(*_args, **_kwargs):
        pytest.fail("D2 must not invoke ANY HTTP transport")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in clone.glob("*.sqlite")}
    yield consent, token, issuer, clock, wire, key
    assert wire.calls == []
    # Tampering tests explicitly mark their controlled source mutation.
    if not getattr(source, "test_tampered", False):
        assert {
            p: hashlib.sha256(p.read_bytes()).hexdigest() for p in clone.glob("*.sqlite")
        } == before
    case.transport.close()


def command(view, **changes):
    return ConsentCommand.model_validate(
        {
            "presentation_id": view.presentation.presentation_id,
            "checkout_id": view.presentation.terms.checkout_id,
            "terms_fingerprint": view.presentation.terms_fingerprint,
            "future_action": ACTION,
            "challenge": view.challenge,
            "decision": "accept",
            "affirmative": True,
            **changes,
        }
    )


def reopen(flow):
    store, _token, issuer, clock, _wire, key = flow
    result = SqliteHumanConsent(
        store.path,
        source=store.source,
        custody_key=key,
        authenticator=issuer,
        clock=lambda: clock[0],
    )
    result.initialize()
    return result


def test_presentation_commit_restart_accept_restart_and_no_authority(flow):
    store, token, _issuer, _clock, _wire, _key = flow
    view = store.present(token)
    terms = view.presentation.terms
    assert terms.checkout_status == "not_ready_for_payment"
    assert terms.total_minor == terms.unit_amount_minor + terms.shipping_amount_minor
    assert terms.pending_for_commercial_execution
    pending = reopen(flow).recover(token, view.presentation.presentation_id)
    assert pending == (view.presentation, None)
    receipt = reopen(flow).decide(token, command(view))
    assert receipt.decision == "accept" and receipt.affirmative
    assert not receipt.continuation_allowed
    assert receipt.identity.assurance == "synthetic_test_human/1"
    assert reopen(flow).recover(token, view.presentation.presentation_id) == (
        view.presentation,
        receipt,
    )
    with closing(sqlite3.connect(store.source.applied.path)) as db:
        assert db.execute(
            "SELECT stage,state_version,checkout_id,order_id FROM purchases"
        ).fetchone() == ("selected", 2, None, None)
    # No import/wiring of legacy approvals, AP2, PurchaseService, Settings or routes.
    assert not hasattr(store, "authorize") and not hasattr(store, "complete_purchase")
    public = canonical_bytes(view.presentation) + canonical_bytes(receipt)
    assert token.get_secret_value().encode() not in public
    assert view.challenge.get_secret_value().encode() not in public
    assert b"synthetic-session" not in store.path.read_bytes()
    assert view.challenge.get_secret_value().encode() not in store.path.read_bytes()


@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_terminal_result_consumes_nonce_and_rejects_any_second_decision(flow, decision):
    store, token, *_ = flow
    view = store.present(token)
    first = command(view, decision=decision, affirmative=decision == "accept")
    store.decide(token, first)
    for next_decision in ("accept", "reject"):
        with pytest.raises(ConsentBlocked, match="ALREADY_DECIDED"):
            store.decide(
                token, command(view, decision=next_decision, affirmative=next_decision == "accept")
            )


@pytest.mark.parametrize(
    "field,value",
    [
        ("checkout_id", "another-checkout"),
        ("terms_fingerprint", "sha256:" + "a" * 64),
        ("presentation_id", "unregistered"),
        ("challenge", SecretStr("wrong-nonce")),
    ],
)
def test_wrong_decision_context(flow, field, value):
    store, token, *_ = flow
    view = store.present(token)
    with pytest.raises(ConsentBlocked):
        store.decide(token, command(view, **{field: value}))
    assert store.recover(token, view.presentation.presentation_id)[1] is None


def test_nonce_cannot_cross_presentations(flow):
    store, token, *_ = flow
    first, second = store.present(token), store.present(token)
    with pytest.raises(ConsentBlocked):
        store.decide(token, command(second, challenge=first.challenge))


@pytest.mark.parametrize("kind", ["principal", "owner", "run", "expired", "forged", "llm"])
def test_human_authentication_and_scope(flow, kind):
    store, token, issuer, clock, *_ = flow
    view = store.present(token)
    identity = issuer.authenticate(token)
    args = dict(
        principal_id=identity.principal_id,
        owner_id=identity.owner_id,
        run_id=identity.run_id,
        authenticated_at=identity.authenticated_at,
        expires_at=identity.expires_at,
    )
    if kind in {"principal", "owner", "run"}:
        args[{"principal": "principal_id", "owner": "owner_id", "run": "run_id"}[kind]] = "foreign"
    if kind == "expired":
        args["expires_at"] = timestamp(clock[0] - timedelta(seconds=1))
    other = issuer.issue(**args)
    if kind == "forged":
        other = SecretStr(other.get_secret_value() + "invalid")
    if kind == "llm":
        # Even trusted-adapter output is strictly revalidated: model_construct is no bypass.
        forged = HumanIdentity.model_construct(**identity.model_dump(exclude={"kind"}), kind="llm")
        store._auth = type("BadBackend", (), {"authenticate": lambda _self, _cred: forged})()
        other = token
    with pytest.raises(ConsentBlocked):
        store.decide(other, command(view))


def test_expiry_and_recovery_are_separate(flow):
    store, token, _issuer, clock, *_ = flow
    view = store.present(token)
    clock[0] = datetime.fromisoformat(view.presentation.valid_until.replace("Z", "+00:00"))
    with pytest.raises(ConsentBlocked, match="EXPIRED"):
        store.decide(token, command(view))
    assert store.recover(token, view.presentation.presentation_id)[1] is None
    clock[0] += timedelta(hours=2)
    with pytest.raises(ConsentBlocked):
        store.present(token)


def test_expired_checkout_cannot_be_presented(flow, monkeypatch):
    store, token, *_ = flow
    terms = store.source.terms()
    monkeypatch.setattr(
        store.source,
        "terms",
        lambda: terms.model_copy(update={"checkout_expires_at": "2020-01-01T00:00:00Z"}),
    )
    with pytest.raises(ConsentBlocked, match="EXPIRED"):
        store.present(token)


def test_concurrent_acceptance_has_one_winner(flow):
    store, token, *_ = flow
    view = store.present(token)
    gate = Barrier(2)

    def accept(_):
        gate.wait()
        try:
            return reopen(flow).decide(token, command(view)).decision
        except ConsentBlocked:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(accept, (1, 2))) == ["accept", "blocked"]
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("SELECT count(*) FROM d2_decisions").fetchone() == (1,)


@pytest.mark.parametrize(
    "field,value",
    [
        ("unit_amount_minor", 1),
        ("shipping_amount_minor", 1),
        ("total_minor", 1),
        ("destination_city", "Cali"),
        ("offer_revision", 999),
        ("offer_digest", "sha256:" + "c" * 64),
        ("future_action", "complete_purchase"),
        ("checkout_id", "another"),
        ("offer_id", "another"),
        ("checkout_expires_at", "2099-01-01T00:00:00Z"),
    ],
)
def test_terms_substitution_fails_exact_snapshot(flow, monkeypatch, field, value):
    store, token, *_ = flow
    view = store.present(token)
    altered = view.presentation.terms.model_copy(update={field: value})
    monkeypatch.setattr(store.source, "terms", lambda: altered)
    with pytest.raises(ConsentBlocked):
        store.decide(token, command(view))


@pytest.mark.parametrize(
    "mode",
    [
        "absent",
        "rejected",
        "not_found",
        "unrecoverable",
        "error",
        "actor",
        "episode",
        "checkout",
        "revision",
        "digest",
        "price",
        "shipping",
        "total",
    ],
)
def test_d17_history_is_verified_not_trusted_as_echo(flow, mode):
    store, token, *_ = flow
    case = store.source.applied
    store.source.test_tampered = True
    with closing(sqlite3.connect(case.path)) as db, db:
        for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='b_d17_events'"
        ).fetchall():
            db.execute(f'DROP TRIGGER "{name}"')
        if mode == "absent":
            db.execute("DELETE FROM b_d17_events")
        else:
            seq, body = db.execute(
                "SELECT sequence,body FROM b_d17_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            event = json.loads(body)
            observation = event["observation"]
            if mode in {"rejected", "not_found", "unrecoverable", "error"}:
                original_proof = observation["proof"]
                observation.update(
                    result=mode,
                    proof=None,
                    reason="historical_rejection"
                    if mode == "rejected"
                    else ("invalid_or_unavailable" if mode == "error" else "uncertain"),
                )
                if mode == "rejected":
                    observation["rejection"] = {
                        "schema_version": "merchant-guarded-acp-rejection/1",
                        "run_id": original_proof["creation"]["run_id"],
                        "actor_id": original_proof["creation"]["actor_id"],
                        "idempotency_sha256": original_proof["creation"]["idempotency_key_sha256"],
                        "guard_fingerprint": original_proof["guard_fingerprint"],
                        "code": "offer_revision_mismatch",
                    }
            else:
                proof = observation["proof"]
                if mode == "actor":
                    proof["creation"]["actor_id"] = "foreign"
                if mode == "episode":
                    proof["creation"]["run_id"] = "foreign"
                if mode == "checkout":
                    proof["creation"]["checkout_id"] = "foreign"
                if mode == "revision":
                    proof["accepted_offer_revision"] += 1
                if mode == "digest":
                    proof["accepted_offer_snapshot_sha256"] = "f" * 64
                if mode == "price":
                    proof["creation"]["original_response"]["line_items"][0]["item"][
                        "unit_amount"
                    ] += 1
                if mode in {"shipping", "total"}:
                    for row in proof["creation"]["original_response"]["totals"]:
                        if row["type"] == ("fulfillment" if mode == "shipping" else "total"):
                            row["amount"] += 1
            altered = canonical_bytes(event)
            # Re-sign event to exercise deeper proof validation, not just B HMAC failure.
            db.execute(
                "UPDATE b_d17_events SET body=?,seal=? WHERE sequence=?",
                (altered, case._mac("event", altered), seq),
            )
    with pytest.raises(ConsentBlocked, match="VERIFIED_FOUND"):
        store.present(token)


@pytest.mark.parametrize("table", ["d2_presentations", "d2_decisions", "d2_meta"])
def test_sqlite_tampering_and_immutable_triggers(flow, table):
    store, token, *_ = flow
    view = store.present(token)
    store.decide(token, command(view))
    with closing(sqlite3.connect(store.path)) as db, db:
        for sql in (f"UPDATE {table} SET seal='bad'", f"DELETE FROM {table}"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)
        row = db.execute(f"SELECT * FROM {table}").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"INSERT OR REPLACE INTO {table} VALUES({','.join('?' for _ in row)})", row)
        db.execute(f"DROP TRIGGER {table}_update")
        db.execute(f"UPDATE {table} SET seal='bad'")
    with pytest.raises(ConsentBlocked):
        store.recover(token, view.presentation.presentation_id)


def test_wrong_custody_key_and_foreign_database(flow, tmp_path):
    store, _token, issuer, *_ = flow
    bad = SqliteHumanConsent(
        store.path,
        source=store.source,
        custody_key=SecretBytes(secrets.token_bytes(32)),
        authenticator=issuer,
    )
    with pytest.raises(ConsentBlocked):
        bad.initialize()
    with pytest.raises(ValueError):
        SqliteHumanConsent(
            store.source.applied.path,
            source=store.source,
            custody_key=SecretBytes(secrets.token_bytes(32)),
            authenticator=issuer,
        )
    other = tmp_path / "foreign.sqlite"
    with closing(sqlite3.connect(other)) as db:
        db.execute("CREATE TABLE foreign_data(id TEXT)")
    bad.path = other
    with pytest.raises(ConsentBlocked):
        bad.initialize()


@pytest.mark.parametrize("phase", ["presentation", "decision"])
def test_persistence_failure_returns_no_success(flow, phase):
    store, token, *_ = flow
    view = store.present(token)
    with closing(sqlite3.connect(store.path)) as blocker:
        blocker.execute("BEGIN EXCLUSIVE")
        # Force immediate failure, preserving the schema verifier and real SQLite.
        original = store._connect

        def connect():
            db = original()
            db.execute("PRAGMA busy_timeout=1")
            return db

        store._connect = connect
        with pytest.raises(sqlite3.OperationalError):
            if phase == "presentation":
                store.present(token)
            else:
                store.decide(token, command(view))
        blocker.rollback()
    assert store.recover(token, view.presentation.presentation_id)[1] is None


def test_explicit_action_and_affirmation_strictness(flow):
    store, token, *_ = flow
    view = store.present(token)
    with pytest.raises(ValidationError):
        command(view, affirmative=False)
    with pytest.raises(ValidationError):
        command(view, affirmative="yes")
    forged = command(view).model_copy(update={"future_action": "complete_purchase"})
    with pytest.raises(ValidationError):
        store.decide(token, forged)
    with pytest.raises(ValidationError):
        parse_model(
            HumanIdentity,
            canonical_bytes(
                {**store._auth.authenticate(token).model_dump(), "assurance": "model_recommended"}
            ),
        )


def test_backend_issuance_is_not_proof_of_human_ui_interaction(flow):
    store, token, issuer, *_ = flow
    view = store.present(token)
    receipt = store.decide(token, command(view))
    assert receipt.identity.assurance == "synthetic_test_human/1"
    assert issuer.authenticate(token).assurance != "authenticated_human_ui/1"
    assert receipt.future_action == "request_checkout_preparation_review"


@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_controlled_asgi_contract_pending_terminal_errors_and_no_commerce(flow, decision):
    store, token, *_ = flow
    app = create_consent_app(store)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://controlled-asgi.invalid"
        ) as client:
            prefix = "/experimental/d2"
            assert (await client.post(prefix + "/presentations", json={})).status_code == 401
            client.headers["Authorization"] = "Bearer " + token.get_secret_value()
            invalid = await client.post(prefix + "/presentations", json={"owner_id": "invented"})
            assert invalid.status_code == 422 and "invented" not in invalid.text
            issued = await client.post(prefix + "/presentations", json={})
            assert issued.status_code == 201 and issued.headers["Cache-Control"] == "no-store"
            payload = issued.json()
            view = payload["presentation"]
            assert payload["delivery_assurance"] == "backend_issued_not_ui_seen"
            url = prefix + "/presentations/" + view["presentation_id"]
            assert (await client.get(url)).json()["state"] == "pending"
            data = dict(
                presentation_id=view["presentation_id"],
                checkout_id=view["terms"]["checkout_id"],
                terms_fingerprint=view["terms_fingerprint"],
                future_action=ACTION,
                challenge=payload["challenge"],
                decision=decision,
                affirmative=decision == "accept",
            )
            result = await client.post(prefix + "/decisions", json=data)
            assert result.status_code == 201 and not result.json()["continuation_allowed"]
            assert (await client.get(url)).json()["state"] == decision
            assert (await client.post(prefix + "/decisions", json=data)).status_code == 409
            assert (await client.post("/complete", json={})).status_code == 404
            assert (await client.post("/checkout_sessions", json={})).status_code == 404

    asyncio.run(exercise())


def test_hmac_tamper_fails_even_if_schema_triggers_restored(flow):
    store, token, *_ = flow
    view = store.present(token)
    with closing(sqlite3.connect(store.path)) as db, db:
        ddl = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='d2_presentations_update'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER d2_presentations_update")
        db.execute("UPDATE d2_presentations SET seal=?", ("a" * 64,))
        db.execute(ddl)
    with pytest.raises(ConsentBlocked, match="CORRUPT"):
        store.recover(token, view.presentation.presentation_id)


def test_initial_proof_without_historical_lookup_is_insufficient(flow):
    store, token, *_ = flow
    case = store.source.applied
    store.source.test_tampered = True
    with closing(sqlite3.connect(case.path)) as db, db:
        db.execute("DROP TRIGGER b_d17_events_DELETE")
        db.execute("DELETE FROM b_d17_events WHERE sequence=2")
    with pytest.raises(ConsentBlocked, match="VERIFIED_FOUND"):
        store.present(token)


def test_readonly_adapter_denies_actual_dml(flow):
    from commerce_agent.persistence.d2_d17_source import _readonly

    store, *_ = flow
    with closing(sqlite3.connect(store.source.applied.path)) as db:
        db.set_authorizer(_readonly)
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("UPDATE purchases SET state_version=state_version WHERE 0")
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("CREATE TABLE injected(id TEXT)")


def test_wrong_b_custody_cannot_present(flow, monkeypatch):
    store, token, *_ = flow
    monkeypatch.setattr(store.source.applied, "_key", SecretBytes(secrets.token_bytes(32)))
    with pytest.raises(ConsentBlocked, match="VERIFIED_FOUND"):
        store.present(token)
