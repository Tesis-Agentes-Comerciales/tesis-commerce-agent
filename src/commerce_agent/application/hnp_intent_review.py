"""The intent reviewer of a delegated purchase (docs/design/hnp-intent-review.md).

D1, D2-HNP and D3-HNP enforce the grant's limits; none reads what the buyer asked for in
`preference_text`. With `AGENT_HNP_REVIEWER=llm` (B2-HNP only) a second model checks the
selector's choice against it, before D1 and any POST, and a choice it does not confirm escalates
(`choice_not_confirmed`). The reviewer sees what the selector saw (the shortlist's structured
catalog observations, the intent and `preference_text`) plus the chosen id, never the selector's
own reasoning, so its judgment is independent. It never picks another variant.

It fails closed: an exception, a timeout or anything but a valid `IntentReview` is
`not_confirmed` / `review_unavailable`.
"""

from dataclasses import dataclass
from typing import Literal, Protocol, get_args

from commerce_agent.domain import PurchaseIntent, RankedCandidate

REVIEW_STAGE = "model.hnp_review"

ReviewDecision = Literal["confirmed", "not_confirmed"]
ReviewReason = Literal[
    "matches_request",
    "contradicts_preference",
    "outside_request",
    "unclear",
    "review_unavailable",
]
_DECISIONS = frozenset(get_args(ReviewDecision))
_REASONS = frozenset(get_args(ReviewReason))


@dataclass(frozen=True)
class IntentReview:
    """`confirmed` goes only with `matches_request`; `not_confirmed` with any other reason."""

    decision: ReviewDecision
    reason: ReviewReason

    def __post_init__(self) -> None:
        if not is_valid(self):
            raise ValueError("An intent review pairs confirmed with matches_request only")


def is_valid(review: object) -> bool:
    if not isinstance(review, IntentReview):
        return False
    # A review built around its own check (no `__init__`) may lack either field.
    decision, reason = getattr(review, "decision", None), getattr(review, "reason", None)
    if decision not in _DECISIONS or reason not in _REASONS:
        return False
    return (decision == "confirmed") == (reason == "matches_request")


REVIEW_UNAVAILABLE = IntentReview("not_confirmed", "review_unavailable")


def checked(review: object) -> IntentReview:
    """The review as given when it is a valid `IntentReview`; otherwise it fails closed."""
    return review if isinstance(review, IntentReview) and is_valid(review) else REVIEW_UNAVAILABLE


class HnpIntentReviewer(Protocol):
    """Reviews one choice of the grant's shortlist; `prompt_id` and `model` go to the evidence."""

    @property
    def prompt_id(self) -> str: ...

    @property
    def model(self) -> str: ...

    def review(
        self,
        shortlist: tuple[RankedCandidate, ...],
        intent: PurchaseIntent,
        preference_text: str | None,
        chosen_variant_id: str,
    ) -> IntentReview: ...
