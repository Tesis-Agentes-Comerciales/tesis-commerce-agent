"""Offline checks of uncertainty and execution evidence; no services or selection."""

import json
from pathlib import Path

from tests.support.p3_contract import CASES, condition
from tests.support.p3_report import report


def test_missing_or_unbound_observation_cannot_be_commercial_false():
    assert condition(None, expected_id="absent", at="invalid", budget=0) == "unknown"
    assert (
        condition(None, expected_id="absent", at="invalid", budget=0, scope_verified=False)
        == "unknown"
    )


def test_report_rejects_empty_collection_skips_and_unverified_success(tmp_path: Path):
    junit = tmp_path / "junit.xml"
    assert report(junit, tmp_path)["counts"]["not_executed"] == len(CASES)
    junit.write_text(
        '<testsuites><testsuite><testcase name="test_offer_checkout_contract[stable-zero]">'
        "<skipped/></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )
    assert report(junit, tmp_path)["counts"]["skipped"] == 1
    junit.write_text(
        '<testsuites><testsuite><testcase name="test_offer_checkout_contract[stable-zero]"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_text(
        json.dumps(
            {
                "scenario": "P3-V01-case",
                "case_id": "stable-zero",
                "status": "passed",
                "cleanup_verified": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = report(junit, tmp_path)
    assert result["counts"]["blocked"] == 1 and not result["validated"]
