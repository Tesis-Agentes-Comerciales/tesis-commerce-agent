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
from commerce_agent.trace.assembler import summarize
from commerce_agent.trace.models import Money, PolicySummary
from tests.assistant_support import settings as bench_settings
from tests.delegation.test_hnp_executor import started
from tests.governance import test_hnp_gate
from tests.governance.test_d1_hnp_gate import settings_for
from tests.governance.test_hnp_gate import GRANT, RUN, Delegated, delegated
from tests.governance.test_shadow_integration import OWNER
from tests.support import hnp_executor
from tests.support.delegation import START
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


def bench_trace(bench: Bench) -> PurchaseTrace:
    return PurchaseTraces(bench_settings(bench.path)).trace(bench.run_id())


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
    # 09b (payment, receipts, order) is not on main: the dispatch is the last step so far.
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


def test_an_offer_changed_at_close_shows_the_guard_and_ends_in_stale_terms(governed) -> None:
    run = governed(freshness="changed")
    d2 = run.flow.decide("D2")
    assert run.flow.finish("escalated", "offer_changed", d2.hooks).status == "closed"

    trace = run.trace()

    (freshness,) = of(trace, "freshness_check")
    assert (freshness.stage, freshness.outcome, freshness.state) == ("close", "changed", "blocked")
    (d2_step,) = of(trace, "authorization_decision")
    assert d2_step.decision.reason_codes == ("GOV_FRESHNESS_UNRESOLVED",)
    assert {result for _, result in rules(d2_step)} == {"not_evaluated"}
    assert [rule for rule, _ in rules(d2_step)] == [f"POL-0{number}" for number in range(3, 10)]
    assert of(trace, "escalated")[0].reason == "offer_changed"
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
