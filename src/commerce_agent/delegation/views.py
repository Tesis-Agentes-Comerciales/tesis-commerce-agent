"""Ledger rows as the buyer's delegation views (docs/design/hnp-governed.md §5.3, §8.1, §8.3).

The task's result drives the view and, while it runs, the grant's state does. An `active` grant
past `expires_at` shows `expired` before any unit of work writes it (§4.4). Texts come from
`HNP_MESSAGES` and amounts from `format_cop`, the UI's notation (K16). Times are `UtcTime` text,
whose order is time order. The strict views are built from JSON-shaped values, as they cross the
API.
"""

from collections.abc import Mapping

from pydantic import TypeAdapter

from commerce_agent.governance.hnp_models import ConsentScope, parse_consent_scope

from .contract import (
    HNP_MESSAGES,
    DelegationView,
    EscalationCode,
    EscalationView,
    RevokeReceipt,
    ScopeView,
    TaskView,
    escalation_message,
)
from .models import Execution, Revocation

Purchase = Mapping[str, str | None]  # the `purchases` row of the task's run (§4.6)
_ESCALATION = TypeAdapter[EscalationCode](EscalationCode)


def scope_view(consent: ConsentScope) -> ScopeView:
    """K7: the consented `ScopeDisplay` has the JSON of `ScopeView`."""
    return ScopeView.model_validate_json(consent.display.model_dump_json())


def delegation_view(ex: Execution, purchase: Purchase | None, now: str) -> DelegationView:
    grant, task = ex.grant, ex.task
    consent = parse_consent_scope(grant.scope_json.encode())
    escalation = None
    if task.state == "escalated":
        reason = _ESCALATION.validate_python(task.outcome_code)
        escalation = EscalationView.model_validate(
            {
                "reason": reason,
                "title": HNP_MESSAGES["escalation.title"],
                "message": escalation_message(reason),
                "prefill_request": HNP_MESSAGES["escalation.prefill_request"].format(
                    title=_title(consent, purchase), total=format_cop(consent.scope.total_max_minor)
                ),
            }
        )
    return DelegationView.model_validate(
        {
            "grant_id": grant.grant_id,
            "state": "expired" if _lapsed(ex, now) else grant.state,
            "revoked_reason": grant.revoked_reason,
            "scope": scope_view(consent),
            "consented_at": grant.consented_at,
            "expires_at": grant.expires_at,
            "task": _task_view(ex, consent, purchase, now),
            "escalation": escalation,
            "last_activity_at": max(grant.updated_at, task.updated_at),
        }
    )


def task_view(ex: Execution, purchase: Purchase | None, now: str) -> TaskView:
    return _task_view(ex, parse_consent_scope(ex.grant.scope_json.encode()), purchase, now)


def revoke_receipt(revocation: Revocation) -> RevokeReceipt:
    """§5.1: a grant closed by another cause is not touched and says so."""
    grant = revocation.grant
    text = "already_closed" if revocation.status == "already_closed" else revocation.effect
    return RevokeReceipt.model_validate(
        {
            "grant_id": grant.grant_id,
            "state": "revoked",
            "revoked_reason": grant.revoked_reason,
            "revoked_at": grant.closed_at,
            "effect": revocation.effect,
            "message": HNP_MESSAGES[f"revoke.{text}"],
        }
    )


def _task_view(
    ex: Execution, consent: ConsentScope, purchase: Purchase | None, now: str
) -> TaskView:
    task = ex.task
    purchased = task.state == "purchased"
    return TaskView.model_validate(
        {
            "task_id": task.task_id,
            "state": task.state,
            "outcome_code": task.outcome_code,
            "run_id": task.run_id,
            "order_id": _order_id(ex) if purchased else None,
            "message": _task_message(ex, consent, purchase, now),
            "updated_at": task.updated_at,
        }
    )


def _task_message(
    ex: Execution, consent: ConsentScope, purchase: Purchase | None, now: str
) -> str | None:
    task = ex.task
    match task.state:
        case "escalated":
            return None  # the EscalationView carries it
        case "scheduled":
            return HNP_MESSAGES["task.grant_expired" if _lapsed(ex, now) else "task.scheduled"]
        case "canceled":
            return HNP_MESSAGES[f"task.{task.outcome_code}"]
        case "purchased":
            if ex.reservation is None:
                raise LookupError("A purchased task without its reservation")
            return HNP_MESSAGES["task.purchased"].format(
                title=_title(consent, purchase),
                total=format_cop(ex.reservation.amount_minor),
                order_id=_order_id(ex),
            )
        case _:
            # running, uncertain and failed; B0-HNP fails with escalation codes (§8.1).
            return HNP_MESSAGES[f"task.{task.state}"]


def _lapsed(ex: Execution, now: str) -> bool:
    return ex.grant.state == "active" and now >= ex.grant.expires_at


def _title(consent: ConsentScope, purchase: Purchase | None) -> str:
    """The variant the run selected; before any selection, the first one of the scope."""
    variants = consent.display.variants
    selected = purchase.get("selected_variant_id") if purchase is not None else None
    return next((item.title for item in variants if item.variant_id == selected), variants[0].title)


def _order_id(ex: Execution) -> str:
    if ex.outbox is None or ex.outbox.order_id is None:
        raise LookupError("A purchased task without its order")
    return ex.outbox.order_id


def format_cop(amount_minor: int) -> str:
    """`formatMoney(amount_minor, "COP")` of the UI, byte for byte (K16).

    es-CO: "$", a no-break space, the pesos grouped by "." and two decimals after ",", then the ISO
    code. ui/src/storybook/fixtures/money-notation.json holds the cases both sides check.
    """
    if amount_minor < 0:
        raise ValueError("A negative amount has no HNP message")
    pesos, cents = divmod(amount_minor, 100)
    grouped = f"{pesos:,}".replace(",", ".")
    return f"$\N{NO-BREAK SPACE}{grouped},{cents:02d} COP"
