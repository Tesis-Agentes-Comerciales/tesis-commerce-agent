"""Governed recovery: explain why a selection cannot proceed instead of a technical error.

B2 mechanism for S02 (docs/design/b2-governed-pilot.md). It is an application control, not a
PDP decision: it never evaluates policy, never creates or authorizes a checkout and never calls
the model. Only enforce with the governed Human Present flow enables it; B0 keeps its errors.
A recovery that cannot be recorded in hp_evidence is not given: the caller answers as B0.
"""

import hashlib
import logging
import textwrap
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from commerce_agent.acp.feed import ProductCandidate, ProductFeed
from commerce_agent.config import Settings
from commerce_agent.domain import HardConstraints, PurchaseIntent, RankedCandidate
from commerce_agent.domain.selection import rejection_reason
from commerce_agent.llm import CandidateDecision
from commerce_agent.llm.prices import format_minor_amount
from commerce_agent.persistence.hp_evidence_sqlite import SqliteHpEvidenceStore

RecoveryKind = Literal[
    "no_candidate", "selection_outside_shortlist", "budget_exceeded_after_checkout"
]
RecoveryCause = Literal[
    "budget",
    "unavailable",
    "currency",
    "category",
    "condition",
    "option",
    "term",
    "not_shortlisted",
    "not_in_catalog",
    "no_match",
]

# Every buyer-facing text lives here. Prices are catalog observations, never final totals.
CAUSE_TEXT: dict[RecoveryCause, str] = {
    "budget": "{title} cuesta {price} en el catálogo y supera tu presupuesto de {budget}.",
    "unavailable": "{title} no está disponible en este momento.",
    "currency": "{title} no se vende en {currency}.",
    "category": "{title} no es de la categoría que pediste ({wanted}).",
    "condition": "{title} no está en la condición que pediste ({wanted}).",
    "option": "{title} no tiene las características que pediste ({wanted}).",
    "term": "{title} no coincide con lo que buscas ({wanted}).",
    "not_shortlisted": "{title} no estaba entre las opciones que verifiqué para tu pedido.",
    "not_in_catalog": "La opción seleccionada no está disponible en el catálogo del comercio.",
    "no_match": "No encontré en el catálogo un producto que cumpla todo lo que pediste{wanted}.",
}
NO_MATCH_CLOSEST = "No encontré un producto que cumpla todo lo que pediste. La opción más cercana:"
NO_CHECKOUT = "No preparé ningún checkout ni se hizo ningún cobro."
CHECKOUT_CANCELED = (
    "Preparé un checkout con el comercio para confirmar el total de {product}, pero ese total "
    "superó tu presupuesto de {budget}, así que lo cancelé. No se hizo ningún cobro ni pedido."
)
CHECKOUT_PRODUCT = "{title} ({price} en el catálogo)"
UNKNOWN_PRODUCT = "tu selección"
NEXT_STEP = "Si quieres, ajusta tu pedido o tu presupuesto y lo busco de nuevo."

logger = logging.getLogger(__name__)


def governed_recovery_enabled(settings: Settings) -> bool:
    return settings.agent_governance_mode == "enforce" and settings.agent_hp_flow == "governed"


def outside_shortlist(
    decision: CandidateDecision, candidates: tuple[RankedCandidate, ...]
) -> str | None:
    """A variant a router returned outside the shortlist (the OpenAI router raises instead)."""
    selected = decision.selected_variant_id
    if selected is None or any(item.candidate.variant_id == selected for item in candidates):
        return None
    return selected


class GovernedRecoveryRecord(BaseModel):
    """Evidence of one recovery. Digests stand in for the buyer's text and the explanation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event: Literal["governed_recovery"] = "governed_recovery"
    # Attribution: S02 improves because of this control, never because of the PDP.
    control: Literal["application"] = "application"
    kind: RecoveryKind
    cause: RecoveryCause
    run_id: str
    variant_id: str | None
    checkout_id: str | None
    currency: str | None
    budget_minor: int | None
    observed_unit_amount_minor: int | None
    rejection_counts: dict[str, int] = Field(default_factory=dict)
    intent_sha256: str
    explanation_sha256: str


class RecoveryEvidenceSink(Protocol):
    def record(self, recovery: GovernedRecoveryRecord) -> None: ...


class RecoveryEvidenceUnavailable(RuntimeError):
    """The recovery left no evidence, so its explanation must not reach the buyer."""


class HpRecoveryEvidence:
    """Appends each recovery to the hp_evidence chain of its run."""

    def __init__(self, store: SqliteHpEvidenceStore) -> None:
        self._store = store

    def record(self, recovery: GovernedRecoveryRecord) -> None:
        self._store.append(recovery.run_id, "governed_recovery", recovery.model_dump(mode="json"))


@dataclass(frozen=True)
class RecoveryContext:
    run_id: str
    feed: ProductFeed
    intent: PurchaseIntent
    rejection_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _Explanation:
    cause: RecoveryCause
    text: str
    candidate: ProductCandidate | None = None
    budget_minor: int | None = None


class GovernedRecovery:
    def __init__(self, evidence: RecoveryEvidenceSink) -> None:
        self._evidence = evidence

    def no_candidate(self, context: RecoveryContext) -> str:
        return self._explain("no_candidate", context, _no_candidate(context.intent, context.feed))

    def outside_shortlist(self, context: RecoveryContext, variant_id: str) -> str:
        candidate = context.feed.candidate(variant_id)
        explanation = (
            _rejected(candidate, context.intent)
            if candidate is not None
            else _Explanation("not_in_catalog", CAUSE_TEXT["not_in_catalog"])
        )
        return self._explain("selection_outside_shortlist", context, explanation)

    def budget_exceeded(
        self, context: RecoveryContext, variant_id: str, checkout_id: str | None
    ) -> str:
        # Only a total budget cancels a created checkout (PurchaseService), so it is set here.
        candidate = context.feed.candidate(variant_id)
        hard = context.intent.hard
        budget = hard.max_total_amount_minor or 0
        values = _values(candidate, hard, budget, "") if candidate else {}
        text = CHECKOUT_CANCELED.format(
            product=CHECKOUT_PRODUCT.format(**values) if candidate else UNKNOWN_PRODUCT,
            budget=_money(budget, candidate.currency if candidate else hard.currency),
        )
        explanation = _Explanation("budget", text, candidate, budget)
        return self._explain(
            "budget_exceeded_after_checkout", context, explanation, checkout_id=checkout_id
        )

    def _explain(
        self,
        kind: RecoveryKind,
        context: RecoveryContext,
        explanation: _Explanation,
        *,
        checkout_id: str | None = None,
    ) -> str:
        # The budget recovery runs after the checkout was canceled, even without a stored id.
        canceled = kind == "budget_exceeded_after_checkout"
        closing = NEXT_STEP if canceled else f"{NO_CHECKOUT} {NEXT_STEP}"
        message = f"{explanation.text} {closing}"
        candidate = explanation.candidate
        record = GovernedRecoveryRecord(
            kind=kind,
            cause=explanation.cause,
            run_id=context.run_id,
            variant_id=candidate.variant_id if candidate else None,
            checkout_id=checkout_id,
            currency=candidate.currency.upper() if candidate else context.intent.hard.currency,
            budget_minor=explanation.budget_minor,
            observed_unit_amount_minor=candidate.observed_unit_amount_minor if candidate else None,
            rejection_counts=dict(context.rejection_counts),
            intent_sha256=_sha256(context.intent.model_dump_json()),
            explanation_sha256=_sha256(message),
        )
        try:
            self._evidence.record(record)
        except Exception as error:
            logger.warning(
                "governed_recovery_evidence_unavailable kind=%s error=%s",
                kind,
                type(error).__name__,
            )
            raise RecoveryEvidenceUnavailable(kind) from error
        return message


def build_governed_recovery(settings: Settings) -> GovernedRecovery | None:
    """Off, shadow and enforce without the governed HP flow neither act nor record."""
    if not governed_recovery_enabled(settings):
        return None
    store = SqliteHpEvidenceStore(settings.agent_database_path)
    store.initialize()
    return GovernedRecovery(HpRecoveryEvidence(store))


def _rejected(candidate: ProductCandidate, intent: PurchaseIntent) -> _Explanation:
    """Recompute, with the domain filters, why a known variant cannot be bought."""
    budget = _exceeded_budget(candidate, intent.hard)
    reason = rejection_reason(candidate, intent)
    cause = cast(RecoveryCause, reason or ("budget" if budget is not None else "not_shortlisted"))
    wanted = _wanted(intent.hard, cause)
    text = CAUSE_TEXT[cause].format(**_values(candidate, intent.hard, budget, wanted))
    return _Explanation(cause, text, candidate, budget if cause == "budget" else None)


def _no_candidate(intent: PurchaseIntent, feed: ProductFeed) -> _Explanation:
    """Name the closest product a single hard constraint excluded; otherwise list them all."""
    without_price = PurchaseIntent(
        hard=intent.hard.model_copy(update={"max_unit_amount_minor": None}),
        preferences=intent.preferences,
    )
    over_budget = [
        candidate
        for candidate in feed.candidates
        if _exceeded_budget(candidate, intent.hard) is not None
        and rejection_reason(candidate, without_price) is None
    ]
    out_of_stock = [
        candidate
        for candidate in feed.candidates
        if not candidate.available
        and rejection_reason(candidate.model_copy(update={"available": True}), intent) is None
    ]
    closest = min(
        over_budget or out_of_stock, key=lambda item: item.observed_unit_amount_minor, default=None
    )
    if closest is not None:
        explanation = _rejected(closest, intent)
        return _Explanation(
            explanation.cause,
            f"{NO_MATCH_CLOSEST} {explanation.text}",
            closest,
            explanation.budget_minor,
        )
    wanted = ", ".join(
        value
        for cause in ("term", "category", "condition", "option", "budget")
        if (value := _wanted(intent.hard, cause))
    )
    text = CAUSE_TEXT["no_match"].format(wanted=f" ({wanted})" if wanted else "")
    return _Explanation("no_match", text)


def _exceeded_budget(candidate: ProductCandidate, hard: HardConstraints) -> int | None:
    # An observed unit price above the total budget can never fit once shipping is added.
    for limit in (hard.max_unit_amount_minor, hard.max_total_amount_minor):
        if limit is not None and candidate.observed_unit_amount_minor > limit:
            return limit
    return None


def _wanted(hard: HardConstraints, cause: str) -> str:
    budget = hard.max_unit_amount_minor or hard.max_total_amount_minor
    values = {
        "term": tuple(f"«{term}»" for term in hard.required_terms),
        "category": hard.categories,
        "condition": hard.conditions,
        "option": tuple(f"{option.name} {option.value}" for option in hard.required_options),
        "budget": (f"hasta {_money(budget, hard.currency)}",) if budget is not None else (),
    }
    return _short(", ".join(values.get(cause, ())))


def _values(
    candidate: ProductCandidate, hard: HardConstraints, budget: int | None, wanted: str
) -> dict[str, str]:
    return {
        "title": _short(candidate.variant_title),
        "price": _money(candidate.observed_unit_amount_minor, candidate.currency),
        "budget": _money(budget, candidate.currency) if budget is not None else "",
        "currency": hard.currency or "",
        "wanted": wanted,
    }


def _money(amount: int, currency: str | None) -> str:
    return format_minor_amount(amount, currency or "").strip()


def _short(text: str) -> str:
    # Feed titles and intent terms are unbounded; the reply allows 1,000 characters.
    return textwrap.shorten(text, 200, placeholder="…")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
