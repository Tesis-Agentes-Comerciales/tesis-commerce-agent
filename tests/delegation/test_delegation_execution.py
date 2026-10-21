"""Delegation ledger, executor up to the commitment (spec §4.4, §4.5, §8.1, §8.4).

Real SQLite, with threads where it matters. HNP0x cases of policy-acceptance-v1 §5 at the store
level: HNP01 (validity), HNP03 and HNP04 (revocation), HNP05 and HNP06 (single use) and HNP09
(restart). HNP10 (timeout) is in the dispatch tests.
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from typing import Literal

import pytest

from commerce_agent.delegation.models import (
    LEASE_SECONDS,
    OPERATOR_REF,
    KillSwitchStage,
    OutcomeCode,
    chain_is_valid,
)
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger, utc_text
from tests.support.delegation import (
    OTHER_OWNER,
    OWNER,
    OWNER_REF,
    AuthorityClock,
    Flow,
    make_ledger,
    proposal_draft,
)


@pytest.fixture
def clock() -> AuthorityClock:
    return AuthorityClock()


@pytest.fixture
def ledger(tmp_path: Path, clock: AuthorityClock) -> SqliteDelegationLedger:
    return make_ledger(tmp_path, clock)


@pytest.fixture
def flow(ledger: SqliteDelegationLedger, clock: AuthorityClock) -> Flow:
    return Flow.consented(ledger, clock)


def dump(tmp_path: Path) -> dict[str, list[tuple[object, ...]]]:
    tables = (
        "delegation_grants",
        "delegation_mandates",
        "grant_reservations",
        "hnp_tasks",
        "dispatch_outbox",
        "delegation_events",
        "hnp_controls",
    )
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection:
        return {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}


def event_types(flow: Flow) -> list[str]:
    return [event.event_type for event in flow.ledger.events(flow.grant_id)]


# Claim, lease and fencing


def test_the_claim_takes_a_lease_and_a_live_lease_cannot_be_claimed_again(
    flow: Flow, clock: AuthorityClock
) -> None:
    step = flow.claim()

    assert step.status == "applied" and step.execution is not None
    task = step.execution.task
    assert (task.state, task.claim_count, task.run_id) == ("running", 1, "run_1")
    assert task.lease_expires_at == utc_text(clock.now.replace(minute=2))
    claimed = flow.ledger.events(flow.grant_id)[-1]
    assert (claimed.event_type, claimed.actor_ref, claimed.run_id) == (
        "task_claimed",
        "service:hnp-executor",
        "run_1",
    )
    assert claimed.body == {
        "run_id": "run_1",
        "claim_count": 1,
        "lease_expires_at": task.lease_expires_at,
        "trigger": "service_token",
    }
    clock.advance(LEASE_SECONDS - 1)
    assert flow.ledger.claim(flow.task_id).status == "not_runnable"
    assert flow.ledger.claim("ht_missing").status == "not_found"


def test_each_fenced_step_renews_the_lease_and_a_lapsed_one_is_reclaimed_with_the_same_run(
    flow: Flow, clock: AuthorityClock
) -> None:
    flow.claim()
    first_lease = flow.lease_id
    clock.advance(100)
    assert flow.reserve().status == "applied"
    assert flow.state().task.lease_expires_at == utc_text(clock.now.replace(minute=3, second=40))

    clock.advance(LEASE_SECONDS)
    step = flow.claim()

    assert step.status == "applied" and flow.lease_id != first_lease
    assert step.execution is not None
    assert (step.execution.task.claim_count, step.execution.task.run_id) == (2, "run_1")


def test_with_a_foreign_lease_every_step_leaves_without_writing(
    tmp_path: Path, flow: Flow, clock: AuthorityClock
) -> None:
    flow.claim()
    flow.reserve()
    stale = flow.lease_id
    clock.advance(LEASE_SECONDS)
    flow.claim()  # another executor takes over
    before = dump(tmp_path)
    flow.lease_id = stale

    assert flow.reserve(2).status == "lease_lost"
    assert flow.close().status == "lease_lost"
    assert flow.commit().status == "lease_lost"
    assert flow.finish("failed", "state_conflict").status == "lease_lost"
    assert dump(tmp_path) == before


# Single use (HNP05, HNP06)


def test_of_two_concurrent_reservations_one_wins(flow: Flow) -> None:
    """HNP05: two checkouts with different keys try to consume the same grant."""
    flow.claim()
    requests = [flow.reservation(1), flow.reservation(2)]
    barrier = Barrier(2)

    def reserve(index: int) -> str:
        barrier.wait(timeout=5)
        return flow.ledger.reserve(requests[index]).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(reserve, (0, 1)))

    assert statuses == ["applied", "blocked"]
    assert event_types(flow).count("reservation_reserved") == 1
    assert flow.state().grant.state_version == 2


def test_the_index_allows_one_live_reservation_per_grant_even_without_the_cas(
    tmp_path: Path, flow: Flow
) -> None:
    flow.claim()
    flow.reserve()
    with (
        closing(sqlite3.connect(tmp_path / "agent.db")) as connection,
        pytest.raises(sqlite3.IntegrityError, match=r"grant_reservations\.grant_id"),
    ):
        connection.execute(
            "INSERT INTO grant_reservations SELECT 'rsv_9', grant_id, owner_id, run_id, "
            "'checkout_9', 'op_9', amount_minor, currency, expected_epoch, 'reserved', 1, "
            "NULL, NULL, NULL, created_at, updated_at FROM grant_reservations"
        )


def test_a_retry_of_the_same_operation_returns_the_same_reservation(flow: Flow) -> None:
    """HNP06: no double count, no new event, hooks only on the first write."""
    flow.claim()
    hooked: list[int] = []
    request = flow.reservation()
    first = flow.ledger.reserve(request, hooks=[lambda _: hooked.append(1)])

    again = flow.ledger.reserve(request, hooks=[lambda _: hooked.append(2)])
    other_amount = flow.ledger.reserve(
        flow.reservation(1, amount_minor=1), hooks=[lambda _: hooked.append(3)]
    )

    assert (first.status, again.status, other_amount.status) == ("applied", "reused", "blocked")
    assert other_amount.outcome == "state_conflict"
    assert first.execution is not None and again.execution is not None
    assert again.execution.reservation == first.execution.reservation
    assert hooked == [1]
    assert event_types(flow).count("reservation_reserved") == 1
    assert flow.state().grant.state_version == 2


def test_the_reservation_records_the_authority_it_reserved_under(flow: Flow) -> None:
    flow.claim()
    step = flow.reserve()

    assert step.execution is not None and step.execution.reservation is not None
    reservation = step.execution.reservation
    assert (reservation.state, reservation.expected_epoch, reservation.state_version) == (
        "reserved",
        1,
        1,
    )
    assert (reservation.run_id, reservation.owner_id, reservation.d2_evidence_id) == (
        "run_1",
        OWNER,
        "gov_d2_1",
    )
    assert flow.ledger.events(flow.grant_id)[-1].body == {
        "reservation_id": "rsv_1",
        "operation_id": "op_1",
        "amount_minor": 120_000,
        "expected_epoch": 1,
        "state_version": 2,
        "d2_evidence_id": "gov_d2_1",
    }


def test_closed_mandates_are_saved_once_and_reused_on_resume(flow: Flow) -> None:
    flow.claim()
    flow.reserve()
    assert flow.close().status == "applied"
    assert flow.close().status == "reused"

    closed = [m for m in flow.ledger.mandates(flow.grant_id) if m.kind.startswith("closed")]
    assert [(m.kind, m.reservation_id) for m in closed] == [
        ("closed_checkout", "rsv_1"),
        ("closed_payment", "rsv_1"),
    ]
    assert event_types(flow).count("mandates_closed") == 1
    assert flow.ledger.events(flow.grant_id)[-1].body["mandates"][0]["agent_key_kid"] == (
        "agent-key-thumbprint"
    )


def test_the_commitment_needs_the_closed_mandates_and_the_current_reservation_version(
    flow: Flow,
) -> None:
    flow.claim()
    flow.reserve()
    assert flow.commit().outcome == "state_conflict"  # not closed yet
    flow.close()
    stale = replace(flow.commitment(), reservation_version=9)
    assert flow.ledger.commit_dispatch(stale).outcome == "state_conflict"
    assert flow.state().outbox is None
    assert flow.commit().status == "applied"


@pytest.mark.parametrize("field", ["state_version", "authority_epoch"])
def test_the_commitment_compares_the_grant_it_read(flow: Flow, field: str) -> None:
    """UoW-2's CAS: an `expected` read before the reservation (or another epoch) is stale."""
    flow.claim()
    before_reservation = flow.state().authority
    flow.reserve()
    flow.close()
    current = flow.state().authority
    expected = (
        before_reservation
        if field == "state_version"
        else replace(current, authority_epoch=current.authority_epoch + 1)
    )

    step = flow.ledger.commit_dispatch(replace(flow.commitment(), expected=expected))

    assert (step.status, step.outcome) == ("blocked", "state_conflict")
    ex = flow.state()
    assert ex.reservation is not None
    assert (ex.outbox, ex.reservation.state) == (None, "reserved")


# Revocation (HNP03, HNP04)


def test_revoking_before_any_reservation_cancels_a_scheduled_task(flow: Flow) -> None:
    revocation = flow.ledger.revoke(OWNER, flow.grant_id)

    assert revocation is not None
    assert (revocation.status, revocation.effect) == ("revoked", "no_effect")
    assert (revocation.grant.state, revocation.grant.revoked_reason) == ("revoked", "owner")
    assert (revocation.grant.authority_epoch, revocation.grant.state_version) == (2, 2)
    task = flow.state().task
    assert (task.state, task.outcome_code, task.run_id) == ("canceled", "revoked_by_owner", None)
    assert flow.claim().status == "not_runnable"
    revoked = flow.ledger.events(flow.grant_id)[-1]
    assert (revoked.event_type, revoked.actor_kind, revoked.actor_ref) == (
        "revoked",
        "user",
        OWNER_REF,
    )
    assert revoked.body == {
        "revoked_reason": "owner",
        "epoch_before": 1,
        "epoch_after": 2,
        "effect": "no_effect",
    }


@pytest.mark.parametrize("point", ["before_d2", "between_d2_and_d3", "between_d3_and_cas"])
def test_revoking_before_the_commitment_leaves_no_outbox(flow: Flow, point: str) -> None:
    """HNP03: the epoch is re-checked under each unit of work; nothing is committed."""
    flow.claim()
    if point != "before_d2":
        flow.reserve()
    if point == "between_d3_and_cas":
        flow.close()
    reservation = flow.reservation()
    commitment = flow.commitment()

    revocation = flow.ledger.revoke(OWNER, flow.grant_id)

    assert revocation is not None
    assert revocation.effect == ("no_effect" if point == "before_d2" else "released")
    blocked = (
        flow.ledger.reserve(reservation)
        if point == "before_d2"
        else flow.ledger.commit_dispatch(commitment)
    )
    assert (blocked.status, blocked.outcome) == ("blocked", "revoked_by_owner")
    closed = flow.finish("canceled", "revoked_by_owner")
    assert closed.status == "closed" and closed.execution is not None
    ex = closed.execution
    assert (ex.task.state, ex.task.outcome_code, ex.outbox) == (
        "canceled",
        "revoked_by_owner",
        None,
    )
    assert (ex.grant.state, ex.grant.revoked_reason, ex.grant.authority_epoch) == (
        "revoked",
        "owner",
        2,
    )
    if point != "before_d2":
        assert ex.reservation is not None
        assert (ex.reservation.state, ex.reservation.release_basis) == ("released", "no_outbox")
        released = [
            e for e in flow.ledger.events(flow.grant_id) if e.event_type == "reservation_released"
        ]
        assert [(e.actor_kind, e.body["release_basis"]) for e in released] == [
            ("user", "no_outbox")
        ]


def test_revoking_after_the_commitment_leaves_the_purchase_in_flight(flow: Flow) -> None:
    """HNP04: no promise to cancel; the reservation and the outbox row are kept."""
    flow.committed()

    revocation = flow.ledger.revoke(OWNER, flow.grant_id)

    assert revocation is not None and revocation.effect == "in_flight"
    ex = flow.state()
    assert ex.reservation is not None and ex.outbox is not None
    assert (ex.reservation.state, ex.outbox.state, ex.task.state) == (
        "committed",
        "committed",
        "running",
    )
    assert (ex.grant.state, ex.grant.revoked_reason, ex.grant.authority_epoch) == (
        "revoked",
        "owner",
        2,
    )
    assert flow.ledger.events(flow.grant_id)[-1].body["effect"] == "in_flight"


def test_the_revocation_is_idempotent_and_owner_scoped(flow: Flow) -> None:
    flow.claim()
    flow.reserve()
    first = flow.ledger.revoke(OWNER, flow.grant_id)
    events = flow.ledger.events(flow.grant_id)

    assert flow.ledger.revoke(OWNER, flow.grant_id) == first
    assert flow.ledger.events(flow.grant_id) == events
    assert first is not None and first.effect == "released"
    assert flow.ledger.revoke(OTHER_OWNER, flow.grant_id) is None
    assert flow.ledger.revoke(OWNER, "dg_missing") is None


def test_a_grant_closed_by_another_cause_or_finished_is_not_revoked_again(
    ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    switched = Flow.consented(ledger, clock)
    ledger.set_kill_switch(True)
    switched.claim()
    ledger.set_kill_switch(False)

    already = ledger.revoke(OWNER, switched.grant_id)

    assert already is not None and already.status == "already_closed"
    assert (already.effect, already.grant.revoked_reason) == ("no_effect", "operator")
    lapsed = Flow.consented(ledger, clock, proposal_draft("dpr_2", presented_at=clock.now))
    clock.advance(3600)
    events = ledger.events(lapsed.grant_id)

    not_revocable = ledger.revoke(OWNER, lapsed.grant_id)

    assert not_revocable is not None and not_revocable.status == "not_revocable"
    assert (not_revocable.effect, not_revocable.grant.state) == (None, "active")
    assert ledger.events(lapsed.grant_id) == events


# Kill switch


def test_the_kill_switch_blocks_the_claim_and_switching_it_off_does_not_revive(
    flow: Flow,
) -> None:
    flow.ledger.set_kill_switch(True)

    step = flow.claim()

    assert (step.status, step.outcome) == ("closed", "kill_switch")
    assert step.execution is not None
    assert (step.execution.task.state, step.execution.task.run_id) == ("canceled", None)
    assert (step.execution.grant.state, step.execution.grant.revoked_reason) == (
        "revoked",
        "operator",
    )
    events = flow.ledger.events(flow.grant_id)
    assert [(e.event_type, e.actor_ref) for e in events[2:]] == [
        ("kill_switch_blocked", "service:hnp-executor"),
        ("revoked", OPERATOR_REF),
        ("task_finished", "service:hnp-executor"),
    ]
    assert events[2].body == {"control_epoch": 2, "stage": "claim"}
    assert events[-1].body == {"state": "canceled", "outcome_code": "kill_switch", "run_id": None}
    flow.ledger.set_kill_switch(False)
    assert flow.claim().status == "not_runnable"


@pytest.mark.parametrize("still_on", [True, False])
def test_the_kill_switch_blocks_the_commitment_even_if_already_off(
    flow: Flow, still_on: bool
) -> None:
    flow.claim()
    flow.reserve()
    flow.close()
    commitment = flow.commitment()
    flow.ledger.set_kill_switch(True)
    if not still_on:
        flow.ledger.set_kill_switch(False)

    blocked = flow.ledger.commit_dispatch(commitment)
    closed = flow.finish("canceled", "kill_switch", stage="uow2")

    assert (blocked.status, blocked.outcome) == ("blocked", "kill_switch")
    ex = closed.execution
    assert ex is not None and ex.reservation is not None
    assert (ex.reservation.state, ex.reservation.release_basis, ex.outbox) == (
        "released",
        "no_outbox",
        None,
    )
    assert (ex.grant.revoked_reason, ex.task.outcome_code) == ("operator", "kill_switch")
    released = next(
        e for e in flow.ledger.events(flow.grant_id) if e.event_type == "reservation_released"
    )
    assert released.actor_kind == "operator"
    blocked_event = next(
        e for e in flow.ledger.events(flow.grant_id) if e.event_type == "kill_switch_blocked"
    )
    assert blocked_event.body == {"control_epoch": 2, "stage": "uow2"}


@pytest.mark.parametrize("lost", ["crash", "failing_hook"])
def test_a_kill_switch_block_whose_terminal_was_lost_does_not_revive_once_off(
    flow: Flow, clock: AuthorityClock, lost: str
) -> None:
    """§8.4: `blocked(kill_switch)` at UoW-2, the terminal is never written (a crash, or the
    hook of `finish_task` raises), the switch goes off and the lease lapses: the re-claim
    still closes the task as `kill_switch`, with no outbox and the reservation released."""

    def failing(_: sqlite3.Connection) -> None:
        raise RuntimeError("hook failed")

    flow.claim()
    flow.reserve()
    flow.close()
    flow.ledger.set_kill_switch(True)
    assert flow.commit().outcome == "kill_switch"
    if lost == "failing_hook":
        with pytest.raises(RuntimeError):
            flow.finish("canceled", "kill_switch", stage="uow2", hooks=[failing])
    flow.ledger.set_kill_switch(False)
    clock.advance(LEASE_SECONDS)

    step = flow.claim()

    assert (step.status, step.outcome) == ("closed", "kill_switch")
    ex = step.execution
    assert ex is not None and ex.reservation is not None
    assert (ex.outbox, ex.reservation.state, ex.reservation.release_basis) == (
        None,
        "released",
        "no_outbox",
    )
    assert (ex.task.state, ex.grant.state, ex.grant.revoked_reason) == (
        "canceled",
        "revoked",
        "operator",
    )
    assert flow.commit().status == "lease_lost"
    assert flow.state().outbox is None


def test_a_switch_toggled_before_the_claim_does_not_block_it(
    flow: Flow, clock: AuthorityClock
) -> None:
    flow.ledger.set_kill_switch(True)
    flow.ledger.set_kill_switch(False)
    clock.advance(1)

    assert flow.claim().status == "applied"
    clock.advance(LEASE_SECONDS)
    assert flow.claim().status == "applied"


def test_the_kill_switch_blocks_the_reservation(flow: Flow) -> None:
    flow.claim()
    request = flow.reservation()
    flow.ledger.set_kill_switch(True)

    step = flow.ledger.reserve(request)

    assert (step.status, step.outcome) == ("blocked", "kill_switch")
    assert flow.state().reservation is None


# Validity (HNP01 at the store)


def test_the_claim_at_expires_at_closes_the_task_as_expired(
    flow: Flow, clock: AuthorityClock
) -> None:
    clock.advance(3600)  # now == expires_at

    step = flow.claim()

    assert (step.status, step.outcome) == ("closed", "grant_expired")
    assert step.execution is not None and step.execution.grant.state == "expired"
    assert step.execution.grant.authority_epoch == 2
    expired = flow.ledger.events(flow.grant_id)[-2]
    assert (expired.event_type, expired.body) == (
        "expired",
        {"expires_at": "2026-10-21T16:00:00.000Z", "observed_at": "2026-10-21T16:00:00.000Z"},
    )


@pytest.mark.parametrize(("left", "status"), [(300, "applied"), (299, "blocked")])
def test_uow1_reserves_only_with_300_seconds_of_validity_left(
    flow: Flow, clock: AuthorityClock, left: int, status: str
) -> None:
    flow.claim()
    clock.advance(3600 - left)

    step = flow.reserve()

    assert step.status == status
    if status == "blocked":
        assert step.outcome == "grant_expired"
        closed = flow.finish("canceled", "grant_expired")
        assert closed.execution is not None and closed.execution.grant.state == "expired"


def test_the_commitment_requires_now_before_expires_at(flow: Flow, clock: AuthorityClock) -> None:
    flow.claim()
    flow.reserve()
    flow.close()
    commitment = flow.commitment()
    clock.advance(3600)

    step = flow.ledger.commit_dispatch(commitment)

    assert (step.status, step.outcome) == ("blocked", "grant_expired")


# Outbox and restart (HNP09)


def test_with_an_outbox_row_nothing_releases_the_reservation(
    flow: Flow, clock: AuthorityClock
) -> None:
    flow.committed()

    assert flow.finish("canceled", "kill_switch", stage="uow2").status == "blocked"
    assert flow.finish("failed", "state_conflict").status == "blocked"
    assert flow.commit().status == "reused"
    ex = flow.state()
    assert ex.reservation is not None and ex.outbox is not None
    assert (ex.reservation.state, ex.outbox.state, ex.grant.state) == (
        "committed",
        "committed",
        "active",
    )
    assert (ex.outbox.authority_epoch, ex.outbox.control_epoch) == (1, 1)


def test_a_reclaim_with_outbox_neither_cancels_nor_releases(
    flow: Flow, clock: AuthorityClock
) -> None:
    flow.committed()
    flow.ledger.revoke(OWNER, flow.grant_id)
    flow.ledger.set_kill_switch(True)
    clock.advance(3600)  # also lapsed

    step = flow.claim()

    assert step.status == "applied" and step.execution is not None
    ex = step.execution
    assert ex.reservation is not None and ex.outbox is not None
    assert (ex.task.state, ex.reservation.state, ex.outbox.state) == (
        "running",
        "committed",
        "committed",
    )
    assert flow.commit().status == "reused"  # the run resumes under the new lease
    assert chain_is_valid(flow.grant_id, flow.ledger.events(flow.grant_id))


def test_the_state_survives_a_restart_and_resumes_without_a_new_close(
    tmp_path: Path, flow: Flow, clock: AuthorityClock
) -> None:
    """HNP09: after a crash the operation, the reservation and the chain are kept and reused."""
    flow.claim()
    flow.reserve()
    flow.close()
    before = flow.state()
    clock.advance(LEASE_SECONDS)

    restarted = Flow(make_ledger(tmp_path, clock), clock, flow.task_id, flow.grant_id)

    assert restarted.state() == before
    assert restarted.claim().status == "applied"
    assert restarted.reserve().status == "reused"
    assert restarted.close().status == "reused"
    assert restarted.commit().status == "applied"
    assert event_types(restarted).count("mandates_closed") == 1


# Hooks (TransactionHook)


def test_hooks_see_the_ledger_writes_and_a_failing_hook_rolls_back_the_whole_unit(
    tmp_path: Path, flow: Flow
) -> None:
    flow.claim()
    seen: list[tuple[object, ...]] = []

    def evidence(connection: sqlite3.Connection) -> None:
        seen.append(connection.execute("SELECT state FROM grant_reservations").fetchone())
        connection.execute("CREATE TABLE IF NOT EXISTS hook_rows (value TEXT)")
        connection.execute("INSERT INTO hook_rows VALUES ('evidence')")

    def failing(_: sqlite3.Connection) -> None:
        raise RuntimeError("GOV_EVIDENCE_UNAVAILABLE")

    before = dump(tmp_path)
    with pytest.raises(RuntimeError):
        flow.reserve(hooks=[evidence, failing])

    assert seen == [("reserved",)]
    assert dump(tmp_path) == before
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection:
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name='hook_rows'").fetchall()
            == []
        )
    assert flow.reserve(hooks=[evidence]).status == "applied"


@pytest.mark.parametrize("unit", ["commit", "finish", "claim"])
def test_a_failing_hook_rolls_back_every_unit_that_takes_hooks(
    tmp_path: Path, flow: Flow, clock: AuthorityClock, unit: str
) -> None:
    def failing(_: sqlite3.Connection) -> None:
        raise RuntimeError("hook failed")

    flow.claim()
    flow.reserve()
    flow.close()
    if unit == "claim":
        flow.ledger.set_kill_switch(True)
        clock.advance(LEASE_SECONDS)
    before = dump(tmp_path)

    with pytest.raises(RuntimeError):
        if unit == "commit":
            flow.commit(hooks=[failing])
        elif unit == "finish":
            flow.finish("failed", "policy_denied", hooks=[failing])
        else:
            flow.ledger.claim(flow.task_id, hooks=[failing])

    assert dump(tmp_path) == before


# Terminals


def test_a_lost_cas_is_classified_and_ends_as_a_failed_state_conflict(flow: Flow) -> None:
    flow.claim()
    stale = flow.reservation()
    flow.reserve()  # moves the grant's state_version
    other = flow.ledger.reserve(replace(stale, operation_id="op_2", reservation_id="rsv_2"))
    assert (other.status, other.outcome) == ("blocked", "state_conflict")

    closed = flow.finish("failed", "state_conflict")

    ex = closed.execution
    assert ex is not None and ex.reservation is not None
    assert (ex.task.state, ex.grant.state, ex.grant.revoked_reason) == (
        "failed",
        "revoked",
        "failed",
    )
    assert (ex.grant.authority_epoch, ex.reservation.state) == (2, "released")


def test_escalating_before_the_close_revokes_the_grant_as_escalated(flow: Flow) -> None:
    flow.claim()
    step = flow.finish("escalated", "no_candidate_in_scope")
    assert step.execution is not None
    assert (step.execution.grant.revoked_reason, step.execution.task.outcome_code) == (
        "escalated",
        "no_candidate_in_scope",
    )


def test_finish_refuses_a_cause_that_did_not_happen(flow: Flow) -> None:
    flow.claim()
    assert flow.finish("canceled", "revoked_by_owner").outcome == "state_conflict"
    assert flow.state().task.state == "running"


@pytest.mark.parametrize(
    ("state", "outcome", "stage"),
    [
        ("canceled", "policy_denied", None),
        ("failed", "kill_switch", "uow1"),
        ("escalated", "policy_denied", None),
        ("failed", "dispatch_timeout", None),
        ("escalated", "verifier_unresolved_constraint", None),
        ("canceled", "kill_switch", None),
        ("failed", "policy_denied", "uow1"),
    ],
)
def test_finish_rejects_outcomes_of_another_terminal(
    flow: Flow,
    state: Literal["escalated", "failed", "canceled"],
    outcome: OutcomeCode,
    stage: KillSwitchStage | None,
) -> None:
    flow.claim()
    with pytest.raises(ValueError):
        flow.finish(state, outcome, stage=stage)
