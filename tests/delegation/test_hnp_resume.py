"""HNP-09b: a re-claim resumes from the durable state, and nothing escapes a run unclassified.

docs/design/hnp-governed.md §4.5 (UoW-C, lease and fencing) and §8.1. The mandates are the
product's (`real_chains`), so a re-claim reads the closed pair back exactly as UoW-C stored it.
"""

import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from commerce_agent.ap2_hnp import closed_checkout_jwt
from commerce_agent.application.hnp_execution import OPERATOR_ALERTS, payment_request_digest
from commerce_agent.delegation.models import LEASE_SECONDS
from commerce_agent.governance.evaluator import GovernanceError
from commerce_agent.governance.hnp_ports import HnpGateDecision
from commerce_agent.governance.models import POLICY_REASONS
from tests.support.hnp_executor import (
    OWNER,
    Bench,
    Crash,
    Effects,
    Gate,
    Verdict,
    deny,
)

POL = POLICY_REASONS
Mode = Literal["off", "enforce"]
COMMITTED = Effects(creates=1, signatures=1, commits=1, cancels=0)
CLOSED = Effects(creates=1, signatures=1, commits=0, cancels=0)


def crash_once(monkeypatch: pytest.MonkeyPatch, target: object, name: str) -> list[int]:
    """The process dies on the first call to `target.name`; later calls go through."""
    original, calls = getattr(target, name), []

    def crashing(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise Crash
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, crashing)
    return calls


def lapse(bench: Bench) -> None:
    bench.clock.advance(LEASE_SECONDS + 1)


def reservation(bench: Bench) -> tuple[str, str | None] | None:
    with closing(sqlite3.connect(bench.path)) as connection:
        return connection.execute(
            "SELECT state, release_basis FROM grant_reservations WHERE grant_id = ?",
            (bench.grant_id,),
        ).fetchone()


def closed_after_uow_c(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Mode) -> Bench:
    """A run that died right after UoW-C stored the closed pair, before UoW-2 committed."""
    bench = Bench(tmp_path, mode=mode, real_chains=True)
    crash_once(monkeypatch, bench.ledger, "commit_dispatch")
    with pytest.raises(Crash):
        bench.run()
    assert bench.effects() == CLOSED and bench.events().count("mandates_closed") == 1
    return bench


# A re-claim after UoW-C (§4.5, «Decisión de spec, UoW-C»)


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_a_reclaim_after_the_close_reuses_it_and_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Mode
) -> None:
    bench = closed_after_uow_c(tmp_path, monkeypatch, mode)
    lapse(bench)

    step = bench.run()

    assert step.status == "applied", step
    assert step.execution is not None and step.execution.outbox is not None
    assert bench.effects() == COMMITTED  # the same checkout and the same close
    assert bench.events().count("mandates_closed") == 1
    assert bench.execution().task.claim_count == 2
    assert bench.purchase() == ("completing", None)
    if mode == "enforce":
        assert isinstance(bench.gate, Gate) and bench.gate.calls == ["D2", "D3", "D3"]


def test_the_reclaim_commits_the_digest_of_the_stored_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = closed_after_uow_c(tmp_path, monkeypatch, "off")
    lapse(bench)

    bench.run()

    outbox, pair = bench.execution().outbox, bench.closer.pair
    assert outbox is not None and pair is not None
    assert outbox.payment_request_digest == payment_request_digest(
        operation_id=outbox.operation_id,
        checkout=bench.merchant.current,
        checkout_jwt=closed_checkout_jwt(pair.checkout.token),
        closed=pair,
    )


def test_a_close_that_expired_before_the_dispatch_fails_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = closed_after_uow_c(tmp_path, monkeypatch, "off")
    bench.clock.advance(300)

    step = bench.run()

    assert (step.status, bench.ended()) == ("closed", ("failed", "close_expired_before_dispatch"))
    assert bench.effects() == CLOSED  # never closed again
    assert reservation(bench) == ("released", "no_outbox")
    assert bench.purchase() == ("failed", "close_expired_before_dispatch")


def test_a_revocation_before_the_reclaim_cancels_without_closing_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = closed_after_uow_c(tmp_path, monkeypatch, "enforce")
    bench.revoke()
    lapse(bench)

    step = bench.run()

    assert (step.status, bench.ended()) == ("closed", ("canceled", "revoked_by_owner"))
    assert bench.effects() == CLOSED
    assert reservation(bench) == ("released", "no_outbox")
    assert bench.purchase() == ("canceled", None)


def test_a_reclaim_whose_closed_pair_is_gone_fails_with_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = closed_after_uow_c(tmp_path, monkeypatch, "off")
    with closing(sqlite3.connect(bench.path)) as connection, connection:
        connection.execute(
            "UPDATE delegation_mandates SET token = 'eyJ.altered.sig~' "
            "WHERE kind = 'closed_checkout'"
        )
    lapse(bench)

    assert bench.run().status == "closed"
    assert bench.ended() == ("failed", "state_conflict")
    assert bench.effects() == CLOSED


# Governance evidence and durable state that fail under a run (§8.1)


class LostEvidence(Gate):
    """The real gate's hooks raise `GOV_EVIDENCE_UNAVAILABLE` when its evidence cannot be
    written (hnp_gate._Record); here the hooks of `phase` always do."""

    def __init__(self, phase: str, **verdicts: Verdict) -> None:
        super().__init__(**verdicts)
        self.phase = phase

    def _decide(self, phase: str, **request: Any) -> HnpGateDecision:
        decision = super()._decide(phase, **request)
        if phase != self.phase:
            return decision

        def lost(connection: sqlite3.Connection) -> None:
            raise GovernanceError("GOV_EVIDENCE_UNAVAILABLE")

        return replace(decision, hooks=(lost,))


@pytest.mark.parametrize(
    ("phase", "effects", "held"),
    [
        ("D2", Effects(creates=1, signatures=0, commits=0, cancels=0), None),
        ("D3", CLOSED, ("released", "no_outbox")),
    ],
)
def test_an_allow_whose_evidence_cannot_be_written_fails_closed(
    tmp_path: Path, phase: str, effects: Effects, held: tuple[str, str] | None
) -> None:
    bench = Bench(tmp_path, mode="enforce", gate=LostEvidence(phase))

    step = bench.run()

    assert (step.status, bench.ended()) == ("closed", ("failed", "governance_unavailable"))
    assert bench.effects() == effects
    assert reservation(bench) == held
    assert bench.purchase() == ("failed", "governance_unavailable")


def test_a_deny_whose_evidence_cannot_be_written_still_ends_the_task(tmp_path: Path) -> None:
    bench = Bench(tmp_path, mode="enforce", gate=LostEvidence("D2", d2=deny(POL["POL-03"])))

    step = bench.run()

    assert (step.status, bench.ended()) == ("closed", ("failed", "governance_unavailable"))
    assert bench.purchase() == ("failed", "governance_unavailable")


def test_a_cancellation_keeps_its_cause_without_its_deny_evidence(tmp_path: Path) -> None:
    bench = Bench(tmp_path, mode="enforce", gate=LostEvidence("D2", d2=deny(POL["POL-07"])))
    assert isinstance(bench.gate, Gate)
    bench.gate.before["D2"] = bench.revoke

    step = bench.run()

    assert (step.status, bench.ended()) == ("closed", ("canceled", "revoked_by_owner"))
    assert bench.purchase() == ("canceled", None)


class Unprepared:
    """`start` that returns a checkout but leaves no purchase behind."""

    def __init__(self, bench: Bench) -> None:
        self.bench = bench

    def start(self, **request: Any) -> Any:
        return SimpleNamespace(checkout=self.bench.merchant.current)


def test_a_durable_row_missing_under_the_run_is_a_conflict(tmp_path: Path) -> None:
    bench = Bench(tmp_path, purchases=Unprepared)

    step = bench.run()

    assert (step.status, bench.ended()) == ("closed", ("failed", "state_conflict"))
    assert bench.execution().grant.state == "revoked"


# The lease across a slow selector (§4.5, «Lease y fencing»)


class Slow:
    """A selector call longer than the lease; `meanwhile` runs once while it lasts."""

    def __init__(self, bench: Callable[[], Bench], meanwhile: Callable[[], object]) -> None:
        self.bench, self.meanwhile, self.calls = bench, meanwhile, 0

    def choose(self, shortlist, intent, preference_text) -> str:
        self.calls += 1
        if self.calls == 1:
            lapse(self.bench())
            self.meanwhile()
        return shortlist[0].candidate.variant_id


def test_a_slow_selector_whose_task_was_reclaimed_stops_before_any_effect(
    tmp_path: Path,
) -> None:
    holder: list[Bench] = []
    inner: list[Any] = []
    selector = Slow(lambda: holder[0], lambda: inner.append(holder[0].run()))
    bench = Bench(tmp_path, selector=selector)
    holder.append(bench)

    step = bench.run()

    assert step.status == "lease_lost"
    assert inner[0].status == "applied"  # the re-claim went on, alone
    assert bench.effects() == COMMITTED
    assert bench.execution().task.claim_count == 2


def test_a_slow_selector_renews_its_own_lease_and_a_second_claim_waits(tmp_path: Path) -> None:
    holder: list[Bench] = []
    selector = Slow(lambda: holder[0], lambda: None)
    bench = Bench(tmp_path, selector=selector)
    holder.append(bench)
    claims: list[str] = []
    bench.merchant.on_create = lambda: claims.append(bench.ledger.claim(bench.task_id).status)

    step = bench.run()

    assert step.status == "applied", step
    assert claims == ["not_runnable"]
    assert bench.effects() == COMMITTED
    assert bench.execution().task.claim_count == 1


# The operator's alert (§8.1)


@pytest.mark.parametrize(
    ("d2", "outcome"),
    [
        (deny(POL["POL-03"], proof="invalid"), "mandate_proof_invalid"),
        (deny(POL["POL-03"]), "policy_denied"),
    ],
)
def test_a_terminal_that_needs_the_operator_raises_one_alert(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, d2: Verdict, outcome: str
) -> None:
    bench = Bench(tmp_path, mode="enforce", gate=Gate(d2=d2))

    with caplog.at_level(logging.ERROR, logger="commerce_agent.hnp.alerts"):
        bench.run()

    alerts = [r for r in caplog.records if r.name == "commerce_agent.hnp.alerts"]
    assert bench.ended() == ("failed", outcome)
    if outcome in OPERATOR_ALERTS:
        [alert] = alerts
        assert alert.levelno == logging.ERROR
        assert bench.task_id in alert.getMessage() and outcome in alert.getMessage()
        assert OWNER not in alert.getMessage()
    else:
        assert alerts == []
