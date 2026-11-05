"""Offline artifact guards only; these synthetic records are never experimental observations."""

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from commerce_evaluation.exp01 import PLAN, read, report, sha


def _bundle(tmp_path, plan_path=PLAN):
    plan = read(plan_path)
    module = plan.get("test_module", "tests/integration/test_exp01_first_batch.py::")
    suite = ET.Element("testsuite")
    for key, oracle in plan["cases"].items():
        directory = tmp_path / key.replace(":", "-")
        directory.mkdir()
        frozen = directory / "exp01-frozen.json"
        frozen.write_text(
            json.dumps({"oracle": oracle, "protocol_sha256": sha(plan_path), "frozen_at_ns": 1})
        )
        observations = dict.fromkeys(
            (
                "merchant_before",
                "merchant_after",
                "psp_before",
                "psp_after",
                "trigger_status",
                "revoked_at_ns",
                "triggered_at_ns",
                "agent_result",
                "agent_state",
                "after_cleanup",
                "idempotency_sha256",
                "create_posts",
            ),
            "offline-contract-fixture-not-a-merchant-observation",
        )
        observations.update(dict.fromkeys(oracle.get("required_observations", []), "offline"))
        (directory / "exp01-observed.json").write_text(
            json.dumps(
                {
                    "case_id": key,
                    "status": "passed",
                    "cleanup_verified": True,
                    "input_sha256": sha(frozen),
                    "started_at_ns": 2,
                    "observations": observations,
                }
            )
        )
        ET.SubElement(
            suite,
            "testcase",
            classname=module.removesuffix(".py::").replace("/", "."),
            name=oracle["test"],
        )
    junit = tmp_path / "junit.xml"
    ET.ElementTree(suite).write(junit)
    outcomes = tmp_path / "outcomes.json"
    outcomes.write_text(
        json.dumps(
            {
                "status": "completed",
                "exit_code": 0,
                "collected": len(suite),
                "deselected": 0,
                "collection_errors": 0,
                "counts": {"passed": len(suite), "failed": 0, "skipped": 0},
            }
        )
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"status": "verified", "protocol_sha256": sha(plan_path)}))
    (tmp_path / "evidence.jsonl").write_text(
        json.dumps({"scenario": "shutdown", "proxy_closed": True, "postgres_removed": True}) + "\n"
    )
    return tmp_path, junit, outcomes, manifest


@pytest.fixture
def bundle(tmp_path):
    return _bundle(tmp_path)


INF31 = PLAN.with_name("exp08-inf31-hp.json")


def test_inf31_protocol_keeps_exact_controls_and_historical_evidence():
    plan = read(INF31)
    assert set(plan["cases"]) == {
        "INF31:proxy_503",
        "INF31:timeout_before_forward",
        "INF31:merchant_503",
        "INF31:timeout_after_accept",
        "INF31:definitive_stock_refusal",
        "INF31:positive",
    }
    assert {(case["scenario"], case["mode"]) for case in plan["cases"].values()} == {
        ("INF31", "HP")
    }
    historical = plan["historical"]
    archive = PLAN.parents[1] / "results/exp-01-first-batch/runs" / f"{historical['run_id']}.zip"
    assert sha(archive) == historical["archive_sha256"]
    assert sha(PLAN) == historical["protocol_sha256"]


@pytest.fixture
def inf31_bundle(tmp_path: Path):
    artifact = _bundle(tmp_path, INF31)
    row_path = tmp_path / "INF31-merchant_503/exp01-observed.json"
    row = read(row_path)
    merchant = {"checkouts": [], "tables": {"request_observations": {"count": 0}}}
    psp = {"synthetic_payments": {"count": 0}}
    row["observations"].update(
        postgres_before=True,
        postgres_before_forward=False,
        postgres_during_response=False,
        postgres_restored=True,
        create_posts=1,
        proxy_fault="pass",
        upstream_answered=True,
        merchant_http_status=503,
        merchant_error_code="lab_session_store_unavailable",
        merchant_request_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        agent_result="d1_dispatch_uncertain",
        agent_state=[["uncertain", None, "offline"]],
        operation_state=[["uncertain"]],
        recovery={"reason": "NOT_FOUND_NOT_ABSENCE"},
        merchant_before=merchant,
        merchant_after=merchant,
        after_cleanup=merchant,
        psp_before=psp,
        psp_after=psp,
        psp_after_cleanup=psp,
        agent_effect_counts={"approvals": 0, "orders": 0, "completion_results": 0},
    )
    row_path.write_text(json.dumps(row))
    return artifact, row_path


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("postgres_before_forward", True),
        ("postgres_restored", False),
        ("merchant_http_status", 500),
        ("proxy_fault", "status_503"),
        ("upstream_answered", False),
        ("merchant_request_id", None),
        ("create_posts", 2),
        ("recovery", {"reason": "VERIFIED_NOT_CREATED"}),
        ("psp_after", {"synthetic_payments": {"count": 1}}),
        ("merchant_after", {"checkouts": ["unexpected"]}),
    ],
)
def test_inf31_green_junit_cannot_replace_native_outage_proof(inf31_bundle, field, value):
    artifact, path = inf31_bundle
    original = report(*artifact, plan_path=INF31, scenario="INF31")
    assert original["validated"] and original["junit"]["passed"] == 6
    row = read(path)
    row["observations"][field] = value
    path.write_text(json.dumps(row))
    changed = report(*artifact, plan_path=INF31, scenario="INF31")
    assert not changed["validated"]
    assert "native_inf31_outage_not_verified" in changed["issues"]
    assert changed["attributed_scenarios"] == {"INF31": "blocked"}


def test_inf31_missing_control_is_not_attributed(inf31_bundle):
    artifact, _ = inf31_bundle
    (artifact[0] / "INF31-positive/exp01-observed.json").unlink()
    result = report(*artifact, plan_path=INF31, scenario="INF31")
    assert not result["validated"]
    assert result["controls"]["INF31:positive"]["status"] == "not_executed"


def test_missing_execution_is_blocked_not_success(tmp_path):
    result = report(
        tmp_path, tmp_path / "absent.xml", tmp_path / "absent.json", tmp_path / "manifest.json"
    )
    assert not result["validated"]
    assert set(result["scenarios"].values()) == {"blocked"}
    assert result["minimum_plan_counts"].get("passed", 0) == 0


def test_workflow_success_without_independent_observations_cannot_validate(bundle):
    root, *_ = bundle
    assert report(*bundle)["validated"]
    path = root / "MA24/exp01-observed.json"
    row = read(path)
    row["observations"] = {}
    path.write_text(json.dumps(row))
    result = report(*bundle)
    assert not result["validated"]
    assert "independent_observations_missing:MA24" in result["issues"]
    assert result["p0_compatible_projection"]["passed"] == 0


def test_changed_oracle_and_skipped_control_are_retained_and_rejected(bundle):
    root, junit, outcomes, _ = bundle
    frozen = root / "MA24/exp01-frozen.json"
    frozen.write_text(frozen.read_text() + " ")
    tree = ET.parse(junit)
    ET.SubElement(tree.getroot()[0], "skipped")
    tree.write(junit)
    recorded = read(outcomes)
    recorded["counts"].update(passed=7, skipped=1)
    outcomes.write_text(json.dumps(recorded))
    result = report(*bundle)
    assert not result["validated"]
    assert result["junit"]["skipped"] == 1
    assert "frozen_input_changed:MA24" in result["issues"]
    assert "required_control_omitted" in result["issues"]


def test_a_failed_control_does_not_erase_other_scenario_observations(bundle):
    root, junit, outcomes, _ = bundle
    tree = ET.parse(junit)
    index = list(read(PLAN)["cases"]).index("INF31:merchant_503")
    ET.SubElement(tree.getroot()[index], "failure")
    tree.write(junit)
    path = root / "INF31-merchant_503/exp01-observed.json"
    row = read(path)
    row["status"] = "error"
    path.write_text(json.dumps(row))
    recorder = read(outcomes)
    recorder.update(status="incomplete_or_failed", exit_code=1)
    recorder["counts"].update(passed=7, failed=1)
    outcomes.write_text(json.dumps(recorder))
    result = report(*bundle)
    assert not result["validated"] and result["issues"] == []
    assert result["scenarios"] == {"MA24": "passed", "INF30": "passed", "INF31": "error"}
    assert result["minimum_plan_counts"] == {"passed": 2, "error": 1, "not_executed": 34}


def test_protocol_selection_requires_exact_controls_and_does_not_claim_other_modes(tmp_path):
    from commerce_evaluation.exp01 import ROOT

    plan_path = ROOT / "evaluation/scenarios/exp02-commercial.json"
    result = report(
        tmp_path,
        tmp_path / "none.xml",
        tmp_path / "none.json",
        tmp_path / "manifest.json",
        plan_path=plan_path,
        scenario="CT15",
    )
    assert result["scenarios"] == {"CT15": "blocked"}
    assert result["measured_modes"] == {"CT15": ["HP"]}
    assert set(result["controls"]) == {"CT15:HP:within", "CT15:HP:above"}
    assert not result["validated"]
    with pytest.raises(ValueError, match="Unknown or empty"):
        report(
            tmp_path,
            tmp_path / "none.xml",
            tmp_path / "none.json",
            tmp_path / "manifest.json",
            plan_path=plan_path,
            scenario="INF27",
        )


@pytest.mark.parametrize(
    "scenario",
    ["AUTH-TRANSPORT", "EXP06-INFRA", "CT19-FIXTURE", "CT19-STOCK", "UNREGISTERED-CONTROL"],
)
def test_transport_validation_cannot_be_counted_as_p0(bundle, scenario):
    root, _, _, manifest = bundle
    plan = read(PLAN)
    for value in plan["cases"].values():
        value["scenario"] = scenario
    protocol = root / "transport-only-unit-protocol.json"
    protocol.write_text(json.dumps(plan))
    for key, oracle in plan["cases"].items():
        directory = root / key.replace(":", "-")
        frozen = directory / "exp01-frozen.json"
        frozen.write_text(
            json.dumps({"oracle": oracle, "protocol_sha256": sha(protocol), "frozen_at_ns": 1})
        )
        observed = directory / "exp01-observed.json"
        row = read(observed)
        row["input_sha256"] = sha(frozen)
        observed.write_text(json.dumps(row))
    manifest.write_text(json.dumps({"status": "verified", "protocol_sha256": sha(protocol)}))
    result = report(*bundle, plan_path=protocol)
    assert result["p0_compatible_projection"]["passed"] == 0
    assert result["attributed_scenarios"] == {}
    assert result["minimum_plan_counts"] == {"not_executed": 37}
    if scenario in {"AUTH-TRANSPORT", "EXP06-INFRA", "CT19-FIXTURE", "CT19-STOCK"}:
        assert result["validated"]
        assert result["non_scenario_controls"] == {scenario: "passed"}
    else:
        assert not result["validated"]
        assert "unregistered_scenario_or_control" in result["issues"]
