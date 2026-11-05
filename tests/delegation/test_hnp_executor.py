"""HNP-09a: the executor up to the dispatch commitment (hnp-governed.md §3.3, §4.5, §8.1).

Real SQLite for the ledger, the store and the purchase; scripted merchant, Agent Key, freshness
and D2/D3-HNP that count what each step reached (tests/support/hnp_executor.py).
"""

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import APIConnectionError

from commerce_agent.application import PurchaseServiceError, hnp_execution
from commerce_agent.application.hnp_execution import (
    OPERATOR_ALERTS,
    HnpExecutionService,
    NullHnpPolicyGate,
    payment_request_digest,
)
from commerce_agent.domain import PurchaseIntent, RankedCandidate
from commerce_agent.governance.hnp_policy import HnpGrant
from commerce_agent.governance.models import POLICY_REASONS
from commerce_agent.llm.conversation_router import IntentInterpretationError
from tests.support.hnp_executor import (
    CHECKOUT_JWT,
    AgentKey,
    Bench,
    Crash,
    Effects,
    Freshness,
    Gate,
    Merchant,
    Verdict,
    deny,
)

POL = POLICY_REASONS
NOTHING = Effects(creates=0, signatures=0, commits=0, cancels=0)
CREATED = Effects(creates=1, signatures=0, commits=0, cancels=0)
CLOSED = Effects(creates=1, signatures=1, commits=0, cancels=0)
COMMITTED = Effects(creates=1, signatures=1, commits=1, cancels=0)
# D4 (p0-ux08-ux10.md): a revocation or kill switch after the POST cancels the checkout it left.
WITHDRAWN = Effects(creates=1, signatures=0, commits=0, cancels=1)


class Choose:
    """A selector that answers a fixed id and keeps what it was shown."""

    def __init__(self, variant_id: str | None) -> None:
        self.variant_id = variant_id
        self.shown: list[tuple[tuple[RankedCandidate, ...], PurchaseIntent, str | None]] = []

    def choose(self, shortlist, intent, preference_text) -> str:
        self.shown.append((shortlist, intent, preference_text))
        if self.variant_id is None:
            raise RuntimeError("no choice")
        return self.variant_id


class Start:
    """`start` as D1-HNP leaves it: the purchase at `stage`, then `code`, a crash (None) or
    an unexpected error ("error")."""

    def __init__(self, bench: Bench, stage: str, code: str | None) -> None:
        self.bench, self.stage, self.code = bench, stage, code

    def start(self, *, run_id: str, request: str, owner_id: str, grant: HnpGrant, **_):
        self.bench.store.create_purchase(run_id, request, owner_id, grant_id=grant.grant_id)
        if self.stage != "received":
            self.bench.store.update_purchase(run_id, stage=self.stage)
        if self.code is None:
            raise Crash
        if self.code == "error":
            raise RuntimeError("start broke")
        raise PurchaseServiceError("start stopped", code=self.code)


def started(stage: str, code: str | None = "d1_denied"):
    return lambda bench: Start(bench, stage, code)


# The path up to the commitment


def test_b0_commits_the_dispatch_after_one_create_and_one_close(tmp_path: Path) -> None:
    bench = Bench(tmp_path)

    step = bench.run()

    assert step.status == "applied"
    ex = bench.execution()
    assert ex.task.state == "running" and ex.grant.state == "active"
    assert ex.reservation is not None and ex.outbox is not None
    assert (ex.reservation.state, ex.reservation.amount_minor, ex.reservation.checkout_id) == (
        "committed",
        74_000,
        "cs_1",
    )
    assert (ex.reservation.d2_evidence_id, ex.outbox.d3_evidence_id) == (None, None)
    assert bench.events()[-4:] == [
        "task_claimed",
        "reservation_reserved",
        "mandates_closed",
        "dispatch_committed",
    ]
    assert bench.purchase() == ("completing", None)
    assert bench.effects() == COMMITTED
    assert bench.closer.pair is not None
    assert ex.outbox.payment_request_digest == payment_request_digest(
        operation_id=ex.outbox.operation_id,
        checkout=bench.merchant.current,
        checkout_jwt=CHECKOUT_JWT,
        closed=bench.closer.pair,
    )
    closed = {m.kind: m.token for m in bench.ledger.mandates(bench.grant_id)}
    assert closed["closed_payment"] == bench.closer.pair.payment.token


def test_b2_applies_each_decision_inside_its_unit_of_work(tmp_path: Path) -> None:
    gate = Gate()
    bench = Bench(tmp_path, mode="enforce", gate=gate)

    assert bench.run().status == "applied"

    assert gate.calls == ["D2", "D3"]
    assert bench.freshness is not None and bench.freshness.stages == ["close"]
    # D2's hook sees the reservation UoW-1 wrote; D3's sees the outbox row of UoW-2.
    assert gate.applied == [
        ("D2", ("running", "reserved", 0)),
        ("D3", ("running", "committed", 1)),
    ]
    ex = bench.execution()
    assert ex.reservation is not None and ex.outbox is not None
    assert (ex.reservation.d2_evidence_id, ex.outbox.d3_evidence_id) == ("gov_d2_1", "gov_d3_1")
    assert bench.purchase() == ("completing", None)
    assert bench.effects() == COMMITTED
    assert gate.lost == []


def test_the_outbox_row_takes_the_id_the_d3_allow_names(tmp_path: Path) -> None:
    """D3's `transition_ref` is the `outbox_id` its evidence was prepared with (§4.3)."""
    gate = Gate()
    gate.outbox_id = "ob_named_by_d3"
    bench = Bench(tmp_path, mode="enforce", gate=gate)

    assert bench.run().status == "applied"

    outbox = bench.execution().outbox
    assert outbox is not None and outbox.outbox_id == "ob_named_by_d3"


@pytest.mark.parametrize(
    ("where", "seen_by_terminal"),
    [("D2", ("canceled", None, 0)), ("D3", ("canceled", "released", 0))],
)
def test_an_allow_whose_unit_of_work_is_blocked_keeps_its_conflict_evidence(
    tmp_path: Path, where: str, seen_by_terminal: tuple[Any, ...]
) -> None:
    """§4.5: in B2 the loser of a CAS leaves evidence. Its allow's hooks never run; its
    `conflict_hooks` do, inside the terminal's unit of work."""
    gate = Gate()
    bench = Bench(tmp_path, mode="enforce", gate=gate)
    gate.before[where] = lambda: bench.ledger.set_kill_switch(True)

    assert bench.run().status == "closed"

    assert bench.ended() == ("canceled", "kill_switch")
    assert gate.lost == [(where, seen_by_terminal)]
    assert [phase for phase, _ in gate.applied] == (["D2"] if where == "D3" else [])


def test_the_selector_sees_the_shortlist_and_never_a_mandate(tmp_path: Path) -> None:
    selector = Choose("SON-01")
    bench = Bench(tmp_path, selector=selector)

    assert bench.run().status == "applied"

    ((shortlist, _, preference),) = selector.shown
    assert [item.candidate.variant_id for item in shortlist] == ["SON-01"]
    assert preference is None
    assert "eyJ" not in repr(selector.shown) and "~" not in repr(selector.shown)


def test_the_modes_never_mix() -> None:
    with pytest.raises(ValueError, match="all off"):
        HnpExecutionService(
            mode="enforce",
            ledger=None,  # type: ignore[arg-type]
            store=None,  # type: ignore[arg-type]
            purchases=None,  # type: ignore[arg-type]
            checkout=None,  # type: ignore[arg-type]
            closer=None,  # type: ignore[arg-type]
            gate=NullHnpPolicyGate(),
            selector=None,  # type: ignore[arg-type]
            feed=None,  # type: ignore[arg-type]
            clock=None,  # type: ignore[arg-type]
            id_factory=None,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="all off"):
        HnpExecutionService(
            mode="off",
            ledger=None,  # type: ignore[arg-type]
            store=None,  # type: ignore[arg-type]
            purchases=None,  # type: ignore[arg-type]
            checkout=None,  # type: ignore[arg-type]
            closer=None,  # type: ignore[arg-type]
            gate=Gate(),
            selector=None,  # type: ignore[arg-type]
            feed=None,  # type: ignore[arg-type]
            clock=None,  # type: ignore[arg-type]
            id_factory=None,  # type: ignore[arg-type]
        )


# Selection (step 2) and K12


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_an_empty_shortlist_ends_before_start(tmp_path: Path, mode: str) -> None:
    bench = Bench(tmp_path, mode=mode, variant_ids=("SON-07",))  # type: ignore[arg-type]

    assert bench.run().status == "closed"

    state = "escalated" if mode == "enforce" else "failed"
    assert bench.ended() == (state, "no_candidate_in_scope")
    assert bench.purchase() is None
    assert bench.effects() == NOTHING


@pytest.mark.parametrize("choice", ["SON-07", "SON-99"], ids=["unavailable", "foreign"])
def test_in_b0_a_choice_outside_the_shortlist_never_reaches_start(
    tmp_path: Path, choice: str
) -> None:
    """K12: B0-HNP has no D1, so the executor's own guard keeps the grant."""
    bench = Bench(tmp_path, selector=Choose(choice))

    assert bench.run().status == "closed"

    assert bench.ended() == ("failed", "selection_not_eligible")
    assert bench.purchase() is None
    assert bench.effects() == NOTHING


@pytest.mark.parametrize("choice", ["SON-07", "SON-99"], ids=["unavailable", "foreign"])
def test_in_b2_a_choice_outside_the_shortlist_goes_to_start(tmp_path: Path, choice: str) -> None:
    """K12 in B2-HNP: no guard before `start`, whose D1-HNP denies a candidate outside the grant
    with POL-01 (test_hnp_runtime, real D1). These two are not even candidates, so `start` itself
    refuses them; either way nothing is created."""
    bench = Bench(tmp_path, mode="enforce", selector=Choose(choice))

    assert bench.run().status == "closed"

    assert bench.ended() == ("failed", "selection_not_eligible")
    assert bench.purchase() == ("failed", "invalid_candidate")
    assert bench.effects() == NOTHING


# A selector that makes no choice (§3.3, §8.1): `selection_unavailable`, never the
# `selection_not_eligible` of a choice K12 or POL-01 refuses.


class Unavailable:
    """A selector that makes no choice: it raises `error`, or answers None against its contract."""

    def __init__(self, error: Exception | None) -> None:
        self.error = error

    def choose(self, shortlist, intent, preference_text) -> str:
        if self.error is not None:
            raise self.error
        return None  # type: ignore[return-value]


SECRET = "catalog text the log must never carry"


def unreachable_model() -> IntentInterpretationError:
    """What `OpenAIDelegatedChooser` raises when the model cannot be reached: the SDK's error is
    its cause (`raise ... from error`)."""
    error = IntentInterpretationError(SECRET)
    request = httpx.Request("POST", "https://model.invalid/v1/responses")
    error.__cause__ = APIConnectionError(message=SECRET, request=request)
    return error


FAILURES = {
    "raises": lambda: Unavailable(RuntimeError(SECRET)),
    "model-unreachable": lambda: Unavailable(unreachable_model()),
    "answers-none": lambda: Unavailable(None),
}


def grant_of(bench: Bench) -> tuple[str, str | None]:
    grant = bench.execution().grant
    return grant.state, grant.revoked_reason


def test_a_selector_that_fails_escalates_before_start_in_b2(tmp_path: Path) -> None:
    """It fails closed like the intent reviewer: the decision goes back to the buyer, nothing is
    created, signed or charged, and the grant closes as an escalation."""
    bench = Bench(tmp_path, mode="enforce", selector=Choose(None))

    assert bench.run().status == "closed"

    assert bench.ended() == ("escalated", "selection_unavailable")
    assert grant_of(bench) == ("revoked", "escalated")
    assert bench.purchase() is None
    assert bench.effects() == NOTHING
    assert isinstance(bench.gate, Gate) and bench.gate.calls == []
    assert "choice_reviewed" not in bench.events()
    assert bench.events()[-1] == "task_finished"


def test_in_b0_a_selector_that_fails_fails_with_the_same_code(tmp_path: Path) -> None:
    """§8.1: B0-HNP never escalates; the cause that escalates in B2 fails with its code."""
    bench = Bench(tmp_path, selector=Choose(None))

    assert bench.run().status == "closed"

    assert bench.ended() == ("failed", "selection_unavailable")
    assert grant_of(bench) == ("revoked", "failed")
    assert bench.purchase() is None
    assert bench.effects() == NOTHING


@pytest.mark.parametrize("failure", FAILURES.values(), ids=FAILURES.keys())
@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_every_way_of_making_no_choice_is_the_same_outcome(
    tmp_path: Path, mode: str, failure: Any
) -> None:
    bench = Bench(tmp_path, mode=mode, selector=failure())  # type: ignore[arg-type]

    assert bench.run().status == "closed"

    state = "escalated" if mode == "enforce" else "failed"
    assert bench.ended() == (state, "selection_unavailable")
    assert bench.purchase() is None
    assert bench.effects() == NOTHING


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_a_selector_failure_alerts_nobody_and_logs_its_kind(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, mode: str
) -> None:
    """The operator's alert is for altered mandates or a misconfigured scope (§8.1), not for a
    model that did not answer. The warning names the task and the classes, never the message."""
    bench = Bench(tmp_path, mode=mode, selector=Unavailable(unreachable_model()))  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING):
        bench.run()

    assert [r for r in caplog.records if r.name == "commerce_agent.hnp.alerts"] == []
    [warning] = [r for r in caplog.records if r.name == hnp_execution.__name__]
    assert warning.levelno == logging.WARNING
    message = warning.getMessage()
    assert bench.task_id in message
    assert "IntentInterpretationError <- APIConnectionError" in message
    assert SECRET not in caplog.text
    assert "selection_unavailable" not in OPERATOR_ALERTS
    assert "selection_not_eligible" in OPERATOR_ALERTS


# `start` (steps 3-4): D1-HNP, the merchant and the budget guard


@pytest.mark.parametrize(
    ("reasons", "ended", "purchase"),
    [
        ((POL["POL-11"],), ("escalated", "budget_not_satisfiable"), ("canceled", None)),
        (
            (POL["POL-01"], POL["POL-11"]),
            ("failed", "selection_not_eligible"),
            ("failed", "selection_not_eligible"),
        ),
        ((POL["POL-02"],), ("failed", "merchant_not_allowed"), ("failed", "merchant_not_allowed")),
        ((), ("failed", "governance_unavailable"), ("failed", "governance_unavailable")),
    ],
)
def test_a_d1_deny_closes_the_received_purchase_with_the_task(
    tmp_path: Path,
    reasons: tuple[str, ...],
    ended: tuple[str, str],
    purchase: tuple[str, str | None],
) -> None:
    bench = Bench(tmp_path, mode="enforce", purchases=started("received"), d1_reasons=reasons)

    assert bench.run().status == "closed"

    assert bench.ended() == ended
    assert bench.purchase() == purchase
    assert bench.effects() == NOTHING


@pytest.mark.parametrize(
    "code", ["d1_allow_unapplied", "d1_dispatch_blocked", "d1_dispatch_uncertain", "error"]
)
def test_a_selected_purchase_is_left_to_d1_recovery(tmp_path: Path, code: str) -> None:
    """HNP-05b and K15: an allow without its dispatch result has no HNP transition, so no hook
    runs, and whatever stopped `start` the task fails `governance_unavailable`."""
    bench = Bench(tmp_path, mode="enforce", purchases=started("selected", code))

    assert bench.run().status == "closed"

    assert bench.ended() == ("failed", "governance_unavailable")
    assert bench.purchase() == ("selected", None)
    assert bench.execution().grant.state == "revoked"


def test_the_budget_guard_fails_the_task_and_keeps_the_canceled_purchase(tmp_path: Path) -> None:
    bench = Bench(tmp_path, budget=80_000, merchant=Merchant(total=90_000))

    assert bench.run().status == "closed"

    assert bench.ended() == ("failed", "budget_guard")
    assert bench.purchase() == ("canceled", None)
    assert bench.effects() == Effects(creates=1, signatures=0, commits=0, cancels=1)


def test_a_merchant_refusal_fails_the_task_and_keeps_its_code(tmp_path: Path) -> None:
    bench = Bench(tmp_path, merchant=Merchant(refuse=True))

    assert bench.run().status == "closed"

    assert bench.ended() == ("failed", "checkout_failed")
    assert bench.purchase() == ("failed", "out_of_stock")
    assert bench.effects() == CREATED


# Freshness (step 5) and D2-HNP (step 6)


@pytest.mark.parametrize(
    ("code", "ended", "cancels"),
    [
        ("checkout_terms_changed", ("escalated", "offer_changed"), 1),
        ("offer_freshness_unavailable", ("failed", "governance_unavailable"), 0),
    ],
)
def test_freshness_at_close_stops_before_d2_and_any_signature(
    tmp_path: Path, code: str, ended: tuple[str, str], cancels: int
) -> None:
    gate = Gate()
    bench = Bench(tmp_path, mode="enforce", gate=gate, freshness=Freshness(code))

    assert bench.run().status == "closed"

    assert bench.ended() == ended
    assert gate.calls == []
    assert bench.purchase() == (("canceled", None) if cancels else ("failed", ended[1]))
    assert bench.effects() == Effects(creates=1, signatures=0, commits=0, cancels=cancels)


@pytest.mark.parametrize(
    ("verdict", "ended"),
    [
        (deny(POL["POL-05"]), ("escalated", "terms_out_of_scope")),
        (deny(POL["POL-05"], POL["POL-08"]), ("escalated", "terms_out_of_scope")),
        (deny(POL["POL-04"], POL["POL-05"]), ("failed", "policy_denied")),
        (
            deny("GOV_AUTHORIZATION_PROFILE_UNRESOLVED"),
            ("escalated", "authorization_profile_unresolved"),
        ),
        (deny("GOV_ENGINE_UNAVAILABLE"), ("failed", "governance_unavailable")),
        (deny("GOV_STATE_CONFLICT"), ("failed", "state_conflict")),
        (deny(POL["POL-05"], proof="invalid"), ("failed", "mandate_proof_invalid")),
        (Verdict("allow", proof="invalid"), ("failed", "mandate_proof_invalid")),
        (Verdict("raise"), ("failed", "governance_unavailable")),
        (Verdict("none"), ("failed", "governance_unavailable")),
    ],
    ids=[
        "pol05",
        "pol05-08",
        "pol04-05",
        "unresolved",
        "engine",
        "gov-state-conflict",
        "proof-invalid-deny",
        "proof-invalid-allow",
        "raises",
        "no-decision",
    ],
)
def test_d2_classifies_before_the_reservation_and_the_close(
    tmp_path: Path, verdict: Verdict, ended: tuple[str, str]
) -> None:
    gate = Gate(d2=verdict)
    bench = Bench(tmp_path, mode="enforce", gate=gate)

    assert bench.run().status == "closed"

    assert bench.ended() == ended
    assert gate.calls == ["D2"]
    assert bench.execution().reservation is None
    escalated = ended[0] == "escalated"
    assert bench.purchase() == (("canceled", None) if escalated else ("failed", ended[1]))
    assert bench.effects() == Effects(creates=1, signatures=0, commits=0, cancels=int(escalated))
    if verdict.outcome == "deny":
        # A deny's evidence commits with the terminal (§4.5).
        assert gate.applied == [("D2", (ended[0], None, 0))]


# D3-HNP (step 9)


@pytest.mark.parametrize(
    ("verdict", "ended"),
    [
        (deny(POL["POL-10"]), ("failed", "policy_denied")),
        (deny(POL["POL-05"]), ("escalated", "terms_out_of_scope")),
        (deny(POL["POL-09"], proof="invalid"), ("failed", "mandate_proof_invalid")),
        (Verdict("raise"), ("failed", "governance_unavailable")),
    ],
)
def test_d3_releases_the_reservation_and_commits_nothing(
    tmp_path: Path, verdict: Verdict, ended: tuple[str, str]
) -> None:
    gate = Gate(d3=verdict)
    bench = Bench(tmp_path, mode="enforce", gate=gate)

    assert bench.run().status == "closed"

    assert bench.ended() == ended
    ex = bench.execution()
    assert ex.reservation is not None and ex.outbox is None
    assert (ex.reservation.state, ex.reservation.release_basis) == ("released", "no_outbox")
    escalated = ended[0] == "escalated"
    assert bench.effects() == Effects(creates=1, signatures=1, commits=0, cancels=int(escalated))
    assert bench.purchase() == (("canceled", None) if escalated else ("failed", ended[1]))


# What the store committed first (§4.5, §8.1 step 1)


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_an_expiry_inside_the_margin_cancels_without_a_close(tmp_path: Path, mode: str) -> None:
    """UoW-1 needs 300 s of grant left: with 250 s the run never reaches the Agent Key."""
    bench = Bench(tmp_path, mode=mode, valid_for=250)  # type: ignore[arg-type]

    assert bench.run().status == "closed"

    assert bench.ended() == ("canceled", "grant_expired")
    assert bench.execution().reservation is None
    assert bench.purchase() == ("canceled", None)
    assert bench.effects() == CREATED


def test_a_revocation_before_uow1_cancels_before_the_reservation(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    bench.merchant.on_create = bench.revoke

    assert bench.run().status == "closed"

    assert bench.ended() == ("canceled", "revoked_by_owner")
    assert bench.execution().reservation is None
    assert bench.purchase() == ("canceled", None)
    assert bench.effects() == WITHDRAWN


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_a_revocation_between_uow1_and_uow2_commits_nothing(tmp_path: Path, mode: str) -> None:
    bench = Bench(tmp_path, mode=mode)  # type: ignore[arg-type]
    bench.closer.before = bench.revoke

    assert bench.run().status == "closed"

    assert bench.ended() == ("canceled", "revoked_by_owner")
    ex = bench.execution()
    assert ex.reservation is not None and ex.reservation.state == "released"
    assert ex.grant.revoked_reason == "owner"
    assert bench.purchase() == ("canceled", None)
    assert bench.effects() == Effects(creates=1, signatures=1, commits=0, cancels=1)


def test_a_d2_deny_after_a_committed_revocation_is_the_revocation(tmp_path: Path) -> None:
    gate = Gate(d2=deny(POL["POL-07"]))
    bench = Bench(tmp_path, mode="enforce", gate=gate)
    gate.before["D2"] = bench.revoke

    assert bench.run().status == "closed"

    assert bench.ended() == ("canceled", "revoked_by_owner")
    assert bench.purchase() == ("canceled", None)


@pytest.mark.parametrize(
    ("where", "stage"),
    [("D2", "uow1"), ("D3", "uow2")],
)
@pytest.mark.parametrize("toggle", ["on", "on-off"])
def test_the_kill_switch_cancels_at_the_next_unit_of_work(
    tmp_path: Path, where: str, stage: str, toggle: str
) -> None:
    """A changed control_epoch cancels even if the switch is already off again (§4.5)."""
    gate = Gate()
    bench = Bench(tmp_path, mode="enforce", gate=gate)

    def flip() -> None:
        bench.ledger.set_kill_switch(True)
        if toggle == "on-off":
            bench.ledger.set_kill_switch(False)

    gate.before[where] = flip

    assert bench.run().status == "closed"

    assert bench.ended() == ("canceled", "kill_switch")
    blocked = [
        e for e in bench.ledger.events(bench.grant_id) if e.event_type == "kill_switch_blocked"
    ]
    assert [event.body["stage"] for event in blocked] == [stage]
    assert bench.effects().commits == 0
    assert bench.purchase() == ("canceled", None)


@pytest.mark.parametrize("mode", ["off", "enforce"])
@pytest.mark.parametrize("where", ["merchant_jwt", "agent_key"])
@pytest.mark.parametrize(
    ("change", "ended"),
    [("pulse", ("canceled", "kill_switch")), ("bump", ("failed", "state_conflict"))],
)
def test_nothing_between_uow1_and_uow2_escapes_the_commitment(
    tmp_path: Path, mode: str, where: str, change: str, ended: tuple[str, str]
) -> None:
    """K10: UoW-C checks neither the switch nor the grant, so UoW-2 compares against what UoW-1
    validated. A switch turned on and off during the close still cancels (§4.5, §8.4)."""
    bench = Bench(tmp_path, mode=mode)  # type: ignore[arg-type]

    def moved() -> None:
        if change == "bump":
            bench.bump_grant()
            return
        bench.ledger.set_kill_switch(True)
        bench.ledger.set_kill_switch(False)

    if where == "merchant_jwt":
        bench.merchant.on_sign = moved
    else:
        bench.closer.before = moved

    assert bench.run().status == "closed"

    assert bench.ended() == ended
    # D4: the kill switch cancels the checkout; a CAS lost without a cause keeps it.
    cancels = int(change == "pulse")
    assert bench.effects() == Effects(creates=1, signatures=1, commits=0, cancels=cancels)
    reservation = bench.execution().reservation
    assert reservation is not None and reservation.state == "released"
    assert bench.purchase() == (("canceled", None) if change == "pulse" else ("failed", ended[1]))


def test_the_kill_switch_at_the_claim_starts_nothing(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    bench.ledger.set_kill_switch(True)

    assert bench.run().status == "closed"

    assert bench.ended() == ("canceled", "kill_switch")
    assert bench.effects() == NOTHING


def test_a_lost_cas_without_a_cause_fails_with_state_conflict(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    bench.merchant.on_create = bench.bump_grant

    assert bench.run().status == "closed"

    assert bench.ended() == ("failed", "state_conflict")
    assert bench.execution().grant.state == "revoked"
    assert bench.purchase() == ("failed", "state_conflict")
    assert bench.effects() == CREATED


def test_a_foreign_lease_ends_the_run_without_writing(tmp_path: Path) -> None:
    bench = Bench(tmp_path)

    def taken() -> None:
        bench.clock.advance(121)
        assert bench.ledger.claim(bench.task_id).status == "applied"

    bench.merchant.on_create = taken

    assert bench.run().status == "lease_lost"

    ex = bench.execution()
    assert (ex.task.state, ex.task.claim_count, ex.reservation) == ("running", 2, None)
    assert bench.purchase() == ("checkout_prepared", None)
    assert bench.effects() == CREATED


def test_the_agent_key_failing_fails_the_task_and_releases(tmp_path: Path) -> None:
    bench = Bench(tmp_path, closer=AgentKey(fail=True))

    assert bench.run().status == "closed"

    assert bench.ended() == ("failed", "checkout_failed")
    reservation = bench.execution().reservation
    assert reservation is not None and reservation.state == "released"
    assert bench.purchase() == ("failed", "checkout_failed")
    assert bench.effects() == CLOSED


# A re-claim (until HNP-09b resumes one)


def test_a_reclaim_after_progress_never_creates_or_closes_again(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    bench.closer.before = lambda: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        bench.run()
    bench.clock.advance(121)
    bench.closer.before = None

    assert bench.run().status == "closed"

    assert bench.ended() == ("failed", "state_conflict")
    reservation = bench.execution().reservation
    assert reservation is not None and reservation.state == "released"
    assert bench.purchase() == ("failed", "state_conflict")
    assert bench.effects() == CREATED


def test_a_committed_task_is_not_run_again(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    assert bench.run().status == "applied"

    assert bench.run().status == "not_runnable"
    bench.clock.advance(121)
    assert bench.run().status == "applied"

    assert bench.effects() == COMMITTED


@pytest.mark.parametrize(
    ("stage", "purchase"),
    [("received", ("canceled", None)), ("selected", ("selected", None))],
)
def test_a_claim_that_cancels_closes_the_purchase_unless_d1_must_recover_it(
    tmp_path: Path, stage: str, purchase: tuple[str, str | None]
) -> None:
    """A `selected` purchase has no transition (HNP-05b): its hook would undo the claim's close."""
    bench = Bench(tmp_path, mode="enforce", purchases=started(stage, code=None))
    with pytest.raises(Crash):
        bench.run()
    bench.ledger.set_kill_switch(True)
    bench.clock.advance(121)

    assert bench.run().status == "closed"

    assert bench.ended() == ("canceled", "kill_switch")
    assert bench.purchase() == purchase


@pytest.mark.parametrize(
    ("stage", "ended", "purchase"),
    [
        ("received", ("failed", "state_conflict"), ("failed", "state_conflict")),
        ("selected", ("failed", "governance_unavailable"), ("selected", None)),
    ],
)
def test_a_reclaim_after_start_crashed_fails_without_running_it_again(
    tmp_path: Path, stage: str, ended: tuple[str, str], purchase: tuple[str, str | None]
) -> None:
    """K15: a `selected` purchase waits for D1's recovery; anything else is a state conflict."""
    bench = Bench(tmp_path, mode="enforce", purchases=started(stage, code=None))
    with pytest.raises(Crash):
        bench.run()
    bench.clock.advance(121)

    assert bench.run().status == "closed"

    assert bench.ended() == ended
    assert bench.purchase() == purchase


@pytest.mark.parametrize(
    "reasons", [(POL["POL-04"],), (POL["POL-05"],)], ids=["deny", "escalation"]
)
def test_a_revocation_that_lands_after_the_classification_still_names_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reasons: tuple[str, ...]
) -> None:
    """§4.5: the owner's revocation committed first, so the terminal is `revoked_by_owner`. A
    revocation after the POST cancels the checkout once (D4), even when the deny it renames is an
    escalation, which cancels its own (§8.2)."""
    gate = Gate(d2=deny(*reasons))
    bench = Bench(tmp_path, mode="enforce", gate=gate)
    read = bench.ledger.execution

    def read_then_revoke(task_id: str):
        current = read(task_id)
        if gate.calls:  # the deny's classification read: `policy_denied`, then the revocation
            bench.revoke()
        return current

    monkeypatch.setattr(bench.ledger, "execution", read_then_revoke)
    step = bench.run()
    monkeypatch.undo()

    assert step.status == "closed"
    assert bench.ended() == ("canceled", "revoked_by_owner")
    assert bench.purchase() == ("canceled", None)
    assert bench.effects() == WITHDRAWN
