"""Build a `PurchaseTrace` from stored records, one function per step kind.

The trace holds only what happened: the UI deduces the disabled policy and the controls a purchase
never reached. A step carries `duration_ms` only when both of its ends were measured.
"""

import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import TypeAdapter

from commerce_agent.config import Settings
from commerce_agent.governance.evidence import DecisionRecord, EvidenceError
from commerce_agent.persistence.hp_evidence_sqlite import HpEvidence

from .models import (
    ApprovalStep,
    Money,
    OrderConfirmedStep,
    PolicyDecisionStep,
    PolicySummary,
    PurchaseHistory,
    PurchaseStatus,
    PurchaseSummary,
    PurchaseTrace,
    StepState,
    TraceActor,
    TraceStep,
)
from .sources import GraphCheckpoints, PurchaseRecords, merchant_origins, read_records

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
GATED = {"approval", "authorization_signed", "payment_authorized", "order_confirmed"}
# Fields offer_freshness.compare_offer compares; a field it adds later stays out of the trace.
AMOUNT_FIELDS = {"subtotal_minor", "shipping_minor", "total_minor", "delivery_days", "stock"}


class TraceUnavailable(Exception):
    """The trace cannot be built without inventing data: evidence that does not verify, or a
    stored value outside the contract. The trace answers 503; the history marks the row."""


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


def policy_rules(
    record: DecisionRecord, records: PurchaseRecords, origins: dict[str, str]
) -> tuple[dict[str, Any], ...]:
    results = {rule.policy_id: rule.result for rule in record.decision.rule_results}
    if not results:  # A runtime guard stopped the purchase before any rule was evaluated.
        results = dict.fromkeys(("POL-01", "POL-02"), "not_evaluated")
        values = {}
    else:
        data = record.policy_input()
        allowed = records.policy.allowed_merchant_refs if records.policy else ()
        values = {
            "POL-01": (data.selected_variant_id, ", ".join(data.eligible_variant_ids)),
            "POL-02": (
                origins.get(data.merchant_ref, data.merchant_ref),
                ", ".join(sorted(origins.get(ref, ref) for ref in allowed)),
            ),
        }
    return tuple(
        {
            "id": rule,
            "result": result,
            "observed": values.get(rule, ("", ""))[0] or None,
            "expected": values.get(rule, ("", ""))[1] or None,
        }
        for rule, result in results.items()
    )


def policy_step(
    record: DecisionRecord, index: int, records: PurchaseRecords, origins: dict[str, str]
) -> TraceStep:
    decision, effect = record.decision, EFFECTS[record.application.disposition]
    binding = decision.policy_binding
    return step(
        "policy_decision",
        utc(decision.decided_at),
        "policy",
        step_id="policy_decision" if index == 0 else f"policy_decision:{index + 1}",
        state="blocked" if effect == "blocked" else "done",
        duration_ms=decision.latency_ms,
        decision={
            "mode": decision.governance_mode,
            "outcome": decision.outcome,
            "effect": effect,
            "policy": {"set_id": binding.policy_set_id, "revision": binding.version},
            "rules": policy_rules(record, records, origins),
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
    # Written right before the merchant call. The D1 enforce path keeps its key elsewhere.
    started = records.operations.get("checkout.create")
    return step("checkout_created", started and utc(started), "merchant", checkout_id=checkout_id)


def stale_terms(records: PurchaseRecords) -> bool:
    """The last freshness check blocked the terms: they can no longer be approved."""
    checks = [item for item in records.hp_evidence if item.record_kind == "freshness_check"]
    return bool(checks) and checks[-1].body.get("decision") == "block"


def approval_step(records: PurchaseRecords) -> TraceStep | None:
    terms, approval = records.terms, records.approval
    if terms is None or records.terms_shown_at is None:
        return None
    shown = utc(records.terms_shown_at)
    approved = utc(approval["approved_at"]) if approval else None
    waiting = records.purchase["stage"] == "awaiting_confirmation" and not stale_terms(records)
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
    """The payment provider returned: the completion was claimed, which happens only after it.

    A merchant 4xx to /complete releases the claim back to `approved`. After the approval, only
    that claim and its release write the purchase row, so a row written after the delegation key
    proves it; a failed delegation leaves the row as the approval wrote it.
    """
    stage = records.purchase["stage"]
    if stage in DELEGATED_STAGES:
        return True
    updated = datetime.fromisoformat(records.purchase["updated_at"])
    return stage == "approved" and updated > datetime.fromisoformat(started)


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
    # The failure code is not stored; only whether the merchant rejected the completion.
    actor = "merchant" if "checkout.complete" in records.operations else "agent"
    at = utc(records.purchase["updated_at"])
    return step("failed", at, actor, state="error", code="purchase_failed")


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


def goes_before(item: TraceStep, current: TraceStep) -> bool:
    """Times are rounded to milliseconds. A freshness check runs right before the step it gates,
    so on a tie it goes first; anything else goes after the steps it ties with."""
    if item.at is None or current.at is None:
        return False
    if current.at != item.at:
        return current.at > item.at
    return item.kind == "freshness_check" and current.kind in GATED


def by_time(steps: list[TraceStep], interleaved: list[TraceStep]) -> list[TraceStep]:
    """Insert each step before the first later one; steps without a time keep their place.

    Freshness checks and merchant webhooks have their own clock: a webhook can arrive before the
    agent records the completion the merchant had already confirmed.
    """
    merged = list(steps)
    for item in interleaved:
        later = (index for index, current in enumerate(merged) if goes_before(item, current))
        merged.insert(next(later, len(merged)), item)
    return merged


def purchase_status(records: PurchaseRecords) -> PurchaseStatus:
    stage = records.purchase["stage"]
    if stage == "awaiting_confirmation" and stale_terms(records):
        return "stale_terms"
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


def assemble(records: PurchaseRecords, origins: dict[str, str]) -> PurchaseTrace:
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
        authorization_step(records),
        payment_step(records),
        order_step(records),
        canceled_step(records),
        failed_step(records),
    )
    ordered = by_time(
        [item for item in found if item is not None],
        [*order_event_steps(records), *freshness_steps(records)],
    )
    steps = (*ordered, *recovery_steps(records))
    terms = records.terms
    return PurchaseTrace(
        run_id=records.purchase["run_id"],
        status=purchase_status(records),
        policy_mode=records.decisions[-1].decision.governance_mode if records.decisions else "off",
        request=records.purchase["request_text"],
        merchant=merchant(records, origins),
        total=Money(minor=terms["total_minor"], currency=terms["currency"].upper())
        if terms
        else None,
        started_at=request.at or utc(records.purchase["created_at"]),
        updated_at=max(utc(records.purchase["updated_at"]), *(s.at for s in steps if s.at)),
        steps=steps,
    )


def finished_at(trace: PurchaseTrace, order: OrderConfirmedStep | None) -> str | None:
    """A completed purchase ends at its order, not at a merchant webhook that arrives later."""
    if trace.status == "completed":
        return order.at if order else None
    return trace.updated_at if trace.status in FINISHED else None


def summarize(trace: PurchaseTrace) -> PurchaseSummary:
    policies = [item.decision for item in trace.steps if isinstance(item, PolicyDecisionStep)]
    approval = next((item for item in trace.steps if isinstance(item, ApprovalStep)), None)
    order = next((item for item in trace.steps if isinstance(item, OrderConfirmedStep)), None)
    last = policies[-1] if policies else None
    return PurchaseSummary(
        run_id=trace.run_id,
        started_at=trace.started_at,
        request=trace.request,
        merchant=trace.merchant,
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
    )


def unavailable_summary(purchase: dict[str, Any]) -> PurchaseSummary:
    """A history row whose trace cannot be built: only what the purchase row itself stores.

    Status, policy, merchant, total, approval and duration come from the evidence and the terms,
    so they are not guessed.
    """
    return PurchaseSummary(
        run_id=purchase["run_id"],
        started_at=utc(purchase["created_at"]),
        request=purchase["request_text"],
        merchant=None,
        total=None,
        status="trace_unavailable",
        policy=None,
        approved_at=None,
        order_id=purchase["order_id"],
        duration_ms=None,
    )


class PurchaseTraces:
    """Traces and history read from the agent database. Ownership is checked by the caller."""

    def __init__(self, settings: Settings) -> None:
        self._database = settings.agent_database_path
        # Same file that runtime.build_runtime gives the LangGraph checkpointer.
        self._graph = self._database.with_name(f"{self._database.stem}.graph.db")
        self._origins = merchant_origins(settings)

    def trace(self, run_id: str, *, checkpoints: GraphCheckpoints | None = None) -> PurchaseTrace:
        """Raises `LookupError` for an unknown run and `TraceUnavailable` instead of a guess."""
        try:
            records = read_records(self._database, self._graph, run_id, checkpoints=checkpoints)
            if records is None:
                raise LookupError("Purchase run does not exist")
            # A ValidationError is a ValueError: a stored value the contract rejects.
            return assemble(records, self._origins)
        except (EvidenceError, ValueError) as error:
            logger.warning("Purchase trace unavailable: %s", type(error).__name__)
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
                "SELECT run_id, request_text, order_id, created_at FROM purchases "
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
