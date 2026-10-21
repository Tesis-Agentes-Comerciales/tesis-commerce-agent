"""Delegation ledger, consent side (spec §4.3, §4.5 consent, §9.1), on real SQLite."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from commerce_agent.delegation.models import EXECUTOR_REF, chain_is_valid, token_digest
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger, utc_text
from tests.support.delegation import (
    CHECKOUT_DETAILS,
    OPEN_CHECKOUT_TOKEN,
    OPEN_PAYMENT_TOKEN,
    OTHER_OWNER,
    OWNER,
    OWNER_REF,
    START,
    AuthorityClock,
    consent_request,
    consented,
    make_ledger,
    proposal_draft,
)


@pytest.fixture
def clock() -> AuthorityClock:
    return AuthorityClock()


@pytest.fixture
def ledger(tmp_path: Path, clock: AuthorityClock) -> SqliteDelegationLedger:
    return make_ledger(tmp_path, clock)


def connect(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(tmp_path / "agent.db")


def test_initialize_creates_the_tables_once_and_bootstraps_the_kill_switch(
    tmp_path: Path, ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    clock.advance(60)
    ledger.initialize()

    with closing(connect(tmp_path)) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        controls = connection.execute("SELECT * FROM hnp_controls").fetchall()
    assert {
        "delegation_proposals",
        "delegation_grants",
        "delegation_mandates",
        "grant_reservations",
        "hnp_tasks",
        "dispatch_outbox",
        "delegation_events",
        "hnp_controls",
    } <= tables
    assert controls == [(1, 0, "bootstrap", utc_text(START))]
    assert ledger.kill_switch().enabled is False


def test_consent_creates_the_grant_open_mandates_scheduled_task_and_two_events(
    ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    clock.advance(30)
    result = consented(ledger)

    grant, task = result.grant, result.task
    assert grant is not None and task is not None
    now = utc_text(clock.now)
    assert (grant.state, grant.revoked_reason, grant.grant_version) == ("active", None, 1)
    assert (grant.authority_epoch, grant.state_version) == (1, 1)
    assert grant.valid_from == grant.consented_at == now
    assert grant.expires_at == "2026-10-21T16:00:00.000Z"  # fixed at proposal: presented + 1 h
    assert (task.state, task.run_id, task.claim_count, task.lease_id) == (
        "scheduled",
        None,
        0,
        None,
    )
    proposal = ledger.proposal(OWNER, "dpr_1")
    assert proposal is not None and proposal.grant_id == grant.grant_id

    mandates = ledger.mandates(grant.grant_id)
    assert [(m.kind, m.reservation_id, m.token_digest) for m in mandates] == [
        ("open_checkout", None, token_digest(OPEN_CHECKOUT_TOKEN)),
        ("open_payment", None, token_digest(OPEN_PAYMENT_TOKEN)),
    ]

    events = ledger.events(grant.grant_id)
    assert [(e.event_type, e.actor_kind, e.actor_ref) for e in events] == [
        ("consented", "user", OWNER_REF),
        ("mandates_issued", "service", EXECUTOR_REF),
    ]
    assert {e.on_behalf_of for e in events} == {OWNER_REF}
    assert {(e.task_id, e.run_id) for e in events} == {(task.task_id, None)}
    assert events[0].body == {
        "proposal_id": "dpr_1",
        "scope_digest": grant.scope_digest,
        "presented_at": utc_text(START),
        "consented_at": now,
        "grant_version": 1,
        "authority_epoch": 1,
        "task_id": task.task_id,
    }
    assert events[1].body == {
        "mandates": [
            {
                "kind": "open_checkout",
                "token_digest": token_digest(OPEN_CHECKOUT_TOKEN),
                "link_hash": "open-checkout-hash",
                "issuer_kid": "issuer-thumbprint",
            },
            {
                "kind": "open_payment",
                "token_digest": token_digest(OPEN_PAYMENT_TOKEN),
                "link_hash": "open-payment-hash",
                "issuer_kid": "issuer-thumbprint",
            },
        ],
        "cnf_kid": "agent-key-thumbprint",
        "exp": grant.expires_at,
        "payment_reference": "open-checkout-hash",
    }
    assert chain_is_valid(grant.grant_id, events)


def test_repeating_the_consent_returns_the_same_grant_without_new_rows(
    ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    draft = proposal_draft()
    first = consented(ledger, draft)
    clock.advance(3600)  # even after the proposal and the grant expired
    resigned = replace(
        consent_request(draft),
        open_checkout=replace(consent_request(draft).open_checkout, token="another-signature"),
    )

    again = ledger.consent(resigned)

    assert again.status == "existing"
    assert again.grant == first.grant and again.task == first.task
    assert first.grant is not None
    assert len(ledger.mandates(first.grant.grant_id)) == 2
    assert ledger.mandates(first.grant.grant_id)[0].token == OPEN_CHECKOUT_TOKEN
    assert len(ledger.events(first.grant.grant_id)) == 2


def test_consent_refuses_another_owner_another_digest_and_an_expired_proposal(
    ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    draft = proposal_draft()
    ledger.create_proposal(draft)

    assert ledger.consent(consent_request(draft, owner_id=OTHER_OWNER)).status == "not_found"
    assert ledger.consent(replace(consent_request(draft), proposal_id="dpr_x")).status == (
        "not_found"
    )
    other_digest = replace(consent_request(draft), scope_digest="sha256:" + "0" * 64)
    assert ledger.consent(other_digest).status == "scope_mismatch"
    clock.advance(600)  # now == expires_at of the proposal: expired
    assert ledger.consent(consent_request(draft)).status == "proposal_expired"
    assert ledger.grants(OWNER) == ()
    proposal = ledger.proposal(OWNER, "dpr_1")
    assert proposal is not None and proposal.grant_id is None


def test_concurrent_consents_create_one_grant(tmp_path: Path, clock: AuthorityClock) -> None:
    ledger = make_ledger(tmp_path, clock)
    draft = proposal_draft()
    ledger.create_proposal(draft)
    barrier = Barrier(2)

    def consent(_: int) -> str:
        barrier.wait(timeout=5)
        return ledger.consent(consent_request(draft)).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(consent, (1, 2)))

    assert statuses == ["created", "existing"]
    assert len(ledger.grants(OWNER)) == 1


def test_reads_of_another_owner_answer_not_found(ledger: SqliteDelegationLedger) -> None:
    grant = consented(ledger).grant
    assert grant is not None

    assert ledger.grant(OTHER_OWNER, grant.grant_id) is None
    assert ledger.proposal(OTHER_OWNER, "dpr_1") is None
    assert ledger.grants(OTHER_OWNER) == ()
    assert ledger.grant(OWNER, grant.grant_id) == grant
    assert ledger.grant(OWNER, "dg_missing") is None


def test_grants_list_newest_first(ledger: SqliteDelegationLedger, clock: AuthorityClock) -> None:
    older = consented(ledger, proposal_draft("dpr_1")).grant
    clock.advance(5)
    newer = consented(ledger, proposal_draft("dpr_2", presented_at=clock.now)).grant
    assert older is not None and newer is not None

    assert [grant.grant_id for grant in ledger.grants(OWNER)] == [newer.grant_id, older.grant_id]


def test_create_proposal_requires_the_canonical_scope_of_that_proposal(
    ledger: SqliteDelegationLedger,
) -> None:
    draft = proposal_draft()
    with pytest.raises(ValueError, match="another proposal"):
        ledger.create_proposal(replace(draft, proposal_id="dpr_other"))
    with pytest.raises(ValueError, match="canonical"):
        ledger.create_proposal(replace(draft, scope_json=" " + draft.scope_json))
    assert ledger.proposal(OWNER, "dpr_1") is None


def test_the_event_chain_verifies_and_detects_any_change(
    tmp_path: Path, ledger: SqliteDelegationLedger
) -> None:
    grant = consented(ledger).grant
    assert grant is not None
    events = ledger.events(grant.grant_id)
    assert chain_is_valid(grant.grant_id, events)

    tampered_body = (replace(events[0], body={**events[0].body, "grant_version": 2}), events[1])
    reordered = (events[1], events[0])
    assert not chain_is_valid(grant.grant_id, tampered_body)
    assert not chain_is_valid(grant.grant_id, reordered)
    assert not chain_is_valid(grant.grant_id, events[1:])
    assert not chain_is_valid("dg_other", events)
    assert not chain_is_valid(grant.grant_id, ())

    with closing(connect(tmp_path)) as connection:
        for statement in (
            "UPDATE delegation_events SET body = '{}'",
            "DELETE FROM delegation_events",
            "INSERT OR REPLACE INTO delegation_events SELECT * FROM delegation_events "
            "WHERE sequence = 1",
            "INSERT INTO delegation_events (grant_id, sequence, event_type, actor_kind, actor_ref, "
            "on_behalf_of, body, recorded_at, previous_digest, digest) VALUES "
            f"('{grant.grant_id}', 4, 'expired', 'service', 'x', 'y', '{{}}', 't', "
            f"'{events[1].digest}', 'sha256:z')",
        ):
            with pytest.raises(sqlite3.IntegrityError), connection:
                connection.execute(statement)
    assert ledger.events(grant.grant_id) == events


def test_events_and_reprs_never_carry_tokens_or_buyer_details(
    ledger: SqliteDelegationLedger,
) -> None:
    result = consented(ledger)
    assert result.grant is not None
    events = ledger.events(result.grant.grant_id)
    mandates = ledger.mandates(result.grant.grant_id)
    proposal = ledger.proposal(OWNER, "dpr_1")

    dumped = repr((events, result, mandates, proposal))
    for secret in (OPEN_CHECKOUT_TOKEN, OPEN_PAYMENT_TOKEN, CHECKOUT_DETAILS, "ana@example.com"):
        assert secret not in dumped
    assert OWNER not in repr([event.body for event in events])
    assert OWNER not in {e.actor_ref for e in events} | {e.on_behalf_of for e in events}


def test_the_state_survives_a_restart(tmp_path: Path, clock: AuthorityClock) -> None:
    first = make_ledger(tmp_path, clock)
    result = consented(first)
    first.set_kill_switch(True)
    assert result.grant is not None

    restarted = make_ledger(tmp_path, clock)

    assert restarted.grant(OWNER, result.grant.grant_id) == result.grant
    assert restarted.task_for_grant(result.grant.grant_id) == result.task
    assert restarted.events(result.grant.grant_id) == first.events(result.grant.grant_id)
    assert restarted.mandates(result.grant.grant_id) == first.mandates(result.grant.grant_id)
    assert restarted.kill_switch() == first.kill_switch()
    assert restarted.kill_switch().control_epoch == 2


def test_the_kill_switch_appends_one_row_per_change(
    tmp_path: Path, ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    clock.advance(10)
    on = ledger.set_kill_switch(True)
    switched_at = utc_text(clock.now)
    clock.advance(10)
    same = ledger.set_kill_switch(True)
    off = ledger.set_kill_switch(False)

    assert (on.enabled, on.control_epoch, on.changed_at) == (True, 2, switched_at)
    assert same == on
    assert (off.enabled, off.control_epoch) == (False, 3)
    assert ledger.kill_switch() == off
    with closing(connect(tmp_path)) as connection:
        assert connection.execute(
            "SELECT control_epoch, actor_ref FROM hnp_controls"
        ).fetchall() == [
            (1, "bootstrap"),
            (2, "operator"),
            (3, "operator"),
        ]
        for statement in (
            "UPDATE hnp_controls SET kill_switch = 0",
            "DELETE FROM hnp_controls",
            "INSERT INTO hnp_controls VALUES (7, 1, 'operator', 't')",
            "INSERT INTO hnp_controls VALUES (4, 1, 'someone', 't')",
        ):
            with pytest.raises(sqlite3.IntegrityError), connection:
                connection.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE delegation_grants SET grant_version = 2",
        "UPDATE delegation_grants SET state = 'revoked'",
        "UPDATE delegation_grants SET revoked_reason = 'owner'",
        "UPDATE delegation_grants SET state = 'paused'",
        "UPDATE hnp_tasks SET state = 'running'",
        "UPDATE hnp_tasks SET state = 'failed'",
        "UPDATE hnp_tasks SET outcome_code = 'made_up', state = 'failed'",
        "UPDATE delegation_mandates SET reservation_id = 'rsv_1' WHERE kind = 'open_checkout'",
        "INSERT INTO grant_reservations VALUES ('rsv_1', 'dg_1', 'o', 'run_1', 'c', 'op_1', 1, "
        "'USD', 1, 'reserved', 1, NULL, NULL, NULL, 't', 't')",
        "INSERT INTO grant_reservations VALUES ('rsv_1', 'dg_1', 'o', 'run_1', 'c', 'op_1', 1, "
        "'COP', 1, 'released', 1, NULL, NULL, NULL, 't', 't')",
        "INSERT INTO grant_reservations VALUES ('rsv_1', 'dg_1', 'o', 'run_1', 'c', 'op_1', 1, "
        "'COP', 1, 'uncertain', 1, NULL, NULL, NULL, 't', 't')",
    ],
)
def test_the_schema_rejects_rows_outside_the_spec(
    tmp_path: Path, ledger: SqliteDelegationLedger, statement: str
) -> None:
    consented(ledger)
    with closing(connect(tmp_path)) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(statement)
