"""Offline validation and counts for the P0 evidence register."""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import ZipFile

from .scenarios_p0 import planned_cells

P0 = Path("evaluation/scenarios/p0")
ACTION_RUN = "https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/"
STATUS_COVERAGE = {
    "passed": "bounded",
    "partial": "partial",
    "failed": "negative",
    "blocked": "incomplete",
    "harness_error": "incomplete",
    "not_executed": "none",
}
RUN_KINDS = {"measurement", "harness_error", "reporting_error", "infrastructure", "blocked"}


def require(condition: object, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load(path: Path) -> Any:
    return json.loads(path.read_bytes(), object_pairs_hook=unique_object)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def preregistration_digest(path: Path) -> str:
    # The two P0 input files have LF Git blobs but Windows may expand their checkout.
    return digest(path.read_bytes().replace(b"\r\n", b"\n"))


def checksums(data: bytes) -> dict[str, str]:
    entries = {}
    for line in data.decode("utf-8").splitlines():
        expected, name = line.split("  ", 1)
        require(name not in entries and re.fullmatch(r"[a-f0-9]{64}", expected), name)
        entries[name] = expected
    return entries


def repo_path(root: Path, ref: str, prefix: str) -> Path:
    path = (root / ref).resolve()
    require(path.is_relative_to(root / prefix), f"Invalid reference: {ref}")
    return path


def junit_counts(data: bytes) -> dict[str, int]:
    nodes = list(ElementTree.fromstring(data).iter("testcase"))
    counts = {
        "failed": sum(node.find("failure") is not None for node in nodes),
        "error": sum(node.find("error") is not None for node in nodes),
        "skipped": sum(node.find("skipped") is not None for node in nodes),
    }
    counts["passed"] = len(nodes) - sum(counts.values())
    return counts


def validate_run(
    root: Path, run_id: str, row: dict[str, Any], matrix_sha: str, oracle_sha: str
) -> tuple[dict[str, Any], dict[str, Any], int]:
    require(run_id.isdecimal() and row["actions"] == ACTION_RUN + run_id, "Invalid Actions run")
    require(row["kind"] in RUN_KINDS, f"Unknown run kind: {run_id}")
    protocol_path = repo_path(root, row["protocol"], "evaluation/scenarios")
    archive = repo_path(root, row["archive"], "evaluation/results")
    ledger = repo_path(root, row["checksum_index"], "evaluation/results")
    require(archive.name == f"{run_id}.zip", f"Wrong archive for run {run_id}")
    ledger_entries = checksums(ledger.read_bytes())
    require(
        ledger_entries.get(f"runs/{run_id}.zip") == row["sha256"]
        and digest(archive.read_bytes()) == row["sha256"],
        f"Archive hash mismatch: {run_id}",
    )
    protocol = load(protocol_path)
    require(
        digest(protocol_path.read_bytes()) == row["protocol_sha256"]
        and protocol["matrix_sha256"] == matrix_sha
        and protocol["ux03_oracle_sha256"] == oracle_sha,
        f"Frozen protocol mismatch: {run_id}",
    )
    with ZipFile(archive) as bundle:
        require(bundle.testzip() is None, f"Corrupt ZIP: {run_id}")
        manifest = json.loads(bundle.read("exp01-manifest.json"))
        names = set(bundle.namelist())
        inner = checksums(bundle.read("exp01-SHA256SUMS")) if "exp01-SHA256SUMS" in names else {}
        for name, expected in inner.items():
            require(digest(bundle.read(name)) == expected, f"Internal hash: {run_id}/{name}")
        junit = junit_counts(bundle.read("exp01-junit.xml")) if "exp01-junit.xml" in names else None
        report = (
            json.loads(bundle.read("exp01-report.json")) if "exp01-report.json" in names else {}
        )
    require(
        junit == row["junit"] and report.get("validated") == row["gate"],
        f"JUnit or evidence gate mismatch: {run_id}",
    )
    if report and junit is not None:
        require(report["junit"] == junit, f"Archived JUnit/report mismatch: {run_id}")
    require(
        manifest["protocol_sha256"] == row["protocol_sha256"]
        and manifest["status"] == row["preflight"]
        and re.fullmatch(r"[0-9a-f]{40}", row["sources"]["agent"]),
        f"Manifest mismatch: {run_id}",
    )
    if manifest["status"] == "verified":
        sources = manifest["sources"]
        require(
            all(sources[key]["clean"] is True for key in ("agent", "merchant", "payment"))
            and row["sources"] == {key: source["sha"] for key, source in sources.items()}
            and row["sources"]["merchant"] == protocol["merchant_sha"]
            and row["sources"]["payment"] == protocol["payment_sha"],
            f"Source SHA or cleanliness mismatch: {run_id}",
        )
    else:
        require(
            row["kind"] == "blocked"
            and row["sources"]["merchant"] is None
            and row["sources"]["payment"] is None,
            f"Unverified sources: {run_id}",
        )
    return protocol, report, len(inner)


def validate(root: Path, observed: dict[str, Any] | None = None) -> dict[str, Any]:
    root = root.resolve()
    matrix_path = root / P0 / "matrix.json"
    oracle_path = root / P0 / "ux03-oracle.json"
    matrix, oracle = load(matrix_path), load(oracle_path)
    data: dict[str, Any] = (
        load(root / P0 / "matrix-observed.json") if observed is None else observed
    )
    matrix_sha = preregistration_digest(matrix_path)
    oracle_sha = preregistration_digest(oracle_path)
    require(
        data["schema_version"] == 1
        and data["preregistration"] == matrix_path.name
        and data["preregistration_sha256"] == matrix_sha,
        "Preregistration mismatch",
    )
    plan = {(row["id"], mode) for row in matrix["scenarios"] for mode in row["modes"]}
    require(
        (len(matrix["scenarios"]), len(plan), len(planned_cells(matrix, oracle))) == (33, 56, 37),
        "P0 denominators changed",
    )
    runs = data["runs"]
    verified = {
        run: validate_run(root, run, row, matrix_sha, oracle_sha) for run, row in runs.items()
    }
    statuses = dict.fromkeys(plan, "not_executed")
    for scenario, row in data["observations"].items():
        require(row["modes"], f"Missing modes: {scenario}")
        for mode, cell in row["modes"].items():
            key = (scenario, mode)
            require(key in plan, f"Mode outside P0: {scenario}/{mode}")
            if cell == {"status": "not_executed"}:
                continue
            status = cell["status"]
            require(
                STATUS_COVERAGE.get(status) == cell["coverage"]
                and cell["scope"].strip()
                and cell["limitation"].strip(),
                f"Inconsistent status/scope: {scenario}/{mode}",
            )
            run = str(cell["run_id"])
            protocol, report, hashes = verified[run]
            case_ids = [
                case_id
                for case_id, case in protocol["cases"].items()
                if case["scenario"] == scenario and case["mode"] == mode
            ]
            require(
                runs[run]["kind"] == "measurement" and hashes and case_ids,
                f"No attributable evidence: {scenario}/{mode}",
            )
            require(
                report["attribution_status"] == "verified"
                and all(
                    report["controls"].get(case_id, {}).get("cleanup_verified") is True
                    for case_id in case_ids
                )
                and report["stack_shutdown"]
                and all(
                    end["postgres_removed"] and end["proxy_closed"]
                    for end in report["stack_shutdown"]
                ),
                f"Missing measured mode or cleanup: {scenario}/{mode}",
            )
            raw_status = report["scenarios"][scenario]
            accepted = {
                "passed": {"passed"},
                "partial": {"passed"},
                "failed": {"failed"},
                "blocked": {"blocked", "error"},
                "harness_error": {"error"},
                "not_executed": {"not_executed"},
            }[status]
            require(raw_status in accepted, f"Outcome mismatch: {scenario}/{mode}")
            if status in {"passed", "partial", "failed"}:
                require(
                    report["attributed_scenarios"].get(scenario) == raw_status,
                    f"Scenario lacks verified attribution: {scenario}/{mode}",
                )
            require(
                len(set(cell["prior_attempts"])) == len(cell["prior_attempts"]),
                f"Duplicate prior attempt: {scenario}/{mode}",
            )
            for attempt in cell["prior_attempts"]:
                prior = str(attempt)
                require(
                    prior != run
                    and runs[prior]["experiment"] == runs[run]["experiment"]
                    and any(
                        case["scenario"] == scenario and case["mode"] == mode
                        for case in verified[prior][0]["cases"].values()
                    ),
                    f"Unrelated prior attempt: {scenario}/{mode}",
                )
            statuses[key] = status
    by_status = Counter(statuses.values())
    return {
        "runs": len(runs),
        "internal_hashes": sum(info[2] for info in verified.values()),
        "denominators": {"scenarios": 33, "scenario_modes": 56, "preregistered_cells": 37},
        "scenario_modes": {status: by_status[status] for status in STATUS_COVERAGE},
        "distinct_scenarios_by_status": {
            status: len({scenario for (scenario, _), value in statuses.items() if value == status})
            for status in STATUS_COVERAGE
        },
        "not_executed": sorted(
            f"{scenario}/{mode}"
            for (scenario, mode), value in statuses.items()
            if value == "not_executed"
        ),
        "scenarios_without_execution": sorted(
            {scenario for scenario, _ in plan}
            - {scenario for (scenario, _), status in statuses.items() if status != "not_executed"}
        ),
    }


if __name__ == "__main__":
    print(json.dumps(validate(Path.cwd()), indent=2))
