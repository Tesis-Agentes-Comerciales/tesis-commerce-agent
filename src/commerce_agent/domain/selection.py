from collections import Counter

from pydantic import BaseModel, ConfigDict, Field

from commerce_agent.acp.feed import ProductCandidate, ProductFeed
from commerce_agent.domain.intent import OptionConstraint, PurchaseIntent


class SelectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RankedCandidate(SelectionModel):
    candidate: ProductCandidate
    preference_score: int = Field(ge=0)
    matched_preferences: tuple[str, ...] = ()


class SelectionResult(SelectionModel):
    ranked: tuple[RankedCandidate, ...]
    selected: ProductCandidate | None
    rejection_counts: dict[str, int]


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _normalized_set(values: tuple[str, ...]) -> set[str]:
    return {_normalized(value) for value in values}


def _option_map(candidate: ProductCandidate) -> dict[str, str]:
    return {_normalized(name): _normalized(value) for name, value in candidate.options}


def _matches_options(
    candidate_options: dict[str, str], constraints: tuple[OptionConstraint, ...]
) -> bool:
    return all(candidate_options.get(option.name) == option.value for option in constraints)


def _search_text(candidate: ProductCandidate) -> str:
    values = [
        candidate.product_title,
        candidate.variant_title,
        *candidate.categories,
        *candidate.conditions,
        *(value for option in candidate.options for value in option),
    ]
    return _normalized(" ".join(values))


def _rejection_reason(candidate: ProductCandidate, intent: PurchaseIntent) -> str | None:
    hard = intent.hard
    if not candidate.available:
        return "unavailable"
    if candidate.currency.upper() != hard.currency:
        return "currency"
    if (
        hard.max_unit_amount_minor is not None
        and candidate.observed_unit_amount_minor > hard.max_unit_amount_minor
    ):
        return "budget"
    if not _normalized_set(hard.categories).issubset(_normalized_set(candidate.categories)):
        return "category"
    if not _normalized_set(hard.conditions).issubset(_normalized_set(candidate.conditions)):
        return "condition"
    if not _matches_options(_option_map(candidate), hard.required_options):
        return "option"
    search_text = _search_text(candidate)
    if not all(term in search_text for term in hard.required_terms):
        return "term"
    return None


def _score(candidate: ProductCandidate, intent: PurchaseIntent) -> tuple[int, tuple[str, ...]]:
    preferences = intent.preferences
    categories = _normalized_set(candidate.categories)
    conditions = _normalized_set(candidate.conditions)
    options = _option_map(candidate)
    search_text = _search_text(candidate)
    matched: list[str] = []
    matched.extend(f"term:{term}" for term in preferences.terms if term in search_text)
    matched.extend(
        f"category:{category}" for category in preferences.categories if category in categories
    )
    matched.extend(
        f"condition:{condition}" for condition in preferences.conditions if condition in conditions
    )
    matched.extend(
        f"option:{option.name}={option.value}"
        for option in preferences.options
        if options.get(option.name) == option.value
    )
    unique_matches = tuple(dict.fromkeys(matched))
    return len(unique_matches), unique_matches


def rank_candidates(feed: ProductFeed, intent: PurchaseIntent) -> SelectionResult:
    ranked: list[RankedCandidate] = []
    rejection_counts: Counter[str] = Counter()
    for candidate in feed.candidates:
        rejection = _rejection_reason(candidate, intent)
        if rejection is not None:
            rejection_counts[rejection] += 1
            continue
        score, matched = _score(candidate, intent)
        ranked.append(
            RankedCandidate(
                candidate=candidate,
                preference_score=score,
                matched_preferences=matched,
            )
        )

    ranked.sort(
        key=lambda item: (
            -item.preference_score,
            item.candidate.observed_unit_amount_minor,
            _normalized(item.candidate.product_title),
            item.candidate.variant_id,
        )
    )
    ranked_tuple = tuple(ranked)
    return SelectionResult(
        ranked=ranked_tuple,
        selected=ranked_tuple[0].candidate if ranked_tuple else None,
        rejection_counts=dict(sorted(rejection_counts.items())),
    )
