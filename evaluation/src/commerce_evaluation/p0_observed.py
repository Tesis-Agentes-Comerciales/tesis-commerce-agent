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
    "not_applicable": "none",
}
RUN_KINDS = {"measurement", "harness_error", "reporting_error", "infrastructure", "blocked"}
CT19_INDEX = "../../results/ct19-hp/runs.json"
CT19_RUN = "42033289049"
CT19_FIXTURE = "41967635493"
INF33_SOURCE = {
    "path": "docs/design/p0-model-failures.md",
    "section": "4. Celdas y oráculos",
    "sha256": "0b70704c51f6b27ceb73f1e5f7a8be9d16cac16d08cf41e7969d6c1957492e1c",
}


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


def validate_ct19_archive(root: Path, run: str, row: dict, protocol_path: Path) -> tuple[dict, int]:
    archive_ref = f"runs/{run}/original.zip"
    require(
        row["url"] == ACTION_RUN + run and row["original_zip"] == archive_ref,
        f"CT19 archive reference mismatch: {run}",
    )
    archive = root / "evaluation/results/ct19-hp" / archive_ref
    data = archive.read_bytes()
    require(
        len(data) == row["bytes"] and digest(data) == row["zip_sha256"], f"CT19 ZIP hash: {run}"
    )
    require(
        digest(protocol_path.read_bytes()) == row["protocol_sha256"],
        f"CT19 protocol hash: {run}",
    )
    protocol = load(protocol_path)
    with ZipFile(archive) as bundle:
        names = bundle.namelist()
        require(
            len(names) == len(set(names)) and bundle.testzip() is None, f"Corrupt CT19 ZIP: {run}"
        )
        inner = checksums(bundle.read("exp01-SHA256SUMS"))
        require(len(inner) == row["entries_verified"], f"CT19 hash count: {run}")
        for name, expected in inner.items():
            require(digest(bundle.read(name)) == expected, f"CT19 internal hash: {run}/{name}")
        for name, field in (
            ("exp01-manifest.json", "manifest_sha256"),
            ("exp01-junit.xml", "junit_sha256"),
            ("exp01-report.json", "report_sha256"),
        ):
            require(inner.get(name) == row[field], f"CT19 index hash: {run}/{name}")
        manifest = json.loads(bundle.read("exp01-manifest.json"))
        report = json.loads(bundle.read("exp01-report.json"))
        junit = bundle.read("exp01-junit.xml")
        tests = [
            (node.get("classname"), node.get("name"))
            for node in ElementTree.fromstring(junit).iter("testcase")
        ]
        module = protocol["test_module"].removesuffix(".py::").replace("/", ".")
        require(
            sorted(tests) == sorted((module, case["test"]) for case in protocol["cases"].values())
            and junit_counts(junit) == report["junit"] == row["junit"],
            f"CT19 JUnit mismatch: {run}",
        )
        require(
            report["manifest"] == manifest
            and manifest["status"] == "verified"
            and manifest["sources"] == row["sources"]
            and manifest["protocol_sha256"] == row["protocol_sha256"]
            and manifest["inputs_sha256"]
            == {
                "evaluation/scenarios/p0/matrix.json": protocol["matrix_sha256"],
                "evaluation/scenarios/p0/ux03-oracle.json": protocol["ux03_oracle_sha256"],
            }
            and set(manifest["sources"]) == {"agent", "merchant", "payment"}
            and all(
                source["clean"] is True
                and re.fullmatch(r"[a-f0-9]{40}", source["sha"])
                and re.fullmatch(r"[a-f0-9]{64}", source["uv_lock_sha256"])
                for source in manifest["sources"].values()
            )
            and manifest["sources"]["merchant"]["sha"] == protocol["merchant_sha"]
            and manifest["sources"]["payment"]["sha"] == protocol["payment_sha"],
            f"CT19 source manifest mismatch: {run}",
        )
        require(
            set(report["controls"]) == set(protocol["cases"])
            and report["validated"] is row["validated"]
            and report["control_counts"] == row["control_counts"]
            and report["issues"] == row["issues"]
            and report["stack_shutdown"] == row["shutdown"],
            f"CT19 outcome mismatch: {run}",
        )
        if report["validated"]:
            frozen_cases = set()
            for name in inner:
                if not name.endswith("/exp01-frozen.json"):
                    continue
                frozen = json.loads(bundle.read(name))
                case = frozen["case_id"]
                control = report["controls"][case]
                observed_name = name.replace("/exp01-frozen.json", "/exp01-observed.json")
                require(
                    case not in frozen_cases
                    and inner[name] == control["input_sha256"]
                    and observed_name in inner
                    and json.loads(bundle.read(observed_name)) == control
                    and frozen["protocol_sha256"] == row["protocol_sha256"]
                    and frozen["oracle"] == protocol["cases"][case]
                    and frozen["frozen_at_ns"] < control["started_at_ns"]
                    and frozen["semantic_evaluation"] is False
                    and control["status"] == "passed"
                    and control["cleanup_verified"] is True,
                    f"CT19 frozen evidence mismatch: {run}/{case}",
                )
                frozen_cases.add(case)
            require(
                frozen_cases == protocol["cases"].keys(), f"Missing CT19 frozen evidence: {run}"
            )
    return report, len(inner)


def validate_ct19(root: Path, cell: dict, matrix_sha: str, oracle_sha: str) -> tuple[int, int]:
    require(
        cell["evidence"] == CT19_INDEX
        and str(cell["run_id"]) == CT19_RUN
        and cell["case"] == "CT19:HP:removed"
        and cell["status"] == "passed"
        and cell["scope"].strip(),
        "Invalid CT19 HP attribution",
    )
    index = load(root / P0 / CT19_INDEX)
    fixture_runs = {"41967362290", CT19_FIXTURE}
    hp_runs = {"41968306631", "41968564766", CT19_RUN}
    require(set(index) == fixture_runs | hp_runs, "CT19 run history incomplete")
    fixture_path = root / "evaluation/scenarios/ct19-fixture.json"
    hp_path = root / "evaluation/scenarios/ct19-hp.json"
    for path in (fixture_path, hp_path):
        protocol = load(path)
        require(
            protocol["matrix_sha256"] == matrix_sha
            and protocol["ux03_oracle_sha256"] == oracle_sha,
            "CT19 prereregistration mismatch",
        )
    fixture = index[CT19_FIXTURE]
    require(
        load(hp_path)["fixture_prerequisite"]
        == {
            "run_id": int(CT19_FIXTURE),
            "agent_sha": fixture["sources"]["agent"]["sha"],
            "zip_sha256": fixture["zip_sha256"],
            "protocol_sha256": fixture["protocol_sha256"],
            "validated": True,
        }
        and fixture["validated"] is True,
        "CT19 fixture prerequisite mismatch",
    )
    verified = {
        run: validate_ct19_archive(root, run, row, fixture_path if run in fixture_runs else hp_path)
        for run, row in index.items()
    }
    report = verified[CT19_RUN][0]
    require(
        cell["agent_sha"] == index[CT19_RUN]["sources"]["agent"]["sha"]
        and report["validated"] is True
        and not report["issues"]
        and report["junit"] == {"passed": 2, "failed": 0, "error": 0, "skipped": 0}
        and report["attribution_status"] == "verified"
        and report["measured_modes"] == {"CT19": ["HP"], "CT19-STOCK": ["HP"]}
        and report["attributed_scenarios"] == {"CT19": "passed"}
        and report["non_scenario_controls"] == {"CT19-STOCK": "passed"}
        and report["stack_shutdown"]
        and all(
            end["postgres_removed"] and end["proxy_closed"] for end in report["stack_shutdown"]
        ),
        "CT19 terminal attribution or cleanup mismatch",
    )
    for case, status, code in (
        ("CT19:HP:removed", 404, "invalid_item"),
        ("CT19:HP:stock_zero", 409, "out_of_stock"),
    ):
        control = report["controls"][case]
        facts = control["observations"]
        offer_id = facts["catalog_before"]["row"]["offer_id"]
        changed = facts["catalog_changed"]["row"]
        calls = [
            c
            for c in facts["acp_calls"]
            if c["method"] == "POST" and c["path"] == "/checkout_sessions"
        ]
        require(
            control["stimulus_verified"] is True
            and len(calls) == facts["create_posts"] == 1
            and calls[0]["status"] == status
            and calls[0]["fault"] == "pass"
            and calls[0]["response"]["detail"]["code"] == facts["agent_result"] == code
            and calls[0]["request"]["line_items"] == [{"id": offer_id}]
            and facts["pre_mutation_agent"]["d1"] == [["decision_record", "allow"]]
            and facts["d1_observed_at_ns"] < facts["mutation_verified_at_ns"]
            and facts["agent_after"]["purchase"] == [["failed", offer_id, None, None, code]]
            and facts["agent_after"]["dispatch"]
            == [["definitive_failure", None, "VERIFIED_NOT_CREATED"]]
            and (
                changed is None
                if status == 404
                else changed["offer_id"] == offer_id
                and changed["snapshot"]["available_quantity"] == 0
            )
            and all(n == 0 for n in facts["agent_effect_counts"].values())
            and not facts["after_cleanup"]["checkouts"]
            and facts["psp_before"] == facts["psp_after"] == facts["psp_after_cleanup"]
            and facts["catalog_restored"]["row"] == facts["catalog_before"]["row"],
            f"CT19 native observation mismatch: {case}",
        )
    return len(verified), sum(count for _, count in verified.values())


def validate_not_applicable(root: Path, key: tuple[str, str], cell: dict) -> None:
    require(key == ("INF33", "HP"), "Unauthorized not_applicable pair")
    require(
        set(cell) == {"status", "justification", "source"}
        and isinstance(cell["justification"], str)
        and cell["justification"].strip(),
        "not_applicable requires justification and no execution evidence",
    )
    require(cell["source"] == INF33_SOURCE, "Unapproved not_applicable source")
    path = root / INF33_SOURCE["path"]
    require(preregistration_digest(path) == INF33_SOURCE["sha256"], "not_applicable source hash")
    section = path.read_text(encoding="utf-8").split("\n## 4. Celdas y oráculos\n", 1)[1]
    require(
        "**INF33-HP no aplica:** en HP no hay revisor LLM; la aprobación la hace el comprador."
        in section.split("\n## ", 1)[0],
        "Source does not declare INF33 HP not applicable",
    )


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
    preregistered_cells = planned_cells(matrix, oracle)
    runs = data["runs"]
    verified = {
        run: validate_run(root, run, row, matrix_sha, oracle_sha) for run, row in runs.items()
    }
    statuses = dict.fromkeys(plan, "not_executed")
    ct19_runs, ct19_hashes = 0, 0
    for scenario, row in data["observations"].items():
        require(row["modes"], f"Missing modes: {scenario}")
        for mode, cell in row["modes"].items():
            key = (scenario, mode)
            require(key in plan, f"Mode outside P0: {scenario}/{mode}")
            if cell == {"status": "not_executed"}:
                continue
            status = cell["status"]
            if status == "not_applicable":
                validate_not_applicable(root, key, cell)
                require(
                    not any(
                        (case["scenario"], case["mode"]) == key
                        for protocol, _, _ in verified.values()
                        for case in protocol["cases"].values()
                    ),
                    "not_applicable cannot hide a registered execution",
                )
                statuses[key] = status
                continue
            if key == ("CT19", "HP"):
                ct19_runs, ct19_hashes = validate_ct19(root, cell, matrix_sha, oracle_sha)
                statuses[key] = status
                continue
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
        "runs": len(runs) + ct19_runs,
        "internal_hashes": sum(info[2] for info in verified.values()) + ct19_hashes,
        "denominators": {
            "scenarios": len(matrix["scenarios"]),
            "scenario_modes": len(plan),
            "measurable_scenario_modes": len(plan) - by_status["not_applicable"],
            "preregistered_cells": len(preregistered_cells),
        },
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
        "not_applicable": sorted(
            f"{scenario}/{mode}"
            for (scenario, mode), value in statuses.items()
            if value == "not_applicable"
        ),
        "scenarios_without_execution": sorted(
            {scenario for scenario, _ in plan}
            - {
                scenario
                for (scenario, _), status in statuses.items()
                if status not in {"not_executed", "not_applicable"}
            }
        ),
    }


if __name__ == "__main__":
    print(json.dumps(validate(Path.cwd()), indent=2))
