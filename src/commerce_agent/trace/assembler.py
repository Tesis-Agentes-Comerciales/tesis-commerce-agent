"""Build a `PurchaseTrace` from stored records, one function per step kind.

The trace holds only what happened: the UI deduces the disabled policy and the controls a purchase
never reached. A step carries `duration_ms` only when both of its ends were measured. A Human Not
Present purchase (docs/design/hnp-governed.md §10) adds its delegation ledger: the consent replaces
the request, and there is no approval or signature by the buyer.
"""

import logging
import sqlite3
from collections import Counter
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import TypeAdapter, ValidationError

from commerce_agent.config import Settings
from commerce_agent.governance.evidence import DecisionRecord, EvidenceError
from commerce_agent.governance.models import APPLICABLE_RULES, PolicyBinding
from commerce_agent.persistence.hp_evidence_sqlite import HpEvidence

from .models import (
    ApprovalStep,
    AuthorizationDecisionStep,
    CanceledStep,
    DispatchDecisionStep,
    FailedStep,
    Money,
    OrderConfirmedStep,
    PolicyDecisionStep,
    PolicySummary,
    PurchaseHistory,
    PurchaseStatus,
    PurchaseSummary,
    PurchaseTrace,
    StepState,
    Text,
    TraceActor,
    TraceStep,
)
from .sources import (
    Delegation,
    GraphCheckpoints,
    PurchaseRecords,
    merchant_origins,
    read_records,
    transient,
)

logger = logging.getLogger(__name__)
STEP: TypeAdapter[TraceStep] = TypeAdapter(TraceStep)
EFFECTS = {"observed_shadow": "observed", "blocked": "blocked", "transition_committed": "continued"}
STAGE_STATUS: dict[str, PurchaseStatus] = {
    "completed": "completed",
    "awaiting_confirmation": "awaiting_approval",
    "canceled": "canceled",
    "failed": "failed",
    "uncertain": "uncertain",
}
# Stages reached only through `claim_completion`, which runs after the payment delegation returns.
DELEGATED_STAGES = {"completing", "uncertain", "completed", "failed"}
FINISHED = {"completed", "blocked", "canceled", "failed"}
CANCELED_BY: dict[str, TraceActor] = {"user": "user", "budget": "agent"}
# What a freshness check gates: it always runs before these are recorded.
GATED = {
    "approval",
    "authorization_decision",
    "authorization_signed",
    "payment_authorized",
    "order_confirmed",
}
# HNP (spec §8.1, §9.1): the task outcomes a cancellation ends with, and how ledger events read.
CANCELLATIONS = {"revoked_by_owner", "kill_switch", "grant_expired"}
REVOKED_BY: dict[str, TraceActor] = {"owner": "user", "operator": "operator"}
RESERVATION_OUTCOMES = {
    "reservation_reserved": "reserved",
    "reservation_released": "released",
    "consumed": "consumed",
    "reservation_uncertain": "uncertain",
}
# Fields offer_freshness.compare_offer compares; a field it adds later stays out of the trace.
AMOUNT_FIELDS = {"subtotal_minor", "shipping_minor", "total_minor", "delivery_days", "stock"}


class TraceUnavailable(Exception):
    """The trace cannot be built without inventing data: evidence that does not verify, or a
    stored value outside the contract. The trace answers 503; the history marks the row.

    `transient` is a lock another connection held (SQLite busy or locked): reading again can work.
    """

    def __init__(self, run_id: str, *, transient: bool = False) -> None:
        super().__init__(run_id)
        self.transient = transient


@dataclass(frozen=True)
class TraceContext:
    """What the trace takes from the configuration and the runtime, not from the purchase."""

    # Merchant pseudonym → origin, so POL-02 shows origins, not refs.
    origins: dict[str, str] = field(default_factory=dict)
    # Origin → the display name its feed declares (`seller.name`).
    merchant_names: dict[str, str] = field(default_factory=dict)
    # AGENT_SANDBOX_CLOCK_AT, as the trace writes times: approvals take this fixed time.
    sandbox_clock_at: str | None = None


def utc(value: str) -> str:
    """ISO-8601 in UTC ending in `Z`. A merchant time without offset is read as UTC."""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def elapsed_ms(start: str | None, end: str | None) -> int | None:
    if start is None or end is None:
        return None
    elapsed = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    return elapsed // timedelta(milliseconds=1) if elapsed >= timedelta(0) else None


def step(
    kind: str,
    at: str | None,
    actor: TraceActor,
    *,
    step_id: str | None = None,
    state: StepState = "done",
    duration_ms: int | None = None,
    **fields: Any,
) -> TraceStep:
    """Every step goes through the contract, so a wrong field fails here and not in the UI."""
    base = {"id": step_id or kind, "kind": kind, "at": at, "duration_ms": duration_ms}
    return STEP.validate_python({**base, "actor": actor, "state": state, **fields})


def request_step(records: PurchaseRecords) -> TraceStep:
    # purchases.created_at is only written after the selection, when the checkout starts.
    started = records.turn.started_at if records.turn else records.purchase["created_at"]
    return step("request", utc(started), "user")


def selection_step(records: PurchaseRecords, requested_at: str | None) -> TraceStep | None:
    turn, terms = records.turn, records.terms
    if turn and turn.candidate and turn.selected_at:
        at, chosen = utc(turn.selected_at), turn.candidate
        product, total = chosen["variant_title"], chosen["observed_unit_amount_minor"]
        currency = chosen["currency"]
    elif terms and terms["lines"]:
        at, line = None, terms["lines"][0]
        product, total = line["name"], line["unit_amount_minor"] * line["quantity"]
        currency = terms["currency"]
    else:
        return None
    return step(
        "selection",
        at,
        "agent",
        duration_ms=elapsed_ms(requested_at, at),
        product=product,
        total_minor=total,
        currency=currency.upper(),
    )


RuleValue = str | dict[str, Any] | None


def cop(minor: int | None) -> dict[str, Any] | None:
    """D1 compares COP minor units only (IntentFacts.currency)."""
    return None if minor is None else {"minor": minor, "currency": "COP"}


def policy_rules(
    record: DecisionRecord, records: PurchaseRecords, origins: dict[str, str]
) -> tuple[dict[str, Any], ...]:
    results = {rule.policy_id: rule.result for rule in record.decision.rule_results}
    values: dict[str, tuple[RuleValue, RuleValue]] = {}
    if not results:  # A runtime guard stopped the purchase before any rule was evaluated.
        rules = records.policy.policy_ids if records.policy else ("POL-01", "POL-02")
        results = dict.fromkeys(rules, "not_evaluated")
    else:
        data = record.policy_input()
        allowed = records.policy.allowed_merchant_refs if records.policy else ()
        values = {
            "POL-01": (data.selected_variant_id, ", ".join(data.eligible_variant_ids)),
            "POL-02": (
                origins.get(data.merchant_ref, data.merchant_ref),
                ", ".join(sorted(origins.get(ref, ref) for ref in allowed)),
            ),
            # The catalog price against the total budget, never the merchant's total.
            "POL-11": (cop(data.selected_unit_minor), cop(data.intent.max_total_minor)),
        }
    return tuple(
        {
            "id": rule,
            "result": result,
            "observed": values.get(rule, (None, None))[0] or None,
            "expected": values.get(rule, (None, None))[1] or None,
        }
        for rule, result in results.items()
    )


def revision(binding: PolicyBinding) -> str:
    return f"{binding.policy_set_id} · revisión {binding.version}"


def authorization_rules(record: DecisionRecord) -> tuple[dict[str, Any], ...]:
    """POL-03..06 check several facts each: a rule shows the values that explain its result.

    D2-HNP adds POL-07..09, which compare the grant and its single use: they show no values.
    """
    mode = record.decision.purchase_mode
    results = {rule.policy_id: rule.result for rule in record.decision.rule_results}
    values: dict[str, tuple[RuleValue, RuleValue]] = {}
    if not results:  # A runtime guard stopped the approval before any rule was evaluated.
        results = dict.fromkeys(APPLICABLE_RULES[("D2", mode)], "not_evaluated")
    else:
        data = record.hnp_authorization_input() if mode == "HNP" else record.authorization_input()
        terms, limit = data.terms, data.intent.max_total_minor
        approved, reviewed = terms.variant_id, data.d1.selected_variant_id
        then, now = revision(data.d1.policy_binding), revision(data.expected_policy)
        # POL-03 checks the owner's identity: it never shows values. The total against the budget
        # explains POL-05 when it passed or when the total exceeds it, not a constraint mismatch.
        over = limit is not None and terms.total_minor > limit
        values = {
            "POL-04": (approved, reviewed) if approved != reviewed else (None, None),
            "POL-05": (cop(terms.total_minor), cop(limit))
            if over or results.get("POL-05") == "pass"
            else (None, None),
            "POL-06": (then, now) if then != now else (None, None),
        }
    return tuple(
        {
            "id": rule,
            "result": result,
            "observed": values.get(rule, (None, None))[0],
            "expected": values.get(rule, (None, None))[1],
        }
        for rule, result in results.items()
    )


def policy_step(
    record: DecisionRecord, index: int, records: PurchaseRecords, origins: dict[str, str]
) -> TraceStep:
    return decision_step("policy_decision", record, index, policy_rules(record, records, origins))


def authorization_decision_step(record: DecisionRecord, index: int) -> TraceStep:
    """D2: the policy checked the approval before anything was signed (actor: the policy)."""
    return decision_step("authorization_decision", record, index, authorization_rules(record))


def dispatch_decision_step(record: DecisionRecord, index: int) -> TraceStep:
    """D3-HNP (POL-09, POL-10): the authority right before committing the dispatch."""
    results = {rule.policy_id: rule.result for rule in record.decision.rule_results}
    if not results:  # A runtime guard.
        results = dict.fromkeys(APPLICABLE_RULES[("D3", "HNP")], "not_evaluated")
    rules = tuple(
        {"id": rule, "result": result, "observed": None, "expected": None}
        for rule, result in results.items()
    )
    return decision_step("dispatch_decision", record, index, rules)


def decision_step(
    kind: str, record: DecisionRecord, index: int, rules: tuple[dict[str, Any], ...]
) -> TraceStep:
    decision, effect = record.decision, EFFECTS[record.application.disposition]
    binding = decision.policy_binding
    return step(
        kind,
        utc(decision.decided_at),
        "policy",
        step_id=kind if index == 0 else f"{kind}:{index + 1}",
        state="blocked" if effect == "blocked" else "done",
        duration_ms=decision.latency_ms,
        decision={
            "mode": decision.governance_mode,
            "outcome": decision.outcome,
            "effect": effect,
            "policy": {"set_id": binding.policy_set_id, "revision": binding.version},
            "rules": rules,
            "reason_codes": decision.reason_codes,
            "latency_ms": decision.latency_ms,
            "decision_id": decision.decision_id,
            "evidence_id": record.evidence_id,
        },
    )


def checkout_step(records: PurchaseRecords) -> TraceStep | None:
    checkout_id = records.purchase["checkout_id"]
    if checkout_id is None:
        return None
    # Written right before the merchant call; the D1 enforce path prepares an envelope instead.
    started = records.operations.get("checkout.create") or records.checkout_prepared_at
    return step("checkout_created", started and utc(started), "merchant", checkout_id=checkout_id)


def stale_terms(records: PurchaseRecords) -> bool:
    """The last freshness check blocked the terms: they can no longer be approved."""
    checks = [item for item in records.hp_evidence if item.record_kind == "freshness_check"]
    return bool(checks) and checks[-1].body.get("decision") == "block"


def authorization_refused(records: PurchaseRecords) -> bool:
    """The last D2 decision stopped the approval: none was recorded and only canceling remains."""
    return (
        bool(records.authorizations)
        and records.authorizations[-1].application.disposition == "blocked"
        and records.approval is None
    )


def approval_step(records: PurchaseRecords) -> TraceStep | None:
    terms, approval = records.terms, records.approval
    if terms is None or records.terms_shown_at is None:
        return None
    shown = utc(records.terms_shown_at)
    approved = utc(approval["approved_at"]) if approval else None
    waiting = (
        records.purchase["stage"] == "awaiting_confirmation"
        and not stale_terms(records)
        and not authorization_refused(records)
    )
    return step(
        "approval",
        approved or shown,
        "user",
        state="done" if approved else "waiting" if waiting else "skipped",
        duration_ms=elapsed_ms(shown, approved),
        fingerprint=terms["fingerprint"],
        expires_at=utc(terms["expires_at"]),
        approved_at=approved,
    )


def authorization_step(records: PurchaseRecords) -> TraceStep | None:
    approval = records.approval
    mandates = tuple(kind for kind in ("checkout", "payment") if approval and approval[kind])
    if approval is None or not mandates:
        return None
    return step("authorization_signed", utc(approval["approved_at"]), "user", mandates=mandates)


def delegated(records: PurchaseRecords, started: str) -> bool:
    """The payment provider returned a token.

    The completion claim happens only after it. A merchant 4xx to /complete releases the claim back
    to `approved`, so that stage needs its own proof: the merchant key (`checkout.complete`), which
    completion creates only once the provider returned. Whatever else later writes the purchase row
    (`updated_at`, `failure_code`) proves nothing. Rows from before this rule created that key
    before delegating: at `approved` they show no payment step rather than a false one.
    """
    if records.purchase["stage"] in DELEGATED_STAGES:
        return True
    merchant_key = records.operations.get("checkout.complete")
    return merchant_key is not None and datetime.fromisoformat(
        merchant_key
    ) > datetime.fromisoformat(started)


def payment_step(records: PurchaseRecords) -> TraceStep | None:
    started, terms = records.operations.get("payment.delegate"), records.terms
    if started is None or terms is None or not delegated(records, started):
        return None
    return step(
        "payment_authorized",
        utc(started),
        "payment",
        max_minor=terms["total_minor"],
        currency=terms["currency"].upper(),
    )


def order_step(records: PurchaseRecords) -> TraceStep | None:
    completion = records.completion
    if completion is None:
        return None
    return step(
        "order_confirmed",
        utc(completion["completed_at"]),
        "merchant",
        order_id=completion["order_id"],
        receipts=tuple(kind for kind in ("checkout", "payment") if completion[kind]),
    )


def order_event_steps(records: PurchaseRecords) -> list[TraceStep]:
    return [
        step("order_event", utc(at), "merchant", step_id=f"order_event:{number}", event_type=event)
        for number, (event, at) in enumerate(records.order_events, start=1)
    ]


def canceled_step(records: PurchaseRecords) -> TraceStep | None:
    if records.purchase["stage"] != "canceled":
        return None
    for by, actor in CANCELED_BY.items():
        if started := records.operations.get(f"checkout.cancel.{by}"):
            return step("canceled", utc(started), actor, by=by)
    return None


def failed_step(records: PurchaseRecords) -> TraceStep | None:
    if records.purchase["stage"] != "failed":
        return None
    actor = "merchant" if "checkout.complete" in records.operations else "agent"
    at = utc(records.purchase["updated_at"])
    # Purchases that failed before the code was stored keep the generic one.
    code = records.purchase["failure_code"] or "purchase_failed"
    return step("failed", at, actor, state="error", code=code)


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _currency(value: object) -> str | None:
    return value.upper() if isinstance(value, str) and len(value) == 3 else None


def _time(value: object) -> str | None:
    try:
        return utc(value) if isinstance(value, str) else None
    except ValueError:
        return None


def freshness_change(field: str, pair: dict[str, Any]) -> dict[str, Any] | None:
    """The checkout value against the live offer's, typed by field as in the contract."""
    read = (
        _count
        if field in AMOUNT_FIELDS
        else {"currency": _currency, "expires_at": _time}.get(field)
    )
    if read is None:
        return None
    return {
        "field": field,
        "presented": read(pair.get("checkout")),
        "current": read(pair.get("offer")),
    }


def freshness_step(record: HpEvidence) -> TraceStep:
    """Application control, attributed to the agent: never a policy decision."""
    body = record.body
    compared = body.get("compared") or {}
    changes = (
        freshness_change(field, compared.get(field) or {}) for field in body["changed_fields"]
    )
    return step(
        "freshness_check",
        utc(body["checked_at"]),
        "agent",
        step_id=f"freshness_check:{record.sequence}",
        state="blocked" if body["decision"] == "block" else "done",
        stage=body["stage"],
        mode=body["governance_mode"],
        outcome=body["outcome"],
        changes=tuple(change for change in changes if change is not None),
        currency=_currency((compared.get("currency") or {}).get("checkout")),
        offer_revision=_count(body.get("observed_revision")),
        read_error=body.get("reason") or None,
        evidence_digest=record.digest,
    )


def freshness_steps(records: PurchaseRecords) -> list[TraceStep]:
    """Every approval attempt, and every reload with AP2, checks again: a repeat adds no row."""
    found: list[TraceStep] = []
    previous = None
    for record in records.hp_evidence:
        if record.record_kind != "freshness_check":
            continue
        current = freshness_step(record)
        result = current.model_dump(exclude={"id", "at", "offer_revision", "evidence_digest"})
        if result != previous:
            found.append(current)
        previous = result
    return found


def recovery_steps(records: PurchaseRecords) -> list[TraceStep]:
    """The record keeps no time; it always follows what it explains, so it goes last."""
    return [
        step(
            "governed_recovery",
            None,
            "agent",
            step_id=f"governed_recovery:{record.sequence}",
            recovery=record.body["kind"],
            cause=record.body["cause"],
            variant_id=record.body.get("variant_id") or None,
            budget_minor=_count(record.body.get("budget_minor")),
            observed_minor=_count(record.body.get("observed_unit_amount_minor")),
            currency=_currency(record.body.get("currency")),
            evidence_digest=record.digest,
        )
        for record in records.hp_evidence
        if record.record_kind == "governed_recovery"
    ]


def sandbox_timed(records: PurchaseRecords, context: TraceContext) -> frozenset[str]:
    """Steps whose time is the sandbox's fixed clock, not when they happened."""
    approval = records.approval
    if (
        context.sandbox_clock_at is None
        or approval is None
        or utc(approval["approved_at"]) != context.sandbox_clock_at
    ):
        return frozenset()
    return frozenset({"approval", "authorization_signed"})


def stored_times(records: PurchaseRecords) -> dict[str, datetime]:
    """Step id → its time as stored, to the microsecond, for a merchant webhook and the steps it
    can share a millisecond with.

    There is no sequence shared by `webhook_events` and the other tables. Each time is taken right
    before its own write, so for two writes made one after the other it keeps their order: the
    trace shows milliseconds, but a tie there is not a tie here.
    """
    stage, completion = records.purchase["stage"], records.completion
    stored = {
        "payment_authorized": records.operations.get("payment.delegate"),
        "order_confirmed": completion["completed_at"] if completion else None,
        "canceled": next(
            (
                records.operations[key]
                for key in (f"checkout.cancel.{by}" for by in CANCELED_BY)
                if key in records.operations
            ),
            None,
        ),
        "failed": records.purchase["updated_at"] if stage == "failed" else None,
        **{
            f"order_event:{number}": at
            for number, (_, at) in enumerate(records.order_events, start=1)
        },
    }
    return {key: precise(value) for key, value in stored.items() if value is not None}


def precise(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Ordering:
    """What `by_time` compares besides the times the trace shows."""

    # Steps that carry the sandbox's fixed time instead of when they happened.
    sandboxed: frozenset[str] = frozenset()
    # Step id → stored time to the microsecond (`stored_times`).
    stored: Mapping[str, datetime] = field(default_factory=dict)


def goes_before(item: TraceStep, current: TraceStep, ordering: Ordering) -> bool:
    """Times are rounded to milliseconds. Within one, the stored times keep the order of the
    writes. If those tie too, or are not known, a freshness check runs right before the step it
    gates, so it goes first; anything else goes after the steps it ties with.

    A step with the sandbox's time is not compared: every check runs before the approval is
    signed, and anything else comes after it.
    """
    if current.kind in ordering.sandboxed:
        return item.kind == "freshness_check"
    if item.at is None or current.at is None:
        return False
    if current.at != item.at:
        return current.at > item.at
    stored_item, stored_current = ordering.stored.get(item.id), ordering.stored.get(current.id)
    if stored_item is not None and stored_current is not None and stored_item != stored_current:
        return stored_current > stored_item
    return item.kind == "freshness_check" and current.kind in GATED


def by_time(
    steps: list[TraceStep], interleaved: list[TraceStep], ordering: Ordering | None = None
) -> list[TraceStep]:
    """Insert each step before the first later one; steps without a time keep their place.

    Freshness checks and merchant webhooks have their own clock: a webhook can arrive before the
    agent records the completion the merchant had already confirmed.
    """
    ordering = ordering or Ordering()
    merged = list(steps)
    for item in interleaved:
        later = (
            index for index, current in enumerate(merged) if goes_before(item, current, ordering)
        )
        merged.insert(next(later, len(merged)), item)
    return merged


def purchase_status(records: PurchaseRecords) -> PurchaseStatus:
    stage = records.purchase["stage"]
    if stage == "awaiting_confirmation" and stale_terms(records):
        return "stale_terms"
    if stage == "awaiting_confirmation" and authorization_refused(records):
        return "blocked"
    if stage in ("completed", "awaiting_confirmation"):
        return STAGE_STATUS[stage]
    if records.decisions and records.decisions[-1].application.disposition == "blocked":
        return "blocked"
    return STAGE_STATUS.get(stage, "in_progress")


def merchant(records: PurchaseRecords, origins: dict[str, str]) -> str | None:
    if records.terms is not None:
        return records.terms["merchant_origin"]
    if records.decisions:
        return origins.get(records.decisions[-1].policy_input().merchant_ref)
    return None


def assemble(records: PurchaseRecords, context: TraceContext) -> PurchaseTrace:
    if records.delegation is not None:
        return assemble_delegated(records, records.delegation, context)
    origins = context.origins
    request = request_step(records)
    found = (
        request,
        selection_step(records, request.at),
        *(
            policy_step(item, index, records, origins)
            for index, item in enumerate(records.decisions)
        ),
        checkout_step(records),
        approval_step(records),
        # After the click and before signing: the approval's time is when it was recorded.
        *(
            authorization_decision_step(item, index)
            for index, item in enumerate(records.authorizations)
        ),
        authorization_step(records),
        payment_step(records),
        order_step(records),
        canceled_step(records),
        failed_step(records),
    )
    ordered = by_time(
        [item for item in found if item is not None],
        [*order_event_steps(records), *freshness_steps(records)],
        Ordering(sandbox_timed(records, context), stored_times(records)),
    )
    steps = (*ordered, *recovery_steps(records))
    terms = records.terms
    origin = merchant(records, origins)
    return PurchaseTrace(
        run_id=records.purchase["run_id"],
        status=purchase_status(records),
        policy_mode=records.decisions[-1].decision.governance_mode if records.decisions else "off",
        request=records.purchase["request_text"],
        merchant=origin,
        merchant_name=context.merchant_names.get(origin) if origin else None,
        total=Money(minor=terms["total_minor"], currency=terms["currency"].upper())
        if terms
        else None,
        started_at=request.at or utc(records.purchase["created_at"]),
        updated_at=max(utc(records.purchase["updated_at"]), *(s.at for s in steps if s.at)),
        steps=steps,
    )


def numbered(kind: str, number: int) -> str:
    return kind if number == 1 else f"{kind}:{number}"


# A task failure the purchase's own cancellation already explains (the budget guard of `start`).
CANCELED_CODES = {"budget": "budget_guard"}


def explained(purchase_end: TraceStep | None) -> str | None:
    if isinstance(purchase_end, FailedStep):
        return purchase_end.code
    if isinstance(purchase_end, CanceledStep):
        return CANCELED_CODES.get(purchase_end.by)
    return None


def delegation_steps(delegation: Delegation, purchase_end: TraceStep | None) -> list[TraceStep]:
    """The ledger's events in their order (spec §9.1). A task failure the purchase already shows
    with the same code adds nothing; with another code (#93) both show."""
    found: list[TraceStep] = []
    counts: Counter[str] = Counter()
    amounts: dict[str, Any] = {}

    def add(kind: str, at: str, actor: TraceActor, **fields: Any) -> None:
        counts[kind] += 1
        found.append(step(kind, at, actor, step_id=numbered(kind, counts[kind]), **fields))

    for event in delegation.events:
        body, at = event.body, utc(event.recorded_at)
        match event.event_type:
            case "consented":
                total, expires = cop(delegation.total_max_minor), utc(delegation.expires_at)
                add(
                    "delegation_consented",
                    at,
                    "user",
                    grant_id=delegation.grant_id,
                    total_max=total,
                    expires_at=expires,
                )
            case "task_claimed":
                add("task_activated", at, "agent", trigger="service")
            case (
                "reservation_reserved"
                | "reservation_released"
                | "consumed"
                | "reservation_uncertain"
            ):
                amount = amounts.setdefault(body["reservation_id"], body.get("amount_minor"))
                outcome = RESERVATION_OUTCOMES[event.event_type]
                add("reservation", at, "agent", outcome=outcome, amount=cop(amount))
            case "mandates_closed":
                add("mandates_closed", at, "agent")
            case "revoked" if body["revoked_reason"] in REVOKED_BY:
                by = body["revoked_reason"]
                add("revoked", at, REVOKED_BY[by], by=by, effect=body["effect"])
            case "expired":
                add("canceled", at, "agent", by="expiry")
            case "task_finished" if body["state"] == "escalated":
                add("escalated", at, "agent", state="blocked", reason=body["outcome_code"])
            case "task_finished" if body["state"] == "failed":
                code = body["outcome_code"]
                if code != explained(purchase_end):
                    step_id = "failed:task" if isinstance(purchase_end, FailedStep) else "failed"
                    found.append(
                        step("failed", at, "agent", step_id=step_id, state="error", code=code)
                    )
            case _:
                pass
    return cause_first(found)


def cause_first(found: list[TraceStep]) -> list[TraceStep]:
    """A revocation and the release it causes share a unit of work, release first: show the
    revocation first."""
    for index in range(len(found) - 1):
        first, second = found[index], found[index + 1]
        released = first.kind == "reservation" and first.outcome == "released"
        if released and second.kind == "revoked" and first.at == second.at:
            found[index], found[index + 1] = second, first
    return found


def delegated_status(records: PurchaseRecords, delegation: Delegation) -> PurchaseStatus:
    """A cancellation first; then what stopped the run, as in HP; then the purchase's stage. A
    blocked allow of a lost CAS is a failure (`state_conflict`), not the policy stopping it.

    K15: a task that ended while `start` left the purchase `selected` (a D1 allow without its
    dispatch result) leaves it waiting for D1's recovery: to be confirmed, never charged.
    """
    finished = next(
        (
            event.body
            for event in reversed(delegation.events)
            if event.event_type == "task_finished"
        ),
        None,
    )
    outcome = finished["outcome_code"] if finished else None
    stage = records.purchase["stage"]
    decisions = sorted(
        (*records.decisions, *records.authorizations, *records.dispatches),
        key=lambda record: record.sequence,
    )
    if stage == "completed":
        return "completed"
    if outcome in CANCELLATIONS:
        return "canceled"
    if stale_terms(records):
        return "stale_terms"
    stopped = bool(decisions) and decisions[-1].application.disposition == "blocked"
    if stopped and outcome != "state_conflict":
        return "blocked"
    if finished is not None and (finished["state"] == "uncertain" or stage == "selected"):
        return "uncertain"
    return STAGE_STATUS.get(stage, "in_progress")


def delegated_total(delegation: Delegation) -> Money | None:
    """The authoritative total the single use reserved, shipping included."""
    amounts = [
        event.body["amount_minor"]
        for event in delegation.events
        if event.event_type == "reservation_reserved"
    ]
    return Money(minor=amounts[-1], currency="COP") if amounts else None


def assemble_delegated(
    records: PurchaseRecords, delegation: Delegation, context: TraceContext
) -> PurchaseTrace:
    origins = context.origins
    # Only a cancellation the purchase records itself (the budget guard): an escalation or a
    # revocation cancels it without an owner's or a guard's operation, and its terminal explains it.
    purchase_end = failed_step(records) or canceled_step(records)
    found = (
        *(
            policy_step(item, index, records, origins)
            for index, item in enumerate(records.decisions)
        ),
        checkout_step(records),
        *(
            authorization_decision_step(item, index)
            for index, item in enumerate(records.authorizations)
        ),
        *(dispatch_decision_step(item, index) for index, item in enumerate(records.dispatches)),
        # No `payment_step`: `payment.delegate` is written before the first send to the PSP and
        # the purchase is `completing` from UoW-2, so neither proves a token. Only the payment
        # receipt `success` does (09b).
        order_step(records),
        purchase_end,
    )
    steps = by_time(
        [item for item in found if item is not None],
        [
            *delegation_steps(delegation, purchase_end),
            *order_event_steps(records),
            *freshness_steps(records),
        ],
        Ordering(stored=stored_times(records)),
    )
    origin = merchant(records, origins) or origins.get(delegation.merchant_ref)
    started = next((item.at for item in steps if item.at), utc(records.purchase["created_at"]))
    return PurchaseTrace(
        run_id=records.purchase["run_id"],
        status=delegated_status(records, delegation),
        policy_mode=records.decisions[-1].decision.governance_mode if records.decisions else "off",
        request=records.purchase["request_text"],
        merchant=origin,
        merchant_name=context.merchant_names.get(origin) if origin else None,
        total=delegated_total(delegation),
        started_at=started,
        updated_at=max(utc(records.purchase["updated_at"]), *(s.at for s in steps if s.at)),
        steps=tuple(steps),
        purchase_mode="HNP",
    )


def finished_at(trace: PurchaseTrace, order: OrderConfirmedStep | None) -> str | None:
    """A completed purchase ends at its order, not at a merchant webhook that arrives later."""
    if trace.status == "completed":
        return order.at if order else None
    return trace.updated_at if trace.status in FINISHED else None


def summarize(trace: PurchaseTrace) -> PurchaseSummary:
    # D2 follows D1: the last decision is the one that let the purchase on or stopped it.
    policies = [
        item.decision
        for item in trace.steps
        if isinstance(item, PolicyDecisionStep | AuthorizationDecisionStep | DispatchDecisionStep)
    ]
    approval = next((item for item in trace.steps if isinstance(item, ApprovalStep)), None)
    order = next((item for item in trace.steps if isinstance(item, OrderConfirmedStep)), None)
    last = policies[-1] if policies else None
    return PurchaseSummary(
        run_id=trace.run_id,
        started_at=trace.started_at,
        request=trace.request,
        merchant=trace.merchant,
        merchant_name=trace.merchant_name,
        total=trace.total,
        status=trace.status,
        policy=PolicySummary(
            outcome=last.outcome,
            failed_rules=tuple(rule.id for rule in last.rules if rule.result == "fail"),
        )
        if last
        else None,
        approved_at=approval.approved_at if approval else None,
        order_id=order.order_id if order else None,
        duration_ms=elapsed_ms(trace.started_at, finished_at(trace, order)),
        purchase_mode=trace.purchase_mode,
    )


STORED_TEXT: TypeAdapter[str] = TypeAdapter(Text)


def stored_text(value: object) -> str | None:
    try:
        return STORED_TEXT.validate_python(value)
    except ValidationError:
        return None


def stored_time(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return utc(value)
    except ValueError:
        return None


def unavailable_summary(purchase: dict[str, Any]) -> PurchaseSummary:
    """A history row whose trace cannot be built: only what the purchase row itself stores.

    Status, policy, merchant, total, approval and duration come from the evidence and the terms,
    so they are not guessed. A value of the row itself outside the contract (written outside the
    store) is left null too, rather than taking the whole history down.
    """
    return PurchaseSummary(
        run_id=purchase["run_id"],
        started_at=stored_time(purchase["created_at"]),
        request=stored_text(purchase["request_text"]),
        merchant=None,
        merchant_name=None,
        total=None,
        status="trace_unavailable",
        policy=None,
        approved_at=None,
        order_id=stored_text(purchase["order_id"]),
        duration_ms=None,
        purchase_mode="HNP" if purchase.get("purchase_mode") == "HNP" else None,
    )


class PurchaseTraces:
    """Traces and history read from the agent database. Ownership is checked by the caller."""

    def __init__(
        self, settings: Settings, *, merchant_names: Mapping[str, str] | None = None
    ) -> None:
        """`merchant_names` maps an origin to its display name (AgentRuntime.merchant_names)."""
        self._database = settings.agent_database_path
        # Same file that runtime.build_runtime gives the LangGraph checkpointer.
        self._graph = self._database.with_name(f"{self._database.stem}.graph.db")
        sandbox = settings.agent_sandbox_clock_at
        self._context = TraceContext(
            origins=merchant_origins(settings),
            merchant_names=dict(merchant_names or {}),
            sandbox_clock_at=utc(sandbox.isoformat()) if sandbox is not None else None,
        )

    def trace(self, run_id: str, *, checkpoints: GraphCheckpoints | None = None) -> PurchaseTrace:
        """Raises `LookupError` for an unknown run and `TraceUnavailable` instead of a guess."""
        try:
            records = read_records(self._database, self._graph, run_id, checkpoints=checkpoints)
            if records is None:
                raise LookupError("Purchase run does not exist")
            # A ValidationError is a ValueError: a stored value the contract rejects.
            return assemble(records, self._context)
        except sqlite3.OperationalError as error:
            if not transient(error):
                raise
            logger.warning("Purchase trace unavailable: run_id=%s error=busy", run_id)
            raise TraceUnavailable(run_id, transient=True) from None
        except (EvidenceError, ValueError) as error:
            logger.warning(
                "Purchase trace unavailable: run_id=%s error=%s", run_id, type(error).__name__
            )
            raise TraceUnavailable(run_id) from None

    def history(self, owner_id: str, *, limit: int, before: str | None) -> PurchaseHistory | None:
        """A page of the owner's purchases, newest first; None if `before` is not one of them."""
        with closing(
            sqlite3.connect(f"{self._database.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        ) as connection:
            cursor = (None, None)
            if before is not None:
                cursor = connection.execute(
                    "SELECT created_at, run_id FROM purchases WHERE run_id = ? AND owner_id = ?",
                    (before, owner_id),
                ).fetchone()
                if cursor is None:
                    return None
            query = connection.execute(
                "SELECT run_id, request_text, order_id, created_at, purchase_mode FROM purchases "
                "WHERE owner_id = ? AND (? IS NULL OR (created_at, run_id) < (?, ?)) "
                "ORDER BY created_at DESC, run_id DESC LIMIT ?",
                (owner_id, before, *cursor, limit + 1),
            )
            columns = [column[0] for column in query.description]
            rows = [dict(zip(columns, row, strict=True)) for row in query.fetchall()]
        page = rows[:limit]
        checkpoints = GraphCheckpoints(self._graph)
        return PurchaseHistory(
            purchases=tuple(self._summary(purchase, checkpoints) for purchase in page),
            next_cursor=page[-1]["run_id"] if len(rows) > limit else None,
        )

    def _summary(self, purchase: dict[str, Any], checkpoints: GraphCheckpoints) -> PurchaseSummary:
        """One broken purchase marks its own row instead of taking the whole history down."""
        try:
            return summarize(self.trace(purchase["run_id"], checkpoints=checkpoints))
        except TraceUnavailable:
            return unavailable_summary(purchase)
