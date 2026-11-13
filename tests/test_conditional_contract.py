"""Contracts around the conditional intent (p0-conditional-intent.md §3.1, §3.10, §7 PR 2).

- The router decision gains `conditional`, only for a purchase, and still converts to the strict
  structured-output schema the router sends; its prompt section is short and never lets the model
  pick a branch.
- `select_product` will carry a `ConditionalView`. The UI reads the same JSON fixtures with
  `parseConditionalView` (ui/src/lib/agent-contract.test.ts); here they must equal what the
  evaluator produces for the UX03 cells, so neither side can drift from the other.
"""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from commerce_agent.application.conditional import (
    ConditionalView,
    ResolvedConditional,
    conditional_view,
    evaluate_branches,
    resolve_branches,
)
from commerce_agent.domain import PurchaseIntent
from commerce_agent.llm import CommerceDecision
from commerce_agent.llm import conversation_router as module
from commerce_agent.llm.claude import strict_schema
from tests.support.conditional import (
    SEARCH,
    T_A,
    T_B,
    A,
    B,
    FakeOfferReader,
    acp_error,
    offer,
    phone_feed,
    ux03,
)

FIXTURE = (
    Path(__file__).parents[1] / "ui" / "src" / "storybook" / "fixtures" / "conditional-views.json"
)
Json = dict[str, Any]


def decision(**fields: Any) -> CommerceDecision:
    return CommerceDecision.model_validate_json(
        json.dumps({"action": "purchase", "assistant_message": "Buscaré.", **fields})
    )


CONDITIONAL = {
    "branches": [
        {"match": {"required_terms": ["xperia 5 ii"]}, "max_total_amount_minor": T_A},
        {"match": {"required_terms": ["oneplus 8t"]}, "max_total_amount_minor": T_B},
    ],
    "max_total_amount_minor": None,
    "currency": "COP",
    "excluded": [],
}


def test_a_purchase_decision_may_carry_a_conditional_intent() -> None:
    parsed = decision(intent=PurchaseIntent().model_dump(mode="json"), conditional=CONDITIONAL)
    assert parsed.conditional is not None
    assert [b.max_total_amount_minor for b in parsed.conditional.branches] == [T_A, T_B]
    assert decision(intent=PurchaseIntent().model_dump(mode="json")).conditional is None


@pytest.mark.parametrize("action", ["respond", "clarify"])
def test_only_a_purchase_decision_carries_a_conditional_intent(action: str) -> None:
    with pytest.raises(ValidationError):
        decision(action=action, conditional=CONDITIONAL)


def test_a_conditional_purchase_still_requires_the_search_intent() -> None:
    with pytest.raises(ValidationError):
        decision(conditional=CONDITIONAL)


def test_the_decision_still_converts_to_the_strict_wire_schema() -> None:
    schema = strict_schema(CommerceDecision)
    assert "conditional" in schema["required"]
    definitions = schema["$defs"]
    for name in ("ConditionalIntent", "PurchaseBranch", "BranchMatch"):
        assert definitions[name]["additionalProperties"] is False
        assert set(definitions[name]["required"]) == set(definitions[name]["properties"])
    limit = definitions["PurchaseBranch"]["properties"]["max_total_amount_minor"]
    # The API moves the bound into the description; the decision's own validation keeps it.
    assert {"type": "integer", "description": "{minimum: 0}"} in limit["anyOf"]


def test_the_router_prompt_has_a_short_conditional_section() -> None:
    instructions = module._INSTRUCTIONS
    section = instructions[instructions.index("Conditional purchases") :]
    section = section[: section.index("\n\n")]
    for rule in (
        "conditional",
        "only_if_previous_out_of_stock",
        "excluded",
        "max_total_amount_minor",
        "clarify",
        "Never invent a limit",
    ):
        assert rule in section
    assert len(section) < 1_600  # it travels on every turn (spec §10)


# The ConditionalView fixtures, read by both sides.


def build(limit_a: int, limit_b: int) -> ResolvedConditional:
    result = resolve_branches(ux03(limit_a, limit_b), phone_feed(), hard=SEARCH)
    assert isinstance(result, ResolvedConditional)
    return result


def evaluated_views() -> dict[str, Json]:
    cells = {
        "A": (build(T_A, T_B - 1), {A: offer(A), B: offer(B)}, None),
        "B": (build(T_A - 1, T_B), {A: offer(A), B: offer(B)}, None),
        "none": (build(T_A - 1, T_B - 1), {A: offer(A), B: offer(B)}, None),
        "undetermined": (build(T_A, T_B), {A: acp_error("timeout"), B: offer(B)}, None),
        "offer_changed": (build(T_A - 1, T_B), {A: offer(A), B: offer(B)}, T_B + 1_000_000),
    }
    views = {}
    for name, (branches, answers, checkout_total) in cells.items():
        result = evaluate_branches(branches, FakeOfferReader(dict(answers)))
        view = conditional_view(branches, result, checkout_total_minor=checkout_total)
        views[name] = view.model_dump(mode="json")
    return views


def fixtures() -> dict[str, Json]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_ui_fixtures_are_what_the_evaluator_produces() -> None:
    assert fixtures() == evaluated_views()


@pytest.mark.parametrize("name", sorted(fixtures()))
def test_each_fixture_validates_and_round_trips(name: str) -> None:
    raw = json.dumps(fixtures()[name])
    assert ConditionalView.model_validate_json(raw).model_dump(mode="json") == json.loads(raw)


def _set(path: str, value: Any) -> Callable[[Json], None]:
    def change(payload: Json) -> None:
        *parents, leaf = path.split(".")
        target: Any = payload
        for key in parents:
            target = target[int(key)] if key.isdigit() else target[key]
        target[leaf] = value

    return change


# Mirrored by the rejection cases of ui/src/lib/agent-contract.test.ts.
REJECTED: dict[str, tuple[str, Callable[[Json], None]]] = {
    "extra-key": ("A", _set("rationale", "x")),
    "float-total": ("A", _set("branches.0.total_minor", 292_400_000.0)),
    "unknown-status": ("A", _set("branches.1.status", "skipped")),
    "items-without-total": ("B", _set("branches.0.total_minor", None)),
    "items-over-total": ("B", _set("branches.0.items_total_minor", 292_400_001)),
    "branch-without-choice": ("A", _set("chosen_index", None)),
    "choice-not-eligible": ("B", _set("chosen_index", 0)),
    "none-with-choice": ("none", _set("chosen_index", 0)),
    "checkout-total-outside-offer-changed": ("A", _set("checkout_total_minor", 1)),
    "offer-changed-without-checkout-total": ("offer_changed", _set("checkout_total_minor", None)),
    "branches-out-of-order": ("A", _set("branches.0.index", 1)),
    "no-branches": ("none", _set("branches", [])),
    "other-currency": ("A", _set("currency", "USD")),
}


@pytest.mark.parametrize("case", sorted(REJECTED))
def test_inconsistent_views_are_rejected(case: str) -> None:
    name, change = REJECTED[case]
    payload = copy.deepcopy(fixtures()[name])
    change(payload)
    with pytest.raises(ValidationError):
        ConditionalView.model_validate_json(json.dumps(payload))
