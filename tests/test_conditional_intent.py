"""The typed conditional intent (docs/design/p0-conditional-intent.md §3.1).

Strict like `PurchaseIntent`: no extra keys, integer minor units only, normalized text. The
router fills it; nothing here reads the feed or the Merchant.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from commerce_agent.domain.conditional import (
    MAX_BRANCHES,
    MAX_EXCLUSIONS,
    BranchMatch,
    ConditionalIntent,
    PurchaseBranch,
)
from commerce_agent.domain.intent import HardConstraints
from commerce_agent.governance.provenance_artifacts import SOURCE_PATHS

ROOT = Path(__file__).parents[1]


def branch(**match: Any) -> dict[str, Any]:
    return {"match": match or {"required_terms": ["xperia 5 ii"]}}


def parse(payload: dict[str, Any]) -> ConditionalIntent:
    """As the router receives it: JSON from Structured Outputs."""
    return ConditionalIntent.model_validate_json(json.dumps(payload))


def test_d1_does_not_measure_the_new_module() -> None:
    measured = {Path(path) for path in SOURCE_PATHS}
    assert Path("src/commerce_agent/domain/conditional.py") not in measured
    assert Path("src/commerce_agent/application/conditional.py") not in measured
    # domain/__init__.py is measured: it must not start importing the conditional types.
    init = (ROOT / "src/commerce_agent/domain/__init__.py").read_text(encoding="utf-8")
    assert "conditional" not in init


def test_branches_keep_their_order_and_normalize_like_hard_constraints() -> None:
    intent = parse(
        {
            "branches": [
                {
                    "match": {
                        "required_terms": ["  Sony ", "Xperia  5 II"],
                        "conditions": ["NEW"],
                        "required_options": [{"name": "Almacenamiento", "value": "256  GB"}],
                    },
                    "max_total_amount_minor": 292_400_000,
                },
                {
                    "match": {"variant_id": "ONP-8T-256-GRN"},
                    "only_if_previous_out_of_stock": True,
                },
            ],
            "max_total_amount_minor": 300_000_000,
            "currency": "cop",
            "excluded": [{"required_terms": ["Moto G54"]}],
        }
    )
    first, second = intent.branches
    assert first.match.required_terms == ("sony", "xperia 5 ii")
    assert first.match.conditions == ("new",)
    assert first.match.required_options[0].value == "256 gb"
    assert second.match.variant_id == "ONP-8T-256-GRN"
    assert second.only_if_previous_out_of_stock is True
    assert intent.currency == "COP"
    assert intent.excluded[0].required_terms == ("moto g54",)


T = 292_400_000


@pytest.mark.parametrize(
    ("branch_limit", "global_limit", "search_limit", "effective"),
    [
        (T, None, None, T),
        (None, T, None, T),
        (None, None, T, T),  # UX06 with only the turn's own limit: never None
        (T, T - 1, None, T - 1),  # the global one is smaller: it wins
        (T - 1, T, None, T - 1),  # a branch limit never widens the global
        (T, T, T - 2, T - 2),  # nor the turn's `intent.hard`
        (T - 3, T - 2, T - 1, T - 3),
        (None, None, None, None),  # only the stock condition remains
        (0, None, T, 0),
    ],
)
def test_the_effective_limit_is_the_smallest_one(
    branch_limit: int | None,
    global_limit: int | None,
    search_limit: int | None,
    effective: int | None,
) -> None:
    intent = ConditionalIntent(
        branches=(
            PurchaseBranch(
                match=BranchMatch(required_terms=("xperia 5 ii",)),
                max_total_amount_minor=branch_limit,
            ),
        ),
        max_total_amount_minor=global_limit,
        currency="COP",
    )
    search = HardConstraints(currency="COP", max_total_amount_minor=search_limit)
    assert intent.effective_limit(0, search) == effective


def test_an_exclusion_alone_is_an_intent() -> None:
    """UX07: «prefiero A, nunca compres B». The preference is soft; only the ban is typed."""
    intent = parse({"excluded": [{"required_terms": ["oneplus 8t"]}]})
    assert intent.branches == ()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="no-branch-and-no-exclusion"),
        pytest.param({"branches": [branch()] * (MAX_BRANCHES + 1)}, id="four-branches"),
        pytest.param(
            {"excluded": [{"required_terms": ["x"]}] * (MAX_EXCLUSIONS + 1)}, id="six-exclusions"
        ),
        pytest.param(
            {"branches": [{**branch(), "only_if_previous_out_of_stock": True}]},
            id="first-branch-has-no-previous",
        ),
        pytest.param({"branches": [{"match": {}}]}, id="match-without-criteria"),
        pytest.param({"excluded": [{}]}, id="exclusion-without-criteria"),
        pytest.param({"branches": [{**branch(), "price": 1}]}, id="extra-branch-key"),
        pytest.param({"branches": [branch(required_terms=["a"], brand="sony")]}, id="extra-match"),
        pytest.param({"branches": [branch()], "fallback": "B"}, id="extra-intent-key"),
        pytest.param(
            {"branches": [{**branch(), "max_total_amount_minor": 292_400_000.0}]},
            id="float-branch-limit",
        ),
        pytest.param(
            {"branches": [branch()], "max_total_amount_minor": 1.5}, id="float-global-limit"
        ),
        pytest.param({"branches": [branch()], "max_total_amount_minor": "100"}, id="string-limit"),
        pytest.param({"branches": [{**branch(), "max_total_amount_minor": -1}]}, id="negative"),
        pytest.param({"branches": [{**branch(), "max_total_amount_minor": True}]}, id="bool"),
        pytest.param({"branches": [branch()], "currency": "pesos"}, id="currency-not-a-code"),
        pytest.param({"branches": [branch()], "currency": "USD"}, id="currency-not-cop"),
        pytest.param({"branches": [branch(variant_id="")]}, id="empty-variant-id"),
        pytest.param({"branches": [branch(variant_id="A B")]}, id="variant-id-not-an-offer-id"),
        pytest.param({"branches": [branch(required_terms=[""])]}, id="empty-term"),
    ],
)
def test_invalid_intents_are_rejected(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(payload)


def test_python_construction_is_as_strict_as_json() -> None:
    with pytest.raises(ValidationError):
        PurchaseBranch(
            match=BranchMatch(required_terms=("a",)),
            max_total_amount_minor=1.0,  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        ConditionalIntent(
            branches=(
                PurchaseBranch(
                    match=BranchMatch(required_terms=("a",)), only_if_previous_out_of_stock=True
                ),
            )
        )


def test_the_intent_is_frozen_and_round_trips() -> None:
    intent = parse(
        {
            "branches": [branch(), {**branch(variant_id="ONP-8T-256-GRN")}],
            "currency": "COP",
        }
    )
    with pytest.raises(ValidationError):
        intent.currency = None  # type: ignore[misc]
    assert parse(intent.model_dump(mode="json")) == intent
