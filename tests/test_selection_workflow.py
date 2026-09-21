from pathlib import Path

import pytest

from commerce_agent.acp import load_product_feed
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.workflow import PurchaseStage, build_selection_workflow

FIXTURE = Path(__file__).parent / "fixtures" / "feed"


class FakeInterpreter:
    def __init__(self, intent: PurchaseIntent) -> None:
        self.intent = intent
        self.requests: list[str] = []

    def interpret(self, request: str) -> PurchaseIntent:
        self.requests.append(request)
        return self.intent


def test_read_only_workflow_interprets_filters_and_selects() -> None:
    feed = load_product_feed(FIXTURE)
    original_feed = feed.model_dump_json()
    interpreter = FakeInterpreter(
        PurchaseIntent(
            hard=HardConstraints(
                currency="USD",
                max_unit_amount_minor=80_000,
                categories=("headphones",),
                conditions=("new",),
            )
        )
    )
    workflow = build_selection_workflow(feed=feed, interpreter=interpreter)

    result = workflow.invoke({"request": "  Quiero audífonos nuevos  "})

    assert interpreter.requests == ["Quiero audífonos nuevos"]
    assert result["stage"] == PurchaseStage.SELECTED
    assert result["selected_candidate"].variant_id == "SON-01"
    assert [row.candidate.variant_id for row in result["ranked_candidates"]] == ["SON-01"]
    assert result["rejection_counts"] == {"unavailable": 1}
    assert result["failure_code"] is None
    assert feed.model_dump_json() == original_feed


def test_read_only_workflow_reports_no_matching_candidates() -> None:
    interpreter = FakeInterpreter(PurchaseIntent(hard=HardConstraints(max_unit_amount_minor=1)))
    workflow = build_selection_workflow(feed=load_product_feed(FIXTURE), interpreter=interpreter)

    result = workflow.invoke({"request": "Audífonos por un centavo"})

    assert result["stage"] == PurchaseStage.FAILED
    assert result["selected_candidate"] is None
    assert result["ranked_candidates"] == ()
    assert result["rejection_counts"] == {"budget": 1, "unavailable": 1}
    assert result["failure_code"] == "no_matching_candidates"


def test_read_only_workflow_rejects_an_empty_request_before_interpretation() -> None:
    interpreter = FakeInterpreter(PurchaseIntent())
    workflow = build_selection_workflow(feed=load_product_feed(FIXTURE), interpreter=interpreter)

    with pytest.raises(ValueError, match="must not be empty"):
        workflow.invoke({"request": "  "})

    assert interpreter.requests == []
