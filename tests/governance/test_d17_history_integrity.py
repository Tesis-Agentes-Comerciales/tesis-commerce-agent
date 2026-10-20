"""I10: local D17 history tampering fails closed; an intact chain verifies. Controlled wire.

Covers the local variant recorded in the HP/B2 evidence (one HMAC hex byte of an event
altered in applied.sqlite after the Agent closed, schema and triggers intact) and the other
chain checks of SqliteGuardedD1._events. Merchant-side or wire corruption is covered by the
lookup fault tests in test_guarded_d1; this module touches only copies under tmp_path.
"""

import json
from contextlib import closing

import pytest

from commerce_agent.acp.authority_observation import ObservationBlocked
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.guarded_creation import CREATE_PATH, LOOKUP_PATH
from commerce_agent.governance.human_consent import ConsentBlocked
from commerce_agent.persistence.d2_d17_source import VerifiedD17Source
from tests.governance.test_guarded_d1 import connect, make_case


def rows(case):
    with closing(connect(case.path)) as db:
        return db.execute(
            "SELECT sequence,body,seal FROM b_d17_events ORDER BY sequence"
        ).fetchall()


def schema(case):
    with closing(connect(case.path)) as db:
        return db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()


def flip_hex_byte(case, seal):
    """Change one hex character of a stored seal in the closed file, outside SQLite."""
    with closing(connect(case.path)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    data = case.path.read_bytes()
    assert data.count(seal.encode()) == 1
    index = data.index(seal.encode())
    flipped = b"1" if data[index : index + 1] == b"0" else b"0"
    case.path.write_bytes(data[:index] + flipped + data[index + 1 :])
    return index, data[index : index + 1]


def restore_byte(case, index, original):
    data = case.path.read_bytes()
    case.path.write_bytes(data[:index] + original + data[index + 1 :])


def rewrite(case, mode):
    """Key-holder rewrite through SQL; every trigger is restored so the schema is identical."""
    with closing(connect(case.path)) as db, db:
        triggers = db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='b_d17_events'"
        ).fetchall()
        for name, _sql in triggers:
            db.execute(f'DROP TRIGGER "{name}"')
        if mode == "gap":
            db.execute("DELETE FROM b_d17_events WHERE sequence=1")
        else:
            sequence, body = db.execute(
                "SELECT sequence,body FROM b_d17_events ORDER BY sequence LIMIT 1"
            ).fetchone()
            event = json.loads(body)
            if mode == "chain":
                # Earlier event rewritten and resealed: the next previous_digest breaks.
                event["recorded_at"] = "2000-01-01T00:00:00.000Z"
            if mode == "sequence":
                event["sequence"] = sequence + 1
            if mode == "run":
                event["run_id"] = "foreign-run"
            altered = canonical_bytes(event)
            db.execute(
                "UPDATE b_d17_events SET body=?,seal=? WHERE sequence=?",
                (altered, case._mac("event", altered), sequence),
            )
        for _name, sql in triggers:
            db.execute(sql)


def assert_fails_closed(case, request, wire):
    before = rows(case)
    creates = wire.calls.count(("POST", CREATE_PATH))
    lookups = wire.calls.count(("POST", LOOKUP_PATH))
    with pytest.raises(ObservationBlocked, match="D17_HISTORY_CONFLICT"):
        case.history(request)
    # The read-only lookup may run; its observation is never appended to a broken chain.
    with pytest.raises(ObservationBlocked, match="D17_HISTORY_CONFLICT"):
        case.recover(request)
    assert wire.calls.count(("POST", LOOKUP_PATH)) == lookups + 1
    assert rows(case) == before
    # The durable claim survives: no replacement creation, no new commercial POST.
    with pytest.raises(ObservationBlocked, match="INITIAL_DISPATCH_ALREADY_CLAIMED"):
        case.dispatch(request)
    assert wire.calls.count(("POST", CREATE_PATH)) == creates


def test_evidence_variant_one_seal_byte_after_close_fails_closed(tmp_path, monkeypatch):
    case, request, wire, _ = make_case(tmp_path)
    case.prepare(request)

    def crash(*args):
        raise SystemExit(77)

    monkeypatch.setattr(case.transport, "create_once", crash)
    with pytest.raises(SystemExit):
        case.dispatch(request)
    monkeypatch.undo()
    wire.mode = "not_found"
    assert case.recover(request).result == "not_found"
    history, layout = case.history(request), schema(case)
    index, original = flip_hex_byte(case, rows(case)[0][2])
    with closing(connect(case.path)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert schema(case) == layout
    assert_fails_closed(case, request, wire)
    assert wire.calls == [("POST", LOOKUP_PATH)] * 2
    # Exactly the altered byte was the reason; nothing was written meanwhile.
    restore_byte(case, index, original)
    assert case.history(request) == history
    case.transport.close()


@pytest.mark.parametrize("mode", ["seal", "gap", "chain", "sequence", "run"])
def test_tampered_found_history_fails_closed_for_d17_and_d2(tmp_path, mode):
    case, request, wire, _ = make_case(tmp_path)
    case.prepare(request)
    assert case.dispatch(request).result == "found"
    assert case.recover(request).result == "found"
    assert VerifiedD17Source(case, request).terms()
    layout = schema(case)
    if mode == "seal":
        flip_hex_byte(case, rows(case)[0][2])
    else:
        rewrite(case, mode)
    assert schema(case) == layout
    with pytest.raises(ConsentBlocked, match="VERIFIED_FOUND_D17_REQUIRED"):
        VerifiedD17Source(case, request).terms()
    assert_fails_closed(case, request, wire)
    assert wire.calls.count(("POST", CREATE_PATH)) == 1
    case.transport.close()


def test_intact_history_verifies_as_an_append_only_chain(tmp_path):
    case, request, wire, _ = make_case(tmp_path)
    case.prepare(request)
    found = case.dispatch(request)
    assert case.recover(request) == found
    assert case.recover(request) == found
    events = case.history(request)
    bodies = [body for _sequence, body, _seal in rows(case)]
    assert [(event["sequence"], event["kind"]) for event in events] == [
        (1, "initial"),
        (2, "lookup"),
        (3, "lookup"),
    ]
    assert [event["previous_digest"] for event in events] == [
        None,
        sha256_digest(bodies[0]),
        sha256_digest(bodies[1]),
    ]
    assert {event["run_id"] for event in events} == {request.run_id}
    assert found.proof is not None
    terms = VerifiedD17Source(case, request).terms()
    assert terms.checkout_id == found.proof.creation.checkout_id
    assert terms.d17_history_digest == sha256_digest(canonical_bytes(list(events)))
    with pytest.raises(ObservationBlocked, match="INITIAL_DISPATCH_ALREADY_CLAIMED"):
        case.dispatch(request)
    assert wire.calls.count(("POST", CREATE_PATH)) == 1
    case.transport.close()
