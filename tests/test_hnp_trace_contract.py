"""The HNP trace contract (docs/design/hnp-governed.md §10): Pydantic validates the same fixtures
as `parsePurchaseTrace` in the UI, and an HP trace never writes `purchase_mode`."""

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from commerce_agent.delegation.contract import EscalationCode
from commerce_agent.trace import PurchaseTrace
from commerce_agent.trace.assembler import summarize
from commerce_agent.trace.models import EscalationReason

FIXTURES = Path(__file__).parents[1] / "ui" / "src" / "storybook" / "trace-fixtures"

HNP_FIXTURES = sorted((FIXTURES / "hnp").glob("*.json"))


def test_every_hnp_ui_fixture_is_present() -> None:
    assert len(HNP_FIXTURES) == 9


@pytest.mark.parametrize("name", ["enforce-completed", "off-completed"])
def test_a_completed_hnp_fixture_pays_the_single_use_before_its_order(name: str) -> None:
    """As the assembler writes it: the payment receipt shares UoW-3 with the order, first."""
    trace = json.loads((FIXTURES / "hnp" / f"{name}.json").read_text(encoding="utf-8"))
    kinds = [item["kind"] for item in trace["steps"]]
    payment = fixture_step(trace, "payment_authorized")
    reserved = fixture_step(trace, "reservation")["amount"]
    assert kinds.index("payment_authorized") + 1 == kinds.index("order_confirmed")
    assert (payment["actor"], payment["max_minor"], payment["currency"]) == (
        "payment",
        reserved["minor"],
        reserved["currency"],
    )
    assert payment["at"] == fixture_step(trace, "order_confirmed")["at"]


@pytest.mark.parametrize("path", HNP_FIXTURES, ids=lambda path: path.stem)
def test_hnp_ui_fixture_validates_and_round_trips(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    trace = PurchaseTrace.model_validate_json(raw)
    assert trace.purchase_mode == "HNP"
    assert trace.model_dump(mode="json") == json.loads(raw)


def fixture_step(trace: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(item for item in trace["steps"] if item["kind"] == kind)


def hnp_fixture(name: str, change: Any) -> str:
    trace = json.loads((FIXTURES / "hnp" / f"{name}.json").read_text(encoding="utf-8"))
    change(trace)
    return json.dumps(trace)


@pytest.mark.parametrize(
    ("name", "change"),
    [
        pytest.param(
            "enforce-completed", lambda trace: trace.update(purchase_mode="AI"), id="mode"
        ),
        pytest.param(
            "enforce-completed",
            lambda trace: fixture_step(trace, "task_activated").update(trigger="user"),
            id="trigger",
        ),
        pytest.param(
            "enforce-completed",
            lambda trace: fixture_step(trace, "delegation_consented").update(total_max=200000000),
            id="total-max",
        ),
        pytest.param(
            "enforce-completed",
            lambda trace: fixture_step(trace, "reservation").update(outcome="committed"),
            id="reservation-outcome",
        ),
        pytest.param(
            "enforce-completed",
            lambda trace: fixture_step(trace, "mandates_closed").update(mandates=[]),
            id="extra-field",
        ),
        pytest.param(
            "enforce-completed",
            lambda trace: fixture_step(trace, "freshness_check").update(stage="dispatch"),
            id="stage",
        ),
        pytest.param(
            "enforce-completed",
            lambda trace: fixture_step(trace, "dispatch_decision").pop("decision"),
            id="no-decision",
        ),
        pytest.param(
            "enforce-completed",
            lambda trace: fixture_step(trace, "payment_authorized").update(max_minor=-1),
            id="payment-amount",
        ),
        pytest.param(
            "revoked-in-flight",
            lambda trace: fixture_step(trace, "revoked").update(effect="canceled"),
            id="effect",
        ),
        pytest.param(
            "revoked-in-flight",
            lambda trace: fixture_step(trace, "revoked").update(actor="admin"),
            id="actor",
        ),
        pytest.param(
            "d2-denied",
            lambda trace: fixture_step(trace, "escalated").update(reason="policy_denied"),
            id="reason",
        ),
    ],
)
def test_the_hnp_contract_is_as_strict_as_the_ui_validator(name: str, change: Any) -> None:
    with pytest.raises(ValidationError):
        PurchaseTrace.model_validate_json(hnp_fixture(name, change))


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.stem)
def test_hp_traces_never_write_a_purchase_mode(path: Path) -> None:
    trace = PurchaseTrace.model_validate_json(path.read_bytes())
    assert trace.purchase_mode is None
    assert "purchase_mode" not in trace.model_dump(mode="json")
    explicit = trace.model_copy(update={"purchase_mode": "HP"})
    assert "purchase_mode" not in json.loads(explicit.model_dump_json())
    assert "purchase_mode" not in summarize(trace).model_dump(mode="json")


def test_the_escalation_reasons_are_the_ledger_escalation_codes() -> None:
    assert get_args(EscalationReason) == get_args(EscalationCode)
