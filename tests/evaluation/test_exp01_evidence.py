"""Offline artifact guards only; these synthetic records are never experimental observations."""

import json
from xml.etree import ElementTree as ET

import pytest

from commerce_evaluation.exp01 import PLAN, read, report, sha


@pytest.fixture
def bundle(tmp_path):
    plan = read(PLAN)
    suite = ET.Element("testsuite")
    for key, oracle in plan["cases"].items():
        directory = tmp_path / key.replace(":", "-")
        directory.mkdir()
        frozen = directory / "exp01-frozen.json"
        frozen.write_text(
            json.dumps({"oracle": oracle, "protocol_sha256": sha(PLAN), "frozen_at_ns": 1})
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
            classname="tests.integration.test_exp01_first_batch",
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
                "collected": 8,
                "deselected": 0,
                "collection_errors": 0,
                "counts": {"passed": 8, "failed": 0, "skipped": 0},
            }
        )
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"status": "verified", "protocol_sha256": sha(PLAN)}))
    (tmp_path / "evidence.jsonl").write_text(
        json.dumps({"scenario": "shutdown", "proxy_closed": True, "postgres_removed": True}) + "\n"
    )
    return tmp_path, junit, outcomes, manifest


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


@pytest.mark.parametrize("scenario", ["AUTH-TRANSPORT", "UNREGISTERED-CONTROL"])
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
    if scenario == "AUTH-TRANSPORT":
        assert result["validated"]
        assert result["non_scenario_controls"] == {"AUTH-TRANSPORT": "passed"}
    else:
        assert not result["validated"]
        assert "unregistered_scenario_or_control" in result["issues"]
