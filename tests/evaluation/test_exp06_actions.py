"""No provider: adversarial dispatches cannot authorize a model request."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from commerce_evaluation import exp06_actions as actions
from commerce_evaluation import exp06_execution as execution
from commerce_evaluation.exp06 import SpendingBlocked


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    inputs = {
        "mode": "smoke",
        "model": execution.protocol()["model"],
        "agent_sha": "a" * 40,
        "max_usd": "9.00",
        "authorization": actions.ACKNOWLEDGEMENT,
        "billing_project_confirmed": "true",
        "standard_pricing_confirmed": "true",
    }
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"inputs": inputs}))
    for name, value in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": actions.REPOSITORY,
        "GITHUB_WORKFLOW_REF": actions.WORKFLOW + "refs/heads/reviewed",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_SHA": "a" * 40,
        "EXP06_MANUAL_SMOKE": "1",
        "GITHUB_EVENT_PATH": str(event),
    }.items():
        monkeypatch.setenv(name, value)
    return event, inputs


def test_exact_explicit_dispatch_can_only_create_a_bound_receipt(dispatch):
    result = actions.dispatch_receipt()
    assert result["model"] == "gpt-5-mini-2025-08-07"
    assert result["executor_sha"] == "a" * 40 and result["stage"] == "smoke"
    assert result["protocol_sha256"] == execution.ORIGINAL_HASH
    assert Decimal(result["max_usd"]) == 9 and actions.matches_dispatch(result)
    assert result["authorization_id"].endswith(":12345")
    assert not actions.matches_dispatch(result | {"max_usd": "8.00"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "validate"),
        ("mode", "batch"),
        ("model", "gpt-5-mini"),
        ("agent_sha", "b" * 40),
        ("agent_sha", "main"),
        ("max_usd", "0"),
        ("max_usd", "9.01"),
        ("max_usd", "NaN"),
        ("max_usd", "Infinity"),
        ("authorization", ""),
        ("billing_project_confirmed", "false"),
        ("standard_pricing_confirmed", "false"),
    ],
)
def test_changed_scope_never_creates_authorization(dispatch, field, value):
    event, inputs = dispatch
    inputs[field] = value
    event.write_text(json.dumps({"inputs": inputs}))
    with pytest.raises(SpendingBlocked):
        actions.dispatch_receipt()


@pytest.mark.parametrize(
    "field,value",
    [
        ("GITHUB_EVENT_NAME", "schedule"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_EVENT_NAME", "workflow_call"),
        (
            "GITHUB_WORKFLOW_REF",
            actions.REPOSITORY + "/.github/workflows/nightly.yml@refs/heads/main",
        ),
        ("GITHUB_REPOSITORY", "other/repo"),
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("EXP06_MANUAL_SMOKE", ""),
    ],
)
def test_automatic_events_callers_and_reruns_are_rejected(dispatch, monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    with pytest.raises(SpendingBlocked):
        actions.dispatch_receipt()


def test_dispatch_receipt_also_passes_runtime_claim_gate(dispatch, tmp_path, monkeypatch):
    data = actions.dispatch_receipt()
    receipt = tmp_path / "approval.json"
    receipt.write_text(json.dumps(data))
    monkeypatch.setattr(execution, "ROOT", tmp_path)
    monkeypatch.setattr(
        execution,
        "git",
        lambda root, *args: (
            str(tmp_path / "git-common") if args[-1] == "--git-common-dir" else "a" * 40
        ),
    )
    budget = execution.authorize(receipt, tmp_path / "out")
    execution.approved_budget(budget)
    assert budget.cap == 9 and budget.max_attempts == 18
    # No client or provider is created by accepting the scope.
    assert budget.attempts == 0


def test_reusable_entry_is_validation_only_and_secrets_are_paid_step_only():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.load(
        (root / ".github/workflows/exp06-smoke.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"workflow_dispatch", "workflow_call"}
    assert workflow["on"]["workflow_dispatch"]["inputs"]["mode"]["default"] == "validate"
    assert workflow["on"]["workflow_dispatch"]["inputs"]["max_usd"]["default"] == "0"
    assert set(workflow["on"]["workflow_call"]["inputs"]) == {"agent_sha"}
    assert set(workflow["on"]["workflow_call"]["secrets"]) == {"MERCHANT_REPO_TOKEN"}
    steps = workflow["jobs"]["protected-run"]["steps"]
    paid = [step for step in steps if "OPENAI_API_KEY" in step.get("env", {})]
    assert len(paid) == 1 and paid[0]["if"] == "env.MODE == 'smoke' && success()"
    assert "--execute" in paid[0]["run"]
    assert all("--execute" not in step.get("run", "") for step in steps if step not in paid)
