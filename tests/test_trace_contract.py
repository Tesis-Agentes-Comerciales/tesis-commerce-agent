"""The Pydantic trace contract validates the same fixtures as `parsePurchaseTrace` in the UI."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from commerce_agent.trace.models import PurchaseSummary, PurchaseTrace

FIXTURES = Path(__file__).parents[1] / "ui" / "src" / "storybook" / "trace-fixtures"
FIXTURE_FILES = sorted(FIXTURES.glob("*.json"))


def test_every_ui_fixture_is_present() -> None:
    assert len(FIXTURE_FILES) >= 11


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda path: path.stem)
def test_ui_fixture_validates_and_round_trips(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    trace = PurchaseTrace.model_validate_json(raw)
    assert trace.model_dump(mode="json") == json.loads(raw)


def fixture(name: str = "completed-shadow") -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def mutated(change) -> dict[str, Any]:
    trace = fixture()
    change(trace)
    return trace


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda trace: trace.update(owner_id="buyer-a"), id="extra-trace-field"),
        pytest.param(lambda trace: trace["steps"][0].update(note="x"), id="extra-step-field"),
        pytest.param(lambda trace: trace["steps"][1].update(kind="unknown"), id="unknown-kind"),
        pytest.param(lambda trace: trace.update(request=""), id="empty-text"),
        pytest.param(lambda trace: trace["steps"][1].update(duration_ms=-1), id="negative-ms"),
        pytest.param(lambda trace: trace["steps"][1].update(duration_ms=1.5), id="fractional-ms"),
        pytest.param(lambda trace: trace["total"].update(minor=True), id="boolean-minor"),
        pytest.param(lambda trace: trace["total"].update(currency="pesos"), id="bad-currency"),
        pytest.param(
            lambda trace: trace.update(started_at="2026-10-20T15:41:20.412+00:00"), id="offset"
        ),
        pytest.param(lambda trace: trace.update(started_at="2026-10-20T15:41:20"), id="naive"),
        pytest.param(lambda trace: trace.update(started_at="2026-02-30T15:41:20Z"), id="no-date"),
        pytest.param(
            lambda trace: trace["steps"][2]["decision"]["policy"].update(revision=-3),
            id="negative-revision",
        ),
        pytest.param(
            lambda trace: trace["steps"][2]["decision"]["rules"][0].update(observed=""),
            id="empty-observed",
        ),
        pytest.param(
            lambda trace: trace["steps"][5].update(mandates=["checkout", "jwt"]),
            id="unknown-mandate",
        ),
    ],
)
def test_contract_is_as_strict_as_the_ui_validator(change) -> None:
    with pytest.raises(ValidationError):
        PurchaseTrace.model_validate_json(json.dumps(mutated(change)))


def test_purchase_summary_matches_the_ui_contract() -> None:
    row = {
        "run_id": "run_1",
        "started_at": "2026-10-20T15:41:20.412Z",
        "request": "Quiero audífonos",
        "merchant": "http://127.0.0.1:4120",
        "total": {"minor": 164990000, "currency": "COP"},
        "status": "blocked",
        "policy": {"outcome": "deny", "failed_rules": ["POL-02"]},
        "approved_at": None,
        "order_id": None,
        "duration_ms": 8722,
    }
    assert PurchaseSummary.model_validate_json(json.dumps(row)).model_dump(mode="json") == row
    with pytest.raises(ValidationError):
        PurchaseSummary.model_validate_json(json.dumps({**row, "owner_id": "buyer-a"}))
