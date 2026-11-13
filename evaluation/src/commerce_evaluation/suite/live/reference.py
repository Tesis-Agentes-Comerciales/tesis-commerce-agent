"""The reference agent of the regression suite: no model, its decisions derived from the case.

A case says how a good agent ends (`expected`); the reference ends there by construction, so
when it passes every code grader the case itself is consistent with the product
(docs/design/eval-suite.md §4, `reference`). It stands where the model stands, so everything
else (search, governance, checkout, approval and evidence) is the product's own.

Presente, `ReferenceRouter`, in the conversation router's place:

- `buy`: the first turn routes a purchase whose intent names the expected variant (its feed
  title as a required term, the expected total as the budget) and chooses it.
- No purchase expected, but the case expects the purchase's `intent` evidence: the first turn
  attempts the variant the user's first message names (the longest feed title it contains),
  so the case's event or the product stops it (an unavailable store, a pending purchase).
- Otherwise it neither searches nor buys: it asks (`clarify`) when the case accepts it, and
  answers (`respond`) otherwise, saying what the case says it must communicate.

Every later turn answers the same way, without a second purchase.

Delegado, `ReferenceSelector`, in the HNP selector's place: the expected variant when the case
expects a purchase; no choice (`selection_unavailable`) when it expects an escalation; otherwise
the shortlist's first, as the deterministic selector picks, so the task meets what blocks it.
"""

import re
from collections.abc import Sequence

from commerce_agent.acp.feed import ProductCandidate, ProductFeed
from commerce_agent.domain import PurchaseIntent, RankedCandidate
from commerce_agent.llm import (
    CandidateDecision,
    CatalogRecovery,
    CommerceDecision,
    ConversationMessage,
)
from commerce_evaluation.suite.cases import DelegatedCase, PresentCase

PURCHASE_MESSAGE = "Voy a preparar la compra."
CHOICE_MESSAGE = "Elegí la opción que pediste."
ASK = "¿Me cuentas un poco más de lo que buscas?"
ANSWER = "Entendido."


class ReferenceGap(ValueError):
    """The case does not give the reference what it needs."""


class NoReferenceChoice(RuntimeError):
    """The reference makes no choice: the task ends `selection_unavailable`."""


class ReferenceRouter:
    def __init__(self, case: PresentCase, feed: ProductFeed):
        self._case = case
        expected = case.expected
        self._target: ProductCandidate | None = None
        self._budget: int | None = None
        if expected.purchase is not None:
            self._target = _candidate(feed, expected.purchase.variant, case.id)
            self._budget = expected.purchase.total_minor
        elif "intent" in expected.evidence:
            self._target = named_variant(feed, case.user.script[0], case.id)
        self._routed = False

    def route(self, messages: tuple[ConversationMessage, ...]) -> CommerceDecision:
        if self._target is None or self._routed:
            return self._answer()
        self._routed = True
        hard: dict[str, object] = {
            "currency": "COP",
            "required_terms": [self._target.variant_title],
        }
        if self._budget is not None:
            hard["max_total_amount_minor"] = self._budget
        return CommerceDecision(
            action="purchase",
            assistant_message=PURCHASE_MESSAGE,
            purchase_request=messages[-1].content,
            intent=PurchaseIntent.model_validate({"hard": hard}),
        )

    def choose_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        candidates: tuple[RankedCandidate, ...],
    ) -> CandidateDecision:
        target = self._target.variant_id if self._target is not None else None
        if any(item.candidate.variant_id == target for item in candidates):
            return CandidateDecision(
                action="purchase", assistant_message=CHOICE_MESSAGE, selected_variant_id=target
            )
        return CandidateDecision(action="clarify", assistant_message=self._said() or ASK)

    def recover_no_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        rejection_counts: dict[str, int],
        alternatives: tuple[ProductCandidate, ...],
    ) -> CatalogRecovery:
        return CatalogRecovery(action="clarify", assistant_message=self._said() or ASK)

    def _answer(self) -> CommerceDecision:
        if "clarify" in self._case.expected.outcome:
            return CommerceDecision(action="clarify", assistant_message=self._said() or ASK)
        return CommerceDecision(action="respond", assistant_message=self._said() or ANSWER)

    def _said(self) -> str:
        """What the case says the agent must communicate, as one message."""
        told = self._case.expected.must_communicate
        return f"Te cuento {' y '.join(told)}." if told else ""


class ReferenceSelector:
    def __init__(self, case: DelegatedCase):
        self._case = case

    def choose(
        self,
        shortlist: tuple[RankedCandidate, ...],
        intent: PurchaseIntent,
        preference_text: str | None,
    ) -> str:
        expected = self._case.expected
        if expected.purchase is not None:
            return expected.purchase.variant
        if "escalate" in expected.outcome and "block" not in expected.outcome:
            raise NoReferenceChoice(self._case.id)
        return shortlist[0].candidate.variant_id


def named_variant(feed: ProductFeed, message: str, case_id: str) -> ProductCandidate:
    """The variant whose feed title the message names; the longest title wins."""
    said = _plain(message)
    named = [item for item in feed.candidates if _plain(item.variant_title) in said]
    if not named:
        raise ReferenceGap(f"{case_id}: the user's first message names no catalog variant")
    longest = max(len(_plain(item.variant_title)) for item in named)
    [chosen, *others] = [item for item in named if len(_plain(item.variant_title)) == longest]
    if others:
        raise ReferenceGap(f"{case_id}: the user's first message names several variants")
    return chosen


def _candidate(feed: ProductFeed, variant: str, case_id: str) -> ProductCandidate:
    found: Sequence[ProductCandidate] = [
        item for item in feed.candidates if item.variant_id == variant
    ]
    if not found:
        raise ReferenceGap(f"{case_id}: {variant} is not in the episode's feed")
    return found[0]


def _plain(text: str) -> str:
    """Lowercase words, storage written as `128gb`, without the «de» before an amount."""
    text = re.sub(r"(\d+)\s*gb\b", r"\1gb", " ".join(text.casefold().split()))
    return re.sub(r"\bde (?=\d)", "", text)
