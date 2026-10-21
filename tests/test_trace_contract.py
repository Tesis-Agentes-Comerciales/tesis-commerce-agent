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
    assert len(FIXTURE_FILES) >= 13


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda path: path.stem)
def test_ui_fixture_validates_and_round_trips(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    trace = PurchaseTrace.model_validate_json(raw)
    assert trace.model_dump(mode="json") == json.loads(raw)


def fixture(name: str = "completed-shadow") -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def mutated(change, name: str = "completed-shadow") -> dict[str, Any]:
    trace = fixture(name)
    change(trace)
    return trace


def kind(trace: dict[str, Any], name: str) -> dict[str, Any]:
    """Last step of a kind: the blocking freshness check in stale-terms."""
    return [item for item in trace["steps"] if item["kind"] == name][-1]


def change(trace: dict[str, Any], index: int = 0) -> dict[str, Any]:
    return kind(trace, "freshness_check")["changes"][index]


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda trace: trace.update(owner_id="buyer-a"), id="extra-trace-field"),
        pytest.param(lambda trace: trace["steps"][0].update(note="x"), id="extra-step-field"),
        pytest.param(lambda trace: trace["steps"][1].update(kind="unknown"), id="unknown-kind"),
        pytest.param(lambda trace: trace.update(request=""), id="empty-text"),
        pytest.param(lambda trace: trace.update(merchant_name=""), id="empty-merchant-name"),
        pytest.param(lambda trace: trace.pop("merchant_name"), id="missing-merchant-name"),
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


@pytest.mark.parametrize(
    ("name", "edit"),
    [
        pytest.param(
            "stale-terms",
            lambda trace: kind(trace, "freshness_check").update(stage="complete"),
            id="unknown-freshness-stage",
        ),
        pytest.param(
            "stale-terms",
            lambda trace: kind(trace, "freshness_check").update(outcome="stale"),
            id="unknown-freshness-outcome",
        ),
        pytest.param(
            "stale-terms",
            lambda trace: kind(trace, "freshness_check").update(decision="block"),
            id="freshness-decision-is-the-state",
        ),
        pytest.param(
            "stale-terms",
            lambda trace: kind(trace, "freshness_check").update(evidence_digest=""),
            id="empty-evidence-digest",
        ),
        pytest.param(
            "stale-terms",
            lambda trace: kind(trace, "freshness_check").update(offer_revision=-1),
            id="negative-offer-revision",
        ),
        pytest.param(
            "stale-terms",
            lambda trace: change(trace).update(field="price"),
            id="unknown-changed-field",
        ),
        pytest.param(
            "stale-terms",
            lambda trace: change(trace).update(presented="1899000"),
            id="amount-as-text",
        ),
        pytest.param(
            "stale-terms",
            lambda trace: change(trace).update(field="currency"),
            id="currency-as-amount",
        ),
        pytest.param(
            "stale-terms",
            lambda trace: change(trace).update(
                field="expires_at", presented="2026-10-20T16:25:31+00:00", current=None
            ),
            id="expiry-with-offset",
        ),
        pytest.param(
            "stale-terms",
            lambda trace: change(trace).update(compared={"checkout": 1}),
            id="extra-change-field",
        ),
        pytest.param(
            "governed-recovery",
            lambda trace: kind(trace, "governed_recovery").update(cause="policy"),
            id="unknown-recovery-cause",
        ),
        pytest.param(
            "governed-recovery",
            lambda trace: kind(trace, "governed_recovery").update(recovery="retry"),
            id="unknown-recovery",
        ),
        pytest.param(
            "governed-recovery",
            lambda trace: kind(trace, "governed_recovery").update(explanation_sha256="a" * 64),
            id="recovery-explanation-digest",
        ),
        pytest.param(
            "governed-recovery",
            lambda trace: kind(trace, "governed_recovery").update(budget_minor=-1),
            id="negative-budget",
        ),
    ],
)
def test_application_controls_are_as_strict_as_the_ui_validator(name: str, edit) -> None:
    with pytest.raises(ValidationError):
        PurchaseTrace.model_validate_json(json.dumps(mutated(edit, name)))


def test_application_control_fixtures_cover_s04_and_s02() -> None:
    stale = PurchaseTrace.model_validate_json(json.dumps(fixture("stale-terms")))
    checks = [item for item in stale.steps if item.kind == "freshness_check"]
    assert stale.status == "stale_terms"
    assert [(item.stage, item.outcome, item.state) for item in checks] == [
        ("present", "fresh", "done"),
        ("approve", "changed", "blocked"),
    ]
    assert all(item.actor == "agent" for item in checks)
    recovery = PurchaseTrace.model_validate_json(json.dumps(fixture("governed-recovery")))
    [step] = [item for item in recovery.steps if item.kind == "governed_recovery"]
    assert (step.recovery, step.cause, step.actor) == (
        "budget_exceeded_after_checkout",
        "budget",
        "agent",
    )


def budget_rule(trace: dict[str, Any]) -> dict[str, Any]:
    return kind(trace, "policy_decision")["decision"]["rules"][2]


def test_the_budget_rule_carries_amounts_and_the_others_texts() -> None:
    trace = PurchaseTrace.model_validate_json(json.dumps(fixture("blocked-budget")))
    [step] = [item for item in trace.steps if item.kind == "policy_decision"]
    rules = {rule.id: rule for rule in step.decision.rules}

    assert step.decision.policy.set_id == "b2-hp-v2"
    assert rules["POL-11"].result == "fail"
    assert rules["POL-11"].observed is not None and rules["POL-11"].expected is not None
    assert isinstance(rules["POL-02"].observed, str)


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            lambda trace: budget_rule(trace)["observed"].update(minor=-1), id="negative-amount"
        ),
        pytest.param(
            lambda trace: budget_rule(trace)["observed"].update(minor="154000000"),
            id="amount-as-text",
        ),
        pytest.param(
            lambda trace: budget_rule(trace)["expected"].update(currency="pesos"),
            id="bad-currency",
        ),
        pytest.param(
            lambda trace: budget_rule(trace)["expected"].update(display="$ 1.000.000"),
            id="extra-amount-field",
        ),
        pytest.param(lambda trace: budget_rule(trace).update(observed=154000000), id="bare-number"),
    ],
)
def test_rule_amounts_are_as_strict_as_the_ui_validator(edit) -> None:
    with pytest.raises(ValidationError):
        PurchaseTrace.model_validate_json(json.dumps(mutated(edit, "blocked-budget")))


def test_purchase_summary_matches_the_ui_contract() -> None:
    row = {
        "run_id": "run_1",
        "started_at": "2026-10-20T15:41:20.412Z",
        "request": "Quiero audífonos",
        "merchant": "http://127.0.0.1:4120",
        "merchant_name": "Synthetic Commerce Merchant",
        "total": {"minor": 192400000, "currency": "COP"},
        "status": "blocked",
        "policy": {"outcome": "deny", "failed_rules": ["POL-02"]},
        "approved_at": None,
        "order_id": None,
        "duration_ms": 8722,
    }
    assert PurchaseSummary.model_validate_json(json.dumps(row)).model_dump(mode="json") == row
    with pytest.raises(ValidationError):
        PurchaseSummary.model_validate_json(json.dumps({**row, "owner_id": "buyer-a"}))


def test_only_a_history_row_can_say_its_trace_is_unavailable() -> None:
    """`SummaryStatus` in trace-contract.ts: the trace itself answers 503 instead."""
    row = {
        "run_id": "run_1",
        "started_at": "2026-10-20T15:41:29.134Z",
        "request": "Quiero audífonos",
        "merchant": None,
        "merchant_name": None,
        "total": None,
        "status": "trace_unavailable",
        "policy": None,
        "approved_at": None,
        "order_id": "ord_1",
        "duration_ms": None,
    }
    assert PurchaseSummary.model_validate_json(json.dumps(row)).model_dump(mode="json") == row
    with pytest.raises(ValidationError):
        PurchaseSummary.model_validate_json(json.dumps({**row, "status": "unavailable"}))
    # A purchase row outside the contract keeps only its run_id; any other row has its start.
    bare = {**row, "started_at": None, "request": None, "order_id": None}
    assert PurchaseSummary.model_validate_json(json.dumps(bare)).model_dump(mode="json") == bare
    for missing in ("started_at", "request"):
        with pytest.raises(ValidationError):
            PurchaseSummary.model_validate_json(
                json.dumps({**row, missing: None, "status": "in_progress"})
            )
    with pytest.raises(ValidationError):
        PurchaseTrace.model_validate_json(
            json.dumps(mutated(lambda trace: trace.update(status="trace_unavailable")))
        )
