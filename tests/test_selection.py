from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from commerce_agent.acp.feed import (
    FeedAvailability,
    FeedMetadata,
    FeedPrice,
    FeedProduct,
    FeedVariant,
    ProductFeed,
)
from commerce_agent.domain import (
    HardConstraints,
    OptionConstraint,
    PurchaseIntent,
    RankedCandidate,
    SoftPreferences,
    rank_candidates,
)


def variant(
    variant_id: str,
    *,
    amount: int = 10_000,
    currency: str = "USD",
    available: bool = True,
    condition: str = "new",
    color: str = "black",
    category: str = "headphones",
) -> FeedVariant:
    return FeedVariant(
        id=variant_id,
        title=f"Headphones {variant_id}",
        url=f"https://merchant.example.test/variants/{variant_id}",
        price=FeedPrice(amount=amount, currency=currency),
        availability=FeedAvailability(
            available=available,
            status="in_stock" if available else "out_of_stock",
        ),
        categories=[{"taxonomy": "merchant", "value": category}],
        condition=[condition],
        variant_options=[{"name": "Color", "value": color}],
    )


def feed_with(*variants: FeedVariant) -> ProductFeed:
    products = tuple(
        FeedProduct(
            id=f"product-{item.id}",
            title=f"Product {item.id}",
            url=f"https://merchant.example.test/products/{item.id}",
            variants=[item],
        )
        for item in variants
    )
    return ProductFeed(
        metadata=FeedMetadata(
            id="feed-test",
            target_country="CO",
            updated_at="2026-09-20T12:00:00Z",
        ),
        products=products,
    )


def ranked_ids(result: Sequence[RankedCandidate]) -> list[str]:
    return [item.candidate.variant_id for item in result]


def test_purchase_intent_normalizes_interpreter_output() -> None:
    intent = PurchaseIntent.model_validate(
        {
            "hard": {
                "currency": "usd",
                "categories": ["  HeadPhones "],
                "conditions": [" NEW"],
                "required_options": [{"name": " COLOR ", "value": " Black "}],
                "required_terms": [" Wireless  Headphones "],
            },
            "preferences": {
                "terms": [" Noise   Cancelling "],
                "options": [{"name": "Color", "value": "BLUE"}],
            },
        }
    )

    assert intent.hard.currency == "USD"
    assert intent.hard.categories == ("headphones",)
    assert intent.hard.conditions == ("new",)
    assert intent.hard.required_options == (OptionConstraint(name="color", value="black"),)
    assert intent.hard.required_terms == ("wireless headphones",)
    assert intent.preferences.terms == ("noise cancelling",)
    assert intent.preferences.options == (OptionConstraint(name="color", value="blue"),)


def test_purchase_intent_rejects_unknown_fields_and_invalid_currency() -> None:
    with pytest.raises(ValidationError):
        PurchaseIntent.model_validate({"hard": {"authoritative_total": 1}})
    with pytest.raises(ValidationError):
        PurchaseIntent.model_validate({"hard": {"currency": "US"}})


def test_unavailable_candidates_are_always_rejected() -> None:
    result = rank_candidates(
        feed_with(
            variant("available", amount=20_000),
            variant("unavailable", amount=1, available=False),
        ),
        PurchaseIntent(),
    )

    assert ranked_ids(result.ranked) == ["available"]
    assert result.rejection_counts == {"unavailable": 1}


def test_currency_is_a_hard_constraint() -> None:
    result = rank_candidates(
        feed_with(variant("usd"), variant("eur", currency="EUR", amount=1)),
        PurchaseIntent(hard=HardConstraints(currency="USD")),
    )

    assert ranked_ids(result.ranked) == ["usd"]
    assert result.rejection_counts == {"currency": 1}


def test_budget_uses_observed_feed_amount_as_a_filter_only() -> None:
    result = rank_candidates(
        feed_with(
            variant("within", amount=9_999),
            variant("limit", amount=10_000),
            variant("over", amount=10_001),
        ),
        PurchaseIntent(hard=HardConstraints(max_unit_amount_minor=10_000)),
    )

    assert ranked_ids(result.ranked) == ["within", "limit"]
    assert result.rejection_counts == {"budget": 1}


def test_condition_and_category_are_hard_constraints() -> None:
    result = rank_candidates(
        feed_with(
            variant("match", condition="new"),
            variant("used", condition="used"),
            variant("speaker", category="speakers"),
        ),
        PurchaseIntent(hard=HardConstraints(categories=("headphones",), conditions=("new",))),
    )

    assert ranked_ids(result.ranked) == ["match"]
    assert result.rejection_counts == {"category": 1, "condition": 1}


def test_required_option_is_case_insensitive_and_exact() -> None:
    result = rank_candidates(
        feed_with(variant("black", color="BLACK"), variant("white", color="white")),
        PurchaseIntent(
            hard=HardConstraints(required_options=(OptionConstraint(name="color", value="black"),))
        ),
    )

    assert ranked_ids(result.ranked) == ["black"]
    assert result.rejection_counts == {"option": 1}


def test_required_terms_are_filters_not_model_selected_products() -> None:
    result = rank_candidates(
        feed_with(variant("studio"), variant("travel")),
        PurchaseIntent(hard=HardConstraints(required_terms=("studio",))),
    )

    assert ranked_ids(result.ranked) == ["studio"]
    assert result.rejection_counts == {"term": 1}


def test_preferences_rank_matches_before_lower_observed_price() -> None:
    result = rank_candidates(
        feed_with(
            variant("preferred", amount=20_000, color="black"),
            variant("cheaper", amount=10_000, color="white"),
        ),
        PurchaseIntent(
            preferences=SoftPreferences(options=(OptionConstraint(name="color", value="black"),))
        ),
    )

    assert ranked_ids(result.ranked) == ["preferred", "cheaper"]
    assert result.ranked[0].preference_score == 1
    assert result.ranked[0].matched_preferences == ("option:color=black",)


def test_duplicate_preferences_do_not_inflate_the_score() -> None:
    result = rank_candidates(
        feed_with(variant("black", color="black")),
        PurchaseIntent(
            preferences=SoftPreferences(
                options=(
                    OptionConstraint(name="color", value="black"),
                    OptionConstraint(name="color", value="black"),
                )
            )
        ),
    )

    assert result.ranked[0].preference_score == 1


def test_ranking_has_a_stable_deterministic_tie_breaker() -> None:
    intent = PurchaseIntent()
    first = rank_candidates(
        feed_with(variant("z", amount=10_000), variant("a", amount=10_000)), intent
    )
    second = rank_candidates(
        feed_with(variant("a", amount=10_000), variant("z", amount=10_000)), intent
    )

    assert ranked_ids(first.ranked) == ["a", "z"]
    assert ranked_ids(second.ranked) == ["a", "z"]


def test_no_candidate_returns_a_structured_empty_result() -> None:
    result = rank_candidates(
        feed_with(variant("only", available=False)),
        PurchaseIntent(),
    )

    assert result.ranked == ()
    assert result.selected is None
    assert result.rejection_counts == {"unavailable": 1}
