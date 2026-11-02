"""P0 preregistration and reporting. No product selector/scorer or service imports."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

SCENARIO_IDS = (
    tuple(f"UX{i:02d}" for i in range(1, 13))
    + tuple(f"CT{i:02d}" for i in range(13, 22))
    + tuple(f"MA{i:02d}" for i in range(22, 27))
    + tuple(f"INF{i:02d}" for i in range(27, 34))
)
CELL_EXPECTATIONS = {
    "UX03-A": "A",
    "UX03-B": "B",
    "UX03-C": "A",
    "UX03-D": "none",
    "UX03-UNKNOWN-A": "undetermined",
}
CELL_ELIGIBILITY = {
    "UX03-A": (True, False),
    "UX03-B": (False, True),
    "UX03-C": (True, True),
    "UX03-D": (False, False),
    "UX03-UNKNOWN-A": (None, True),
}
OUTCOMES = ("passed", "failed", "skipped", "xfail", "xpass", "not_executed")
FACT_KEYS = {"verification", "condition_met", "in_stock", "total_minor", "budget_minor", "currency"}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def eligibility(facts: dict[str, Any]) -> bool | None:
    """Synthetic mathematical oracle; unknown is distinct from ineligible."""
    if set(facts) != FACT_KEYS:
        raise ValueError("Unexpected or missing branch facts")
    for key in ("budget_minor", "total_minor"):
        value = facts[key]
        if key == "total_minor" and value is None:
            continue
        if type(value) is not int or value < 0:
            raise ValueError(f"{key} must be a nonnegative integer in minor units")
    if not isinstance(facts["currency"], str) or not re.fullmatch(r"[A-Z]{3}", facts["currency"]):
        raise ValueError("Currency must be explicit; no currency conversion")
    if facts["verification"] == "technical_unknown":
        if any(facts[key] is not None for key in ("condition_met", "in_stock", "total_minor")):
            raise ValueError("Unknown facts must not assert verified observations")
        return None
    if facts["verification"] != "verified":
        raise ValueError("Unknown verification state")
    if any(type(facts[key]) is not bool for key in ("condition_met", "in_stock")):
        raise ValueError("Verified conditions and stock must be booleans")
    if facts["total_minor"] is None:
        raise ValueError("A verified branch requires an authoritative total")
    return bool(
        facts["condition_met"]
        and facts["in_stock"]
        and facts["total_minor"] <= facts["budget_minor"]
    )


def choose_ux03(a: dict[str, Any], b: dict[str, Any]) -> str:
    """Return a decision label, never approval or authority to create a checkout."""
    first, second = eligibility(a), eligibility(b)
    if a["currency"] != b["currency"]:
        raise ValueError("This preregistration does not support mixed currencies")
    if first is None:
        return "undetermined"
    if first:
        return "A"
    if second is None:
        return "undetermined"
    return "B" if second else "none"


def validate_contract(matrix: dict[str, Any], oracle: dict[str, Any]) -> None:
    if matrix["schema_version"] != 1 or oracle["schema_version"] != 1:
        raise ValueError("Unsupported schema")
    rows = matrix["scenarios"]
    if tuple(row["id"] for row in rows) != SCENARIO_IDS:
        raise ValueError("Expected the exact, unique ordered 33 scenario IDs")
    required = {
        "description",
        "stimulus",
        "expected_proposed",
        "modes",
        "coverage",
        "gap",
        "test_type",
        "required_data",
        "implementation_status",
        "execution",
        "source_text",
        "oracle_proposed",
        "required_evidence",
        "capability_note",
        "id",
    }
    for row in rows:
        if set(row) != required or not all(row[key] for key in required - {"capability_note"}):
            raise ValueError(f"Incomplete row: {row['id']}")
        if not row["modes"] or not set(row["modes"]) <= {"HP", "HNP"}:
            raise ValueError("Invalid mode")
        if row["execution"] != {"status": "not_executed", "evidence": []}:
            raise ValueError("Preregistration must not claim scenario executions")
    if rows[2]["modes"] != ["HP"] or oracle["mode"] != "HP":
        raise ValueError("The first UX03 increment is HP only")
    if oracle["scenario"] != "UX03" or oracle["data_kind"] != "synthetic_math_only":
        raise ValueError("Oracle fixtures must be explicitly synthetic")
    cells = oracle["cells"]
    if [cell["id"] for cell in cells] != list(CELL_EXPECTATIONS):
        raise ValueError("Expected five distinct preregistered UX03 cells")
    for cell in cells:
        validate_cell(cell)


def validate_cell(cell: dict[str, Any]) -> None:
    cell_id = cell["id"]
    if cell_id not in CELL_EXPECTATIONS or cell["expected"] != CELL_EXPECTATIONS[cell_id]:
        raise ValueError("Preregistered expectation changed")
    if (eligibility(cell["a"]), eligibility(cell["b"])) != CELL_ELIGIBILITY[cell_id]:
        raise ValueError("Branch facts no longer represent the preregistered cell")
    if choose_ux03(cell["a"], cell["b"]) != cell["expected"]:
        raise ValueError("Inconsistent mathematical fixture")


def score_ux03(cell: dict[str, Any], observed: str | None) -> str:
    """Compare a decision label only; this never validates purchase effects or HP approval."""
    validate_cell(cell)
    if observed is None:
        return "not_executed"
    if not isinstance(observed, str) or not observed:
        raise ValueError("Invalid observed decision")
    return "passed" if observed == cell["expected"] else "failed"


def planned_cells(matrix: dict[str, Any], oracle: dict[str, Any]) -> list[str]:
    validate_contract(matrix, oracle)
    return [
        cell_id
        for row in matrix["scenarios"]
        for cell_id in (list(CELL_EXPECTATIONS) if row["id"] == "UX03" else [row["id"]])
    ]


def count_plan(plan: list[str], observations: dict[str, str]) -> dict[str, int]:
    """Pure plan comparison. Harness outcomes are never scenario observations."""
    if len(set(plan)) != len(plan) or not observations.keys() <= set(plan):
        raise ValueError("Duplicate plan IDs or observations outside the plan")
    if any(outcome not in OUTCOMES for outcome in observations.values()):
        raise ValueError("Unknown outcome")
    counts = Counter(observations.get(cell, "not_executed") for cell in plan)
    return {status: counts[status] for status in OUTCOMES}


def report_outcome(outcome: str, when: str, wasxfail: bool, strict_xpass: bool) -> str | None:
    """Classify pytest phases, preserving xfail and both XPASS variants."""
    if outcome not in {"passed", "failed", "skipped"} or when not in {"setup", "call", "teardown"}:
        raise ValueError("Invalid pytest phase or outcome")
    if strict_xpass or (outcome == "passed" and wasxfail):
        return "xpass"
    if outcome == "failed":
        return "failed"
    if outcome == "skipped":
        return "xfail" if wasxfail else "skipped"
    return "passed" if outcome == "passed" and when == "call" else None


def python_evidence(pytest_path: Path, junit_path: Path) -> dict[str, Any]:
    """Validate artifact structure and completeness; JUnit cannot distinguish non-strict XPASS."""
    if not pytest_path.exists() and not junit_path.exists():
        return {
            "status": "not_executed",
            "artifacts_valid": False,
            "reason": "Python and JUnit reports absent; counts unavailable",
        }
    try:
        report = load_json(pytest_path)
        counts = report["counts"]
        if set(counts) != set(OUTCOMES) or any(
            type(value) is not int or value < 0 for value in counts.values()
        ):
            raise ValueError("Invalid Python counts")
        for key in ("exit_code", "collected", "deselected", "collection_errors"):
            if type(report[key]) is not int or report[key] < 0:
                raise ValueError("Invalid session metadata")
        if sum(counts.values()) != report["collected"]:
            raise ValueError("Counts do not match collection")
        completed = report["exit_code"] == 0
        if report["status"] != ("completed" if completed else "incomplete_or_failed"):
            raise ValueError("Session status conflicts with exit code")
        if completed and (
            counts["failed"] or counts["not_executed"] or report["collection_errors"]
        ):
            raise ValueError("Incomplete or failed tests cannot claim completed success")
        tree = ElementTree.parse(junit_path).getroot()
        if tree.tag not in {"testsuite", "testsuites"}:
            raise ValueError("Invalid JUnit root")
        suites = [tree] if tree.tag == "testsuite" else list(tree.findall("testsuite"))
        if not suites:
            raise ValueError("JUnit has no suites")
        cases = []
        for suite in suites:
            children = list(suite.findall("testcase"))
            declared = {
                key: int(suite.attrib[key]) for key in ("tests", "failures", "errors", "skipped")
            }
            if any(value < 0 for value in declared.values()):
                raise ValueError("Negative JUnit counts")
            for tag, key in (("failure", "failures"), ("error", "errors"), ("skipped", "skipped")):
                if sum(len(case.findall(tag)) for case in children) != declared[key]:
                    raise ValueError("JUnit declared counts do not match its cases")
            # pytest can emit duplicate cases for call + teardown errors.
            if not declared["errors"] and declared["tests"] != len(children):
                raise ValueError("JUnit cases are incomplete")
            if completed and (declared["failures"] or declared["errors"]):
                raise ValueError("JUnit failure conflicts with successful session")
            cases.extend(children)
        node_outcomes: dict[tuple[str | None, str | None], str] = {}
        priority = {"passed": 1, "skipped": 2, "failed": 3}
        for case in cases:
            if not case.get("name"):
                raise ValueError("JUnit case has no identity")
            identity = (case.get("classname"), case.get("name"))
            outcome = (
                "failed"
                if case.find("failure") is not None or case.find("error") is not None
                else "skipped"
                if case.find("skipped") is not None
                else "passed"
            )
            if priority[outcome] > priority.get(node_outcomes.get(identity, ""), 0):
                node_outcomes[identity] = outcome
        junit_counts = Counter(node_outcomes.values())
        if (
            counts["passed"] > junit_counts["passed"]
            or junit_counts["passed"] > counts["passed"] + counts["xpass"]
            or counts["failed"] > junit_counts["failed"]
            or counts["xpass"]
            > (
                junit_counts["passed"]
                - counts["passed"]
                + junit_counts["failed"]
                - counts["failed"]
            )
            or counts["skipped"] + counts["xfail"] > junit_counts["skipped"]
        ):
            raise ValueError("Python outcomes conflict with JUnit")
        identities = set(node_outcomes)
        if len(identities) < report["collected"] - counts["not_executed"]:
            raise ValueError("JUnit omits executed Python tests")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ElementTree.ParseError):
        # Never echo arbitrary artifact content, exceptions or local paths into the summary.
        return {
            "status": "invalid_evidence",
            "artifacts_valid": False,
            "reason": "Python/JUnit evidence missing, malformed or inconsistent",
        }
    return {**report, "artifacts_valid": True}


def build_report(root: Path, pytest_path: Path, junit_path: Path | None = None) -> dict[str, Any]:
    data_dir = root / "evaluation" / "scenarios" / "p0"
    matrix = load_json(data_dir / "matrix.json")
    oracle = load_json(data_dir / "ux03-oracle.json")
    plan = planned_cells(matrix, oracle)
    pytest_report = python_evidence(
        pytest_path,
        junit_path if junit_path is not None else pytest_path.with_name("python-junit.xml"),
    )
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    return {
        "schema_version": 1,
        "provenance": {
            "agent_sha": sha,
            "audit_base": matrix["source"]["audit_base"],
            "agent_worktree_dirty": bool(
                subprocess.check_output(
                    ["git", "-C", str(root), "status", "--porcelain=v1"], text=True
                ).strip()
            ),
            "merchant_sha": None,
            "psp_sha": None,
            "siblings_reason": "Not checked out or executed by this offline CI job",
            "input_sha256": {
                str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in (
                    data_dir / "matrix.json",
                    data_dir / "ux03-oracle.json",
                    root / "uv.lock",
                )
            },
        },
        "python_tests": pytest_report,
        "scenario_executions": {
            "counts": count_plan(plan, {}),
            "cells": {cell: "not_executed" for cell in plan},
            "reason": "P1 has no scenario execution adapter; pytest tests the harness only",
        },
        "integration": {"status": "not_executed", "reason": "Requires nightly live-stack evidence"},
        "llm_real": {"status": "not_executed"},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--pytest-report", type=Path, required=True)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.root.resolve(), args.pytest_report, args.junit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    text = (
        "### P0 — estado de ejecución\n\n"
        + "Python (producto + arneses; no equivale a escenarios): "
        + json.dumps(report["python_tests"], ensure_ascii=False)
        + "\n\nEscenarios: "
        + json.dumps(report["scenario_executions"]["counts"])
        + "\n\nIntegración: **not_executed** en este job. "
        + "LLM real: **not_executed**. Consultar evidencia independiente de nightly.\n"
    )
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(text, encoding="utf-8")
    if not report["python_tests"]["artifacts_valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
