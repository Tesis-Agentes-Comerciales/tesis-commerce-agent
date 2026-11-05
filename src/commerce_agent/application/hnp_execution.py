"""Autonomous execution of one Human Not Present task up to the dispatch commitment.

docs/design/hnp-governed.md §3.3 (steps 1 to 10), §4.5 and §8.1. Nobody is present: the executor
reads the grant from the ledger, never from a model or a buyer token, and the model at most picks
a variant of the grant's shortlist. Each external effect waits for the step before it:

1. claim, with a lease; 2. shortlist and selector (one that fails or does not answer ends
`selection_unavailable` before any effect), and in B2-HNP with `AGENT_HNP_REVIEWER=llm` the
intent reviewer (docs/design/hnp-intent-review.md), whose choice not confirmed escalates
`choice_not_confirmed` before any effect; 3-4. `PurchaseService.start` with the grant (D1-HNP in
enforce) up to `checkout_prepared`; 5. freshness `close`; 6. D2-HNP; 7. UoW-1
reserves; 8. the merchant's signed checkout, the Agent Key close and UoW-C; 9. D3-HNP; 10. UoW-2
commits the outbox row and moves the purchase to `completing`; 11-12. the `Dispatcher`
(`application/hnp_dispatch.py`) sends it and UoW-3 records the order.

B0-HNP (`off`) runs the same ledger steps with `NullHnpPolicyGate`, without D1, freshness or
escalation: what B2-HNP escalates ends `failed` with the same code (§8.1, §11).

A blocked unit of work already classified why (§4.5): the executor ends the task with that
outcome, by CAS on its lease. UoW-1 expects the claim's authority, and D3 and UoW-2 the one UoW-1
validated (UoW-C checks neither the kill switch nor the grant): never a re-read one (K10). A lost
lease ends nothing.

A re-claim resumes from the durable state (§4.5): with an outbox row it dispatches it; with the
closed mandates UoW-C kept it reuses them for D3 and UoW-2 while they are valid; any other
progress ends `state_conflict`. Nothing is created or closed twice.
"""

import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, NoReturn, Protocol, get_args

import jwt

from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.feed import ProductFeed
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.ap2 import checkout_hash
from commerce_agent.ap2_hnp import (
    CLOSED_TTL_SECONDS,
    MERCHANT_ID,
    MandatePair,
    closed_checkout_jwt,
    closed_issued_at,
)
from commerce_agent.ap2_hnp import SignedMandate as SignedAp2Mandate
from commerce_agent.application.hnp_intent_review import (
    REVIEW_UNAVAILABLE,
    HnpIntentReviewer,
    IntentReview,
    checked,
)
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
    MandateRecord,
    MerchantRefusal,
    OutcomeCode,
    ReservationRequest,
    SignedMandate,
    Step,
    TaskFinish,
    token_digest,
)
from commerce_agent.delegation.ports import TransactionHook
from commerce_agent.domain import PurchaseIntent, RankedCandidate, rank_candidates
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.evaluator import GovernanceError
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.hnp_models import ConsentScope, parse_consent_scope
from commerce_agent.governance.hnp_policy import HnpGrant
from commerce_agent.governance.hnp_ports import HnpGateDecision, HnpPolicyGate
from commerce_agent.governance.models import POLICY_REASONS
from commerce_agent.governance.runtime_dispatch import DEFINITIVE_CREATION_REFUSALS
from commerce_agent.llm.model_failures import ModelFailureKind, model_failure_kind
from commerce_agent.llm.model_requests import model_request_scope
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
        "choice_not_confirmed",
        "selection_unavailable",
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
# CT13: the merchant's typed refusals of the creation, which `checkout_failed` may name.
_REFUSALS: dict[str, MerchantRefusal] = {code: code for code in get_args(MerchantRefusal)}
# §8.1, «alerta al operador»: these terminals may mean altered mandates or a scope the operator
# misconfigured. Each is logged once at ERROR on `commerce_agent.hnp.alerts`, with ids only.
OPERATOR_ALERTS: frozenset[OutcomeCode] = frozenset(
    {
        "selection_not_eligible",
        "merchant_not_allowed",
        "mandate_proof_invalid",
        "verifier_invalid_mandate",
        "verifier_invalid_credential",
    }
)
_log = logging.getLogger(__name__)
_alerts = logging.getLogger("commerce_agent.hnp.alerts")


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


class CheckoutCanceler(Protocol):
    def cancel_checkout(
        self, checkout_id: str, *, idempotency_key: str, reason_code: str | None = None
    ) -> CheckoutSession: ...


class Dispatcher(Protocol):
    """Steps 11 and 12 (`HnpDispatcher`): resumes a claimed task's outbox row from its state."""

    def dispatch(self, execution: Execution) -> Step: ...


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
    `merchant_id` is the profile's `MERCHANT_ID`, the payee of both closed mandates, while the PSP
    client sends its own: the same id by default, which no setting or caller overrides (the runtime
    test pins the request it sends). A PSP of another merchant refuses the allowance anyway
    (`merchant_mismatch`, no receipt), so a divergence ends in doubt, never in a payment.
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


@dataclass(frozen=True)
class ClosedRequest:
    """What UoW-C fixed for a reservation: the merchant's signed checkout and both closed chains."""

    checkout: CheckoutSession
    checkout_jwt: str = field(repr=False)
    closed: MandatePair = field(repr=False)


def closed_request(
    ledger: SqliteDelegationLedger, grant_id: str, reservation_id: str
) -> ClosedRequest | None:
    """The closed pair UoW-C stored for the reservation and the checkout its Checkout JWT carries;
    None without both or with a checkout that cannot be read back."""
    kept = {m.kind: m for m in ledger.mandates(grant_id) if m.reservation_id == reservation_id}
    checkout_mandate, payment_mandate = kept.get("closed_checkout"), kept.get("closed_payment")
    if checkout_mandate is None or payment_mandate is None:
        return None
    checkout_jwt = closed_checkout_jwt(checkout_mandate.token)
    try:
        # Step 8 verified its signature against the merchant's JWKS before closing; the outbox
        # digest or D3's `closed_proof` checks that it is still that token.
        claims = jwt.decode(checkout_jwt, options={"verify_signature": False})
        checkout = CheckoutSession.model_validate(claims["checkout"])
        closed = MandatePair(_ap2_mandate(checkout_mandate), _ap2_mandate(payment_mandate))
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        return None
    return ClosedRequest(checkout, checkout_jwt, closed)


def purchase_terminal(task_id: str, run_id: str, version: int) -> TransactionHook:
    """The purchase side of a task terminal (§4.6), from the task the unit of work just closed:
    `failed` with its code, `uncertain`, or `canceled` for an escalation or a cancellation."""

    def close(connection: sqlite3.Connection) -> None:
        state, outcome = connection.execute(
            "SELECT state, outcome_code FROM hnp_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        stage = state if state in ("failed", "uncertain") else "canceled"
        SqliteAgentStore.transition_hnp_purchase(
            connection,
            run_id,
            expected_state_version=version,
            stage=stage,
            failure_code=outcome if stage == "failed" else None,
        )

    return close


def cancel_escalated_checkout(
    store: SqliteAgentStore, merchant: CheckoutCanceler, run_id: str, checkout_id: str
) -> None:
    """§8.2: an escalation cancels its own checkout, once per run, at best effort."""
    try:
        key = store.idempotency_key(run_id, "checkout.cancel.escalation")
        merchant.cancel_checkout(checkout_id, idempotency_key=key, reason_code="other")
    except Exception:
        # Harmless: without the closed mandates, or after the PSP's rejection, nobody completes it.
        _log.warning("HNP escalation could not cancel its checkout")


def alert_operator(step: Step) -> None:
    """The operator's alert of §8.1, for a terminal that needs a person to look at it."""
    ex = step.execution
    if step.status == "closed" and ex is not None and ex.task.outcome_code in OPERATOR_ALERTS:
        _alerts.error(
            "HNP task %s ended %s(%s): the operator must review it",
            ex.task.task_id,
            ex.task.state,
            ex.task.outcome_code,
        )


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
        dispatcher: Dispatcher | None = None,
        reviewer: HnpIntentReviewer | None = None,
    ) -> None:
        # B0-HNP runs no PDP and no freshness; B2-HNP never runs without them (spec §11).
        if (mode == "off") != isinstance(gate, NullHnpPolicyGate) or (mode == "off") != (
            freshness is None and d1_denials is None
        ):
            raise ValueError("HNP governance must be all off (B0-HNP) or all enforced (B2-HNP)")
        if reviewer is not None and mode != "enforce":
            # B0-HNP has no escalation: the reviewer only runs in enforce (hnp-intent-review §2).
            raise ValueError("The HNP intent reviewer runs only in enforce (B2-HNP)")
        self._mode: Mode = mode
        self._ledger, self._store, self._purchases = ledger, store, purchases
        self._checkout, self._closer, self._gate, self._selector = checkout, closer, gate, selector
        self._feed, self._clock, self._new_id = feed, clock, id_factory
        self._freshness, self._d1_denials = freshness, d1_denials
        self._dispatcher, self._reviewer = dispatcher, reviewer

    def run(self, task_id: str) -> Step:
        """Steps 1 to 12 of one task in this thread: `claim`, then `resume` if it got the lease."""
        claimed = self.claim(task_id)
        if claimed.status != "applied" or claimed.execution is None:
            return claimed
        return self.resume(claimed.execution)

    def claim(self, task_id: str) -> Step:
        """Step 1: `applied` with a new lease, `closed` with the terminal the claim imposed, or
        `not_found` / `not_runnable` (§4.5). The service route answers with it before resuming."""
        return self._ledger.claim(task_id, hooks=self._reclaim_hooks(task_id))

    def resume(self, ex: Execution) -> Step:
        """Steps 2 to 12 of a task this run claimed, from its durable state. The result is the step
        that ended the run: `closed` with a terminal (`purchased` among them), `lease_lost`,
        `applied` once UoW-2 committed when no dispatcher is plugged in, or the dispatch's own."""
        step = Step("applied", ex) if ex.outbox is not None else self._prepare(ex)
        committed = step.execution if step.status == "applied" else None
        if self._dispatcher is not None and committed is not None and committed.outbox is not None:
            step = self._dispatcher.dispatch(committed)
        alert_operator(step)
        return step

    def _prepare(self, ex: Execution) -> Step:
        """Steps 1 to 10, or their recovery after a crash: the UoW-2 step, or the terminal."""
        run = _Run.of(ex)
        try:
            try:
                if ex.reservation is not None or self._store.purchase(run.run_id) is not None:
                    return self._recover(run, ex)
                return self._execute(run, ex)
            except (GovernanceError, LookupError) as error:
                # §8.1: evidence that cannot be written fails closed (`GOV_EVIDENCE_UNAVAILABLE`),
                # and a durable row that went missing under the run is a conflict.
                governance = isinstance(error, GovernanceError)
                _log.warning("HNP execution stopped: %s", type(error).__name__)
                outcome = "governance_unavailable" if governance else "state_conflict"
                self._end(run, outcome, purchase=self._closable(run.run_id))
        except _Stopped as stopped:
            return stopped.step

    def _recover(self, run: _Run, ex: Execution) -> Step:
        """A re-claim after progress, without an outbox row (§4.5, UoW-C). Only a reservation whose
        closed mandates UoW-C kept goes on, reusing them while they are valid; nothing is created
        or closed again."""
        version, reservation = self._closable(run.run_id), ex.reservation
        request = None
        prepared = self._stage(run.run_id) == PurchaseStage.CHECKOUT_PREPARED
        if reservation is not None and reservation.state == "reserved" and prepared:
            request = closed_request(self._ledger, run.grant_id, reservation.reservation_id)
        if (
            reservation is None
            or request is None
            or version is None
            or (request.checkout.id, request.checkout.total_minor)
            != (reservation.checkout_id, reservation.amount_minor)
        ):
            self._end(run, self._unrecovered(run.run_id) or "state_conflict", purchase=version)
        try:
            closed_at = closed_issued_at(request.closed.payment.token)
        except ValueError:
            closed_at = 0
        if self._clock().timestamp() >= closed_at + CLOSED_TTL_SECONDS:
            self._end(run, "close_expired_before_dispatch", stage="uow2", purchase=version)
        digest = payment_request_digest(
            operation_id=reservation.operation_id,
            checkout=request.checkout,
            checkout_jwt=request.checkout_jwt,
            closed=request.closed,
        )
        ids = (reservation.reservation_id, reservation.operation_id)
        # The re-claim's authority stands for the claim's: the one UoW-1 validated is gone.
        return self._commit(
            run, request.checkout, ids, ex.authority, reservation.state_version, digest, version
        )

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
        shortlist = self._shortlist(run, feed, intent, consent)
        variant_id = self._select(run, shortlist, intent, consent)
        self._hold(run)
        if self._reviewer is not None and _listed(shortlist, variant_id):
            self._review(run, self._reviewer, shortlist, intent, consent, variant_id)
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
        reserved = self._applied(
            run, self._ledger.reserve(reserve, hooks=d2.hooks), purchase, lost=d2.conflict_hooks
        )
        if reserved.reservation is None:
            raise LookupError("UoW-1 returned without the reservation")
        digest = self._close(run, checkout, reserved, operation_id, purchase)
        # K10: UoW-C checks neither the switch nor the grant, so D3 and UoW-2 expect the authority
        # and the reservation UoW-1 validated, never what UoW-C read back.
        version = reserved.reservation.state_version
        return self._commit(run, checkout, ids, reserved.authority, version, digest, purchase)

    def _commit(
        self,
        run: _Run,
        checkout: CheckoutSession,
        ids: tuple[str, str],
        expected: Authority,
        reservation_version: int,
        digest: str,
        purchase: int,
    ) -> Step:
        """Steps 9 and 10: D3-HNP and UoW-2, which commits the outbox row."""
        d3 = self._decide(run, "D3", checkout, ids, expected, purchase)
        commit = DispatchCommit(
            task_id=run.task_id,
            lease_id=run.lease_id,
            expected=expected,
            reservation_id=ids[0],
            reservation_version=reservation_version,
            # The id the D3 allow names as its transition (§4.3); B0-HNP has none.
            outbox_id=d3.transition_ref or self._new_id("ob"),
            checkout_expires_at=checkout.expires_at,
            payment_request_digest=digest,
            d3_evidence_id=d3.evidence_id,
        )
        hooks = (*d3.hooks, self._completing(run.run_id, purchase))
        step = self._ledger.commit_dispatch(commit, hooks=hooks)
        self._applied(run, step, purchase, stage="uow2", lost=d3.conflict_hooks)
        return step

    def _hold(self, run: _Run) -> None:
        """Before the first effect (`start`): the selector may be a model call longer than the
        lease, so the run renews the lease it still holds, or stops if another run re-claimed the
        task meanwhile (§4.5). The lease's CAS lets only one of them advance."""
        step = self._ledger.renew_lease(run.task_id, run.lease_id)
        if step.status != "reused":
            raise _Stopped(step)

    # Steps 2 to 5

    def _shortlist(
        self, run: _Run, feed: ProductFeed, intent: PurchaseIntent, consent: ConsentScope
    ) -> tuple[RankedCandidate, ...]:
        granted = set(consent.scope.variant_ids)
        shortlist = tuple(
            item
            for item in rank_candidates(feed, intent).ranked
            if item.candidate.variant_id in granted
        )
        if not shortlist:
            self._end(run, "no_candidate_in_scope")
        return shortlist

    def _select(
        self,
        run: _Run,
        shortlist: tuple[RankedCandidate, ...],
        intent: PurchaseIntent,
        consent: ConsentScope,
    ) -> str:
        failure_kind: ModelFailureKind | None = None
        try:
            # A model selector records its request under the task's run (run-traceability §4.4).
            with model_request_scope(run_id=run.run_id):
                chosen = self._selector.choose(shortlist, intent, consent.preference_text)
        except Exception as error:
            # The classes only, never the message: it may carry catalog or request text. The
            # request itself, with its `error_kind`, is in `model_requests` under the run.
            _log.warning("HNP selector unavailable for task %s: %s", run.task_id, _kind(error))
            chosen, failure_kind = None, model_failure_kind(error)
        if chosen is None:
            # No choice is not a choice outside the grant (§8.1): it fails closed like the intent
            # reviewer, before any effect, and escalates in B2-HNP without the operator's alert.
            # `task_finished` keeps why the model failed (p0-model-failures.md §3.5).
            self._end(run, "selection_unavailable", failure_kind=failure_kind)
        if not _listed(shortlist, chosen) and self._mode == "off":
            # K12: B0-HNP has no D1, so its own guard stops a choice outside the shortlist. B2-HNP
            # has no guard here: D1-HNP denies that choice with POL-01, with its evidence, before
            # any POST, and the terminal is the same `selection_not_eligible` (§3.3).
            self._end(run, "selection_not_eligible")
        return chosen

    def _review(
        self,
        run: _Run,
        reviewer: HnpIntentReviewer,
        shortlist: tuple[RankedCandidate, ...],
        intent: PurchaseIntent,
        consent: ConsentScope,
        variant_id: str,
    ) -> None:
        """The intent reviewer (hnp-intent-review.md §6), before D1 and any POST. The purchase
        row exists first (`start` creates the same one, idempotently) so that the trace of a choice
        not confirmed shows the review. It fails closed, and only a confirmation goes on."""
        self._store.create_purchase(run.run_id, HNP_REQUEST, run.owner_id, grant_id=run.grant_id)
        failure_kind: ModelFailureKind | None = None
        try:
            # Like the selector's, its model request goes under the task's run (§4.4).
            with model_request_scope(run_id=run.run_id):
                result: object = reviewer.review(
                    shortlist=shortlist,
                    intent=intent,
                    preference_text=consent.preference_text,
                    chosen_variant_id=variant_id,
                )
        except Exception as error:
            _log.warning("HNP intent review unavailable")
            result, failure_kind = REVIEW_UNAVAILABLE, model_failure_kind(error)
        review: IntentReview = checked(result)
        body = {
            "decision": review.decision,
            "reason": review.reason,
            "prompt_id": reviewer.prompt_id,
            "model": reviewer.model,
        }
        if failure_kind is not None:
            # Why the model could not review (p0-model-failures.md §3.5); only then, so every
            # other review keeps its body.
            body["failure_kind"] = failure_kind
        step = self._ledger.record_choice_review(run.task_id, run.lease_id, body)
        if step.status != "reused":
            raise _Stopped(step)  # another lease owns the task: it writes the terminal
        if review.decision != "confirmed":
            self._end(run, "choice_not_confirmed", purchase=self._closable(run.run_id))

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
            # B2-HNP: D1-HNP raises a refusal's own code only after recording that its one POST
            # created nothing (`definitive_failure`); any other failure there is uncertain.
            outcome, refused = self._start_outcome(run.run_id, error.code), error.code
        except ACPClientError as error:
            # B0-HNP: the merchant's answer to the POST of `start` or to a later call of it.
            outcome, refused = "checkout_failed", _creation_refusal(error)
        except Exception:
            outcome, refused = "checkout_failed", None
        else:
            return prepared.checkout
        # §4.6: the terminal fails or cancels a `received` purchase and keeps what `start` left.
        outcome = self._unrecovered(run.run_id) or outcome
        refusal = self._refusal(run.run_id, outcome, refused)
        self._end(run, outcome, purchase=self._closable(run.run_id), merchant_refusal=refusal)

    def _refusal(
        self, run_id: str, outcome: OutcomeCode, code: str | None
    ) -> MerchantRefusal | None:
        """CT13: the merchant's refusal a `checkout_failed` names, only when it is certain: a
        typed refusal of the creation, with the purchase failed and no checkout stored. The same
        code on the details update comes with the checkout already created, so it names none."""
        refusal = _REFUSALS.get(code or "") if outcome == "checkout_failed" else None
        purchase = self._store.purchase(run_id) if refusal is not None else None
        if purchase is None or purchase["checkout_id"] is not None:
            return None
        return refusal if purchase["stage"] == PurchaseStage.FAILED else None

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
    ) -> str:
        """Step 8 and UoW-C; the digest of the PSP request the dispatch will send."""
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
        self._applied(run, self._ledger.record_closed_mandates(record), purchase, stage="uow2")
        return payment_request_digest(
            operation_id=operation_id, checkout=checkout, checkout_jwt=checkout_jwt, closed=pair
        )

    # Ending a run

    def _applied(
        self,
        run: _Run,
        step: Step,
        purchase: int,
        *,
        stage: KillSwitchStage = "uow1",
        lost: Sequence[TransactionHook] = (),
    ) -> Execution:
        """`lost`: the allow's `conflict_hooks`, kept as `blocked` with the terminal (§4.5)."""
        if step.status in ("applied", "reused") and step.execution is not None:
            return step.execution
        if step.status == "blocked":
            outcome = step.outcome or "state_conflict"
            self._end(run, outcome, stage=stage, purchase=purchase, hooks=lost)
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
        failure_kind: ModelFailureKind | None = None,
        merchant_refusal: MerchantRefusal | None = None,
    ) -> NoReturn:
        """The terminal of §8.1 by CAS, with the purchase closed in the same unit of work when
        `start` prepared it; an escalation also cancels its own checkout (§8.2)."""
        state = _terminal(outcome, escalates=self._mode == "enforce")
        closing = (
            () if purchase is None else (purchase_terminal(run.task_id, run.run_id, purchase),)
        )
        try:
            step = self._finish(
                run, state, outcome, stage, (*hooks, *closing), failure_kind, merchant_refusal
            )
        except GovernanceError:
            if not hooks:
                raise
            # §8.1: the decision's evidence could not be written (`GOV_EVIDENCE_UNAVAILABLE`), so
            # the terminal goes without it. A cancellation keeps its cause; anything else fails.
            _log.warning("HNP terminal evidence unavailable")
            if outcome not in _CANCELLATIONS:
                state, outcome = "failed", "governance_unavailable"
            step = self._finish(run, state, outcome, stage, closing)
        if state == "escalated" and checkout is not None and step.status == "closed":
            cancel_escalated_checkout(self._store, self._checkout, run.run_id, checkout.id)
        raise _Stopped(step)

    def _finish(
        self,
        run: _Run,
        state: Terminal,
        outcome: OutcomeCode,
        stage: KillSwitchStage,
        hooks: Sequence[TransactionHook],
        failure_kind: ModelFailureKind | None = None,
        merchant_refusal: MerchantRefusal | None = None,
    ) -> Step:
        kill_stage = stage if outcome == "kill_switch" else None
        request = TaskFinish(
            run.task_id, run.lease_id, state, outcome, kill_stage, failure_kind, merchant_refusal
        )
        step = self._ledger.finish_task(request, hooks=hooks)
        revoked = step.execution is not None and step.execution.grant.revoked_reason == "owner"
        if step.status == "blocked" and revoked and outcome != "revoked_by_owner":
            # §4.5: the owner's revocation committed first, so it names the terminal.
            owner = TaskFinish(run.task_id, run.lease_id, "canceled", "revoked_by_owner")
            return self._ledger.finish_task(owner, hooks=hooks)
        return step

    # The purchase (§4.6): its transitions run inside the ledger's unit of work.

    def _stage(self, run_id: str) -> str | None:
        purchase = self._store.purchase(run_id)
        return None if purchase is None else purchase["stage"]

    def _unrecovered(self, run_id: str) -> OutcomeCode | None:
        """K15: a purchase `start` left `selected` (a D1 allow without its dispatch result) waits
        for D1's recovery, so its task fails `governance_unavailable` whatever stopped `start`."""
        return "governance_unavailable" if self._stage(run_id) == PurchaseStage.SELECTED else None

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
        return (purchase_terminal(task_id, run_id, version),)

    @staticmethod
    def _completing(run_id: str, version: int) -> TransactionHook:
        def complete(connection: sqlite3.Connection) -> None:
            SqliteAgentStore.transition_hnp_purchase(
                connection, run_id, expected_state_version=version, stage="completing"
            )

        return complete


def _creation_refusal(error: ACPClientError) -> str | None:
    """The merchant's definitive refusal of a creation, by exact status and code, as the governed
    dispatch reads it (`runtime_dispatch`); anything else proves nothing."""
    if error.status_code is None:
        return None
    return DEFINITIVE_CREATION_REFUSALS.get((error.status_code, error.code))


def _listed(shortlist: tuple[RankedCandidate, ...], variant_id: str) -> bool:
    return variant_id in {item.candidate.variant_id for item in shortlist}


def _kind(error: BaseException) -> str:
    """The class of an error and of its direct cause, e.g. `IntentInterpretationError <-
    APIConnectionError`: the model client wraps the SDK's error (`raise ... from error`)."""
    cause = error.__cause__
    kind = type(error).__name__
    return kind if cause is None else f"{kind} <- {type(cause).__name__}"


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


def _ap2_mandate(record: MandateRecord) -> SignedAp2Mandate:
    return SignedAp2Mandate(record.token, record.link_hash, json.loads(record.public_jwk_json))


def _ledger_mandate(mandate: SignedAp2Mandate) -> SignedMandate:
    return SignedMandate(
        mandate.token, mandate.link_hash, canonical_bytes(mandate.public_jwk).decode()
    )
