"""P2 evidence validator tests; synthetic validator inputs, never commercial outcomes."""

import json
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest

from tests.support.nightly_evidence import contract, expected_cases, summarize


def inputs(root: Path, scope: str = "p0-catalog-bridge"):
    plan = expected_cases(scope)
    suite = Element("testsuites")
    children = SubElement(
        suite, "testsuite", tests=str(len(plan)), errors="0", failures="0", skipped="0"
    )
    for node in plan:
        module, name = node.split("::")
        SubElement(children, "testcase", classname=module[:-3].replace("/", "."), name=name)
    junit = root / "junit.xml"
    junit.write_bytes(tostring(suite))
    evidence = root / "evidence.jsonl"
    rows = [
        {"scenario": "bootstrap"},
        {
            "scenario": "P2-source-pins",
            "merchant_sha": contract()["merchant_sha"],
            "psp_sha": contract()["payment_sha"],
        },
    ]
    rows += [
        {"scenario": f"P2-PROV-0{i}", "status": "passed", "cleanup_verified": True}
        for i in (1, 2, 3)
    ]
    rows += [{"scenario": "shutdown", "proxy_closed": True, "postgres_removed": True}]
    evidence.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    outcomes = root / "outcomes.json"
    outcomes.write_text(
        json.dumps(
            {
                "status": "completed",
                "exit_code": 0,
                "counts": {
                    "passed": len(plan),
                    "failed": 0,
                    "skipped": 0,
                    "xfail": 0,
                    "xpass": 0,
                    "not_executed": 0,
                },
                "collected": len(plan),
                "deselected": 0,
                "collection_errors": 0,
                "junit_subtests": {"passed": 0, "failed": 0, "skipped": 0},
            }
        ),
        encoding="utf-8",
    )
    return junit, outcomes, evidence


def test_full_and_p2_are_separate_required_plans():
    assert len(expected_cases("")) == len(expected_cases("full")) == 95
    assert len(expected_cases("p0-catalog-bridge")) == 3
    assert set(expected_cases("p0-catalog-bridge")) < set(expected_cases("full"))
    with pytest.raises(ValueError):
        expected_cases("compatibility")  # Not silently a new-version validation profile.


def test_complete_p2_evidence_can_pass(tmp_path: Path):
    junit, outcomes, _ = inputs(tmp_path)
    result = summarize("p0-catalog-bridge", junit, outcomes, tmp_path)
    assert result["validated"] and result["counts"]["passed"] == 3
    assert result["scenario_cells_executed"] == 0


@pytest.mark.parametrize(
    "tag,status", [("skipped", "skipped"), ("error", "error"), ("failure", "failed")]
)
def test_required_omissions_and_errors_cannot_be_green(tmp_path: Path, tag: str, status: str):
    junit, outcomes, _ = inputs(tmp_path)
    import xml.etree.ElementTree as ET

    tree = ET.parse(junit)
    SubElement(tree.getroot().findall(".//testcase")[0], tag)
    tree.write(junit)
    result = summarize("p0-catalog-bridge", junit, outcomes, tmp_path)
    assert not result["validated"] and result["counts"][status] == 1


def test_missing_case_or_empty_collection_cannot_be_green(tmp_path: Path):
    junit, outcomes, _ = inputs(tmp_path)
    junit.write_text("<testsuites/>", encoding="utf-8")
    result = summarize("p0-catalog-bridge", junit, outcomes, tmp_path)
    assert not result["validated"] and result["counts"]["not_executed"] == 3


def test_missing_evidence_or_unverified_cleanup_blocks_success(tmp_path: Path):
    junit, outcomes, evidence = inputs(tmp_path)
    original = evidence.read_text()
    evidence.write_text(original.replace('"cleanup_verified": true', '"cleanup_verified": false'))
    assert not summarize("p0-catalog-bridge", junit, outcomes, tmp_path)["validated"]
    evidence.write_text("")
    assert not summarize("p0-catalog-bridge", junit, outcomes, tmp_path)["validated"]


def test_p2_green_does_not_validate_full(tmp_path: Path):
    junit, outcomes, _ = inputs(tmp_path)
    result = summarize("full", junit, outcomes, tmp_path)
    assert not result["validated"] and result["counts"]["not_executed"] == 92
