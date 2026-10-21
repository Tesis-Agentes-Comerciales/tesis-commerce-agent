"""D2-HP in the purchase trace, the history and the restored chat (hp-p3-d2-authorization).

Same API and SQLite store as test_d2_authorization: mock merchant and PSP, no model.
"""

from pathlib import Path
from typing import Any

import pytest

from commerce_agent.governance.evaluator import ReferenceAuthorizationEvaluator
from commerce_agent.governance.models import PolicyDecision
from tests.governance.test_d2_authorization import (
    assert_refused,
    d2_records,
    d2_settings,
    start_purchase,
    with_policy,
)
from tests.governance.test_hp_continuation import StoreMerchant, app_client, approve

D2_RULES = ["POL-03", "POL-04", "POL-05", "POL-06"]


def steps(trace: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [item for item in trace["steps"] if item["kind"] == kind]


def rules(step: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {rule["id"]: rule for rule in step["decision"]["rules"]}


def history_row(client: Any, run_id: str) -> dict[str, Any]:
    page = client.get("/me/purchases").json()
    return next(row for row in page["purchases"] if row["run_id"] == run_id)


def test_an_allowed_approval_shows_the_authorization_before_paying(tmp_path: Path) -> None:
    settings = d2_settings(tmp_path / "agent.db")
    with app_client(settings, StoreMerchant()) as client:
        run_id = start_purchase(client, "turn-trace-allow")
        assert approve(client, run_id).status_code == 200
        assert client.post(f"/purchases/{run_id}/complete").status_code == 200
        trace = client.get(f"/purchases/{run_id}/trace").json()
        row = history_row(client, run_id)
    kinds = [item["kind"] for item in trace["steps"]]
    assert kinds.index("approval") < kinds.index("authorization_decision")
    assert kinds.index("authorization_decision") < kinds.index("payment_authorized")
    (step,) = steps(trace, "authorization_decision")
    (record,) = d2_records(settings.agent_database_path)
    assert (step["id"], step["actor"], step["state"]) == (
        "authorization_decision",
        "policy",
        "done",
    )
    decision = step["decision"]
    assert (decision["outcome"], decision["effect"], decision["mode"]) == (
        "allow",
        "continued",
        "enforce",
    )
    assert (decision["decision_id"], decision["evidence_id"]) == (
        record.decision.decision_id,
        record.evidence_id,
    )
    by_id = rules(step)
    assert list(by_id) == D2_RULES
    assert all(rule["result"] == "pass" for rule in by_id.values())
    # The owner's identity (POL-03) never shows; equal variants and revisions explain nothing.
    assert all(by_id[rule]["observed"] is None for rule in ("POL-03", "POL-04", "POL-06"))
    assert by_id["POL-05"]["observed"] == {"minor": 74_000, "currency": "COP"}
    assert steps(trace, "approval")[0]["state"] == "done"
    assert trace["status"] == "completed"
    assert row["policy"] == {"outcome": "allow", "failed_rules": []}


def test_a_denied_approval_stops_the_trace_and_restores_as_denied(tmp_path: Path) -> None:
    first = d2_settings(tmp_path / "agent.db")
    merchant = StoreMerchant()
    with app_client(first, merchant) as client:
        run_id = start_purchase(client, "turn-trace-deny")
    second = with_policy(first, revision=2)
    with app_client(second, merchant) as client:
        assert_refused(approve(client, run_id), "d2_denied")
        trace = client.get(f"/purchases/{run_id}/trace").json()
        pending = client.get("/me/pending-purchase").json()
        row = history_row(client, run_id)
        assert client.post(f"/purchases/{run_id}/cancel").status_code == 200
        canceled = client.get(f"/purchases/{run_id}/trace").json()
    assert trace["status"] == "blocked"
    (approval,) = steps(trace, "approval")
    assert (approval["state"], approval["approved_at"]) == ("skipped", None)
    (step,) = steps(trace, "authorization_decision")
    assert step["state"] == "blocked"
    assert (step["decision"]["outcome"], step["decision"]["effect"]) == ("deny", "blocked")
    assert step["decision"]["reason_codes"] == ["POLICY_CONTEXT_MISMATCH"]
    pol06 = rules(step)["POL-06"]
    assert pol06["result"] == "fail"
    assert pol06["observed"].endswith("revisión 1") and pol06["expected"].endswith("revisión 2")
    assert steps(trace, "authorization_signed") == []
    # The chat comes back to the denial, not to an approval card that would be refused again.
    assert pending["purchase"]["stage"] == "awaiting_confirmation"
    assert (pending["approval_terms"], pending["terms_status"]) == (None, "authorization_denied")
    assert row["status"] == "blocked"
    assert row["policy"] == {"outcome": "deny", "failed_rules": ["POL-06"]}
    assert canceled["status"] == "canceled"


def test_an_engine_failure_shows_no_rule_and_restores_as_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: ReferenceAuthorizationEvaluator, policy_input: Any) -> PolicyDecision:
        raise RuntimeError("engine down")

    monkeypatch.setattr(ReferenceAuthorizationEvaluator, "evaluate", broken)
    settings = d2_settings(tmp_path / "agent.db")
    with app_client(settings, StoreMerchant()) as client:
        run_id = start_purchase(client, "turn-trace-engine")
        assert_refused(approve(client, run_id), "d2_unavailable")
        trace = client.get(f"/purchases/{run_id}/trace").json()
        pending = client.get("/me/pending-purchase").json()
    (step,) = steps(trace, "authorization_decision")
    assert (step["state"], step["decision"]["outcome"]) == ("blocked", "deny")
    assert [rule["result"] for rule in step["decision"]["rules"]] == ["not_evaluated"] * 4
    assert trace["status"] == "blocked"
    assert pending["terms_status"] == "authorization_unverified"


def test_without_d2_the_pending_purchase_keeps_its_terms(tmp_path: Path) -> None:
    settings = with_policy(d2_settings(tmp_path / "agent.db"), authorization=None)
    with app_client(settings, StoreMerchant()) as client:
        run_id = start_purchase(client, "turn-trace-v1")
        pending = client.get("/me/pending-purchase").json()
        trace = client.get(f"/purchases/{run_id}/trace").json()
    assert pending["terms_status"] is None and pending["approval_terms"] is not None
    assert steps(trace, "authorization_decision") == []
