"""Autonomous execution of one Human Not Present task up to the dispatch commitment.

docs/design/hnp-governed.md §3.3 (steps 1 to 10), §4.5 and §8.1. Nobody is present: the executor
reads the grant from the ledger, never from a model or a buyer token, and the model at most picks
a variant of the grant's shortlist. Each external effect waits for the step before it:

1. claim, with a lease; 2. shortlist and selector; 3-4. `PurchaseService.start` with the grant
(D1-HNP in enforce) up to `checkout_prepared`; 5. freshness `close`; 6. D2-HNP; 7. UoW-1
reserves; 8. the merchant's signed checkout, the Agent Key close and UoW-C; 9. D3-HNP; 10. UoW-2
commits the outbox row and moves the purchase to `completing`. Dispatching it is HNP-09b's.

B0-HNP (`off`) runs the same ledger steps with `NullHnpPolicyGate`, without D1, freshness or
escalation: what B2-HNP escalates ends `failed` with the same code (§8.1, §11).

A blocked unit of work already classified why (§4.5): the executor ends the task with that
outcome, by CAS on its lease, and expects the authority its own last step returned, never a
re-read one (K10). A lost lease ends nothing.
"""

import logging
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, NoReturn, Protocol

from commerce_agent.acp.feed import ProductFeed
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.ap2 import checkout_hash
from commerce_agent.ap2_hnp import MERCHANT_ID, MandatePair
from commerce_agent.ap2_hnp import SignedMandate as SignedAp2Mandate
from commerce_agent.application.hnp_selection import HnpSelector
from commerce_agent.application.offer_freshness import FreshnessStage
from commerce_agent.application.purchase import (
    CheckoutDetails,
    CheckoutGateway,
    PurchasePreparation,
    PurchaseServiceError,
    PurchaseStage,
)
from commerce_agent.approval import ApprovalError
from commerce_agent.delegation.models import (
    Authority,
    ClosedMandates,
    DispatchCommit,
    Execution,
    KillSwitchStage,
    OutcomeCode,
    ReservationRequest,
    SignedMandate,
    Step,
    TaskFinish,
    token_digest,
)
from commerce_agent.delegation.ports import TransactionHook
from commerce_agent.domain import PurchaseIntent, rank_candidates
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.hnp_models import ConsentScope, parse_consent_scope
from commerce_agent.governance.hnp_policy import HnpGrant
from commerce_agent.governance.hnp_ports import HnpGateDecision, HnpPolicyGate
from commerce_agent.governance.models import POLICY_REASONS
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger, utc_moment
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore

HNP_REQUEST = "Compra delegada"
PAYMENT_REQUEST_DOMAIN = b"hnp-payment-request/v1\0"
Mode = Literal["off", "enforce"]
Terminal = Literal["escalated", "failed", "canceled"]

_CANCELLATIONS = frozenset({"revoked_by_owner", "kill_switch", "grant_expired"})
_ESCALATIONS = frozenset(
    {
        "budget_not_satisfiable",
        "no_candidate_in_scope",
        "offer_changed",
        "terms_out_of_scope",
        "authorization_profile_unresolved",
    }
)
# §8.1, step 4 of the classification: only POL-05 and/or POL-08 escalate.
_ESCALATING_REASONS = frozenset({POLICY_REASONS["POL-05"], POLICY_REASONS["POL-08"]})
# A D1 deny, fail-closed first: an ineligible selection or merchant fails before a budget escalates.
_D1_OUTCOMES: tuple[tuple[str, OutcomeCode], ...] = (
    (POLICY_REASONS["POL-01"], "selection_not_eligible"),
    (POLICY_REASONS["POL-02"], "merchant_not_allowed"),
    (POLICY_REASONS["POL-11"], "budget_not_satisfiable"),
)
# `start` errors where D1 could not decide, as opposed to a merchant that refused the checkout.
_D1_UNAVAILABLE = frozenset(
    {
        "d1_unavailable",
        "d1_capture_failed",
        "d1_invalid_result",
        "d1_recovery_required",
        "d1_allow_unapplied",
        "d1_dispatch_blocked",
    }
)
_START_OUTCOMES: dict[str, OutcomeCode] = {
    "budget_exceeded": "budget_guard",
    "invalid_candidate": "selection_not_eligible",
    "no_candidate": "no_candidate_in_scope",
}
_log = logging.getLogger(__name__)


class PurchaseStarter(Protocol):
    """`PurchaseService.start` with a grant: D1-HNP in enforce, up to `checkout_prepared`."""

    def start(
        self,
        *,
        run_id: str,
        request: str,
        intent: PurchaseIntent,
        selected_variant_id: str,
        details: CheckoutDetails,
        owner_id: str,
        feed: ProductFeed,
        grant: HnpGrant,
    ) -> PurchasePreparation: ...


class FreshnessCheck(Protocol):
    """`OfferFreshnessGuard` in enforce: raises unless the live offer still matches."""

    def check(self, *, run_id: str, checkout: CheckoutSession, stage: FreshnessStage) -> None: ...


class MandateCloser(Protocol):
    """The Agent Key (`ap2_hnp.AgentKeyCloser`): only this service reaches it."""

    def close(
        self,
        open_checkout: str,
        open_payment: str,
        *,
        checkout_jwt: str,
        checkout: CheckoutSession,
        closed_at: datetime,
    ) -> MandatePair: ...


class NullHnpPolicyGate:
    """B0-HNP (`off`): evaluates nothing and fabricates no decision (spec §11)."""

    def decide_d2(
        self,
        *,
        run_id: str,
        grant_id: str,
        checkout: CheckoutSession,
        reservation_id: str,
        operation_id: str,
    ) -> HnpGateDecision:
        return HnpGateDecision(decision=None, proof=None, evidence_id=None)

    decide_d3 = decide_d2


def payment_request_digest(
    *, operation_id: str, checkout: CheckoutSession, checkout_jwt: str, closed: MandatePair
) -> str:
    """The PSP request the dispatch sends, fixed at the commitment (`dispatch_outbox`, §4.3).

    Every send of `<operation_id>:payment` must rebuild exactly this request from the outbox row
    and the stored closed mandates. The sandbox card comes from configuration and is not part of it.
    """
    request = {
        "idempotency_key": f"{operation_id}:payment",
        "allowance": {
            "max_amount": checkout.total_minor,
            "currency": checkout.currency,
            "checkout_session_id": checkout.id,
            "merchant_id": MERCHANT_ID,
            "expires_at": checkout.expires_at,
        },
        "ap2_checkout_jwt": token_digest(checkout_jwt),
        "ap2_payment_mandate": token_digest(closed.payment.token),
        "ap2_checkout_mandate": token_digest(closed.checkout.token),
    }
    return sha256_digest(PAYMENT_REQUEST_DOMAIN + canonical_bytes(request))


def sqlite_d1_denials(path: Path) -> Callable[[str], tuple[str, ...]]:
    """The reason codes of a run's latest D1 decision when it denied, from `governance_evidence`."""

    def reasons(run_id: str) -> tuple[str, ...]:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            records = [
                item
                for item in SqliteGovernanceEvidenceStore(connection).for_run(run_id)
                if isinstance(item, DecisionRecord) and item.decision.phase == "D1"
            ]
        if not records or records[-1].decision.outcome != "deny":
            return ()
        return records[-1].decision.reason_codes

    return reasons


@dataclass(frozen=True)
class _Run:
    task_id: str
    lease_id: str
    run_id: str
    grant_id: str
    owner_id: str

    @classmethod
    def of(cls, ex: Execution) -> "_Run":
        task = ex.task
        if task.lease_id is None or task.run_id is None:
            raise LookupError("A claimed task without its lease or run")
        return cls(task.task_id, task.lease_id, task.run_id, task.grant_id, task.owner_id)


class _Stopped(Exception):
    """The run ended at `step`: a terminal, a lost lease or a step that cannot go on."""

    def __init__(self, step: Step) -> None:
        super().__init__(step.status)
        self.step = step


class HnpExecutionService:
    def __init__(
        self,
        *,
        mode: Mode,
        ledger: SqliteDelegationLedger,
        store: SqliteAgentStore,
        purchases: PurchaseStarter,
        checkout: CheckoutGateway,
        closer: MandateCloser,
        gate: HnpPolicyGate,
        selector: HnpSelector,
        feed: Callable[[], ProductFeed],
        clock: Callable[[], datetime],
        id_factory: Callable[[str], str],
        freshness: FreshnessCheck | None = None,
        d1_denials: Callable[[str], tuple[str, ...]] | None = None,
    ) -> None:
        # B0-HNP runs no PDP and no freshness; B2-HNP never runs without them (spec §11).
        if (mode == "off") != isinstance(gate, NullHnpPolicyGate) or (mode == "off") != (
            freshness is None and d1_denials is None
        ):
            raise ValueError("HNP governance must be all off (B0-HNP) or all enforced (B2-HNP)")
        self._mode: Mode = mode
        self._ledger, self._store, self._purchases = ledger, store, purchases
        self._checkout, self._closer, self._gate, self._selector = checkout, closer, gate, selector
        self._feed, self._clock, self._new_id = feed, clock, id_factory
        self._freshness, self._d1_denials = freshness, d1_denials

    def run(self, task_id: str) -> Step:
        """Steps 1 to 10 of one task. The result is the step that ended this run: `applied` once
        UoW-2 committed (the dispatch is pending), `closed` with a terminal, `lease_lost`, or the
        claim's own answer."""
        claimed = self._ledger.claim(task_id, hooks=self._reclaim_hooks(task_id))
        if claimed.status != "applied" or claimed.execution is None:
            return claimed
        ex = claimed.execution
        if ex.outbox is not None:
            return claimed  # committed before a crash: its dispatch resumes it (HNP-09b)
        run = _Run.of(ex)
        try:
            if ex.reservation is not None or self._store.purchase(run.run_id) is not None:
                # A re-claim after progress. Resuming it is HNP-09b's; until then a re-claim
                # never repeats a checkout or a close (§4.5, UoW-C).
                self._end(run, "state_conflict", purchase=self._closable(run.run_id))
            return self._execute(run, ex)
        except _Stopped as stopped:
            return stopped.step

    def _execute(self, run: _Run, ex: Execution) -> Step:
        try:
            consent = parse_consent_scope(ex.grant.scope_json.encode())
            intent = PurchaseIntent.model_validate_json(ex.grant.intent_json)
            details = CheckoutDetails.model_validate_json(ex.grant.checkout_details_json)
        except ValueError:
            self._end(run, "state_conflict")
        try:
            feed = self._feed()
        except Exception:
            self._end(run, "checkout_failed")
        variant_id = self._select(run, feed, intent, consent)
        checkout = self._start(run, feed, intent, consent, details, variant_id)
        purchase = self._store.purchase_state_version(run.run_id)
        if self._stage(run.run_id) != PurchaseStage.CHECKOUT_PREPARED or purchase is None:
            raise LookupError("start returned without a prepared HNP purchase")
        self._check_freshness(run, checkout, purchase)
        reservation_id, operation_id = self._new_id("rsv"), self._new_id("op")
        ids = (reservation_id, operation_id)
        d2 = self._decide(run, "D2", checkout, ids, ex.authority, purchase)
        reserve = ReservationRequest(
            task_id=run.task_id,
            lease_id=run.lease_id,
            expected=ex.authority,  # K10: the claim's, never re-read
            reservation_id=reservation_id,
            operation_id=operation_id,
            checkout_id=checkout.id,
            amount_minor=checkout.total_minor,
            d2_evidence_id=d2.evidence_id,
        )
        reserved = self._applied(run, self._ledger.reserve(reserve, hooks=d2.hooks), purchase)
        closed, digest = self._close(run, checkout, reserved, operation_id, purchase)
        d3 = self._decide(run, "D3", checkout, ids, closed.authority, purchase)
        if closed.reservation is None:
            raise LookupError("UoW-C returned without the reservation")
        commit = DispatchCommit(
            task_id=run.task_id,
            lease_id=run.lease_id,
            expected=closed.authority,
            reservation_id=reservation_id,
            reservation_version=closed.reservation.state_version,
            outbox_id=self._new_id("ob"),
            checkout_expires_at=checkout.expires_at,
            payment_request_digest=digest,
            d3_evidence_id=d3.evidence_id,
        )
        hooks = (*d3.hooks, self._completing(run.run_id, purchase))
        step = self._ledger.commit_dispatch(commit, hooks=hooks)
        self._applied(run, step, purchase, stage="uow2")
        return step

    # Steps 2 to 5

    def _select(
        self, run: _Run, feed: ProductFeed, intent: PurchaseIntent, consent: ConsentScope
    ) -> str:
        granted = set(consent.scope.variant_ids)
        shortlist = tuple(
            item
            for item in rank_candidates(feed, intent).ranked
            if item.candidate.variant_id in granted
        )
        if not shortlist:
            self._end(run, "no_candidate_in_scope")
        try:
            chosen = self._selector.choose(shortlist, intent, consent.preference_text)
        except Exception:
            _log.warning("HNP selector made no choice")
            chosen = None
        if chosen is None or chosen not in {item.candidate.variant_id for item in shortlist}:
            # K12: a choice outside the shortlist is never bought, in B0-HNP too.
            self._end(run, "selection_not_eligible")
        return chosen

    def _start(
        self,
        run: _Run,
        feed: ProductFeed,
        intent: PurchaseIntent,
        consent: ConsentScope,
        details: CheckoutDetails,
        variant_id: str,
    ) -> CheckoutSession:
        grant = HnpGrant(grant_id=run.grant_id, variant_ids=consent.scope.variant_ids)
        try:
            prepared = self._purchases.start(
                run_id=run.run_id,
                request=HNP_REQUEST,
                intent=intent,
                selected_variant_id=variant_id,
                details=details,
                owner_id=run.owner_id,
                feed=feed,
                grant=grant,
            )
        except PurchaseServiceError as error:
            outcome = self._start_outcome(run.run_id, error.code)
        except Exception:
            outcome = "checkout_failed"
        else:
            return prepared.checkout
        # §4.6: the terminal fails or cancels a `received` purchase and keeps what `start` left.
        self._end(run, outcome, purchase=self._closable(run.run_id))

    def _start_outcome(self, run_id: str, code: str) -> OutcomeCode:
        if code == "d1_denied":
            try:
                reasons = set(self._d1_denials(run_id)) if self._d1_denials else set()
            except Exception:
                return "governance_unavailable"
            for reason, outcome in _D1_OUTCOMES:
                if reason in reasons:
                    return outcome
            return "governance_unavailable"
        if code in _D1_UNAVAILABLE:
            return "governance_unavailable"
        return _START_OUTCOMES.get(code, "checkout_failed")

    def _check_freshness(self, run: _Run, checkout: CheckoutSession, purchase: int) -> None:
        if self._freshness is None:
            return
        try:
            self._freshness.check(run_id=run.run_id, checkout=checkout, stage="close")
        except Exception as error:
            changed = isinstance(error, ApprovalError) and error.code == "checkout_terms_changed"
            outcome = "offer_changed" if changed else "governance_unavailable"
            self._end(run, outcome, purchase=purchase, checkout=checkout)

    # D2-HNP and D3-HNP (§8.1)

    def _decide(
        self,
        run: _Run,
        phase: Literal["D2", "D3"],
        checkout: CheckoutSession,
        ids: tuple[str, str],
        expected: Authority,
        purchase: int,
    ) -> HnpGateDecision:
        decide = self._gate.decide_d2 if phase == "D2" else self._gate.decide_d3
        stage: KillSwitchStage = "uow1" if phase == "D2" else "uow2"
        try:
            result = decide(
                run_id=run.run_id,
                grant_id=run.grant_id,
                checkout=checkout,
                reservation_id=ids[0],
                operation_id=ids[1],
            )
        except Exception:
            _log.warning("HNP %s decision unavailable", phase)
            self._end(run, "governance_unavailable", stage=stage, purchase=purchase)
        decision = result.decision
        if (decision is None) != (self._mode == "off"):
            # off fabricates no decision; enforce never proceeds without one.
            self._end(run, "governance_unavailable", stage=stage, purchase=purchase)
        if decision is None:
            return result
        if decision.outcome == "allow":
            if result.proof != "invalid":
                return result
            self._end(run, "mandate_proof_invalid", stage=stage, purchase=purchase)
        outcome = self._denial(run, result, expected)
        self._end(
            run, outcome, stage=stage, purchase=purchase, checkout=checkout, hooks=result.hooks
        )

    def _denial(self, run: _Run, result: HnpGateDecision, expected: Authority) -> OutcomeCode:
        """The order of §8.1: what the store already committed, an invalid proof, then the rules."""
        ex = self._ledger.execution(run.task_id)
        cancellation = None if ex is None else _cancellation(ex, expected, self._clock())
        if cancellation is not None:
            return cancellation
        if result.proof == "invalid":
            return "mandate_proof_invalid"
        reasons = set(result.decision.reason_codes if result.decision else ())
        if "GOV_AUTHORIZATION_PROFILE_UNRESOLVED" in reasons:
            return "authorization_profile_unresolved"
        if "GOV_STATE_CONFLICT" in reasons:
            return "state_conflict"
        if any(reason.startswith("GOV_") for reason in reasons) or not reasons:
            return "governance_unavailable"
        return "terms_out_of_scope" if reasons <= _ESCALATING_REASONS else "policy_denied"

    # Step 8

    def _close(
        self,
        run: _Run,
        checkout: CheckoutSession,
        reserved: Execution,
        operation_id: str,
        purchase: int,
    ) -> tuple[Execution, str]:
        reservation = reserved.reservation
        if reservation is None:
            raise LookupError("UoW-1 returned without the reservation")
        opened = {mandate.kind: mandate.token for mandate in self._ledger.mandates(run.grant_id)}
        try:
            checkout_jwt = self._checkout.signed_checkout(checkout)
            self._checkout.verify_signed_checkout(checkout_jwt, checkout)
            pair = self._closer.close(
                opened["open_checkout"],
                opened["open_payment"],
                checkout_jwt=checkout_jwt,
                checkout=checkout,
                closed_at=self._clock(),
            )
        except Exception:
            _log.warning("HNP close unavailable")
            self._end(run, "checkout_failed", stage="uow2", purchase=purchase)
        record = ClosedMandates(
            task_id=run.task_id,
            lease_id=run.lease_id,
            reservation_id=reservation.reservation_id,
            checkout_hash=checkout_hash(checkout_jwt),
            closed_checkout=_ledger_mandate(pair.checkout),
            closed_payment=_ledger_mandate(pair.payment),
        )
        closed = self._applied(
            run, self._ledger.record_closed_mandates(record), purchase, stage="uow2"
        )
        digest = payment_request_digest(
            operation_id=operation_id, checkout=checkout, checkout_jwt=checkout_jwt, closed=pair
        )
        return closed, digest

    # Ending a run

    def _applied(
        self, run: _Run, step: Step, purchase: int, *, stage: KillSwitchStage = "uow1"
    ) -> Execution:
        if step.status in ("applied", "reused") and step.execution is not None:
            return step.execution
        if step.status == "blocked":
            self._end(run, step.outcome or "state_conflict", stage=stage, purchase=purchase)
        raise _Stopped(step)  # another lease owns the task: it writes the terminal, not this run

    def _end(
        self,
        run: _Run,
        outcome: OutcomeCode,
        *,
        stage: KillSwitchStage = "uow1",
        purchase: int | None = None,
        checkout: CheckoutSession | None = None,
        hooks: Sequence[TransactionHook] = (),
    ) -> NoReturn:
        """The terminal of §8.1 by CAS, with the purchase closed in the same unit of work when
        `start` prepared it; an escalation also cancels its own checkout (§8.2)."""
        state = _terminal(outcome, escalates=self._mode == "enforce")
        if purchase is not None:
            hooks = (*hooks, self._closing(run, purchase))
        step = self._finish(run, state, outcome, stage, hooks)
        if state == "escalated" and checkout is not None and step.status == "closed":
            self._cancel(run.run_id, checkout)
        raise _Stopped(step)

    def _finish(
        self,
        run: _Run,
        state: Terminal,
        outcome: OutcomeCode,
        stage: KillSwitchStage,
        hooks: Sequence[TransactionHook],
    ) -> Step:
        kill_stage = stage if outcome == "kill_switch" else None
        request = TaskFinish(run.task_id, run.lease_id, state, outcome, kill_stage)
        step = self._ledger.finish_task(request, hooks=hooks)
        revoked = step.execution is not None and step.execution.grant.revoked_reason == "owner"
        if step.status == "blocked" and revoked and outcome != "revoked_by_owner":
            # §4.5: the owner's revocation committed first, so it names the terminal.
            owner = TaskFinish(run.task_id, run.lease_id, "canceled", "revoked_by_owner")
            return self._ledger.finish_task(owner, hooks=hooks)
        return step

    def _cancel(self, run_id: str, checkout: CheckoutSession) -> None:
        try:
            key = self._store.idempotency_key(run_id, "checkout.cancel.escalation")
            self._checkout.cancel_checkout(checkout.id, idempotency_key=key, reason_code="other")
        except Exception:
            # Harmless: without the closed mandates nobody can complete that checkout.
            _log.warning("HNP escalation could not cancel its checkout")

    # The purchase (§4.6): its transitions run inside the ledger's unit of work.

    def _stage(self, run_id: str) -> str | None:
        purchase = self._store.purchase(run_id)
        return None if purchase is None else purchase["stage"]

    def _closable(self, run_id: str) -> int | None:
        """The version of the purchase a task terminal closes (or keeps, §4.6); None without one.

        Also None when `start` left it `selected`: a D1 allow without its dispatch result has no
        HNP transition and waits for D1's recovery (§4.6). Its hook would raise and roll the whole
        terminal back, so the task ends without touching it.
        """
        stage = self._stage(run_id)
        if stage is None or stage == PurchaseStage.SELECTED:
            return None
        return self._store.purchase_state_version(run_id)

    def _reclaim_hooks(self, task_id: str) -> tuple[TransactionHook, ...]:
        """A claim that ends a task after `start` also closes its purchase."""
        ex = self._ledger.execution(task_id)
        run_id = None if ex is None else ex.task.run_id
        version = None if run_id is None else self._closable(run_id)
        if ex is None or run_id is None or version is None:
            return ()
        run = _Run(task_id, "", run_id, ex.task.grant_id, ex.task.owner_id)
        return (self._closing(run, version),)

    @staticmethod
    def _completing(run_id: str, version: int) -> TransactionHook:
        def complete(connection: sqlite3.Connection) -> None:
            SqliteAgentStore.transition_hnp_purchase(
                connection, run_id, expected_state_version=version, stage="completing"
            )

        return complete

    @staticmethod
    def _closing(run: _Run, version: int) -> TransactionHook:
        """`failed` with the task's code, or `canceled` for an escalation or a cancellation."""

        def close(connection: sqlite3.Connection) -> None:
            state, outcome = connection.execute(
                "SELECT state, outcome_code FROM hnp_tasks WHERE task_id = ?", (run.task_id,)
            ).fetchone()
            failed = state == "failed"
            SqliteAgentStore.transition_hnp_purchase(
                connection,
                run.run_id,
                expected_state_version=version,
                stage="failed" if failed else "canceled",
                failure_code=outcome if failed else None,
            )

        return close


def _terminal(outcome: OutcomeCode, *, escalates: bool) -> Terminal:
    if outcome in _CANCELLATIONS:
        return "canceled"
    return "escalated" if escalates and outcome in _ESCALATIONS else "failed"


def _cancellation(ex: Execution, expected: Authority, now: datetime) -> OutcomeCode | None:
    """§8.1, step 1: a revocation, kill switch or expiry the store already committed wins."""
    grant, control = ex.grant, ex.control
    if grant.revoked_reason == "owner":
        return "revoked_by_owner"
    if control.enabled or control.control_epoch != expected.control_epoch:
        return "kill_switch"
    if grant.state == "expired" or now >= utc_moment(grant.expires_at):
        return "grant_expired"
    return None


def _ledger_mandate(mandate: SignedAp2Mandate) -> SignedMandate:
    return SignedMandate(
        mandate.token, mandate.link_hash, canonical_bytes(mandate.public_jwk).decode()
    )
