"""Offline checks of uncertainty and execution evidence; no services or selection."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support.p3_contract import (
    AGENT_BASE,
    CASES,
    INTEGRATED_AGENT_BASE,
    INTEGRATED_PRODUCT_TREE,
    MERCHANT,
    PSP,
    condition,
)
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


@pytest.mark.parametrize("corruption", ["wrong_agent", "wrong_tree", "missing_psp", "missing_pg"])
def test_green_junit_needs_verified_sources_and_stack_cleanup(tmp_path: Path, corruption: str):
    junit = tmp_path / "junit.xml"
    suite = ET.SubElement(ET.Element("testsuites"), "testsuite")
    for case in CASES:
        ET.SubElement(suite, "testcase", name=f"test_offer_checkout_contract[{case}]")
    ET.ElementTree(suite).write(junit)
    agent = "1" * 40
    manifest = {
        "scenario": "P3-V01-manifest",
        "agent_sha": agent,
        "agent_base": INTEGRATED_AGENT_BASE,
        "historical_agent_base": AGENT_BASE,
        "product_tree": INTEGRATED_PRODUCT_TREE,
        "base_product_tree": INTEGRATED_PRODUCT_TREE,
        "required_cases": list(CASES),
        "llm": False,
        "payments": False,
        "pins": {
            "MERCHANT_REPO": {"sha": MERCHANT, "lock_sha256": "2" * 64},
            "PAYMENT_REPO": {"sha": PSP, "lock_sha256": "3" * 64},
        },
    }
    pg = {"scenario": "shutdown", "proxy_closed": True, "postgres_removed": True}
    psp = {"scenario": "P3-V01-psp-shutdown", "psp_stopped": True}
    records = [
        {
            "scenario": "P3-V01-case",
            "case_id": case,
            "status": "passed",
            "cleanup_verified": True,
        }
        for case in CASES
    ]
    evidence = tmp_path / "evidence.jsonl"

    def write_evidence() -> None:
        evidence.write_text(
            "".join(json.dumps(row) + "\n" for row in [manifest, pg, psp, *records]),
            encoding="utf-8",
        )

    write_evidence()
    assert report(junit, tmp_path, agent)["validated"]
    if corruption == "wrong_agent":
        manifest["agent_sha"] = "4" * 40
    elif corruption == "wrong_tree":
        manifest["product_tree"] = "4" * 40
    elif corruption == "missing_psp":
        psp["psp_stopped"] = False
    else:
        pg["postgres_removed"] = False
    write_evidence()
    result = report(junit, tmp_path, agent)
    assert not result["validated"]
    assert result["issues"] == [
        "manifest_unverified" if corruption.startswith("wrong") else "stack_cleanup_unverified"
    ]
