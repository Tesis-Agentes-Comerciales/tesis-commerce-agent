"""Human Not Present steps in the purchase trace (docs/design/hnp-governed.md §10, HNP-11).

B2-HNP runs through the real gate as in tests/governance/test_hnp_gate.py; B0-HNP, K15 and the
purchase failures through the executor bench of tests/support/hnp_executor.py. One SQLite file per
test: no model, no network.
"""

import dataclasses
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from commerce_agent.config import Settings
from commerce_agent.delegation.models import ReservationRequest
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger
from commerce_agent.trace import PurchaseTrace, PurchaseTraces, TraceUnavailable
from commerce_agent.trace.assembler import by_time, step, summarize
from commerce_agent.trace.models import Money, PolicySummary
from tests.assistant_support import settings as bench_settings
from tests.delegation.test_hnp_executor import started
from tests.delegation.test_hnp_intent_review import CONFIRMED, CONTRADICTS, FakeReviewer
from tests.governance import test_hnp_gate
from tests.governance.test_d1_hnp_gate import settings_for
from tests.governance.test_hnp_gate import GRANT, RUN, Delegated, delegated
from tests.governance.test_shadow_integration import OWNER
from tests.support import hnp_executor
from tests.support.delegation import START, AuthorityClock
from tests.support.hnp_dispatch import MerchantApi, Psp, dispatching
from tests.support.hnp_executor import Bench, Merchant
from tests.test_purchase_service import details

HP_ONLY = {"request", "selection", "approval", "authorization_signed"}


def kinds(trace: PurchaseTrace) -> list[str]:
    return [item.kind for item in trace.steps]


def of(trace: PurchaseTrace, kind: str) -> list[Any]:
    return [item for item in trace.steps if item.kind == kind]


def rules(step: Any) -> list[tuple[str, str]]:
    return [(rule.id, rule.result) for rule in step.decision.rules]


@dataclasses.dataclass
class Governed:
    flow: Delegated
    settings: Settings

    def trace(self) -> PurchaseTrace:
        return PurchaseTraces(self.settings).trace(RUN)


@pytest.fixture
def governed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """B2-HNP at `checkout_prepared`, the task claimed; the ledger on the real clock from then on,
    so its events interleave with the decisions as in a run."""

    def build(**options: Any) -> Governed:
        settings = settings_for(tmp_path)
        # One settings object: its D1 keys are generated per call, and the trace reads them back.
        monkeypatch.setattr(test_hnp_gate, "settings_for", lambda path, **changes: settings)
        flow = delegated(tmp_path, **options)
        return Governed(
            dataclasses.replace(flow, ledger=SqliteDelegationLedger(flow.path)), settings
        )

    return build


class Ticking:
    """The bench's clocks, one millisecond per reading: ledger and purchase times interleave."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        self.now += timedelta(milliseconds=1)
        return self.now


@pytest.fixture
def ticking(monkeypatch: pytest.MonkeyPatch) -> Ticking:
    clock = Ticking()
    monkeypatch.setattr(hnp_executor, "AuthorityClock", lambda: clock)
    monkeypatch.setattr(
        "commerce_agent.persistence.sqlite._timestamp",
        lambda: clock().isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )
    return clock


@pytest.fixture
def one_instant(monkeypatch: pytest.MonkeyPatch) -> Ticking:
    """As in a run, the purchase writes at the ledger's last instant: UoW-3 records the receipts
    and, in the same transaction, the order, both within one millisecond."""
    clock = Ticking()
    monkeypatch.setattr(hnp_executor, "AuthorityClock", lambda: clock)
    monkeypatch.setattr(
        "commerce_agent.persistence.sqlite._timestamp",
        lambda: clock.now.isoformat().replace("+00:00", "Z"),
    )
    return clock


def bench_trace(bench: Bench) -> PurchaseTrace:
    return PurchaseTraces(bench_settings(bench.path)).trace(bench.run_id())


def dispatching_bench(tmp_path: Path, psp: Psp | None = None, **options: Any) -> Bench:
    """The executor with the product's mandates and the dispatcher over the PSP and merchant
    doubles: UoW-3 records both verified receipts and completes the purchase."""
    return Bench(
        tmp_path,
        real_chains=True,
        dispatcher=dispatching(psp or Psp(), MerchantApi()),
        **options,
    )


def recorded_receipts(bench: Bench) -> list[tuple[str, str, str]]:
    """(kind, status, recorded_at) of each `receipt_recorded` of the grant, in ledger order."""
    return [
        (event.body["kind"], event.body["status"], event.recorded_at)
        for event in bench.ledger.events(bench.grant_id)
        if event.event_type == "receipt_recorded"
    ]


# B2-HNP: the consent replaces the request; the policy decides three times, the buyer never.


def test_b2_hnp_shows_the_consent_the_three_decisions_and_the_single_use(governed) -> None:
    run = governed()
    flow = run.flow
    d2 = flow.decide("D2")
    assert flow.reserve(d2).status == "applied"
    assert flow.close().status == "applied"
    assert flow.commit(flow.decide("D3")).status == "applied"

    trace = run.trace()

    assert kinds(trace) == [
        "delegation_consented",
        "task_activated",
        "policy_decision",
        "checkout_created",
        "freshness_check",
        "authorization_decision",
        "reservation",
        "mandates_closed",
        "dispatch_decision",
    ]
    assert not HP_ONLY & set(kinds(trace))
    consented, activated = trace.steps[:2]
    assert (consented.actor, consented.grant_id, consented.total_max) == (
        "user",
        GRANT,
        Money(minor=80_000, currency="COP"),
    )
    assert (activated.id, activated.actor, activated.trigger) == (
        "task_activated",
        "agent",
        "service",
    )
    (freshness,) = of(trace, "freshness_check")
    assert (freshness.stage, freshness.outcome, freshness.state) == ("close", "fresh", "done")
    (d1,) = of(trace, "policy_decision")
    assert (d1.decision.policy.set_id, d1.decision.policy.revision) == ("b2-hnp-v1", 2)
    by_id = {rule.id: rule for rule in d1.decision.rules}
    assert by_id["POL-02"].observed == run.settings.merchant_base_url
    (d2_step,) = of(trace, "authorization_decision")
    assert rules(d2_step) == [(f"POL-0{number}", "pass") for number in range(3, 10)]
    amounts = {rule.id: (rule.observed, rule.expected) for rule in d2_step.decision.rules}
    assert amounts["POL-05"] == (
        Money(minor=74_000, currency="COP"),
        Money(minor=80_000, currency="COP"),
    )
    (reservation,) = of(trace, "reservation")
    assert (reservation.outcome, reservation.amount.minor) == ("reserved", 74_000)
    (d3,) = of(trace, "dispatch_decision")
    assert (d3.actor, d3.decision.outcome, d3.decision.effect) == ("policy", "allow", "continued")
    assert rules(d3) == [("POL-09", "pass"), ("POL-10", "pass")]
    # Nothing was sent yet: without the payment receipt there is no payment and no order.
    assert (trace.status, trace.policy_mode, trace.purchase_mode) == (
        "in_progress",
        "enforce",
        "HNP",
    )
    assert (trace.total, trace.merchant) == (
        Money(minor=74_000, currency="COP"),
        run.settings.merchant_base_url,
    )
    assert json.loads(trace.model_dump_json())["purchase_mode"] == "HNP"
    history = PurchaseTraces(run.settings).history(OWNER, limit=10, before=None)
    assert history is not None
    (summary,) = history.purchases
    assert summary == summarize(trace)
    assert (summary.purchase_mode, summary.status) == ("HNP", "in_progress")
    assert summary.policy == PolicySummary(outcome="allow", failed_rules=())
    assert json.loads(summary.model_dump_json())["purchase_mode"] == "HNP"


def test_a_d2_deny_blocks_and_escalates_to_the_buyer(governed) -> None:
    other = {**details().fulfillment.model_dump(mode="json"), "name": "Otra Persona"}
    run = governed(destination=other)
    d2 = run.flow.decide("D2")
    assert run.flow.finish("escalated", "terms_out_of_scope", d2.hooks).status == "closed"

    trace = run.trace()

    assert kinds(trace)[-2:] == ["authorization_decision", "escalated"]
    (d2_step,) = of(trace, "authorization_decision")
    assert (d2_step.state, d2_step.decision.outcome, d2_step.decision.effect) == (
        "blocked",
        "deny",
        "blocked",
    )
    assert ("POL-08", "fail") in rules(d2_step)
    (escalated,) = of(trace, "escalated")
    assert (escalated.actor, escalated.state, escalated.reason) == (
        "agent",
        "blocked",
        "terms_out_of_scope",
    )
    assert not of(trace, "reservation") and trace.total is None
    assert trace.status == "blocked"
    assert summarize(trace).policy == PolicySummary(outcome="deny", failed_rules=("POL-08",))


def test_an_offer_changed_at_close_stops_before_d2_and_ends_in_stale_terms(governed) -> None:
    run = governed(freshness="changed")
    flow = run.flow
    version = SqliteAgentStore(flow.path).purchase_state_version(RUN)
    assert version is not None

    def close(connection: sqlite3.Connection) -> None:  # The executor cancels it on escalation.
        SqliteAgentStore.transition_hnp_purchase(
            connection, RUN, expected_state_version=version, stage="canceled"
        )

    assert flow.finish("escalated", "offer_changed", (close,)).status == "closed"

    trace = run.trace()

    (freshness,) = of(trace, "freshness_check")
    assert (freshness.stage, freshness.outcome, freshness.state) == ("close", "changed", "blocked")
    # The executor stops at the freshness check: D2 never decides.
    assert not of(trace, "authorization_decision")
    assert of(trace, "escalated")[0].reason == "offer_changed"
    # The escalation canceled the purchase: no step claims the buyer or a guard canceled it.
    assert not of(trace, "canceled")
    assert trace.status == "stale_terms"


def test_an_owner_revocation_after_the_commitment_stays_in_flight(governed) -> None:
    run = governed()
    flow = run.flow
    assert flow.reserve(flow.decide("D2")).status == "applied"
    assert flow.close().status == "applied"
    assert flow.commit(flow.decide("D3")).status == "applied"
    revocation = flow.ledger.revoke(OWNER, GRANT)
    assert revocation is not None and revocation.effect == "in_flight"

    trace = run.trace()

    assert kinds(trace)[-2:] == ["dispatch_decision", "revoked"]
    (revoked,) = of(trace, "revoked")
    assert (revoked.actor, revoked.by, revoked.effect) == ("user", "owner", "in_flight")
    # The dispatch continues and is reconciled: nothing was canceled.
    assert trace.status == "in_progress"


def test_an_owner_revocation_before_the_commitment_releases_the_single_use(governed) -> None:
    run = governed()
    flow = run.flow
    assert flow.reserve(flow.decide("D2")).status == "applied"
    revocation = flow.ledger.revoke(OWNER, GRANT)
    assert revocation is not None and revocation.effect == "released"
    assert flow.finish("canceled", "revoked_by_owner", ()).status == "closed"

    trace = run.trace()

    assert kinds(trace)[-3:] == ["reservation", "revoked", "reservation"]
    first, last = of(trace, "reservation")
    assert (first.outcome, last.outcome, last.id) == ("reserved", "released", "reservation:2")
    assert last.amount == first.amount == Money(minor=74_000, currency="COP")
    assert of(trace, "revoked")[0].effect == "released"
    assert trace.status == "canceled"


def test_the_kill_switch_revokes_by_the_operator_and_cancels(governed) -> None:
    run = governed()
    flow = run.flow
    d2 = flow.decide("D2")
    flow.ledger.set_kill_switch(True)
    assert flow.reserve(d2).outcome == "kill_switch"
    assert (
        flow.finish("canceled", "kill_switch", d2.conflict_hooks, stage="uow1").status == "closed"
    )

    trace = run.trace()

    (d2_step,) = of(trace, "authorization_decision")
    assert (d2_step.decision.outcome, d2_step.decision.effect) == ("allow", "blocked")
    (revoked,) = of(trace, "revoked")
    assert (revoked.actor, revoked.by, revoked.effect) == ("operator", "operator", "no_effect")
    assert kinds(trace)[-1] == "revoked" and not of(trace, "reservation")
    assert trace.status == "canceled"


def test_a_lost_cas_fails_the_purchase_and_is_not_the_policy_stopping_it(governed) -> None:
    run = governed()
    flow = run.flow
    d2 = flow.decide("D2")
    execution = flow.ledger.execution(flow.task_id)
    assert execution is not None
    with closing(sqlite3.connect(flow.path)) as connection, connection:
        connection.execute(
            "UPDATE delegation_grants SET state_version = state_version + 1 WHERE grant_id = ?",
            (GRANT,),
        )
    request = ReservationRequest(
        task_id=flow.task_id,
        lease_id=flow.lease_id,
        expected=execution.authority,  # Read before the grant moved: the CAS loses.
        reservation_id="rsv_1",
        operation_id="op_1",
        checkout_id=flow.checkout.id,
        amount_minor=flow.checkout.total_minor,
        d2_evidence_id=d2.evidence_id,
    )
    lost = flow.ledger.reserve(request, hooks=d2.hooks)
    assert (lost.status, lost.outcome) == ("blocked", "state_conflict")
    version = SqliteAgentStore(flow.path).purchase_state_version(RUN)
    assert version is not None

    def close(connection: sqlite3.Connection) -> None:  # As the executor closes its purchase.
        SqliteAgentStore.transition_hnp_purchase(
            connection,
            RUN,
            expected_state_version=version,
            stage="failed",
            failure_code="state_conflict",
        )

    assert flow.finish("failed", "state_conflict", (*d2.conflict_hooks, close)).status == "closed"

    trace = run.trace()

    (d2_step,) = of(trace, "authorization_decision")
    assert (d2_step.decision.outcome, d2_step.decision.effect) == ("allow", "blocked")
    # The task's code is the purchase's: one failed step.
    assert [(item.id, item.code) for item in of(trace, "failed")] == [("failed", "state_conflict")]
    assert trace.status == "failed"


def test_a_broken_delegation_chain_makes_the_trace_unavailable(governed) -> None:
    run = governed()
    with closing(sqlite3.connect(run.flow.path)) as connection, connection:
        connection.execute("DROP TRIGGER delegation_events_no_update")
        connection.execute(
            "UPDATE delegation_events SET body = '{}' WHERE grant_id = ? AND sequence = 1", (GRANT,)
        )

    with pytest.raises(TraceUnavailable):
        run.trace()
    history = PurchaseTraces(run.settings).history(OWNER, limit=5, before=None)
    assert history is not None
    ((row,),) = (history.purchases,)
    assert (row.status, row.purchase_mode) == ("trace_unavailable", "HNP")


# B0-HNP and the executor's terminals.


def test_b0_hnp_has_no_policy_steps_and_reads_the_ledger(tmp_path: Path, ticking: Ticking) -> None:
    bench = Bench(tmp_path)
    assert bench.run().status == "applied"

    trace = bench_trace(bench)

    assert kinds(trace) == [
        "delegation_consented",
        "task_activated",
        "checkout_created",
        "reservation",
        "mandates_closed",
    ]
    assert (trace.policy_mode, trace.purchase_mode, trace.status) == ("off", "HNP", "in_progress")
    assert trace.total == Money(minor=74_000, currency="COP")
    assert summarize(trace).policy is None


def test_a_payment_attempt_without_its_receipt_shows_no_payment(
    tmp_path: Path, ticking: Ticking
) -> None:
    """09b writes `payment.delegate` before the first send to the PSP, and the purchase is
    `completing` from UoW-2: neither proves that the PSP returned a token, so a rejection, a
    timeout or a key without a send must not read as an authorized payment. Only the payment
    receipt `success` does."""
    bench = Bench(tmp_path)
    assert bench.run().status == "applied"
    store, run_id = SqliteAgentStore(bench.path), bench.run_id()
    store.idempotency_key(run_id, "payment.delegate")
    # Even with the terms HP prices its payment step with: the HP inference must not apply.
    origin = bench_trace(bench).merchant
    terms = {"merchant_origin": origin, "total_minor": 74_000, "currency": "cop"}
    store.save_approval_terms(run_id, terms)
    assert bench.purchase() == ("completing", None)

    trace = bench_trace(bench)

    assert not of(trace, "payment_authorized")
    assert not of(trace, "order_confirmed")
    assert (kinds(trace)[-1], trace.status) == ("mandates_closed", "in_progress")


# The payment receipt `success` (UoW-3): the only proof of the PSP's token.


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_a_completed_purchase_shows_its_payment_receipt_before_the_order(
    tmp_path: Path, ticking: Ticking, mode: str
) -> None:
    bench = dispatching_bench(tmp_path, mode=mode)
    assert bench.run().status == "closed"
    assert (bench.ended(), bench.purchase()) == (("purchased", None), ("completed", None))

    trace = bench_trace(bench)

    (payment,) = of(trace, "payment_authorized")
    reserved, consumed = of(trace, "reservation")
    # The checkout receipt is not a payment: one step, from the payment receipt, at its time.
    [(_, _, paid_at), (kind, _, _)] = recorded_receipts(bench)
    assert kind == "checkout"
    assert (payment.id, payment.at, payment.actor, payment.state) == (
        "payment_authorized",
        paid_at,
        "payment",
        "done",
    )
    # Same shape as HP: the single use it paid, in COP.
    assert (payment.max_minor, payment.currency, payment.duration_ms) == (
        reserved.amount.minor,
        "COP",
        None,
    )
    assert trace.total == reserved.amount == consumed.amount
    order = kinds(trace).index("order_confirmed")
    assert kinds(trace).index("mandates_closed") < kinds(trace).index("payment_authorized") < order
    assert (trace.status, trace.purchase_mode) == ("completed", "HNP")


def test_the_payment_goes_before_the_order_it_shares_uow3_with(
    tmp_path: Path, one_instant: Ticking
) -> None:
    """UoW-3 writes the receipts, the consumed single use and the order in one transaction: they
    share their millisecond, and the payment the order needed goes first."""
    bench = dispatching_bench(tmp_path)
    assert bench.run().status == "closed"

    trace = bench_trace(bench)

    payment, order = of(trace, "payment_authorized")[0], of(trace, "order_confirmed")[0]
    assert payment.at == order.at
    assert kinds(trace)[-3:] == ["payment_authorized", "order_confirmed", "reservation"]
    assert trace.steps[-1].id == "reservation:2"


def test_a_psp_error_receipt_is_not_an_authorized_payment(tmp_path: Path, ticking: Ticking) -> None:
    bench = dispatching_bench(tmp_path, Psp("invalid_credential"), mode="enforce")
    bench.run()
    assert bench.ended() == ("failed", "verifier_invalid_credential")
    assert [receipt[:2] for receipt in recorded_receipts(bench)] == [("payment", "error")]

    trace = bench_trace(bench)

    assert not of(trace, "payment_authorized")
    assert not of(trace, "order_confirmed")
    assert trace.status == "failed"


def test_k15_a_task_failed_with_the_purchase_selected_waits_for_d1(
    tmp_path: Path, ticking: Ticking
) -> None:
    bench = Bench(tmp_path, mode="enforce", purchases=started("selected"))
    bench.run()
    assert bench.ended() == ("failed", "governance_unavailable")
    assert bench.purchase() == ("selected", None)

    trace = bench_trace(bench)

    (failed,) = of(trace, "failed")
    assert (failed.id, failed.code, failed.state) == ("failed", "governance_unavailable", "error")
    assert trace.status == "uncertain"


def test_the_budget_guard_shows_the_purchase_it_canceled(tmp_path: Path, ticking: Ticking) -> None:
    bench = Bench(tmp_path, budget=80_000, merchant=Merchant(total=90_000))
    bench.run()
    assert (bench.ended(), bench.purchase()) == (("failed", "budget_guard"), ("canceled", None))

    trace = bench_trace(bench)

    (canceled,) = of(trace, "canceled")
    assert (canceled.by, canceled.actor) == ("budget", "agent")
    # The cancellation explains the task's `budget_guard`: no failed step repeats it.
    assert not of(trace, "failed")
    assert (kinds(trace)[-1], trace.status) == ("canceled", "canceled")


def test_a_purchase_failure_keeps_its_code_next_to_the_task_failure(
    tmp_path: Path, ticking: Ticking
) -> None:
    bench = Bench(tmp_path, merchant=Merchant(refuse=True))
    bench.run()
    assert (bench.ended(), bench.purchase()) == (
        ("failed", "checkout_failed"),
        ("failed", "out_of_stock"),
    )

    trace = bench_trace(bench)

    assert [(item.id, item.code) for item in of(trace, "failed")] == [
        ("failed", "out_of_stock"),
        ("failed:task", "checkout_failed"),
    ]
    assert trace.status == "failed"


# The intent reviewer (docs/design/hnp-intent-review.md §8)


def test_a_choice_not_confirmed_shows_its_review_and_the_escalation(
    tmp_path: Path, ticking: Ticking
) -> None:
    bench = Bench(tmp_path, mode="enforce", reviewer=FakeReviewer(CONTRADICTS))
    bench.run()
    assert bench.ended() == ("escalated", "choice_not_confirmed")

    trace = bench_trace(bench)

    # The shape of ui/src/storybook/trace-fixtures/hnp/enforce-review-not-confirmed.json.
    assert kinds(trace) == [
        "delegation_consented",
        "task_activated",
        "choice_reviewed",
        "escalated",
    ]
    (review,) = of(trace, "choice_reviewed")
    assert (review.actor, review.state, review.decision, review.reason) == (
        "agent",
        "blocked",
        "not_confirmed",
        "contradicts_preference",
    )
    (escalated,) = of(trace, "escalated")
    assert (escalated.state, escalated.reason) == ("blocked", "choice_not_confirmed")
    # No decision was taken, but the reviewer only runs in B2-HNP.
    assert (trace.status, trace.policy_mode, trace.total) == ("canceled", "enforce", None)
    assert summarize(trace).policy is None


def test_a_confirmed_review_goes_after_the_claim_and_before_the_checkout(
    tmp_path: Path, ticking: Ticking
) -> None:
    bench = Bench(tmp_path, mode="enforce", reviewer=FakeReviewer(CONFIRMED))
    assert bench.run().status == "applied"

    trace = bench_trace(bench)

    order = kinds(trace)
    assert order[:4] == [
        "delegation_consented",
        "task_activated",
        "choice_reviewed",
        "checkout_created",
    ]
    (review,) = of(trace, "choice_reviewed")
    assert (review.state, review.decision, review.reason) == (
        "done",
        "confirmed",
        "matches_request",
    )
    assert trace.policy_mode == "enforce"


def test_b0_hnp_without_a_reviewer_shows_no_review(tmp_path: Path, ticking: Ticking) -> None:
    bench = Bench(tmp_path)
    bench.run()

    assert not of(bench_trace(bench), "choice_reviewed")


def test_in_b2_hnp_the_review_goes_before_d1(governed) -> None:
    run = governed()
    flow = run.flow
    # The executor reviews right after the claim (at the flow's consent time) and before `start`.
    claimed = flow.ledger.execution(flow.task_id)
    assert claimed is not None
    at = datetime.fromisoformat(claimed.task.updated_at.replace("Z", "+00:00"))
    reviewing = SqliteDelegationLedger(flow.path, clock=AuthorityClock(at + timedelta(seconds=1)))
    body = {"decision": "confirmed", "reason": "matches_request", "prompt_id": "p", "model": "m"}
    assert reviewing.record_choice_review(flow.task_id, flow.lease_id, body).status == "reused"

    trace = run.trace()

    assert kinds(trace)[:4] == [
        "delegation_consented",
        "task_activated",
        "choice_reviewed",
        "policy_decision",
    ]


def test_a_review_in_the_same_millisecond_goes_before_d1_and_the_checkout() -> None:
    at = "2026-10-22T15:00:00.000Z"
    d1 = step("checkout_created", at, "merchant", checkout_id="cs_1")
    review = step("choice_reviewed", at, "agent", decision="confirmed", reason="matches_request")

    assert [item.kind for item in by_time([d1], [review])] == [
        "choice_reviewed",
        "checkout_created",
    ]
