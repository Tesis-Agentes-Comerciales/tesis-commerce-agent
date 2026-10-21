"""Delegation ledger, dispatch after the commitment (spec §4.4 outbox, §4.5 dispatch and UoW-3,
§8.1), on real SQLite. HNP10 (a timeout keeps the reservation `uncertain`) at the store level, and
HNP04 and HNP09 through to the order.
"""

import sqlite3
from pathlib import Path

import pytest

from commerce_agent.delegation.models import LEASE_SECONDS, Ap2Error, chain_is_valid
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger
from tests.support.delegation import (
    OWNER,
    OWNER_REF,
    AuthorityClock,
    Flow,
    make_ledger,
    receipt,
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


def event_types(flow: Flow) -> list[str]:
    return [event.event_type for event in flow.ledger.events(flow.grant_id)]


def complete(flow: Flow) -> None:
    flow.advance("payment_sent", "payment_accepted", "complete_sent", "completed")
    step = flow.ledger.record_completion(
        flow.task_id, flow.lease_id, receipt("payment"), receipt("checkout")
    )
    assert step.status == "closed", step


def test_a_dispatch_timeout_becomes_uncertain_without_releasing(flow: Flow) -> None:
    """HNP10: the reservation is kept, nothing is closed again and the task cannot rerun."""
    flow.committed()
    flow.advance("payment_sent")

    step = flow.ledger.mark_uncertain(flow.task_id, flow.lease_id, "dispatch_timeout")

    assert step.status == "closed" and step.execution is not None
    ex = step.execution
    assert ex.reservation is not None and ex.outbox is not None
    assert (ex.reservation.state, ex.reservation.uncertain_reason) == (
        "uncertain",
        "dispatch_timeout",
    )
    assert (ex.outbox.state, ex.task.state, ex.task.outcome_code, ex.grant.state) == (
        "uncertain",
        "uncertain",
        "dispatch_timeout",
        "active",
    )
    assert flow.claim().status == "not_runnable"
    assert event_types(flow)[-2:] == ["reservation_uncertain", "task_finished"]


def test_uncertainty_needs_a_send(flow: Flow) -> None:
    """§4.4: only `payment_sent` or `complete_sent` can end `uncertain`."""
    flow.committed()
    ledger, task_id, lease = flow.ledger, flow.task_id, flow.lease_id
    assert ledger.mark_uncertain(task_id, lease, "dispatch_timeout").status == "blocked"
    flow.advance("payment_sent", "payment_accepted")
    assert ledger.mark_uncertain(task_id, lease, "dispatch_timeout").status == "blocked"
    flow.advance("complete_sent")
    assert ledger.mark_uncertain(task_id, lease, "receipt_mismatch").status == "closed"


@pytest.mark.parametrize(
    ("ap2_error", "escalate", "state", "reason"),
    [
        ("unresolved_constraint", True, "escalated", "escalated"),
        ("unresolved_constraint", False, "failed", "failed"),
        ("invalid_mandate", True, "failed", "failed"),
    ],
)
def test_a_psp_error_receipt_releases_the_reservation(
    flow: Flow, ap2_error: Ap2Error, escalate: bool, state: str, reason: str
) -> None:
    flow.committed()
    flow.advance("payment_sent")
    error = receipt("payment", error=ap2_error)

    step = flow.ledger.reject_dispatch(
        flow.task_id,
        flow.lease_id,
        ap2_error,
        error,
        escalate=escalate,
    )

    assert step.status == "closed" and step.execution is not None
    ex = step.execution
    assert ex.reservation is not None and ex.outbox is not None
    assert (ex.reservation.state, ex.reservation.release_basis) == ("released", "psp_error_receipt")
    assert (ex.outbox.state, ex.outbox.rejected_by, ex.outbox.ap2_error) == (
        "rejected",
        "psp",
        ap2_error,
    )
    assert ex.outbox.error_receipt_jwt == error.jwt
    assert (ex.task.state, ex.task.outcome_code) == (state, f"verifier_{ap2_error}")
    assert (ex.grant.state, ex.grant.revoked_reason) == ("revoked", reason)
    assert event_types(flow)[-4:] == [
        "receipt_recorded",
        "reservation_released",
        "revoked",
        "task_finished",
    ]


def test_a_merchant_error_receipt_after_the_psp_token_is_uncertain(flow: Flow) -> None:
    flow.committed()
    flow.advance("payment_sent", "payment_accepted", "complete_sent")

    step = flow.ledger.reject_dispatch(
        flow.task_id,
        flow.lease_id,
        "unresolved_constraint",
        receipt("checkout", error="unresolved_constraint"),
    )

    ex = step.execution
    assert ex is not None and ex.reservation is not None
    assert (ex.reservation.state, ex.reservation.uncertain_reason) == (
        "uncertain",
        "partial_verifier_failure",
    )
    assert (ex.task.state, ex.grant.state) == ("uncertain", "active")
    assert ex.outbox is not None and ex.outbox.rejected_by == "merchant"


def test_the_outbox_only_moves_forward(flow: Flow) -> None:
    flow.committed()
    ledger, task_id, lease = flow.ledger, flow.task_id, flow.lease_id

    assert ledger.advance_dispatch(task_id, lease, "complete_sent").status == "blocked"
    flow.advance("payment_sent")
    assert ledger.advance_dispatch(task_id, lease, "payment_sent").status == "reused"
    with pytest.raises(ValueError, match="order"):
        ledger.advance_dispatch(task_id, lease, "completed")
    flow.advance("payment_accepted", "complete_sent")
    before = flow.state()
    completion = ledger.record_completion(task_id, lease, receipt("payment"), receipt("checkout"))
    assert completion.status == "blocked"
    assert flow.state() == before


def test_a_full_purchase_consumes_the_grant_and_its_event_chain_verifies(flow: Flow) -> None:
    flow.committed()
    flow.advance("payment_sent", "payment_accepted", "complete_sent", "completed")
    payment, checkout = receipt("payment"), receipt("checkout")

    step = flow.ledger.record_completion(flow.task_id, flow.lease_id, payment, checkout)

    assert step.status == "closed" and step.execution is not None
    ex = step.execution
    assert ex.reservation is not None and ex.outbox is not None
    assert (ex.task.state, ex.task.outcome_code, ex.task.lease_id) == ("purchased", None, None)
    assert (ex.grant.state, ex.grant.authority_epoch, ex.grant.state_version) == ("consumed", 2, 4)
    assert (ex.reservation.state, ex.outbox.order_id) == ("consumed", "order_1")
    events = flow.ledger.events(flow.grant_id)
    assert [e.event_type for e in events] == [
        "consented",
        "mandates_issued",
        "task_claimed",
        "reservation_reserved",
        "mandates_closed",
        "dispatch_committed",
        "receipt_recorded",
        "receipt_recorded",
        "consumed",
        "task_finished",
    ]
    assert chain_is_valid(flow.grant_id, events)
    assert {e.on_behalf_of for e in events} == {OWNER_REF}
    assert events[6].body["receipt_digest"].startswith("sha256:")
    assert events[7].body["order_id"] == "order_1"
    assert events[8].body == {"reservation_id": "rsv_1", "order_id": "order_1"}
    assert payment.jwt not in repr(events)


def test_after_a_revocation_in_flight_the_order_still_consumes_the_reservation(
    flow: Flow,
) -> None:
    """HNP04: the dispatch goes on; the grant stays revoked and the reservation is consumed."""
    flow.committed()
    revocation = flow.ledger.revoke(OWNER, flow.grant_id)
    assert revocation is not None and revocation.effect == "in_flight"

    complete(flow)

    ex = flow.state()
    assert ex.reservation is not None
    assert (ex.reservation.state, ex.task.state) == ("consumed", "purchased")
    assert (ex.grant.state, ex.grant.revoked_reason, ex.grant.authority_epoch) == (
        "revoked",
        "owner",
        2,
    )


def test_a_consumed_grant_is_not_revocable(flow: Flow) -> None:
    flow.committed()
    complete(flow)

    revocation = flow.ledger.revoke(OWNER, flow.grant_id)

    assert revocation is not None
    assert (revocation.status, revocation.grant.state) == ("not_revocable", "consumed")


def test_a_reclaim_with_outbox_resumes_the_dispatch_after_a_crash(
    tmp_path: Path, flow: Flow, clock: AuthorityClock
) -> None:
    """HNP09: after the token the restarted executor resumes the same operation."""
    flow.committed()
    flow.advance("payment_sent", "payment_accepted")
    flow.ledger.set_kill_switch(True)
    clock.advance(LEASE_SECONDS)

    restarted = Flow(make_ledger(tmp_path, clock), clock, flow.task_id, flow.grant_id)
    assert restarted.claim().status == "applied"
    assert restarted.commit().status == "reused"
    restarted.advance("complete_sent", "completed")
    done = restarted.ledger.record_completion(
        restarted.task_id, restarted.lease_id, receipt("payment"), receipt("checkout")
    )
    assert done.status == "closed"

    ex = restarted.state()
    assert ex.outbox is not None and ex.reservation is not None
    assert (ex.outbox.outbox_id, ex.outbox.operation_id, ex.reservation.state) == (
        "ob_1",
        "op_1",
        "consumed",
    )


def test_with_a_foreign_lease_the_dispatch_writes_nothing(
    flow: Flow, clock: AuthorityClock
) -> None:
    flow.committed()
    stale = flow.lease_id
    clock.advance(LEASE_SECONDS)
    flow.claim()
    before = flow.state()
    ledger, task_id = flow.ledger, flow.task_id

    assert ledger.advance_dispatch(task_id, stale, "payment_sent").status == "lease_lost"
    assert ledger.mark_uncertain(task_id, stale, "dispatch_timeout").status == "lease_lost"
    rejected = ledger.reject_dispatch(task_id, stale, "invalid_mandate", receipt("payment"))
    completed = ledger.record_completion(task_id, stale, receipt("payment"), receipt("checkout"))
    assert (rejected.status, completed.status) == ("lease_lost", "lease_lost")
    assert flow.state() == before


@pytest.mark.parametrize("unit", ["completion", "uncertain", "rejection"])
def test_a_failing_hook_rolls_back_the_dispatch_terminals(flow: Flow, unit: str) -> None:
    def failing(_: sqlite3.Connection) -> None:
        raise RuntimeError("hook failed")

    flow.committed()
    flow.advance("payment_sent")
    if unit == "completion":
        flow.advance("payment_accepted", "complete_sent", "completed")
    before, events = flow.state(), event_types(flow)

    with pytest.raises(RuntimeError):
        if unit == "completion":
            flow.ledger.record_completion(
                flow.task_id,
                flow.lease_id,
                receipt("payment"),
                receipt("checkout"),
                hooks=[failing],
            )
        elif unit == "uncertain":
            flow.ledger.mark_uncertain(
                flow.task_id, flow.lease_id, "dispatch_timeout", hooks=[failing]
            )
        else:
            flow.ledger.reject_dispatch(
                flow.task_id,
                flow.lease_id,
                "invalid_mandate",
                receipt("payment", error="invalid_mandate"),
                hooks=[failing],
            )

    assert (flow.state(), event_types(flow)) == (before, events)
