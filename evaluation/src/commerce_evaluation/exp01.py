"""EXP-01 preflight and evidence gate, over the existing P0 report contract and live stack.

Stdlib-only so setup failures can still publish blocked/not-executed observations.
"""

import argparse
import hashlib
import json
import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .scenarios_p0 import count_plan, planned_cells

ROOT = Path(__file__).resolve().parents[3]
PLAN = ROOT / "evaluation/scenarios/exp01-first-batch.json"
MODULE = "tests/integration/test_exp01_first_batch.py::"
NON_SCENARIO_CONTROLS = frozenset({"AUTH-TRANSPORT", "EXP06-INFRA"})


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def preflight(plan_path: Path = PLAN) -> dict[str, Any]:
    plan = read(plan_path)
    result: dict[str, Any] = {
        "schema": "exp01-preflight/1",
        "status": "blocked",
        "sources": {},
        "protocol_sha256": sha(plan_path),
        "inputs_sha256": {},
    }
    try:
        if (
            os.name != "posix"
            or any(os.environ.get(k) == "1" for k in ("HNP_LIVE", "RUN_LIVE_LLM"))
            or os.environ.get("OPENAI_API_KEY")
        ):
            raise ValueError("Isolated POSIX stack without model credentials/flags required")
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", plan["agent_base"], "HEAD"],
            cwd=ROOT,
            capture_output=True,
        )
        if ancestry.returncode:
            raise ValueError(
                "Approved base unavailable or not an ancestor; full Git history required"
            )
        for role, root, expected in (
            ("agent", ROOT, os.environ["GITHUB_SHA"]),
            ("merchant", Path(os.environ["MERCHANT_REPO"]), plan["merchant_sha"]),
            ("payment", Path(os.environ["PAYMENT_REPO"]), plan["payment_sha"]),
        ):
            source = {
                "sha": git(root, "rev-parse", "HEAD"),
                "clean": not git(root, "status", "--porcelain"),
                "uv_lock_sha256": sha(root / "uv.lock"),
            }
            result["sources"][role] = source
            if source["sha"] != expected or not source["clean"]:
                raise ValueError("Pinned clean checkout required: " + role)
        for name, expected in (
            ("evaluation/scenarios/p0/matrix.json", plan["matrix_sha256"]),
            ("evaluation/scenarios/p0/ux03-oracle.json", plan["ux03_oracle_sha256"]),
        ):
            result["inputs_sha256"][name] = sha(ROOT / name)
            if result["inputs_sha256"][name] != expected:
                raise ValueError("Preregistered source changed: " + name)
        if (
            sha(ROOT / "uv.lock")
            != "3b9396ebc12f4306e31739df1f0b9ecea885135c35b17cc768ad12a3bce1e7b9"
        ):
            raise ValueError("Agent lock changed from approved base")
        for env, file, marker in (
            ("MERCHANT_REPO", "src/commerce_lab/ap2_checkout.py", 'CHAIN_SEPARATOR = "~~"'),
            ("PAYMENT_REPO", "src/tesis_payment_provider/ap2.py", "def is_mandate_chain("),
        ):
            if marker not in (Path(os.environ[env]) / file).read_text():
                raise ValueError("Required HNP dependency unavailable")
        result["status"] = "verified"
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        result["reason"] = str(error) if isinstance(error, ValueError) else type(error).__name__
    return result


def report(
    evidence: Path,
    junit: Path,
    outcomes: Path,
    manifest: Path,
    *,
    plan_path: Path = PLAN,
    scenario: str | None = None,
) -> dict[str, Any]:
    plan = read(plan_path)
    cases = {
        key: value
        for key, value in plan["cases"].items()
        if scenario is None or value["scenario"] == scenario
    }
    if not cases:
        raise ValueError("Unknown or empty experimental scenario")
    module = plan.get("test_module", MODULE)
    problems: list[str] = []
    try:
        pre = read(manifest)
    except (OSError, ValueError):
        pre = {"status": "blocked", "reason": "preflight_not_executed"}
    if pre.get("status") != "verified" or pre.get("protocol_sha256") != sha(plan_path):
        problems.append("preflight_not_verified")
    try:
        nodes = ET.parse(junit).getroot().findall(".//testcase")
        recorder = read(outcomes)
    except (OSError, ValueError, ET.ParseError):
        nodes, recorder = [], {}
        problems.append("pytest_evidence_missing_or_invalid")
    expected = {module + value["test"] for value in cases.values()}
    observed = [
        n.get("classname", "").replace(".", "/") + ".py::" + n.get("name", "") for n in nodes
    ]
    if set(observed) != expected or len(observed) != len(expected):
        problems.append("required_controls_missing_duplicate_or_extra")
    junit_counts = {
        "failed": sum(n.find("failure") is not None for n in nodes),
        "skipped": sum(n.find("skipped") is not None for n in nodes),
        "error": sum(n.find("error") is not None for n in nodes),
    }
    junit_counts["passed"] = len(nodes) - sum(junit_counts.values())
    terminal = recorder.get("status") == "completed" and recorder.get("exit_code") == 0
    terminal |= (
        recorder.get("status") == "incomplete_or_failed"
        and recorder.get("exit_code") == 1
        and recorder.get("counts", {}).get("failed", 0) > 0
    )
    if (
        recorder.get("collected") != len(nodes)
        or not terminal
        or recorder.get("deselected") != 0
        or recorder.get("collection_errors") != 0
    ):
        problems.append("recorder_incomplete")
    for status in ("passed", "failed", "skipped"):
        if recorder.get("counts", {}).get(status) != junit_counts[status]:
            problems.append("junit_recorder_mismatch:" + status)
    if any(
        recorder.get("counts", {}).get(k, 0) for k in ("xfail", "xpass", "skipped", "not_executed")
    ):
        problems.append("required_control_omitted")
    records: dict[str, Any] = {}
    seen: set[str] = set()
    for path in evidence.rglob("exp01-observed.json"):
        if sha(path) in seen:
            continue
        seen.add(sha(path))
        row = read(path)
        case_id = row["case_id"]
        if case_id in records or case_id not in cases:
            problems.append("duplicate_or_unknown_control")
        frozen = path.with_name("exp01-frozen.json")
        if not frozen.is_file() or sha(frozen) != row.get("input_sha256"):
            problems.append("frozen_input_changed:" + case_id)
        else:
            original = read(frozen)
            if (
                original["protocol_sha256"] != sha(plan_path)
                or original["oracle"] != cases[case_id]
                or original["frozen_at_ns"] >= row["started_at_ns"]
            ):
                problems.append("oracle_or_temporal_provenance_invalid:" + case_id)
        if row.get("cleanup_verified") is not True:
            problems.append("cleanup_not_verified:" + case_id)
        observations = row.get("observations", {})
        required = {"merchant_before", "merchant_after", "psp_before", "psp_after"}
        required |= (
            {"trigger_status", "revoked_at_ns", "triggered_at_ns"}
            if case_id == "MA24"
            else {
                "agent_result",
                "agent_state",
                "after_cleanup",
                "idempotency_sha256",
                "create_posts",
            }
        )
        required = set(cases[case_id].get("required_observations", required))
        if row.get("status") == "passed" and not required <= observations.keys():
            problems.append("independent_observations_missing:" + case_id)
        records[case_id] = row
    controls = {
        key: records.get(
            key,
            {
                "case_id": key,
                "status": "blocked" if pre.get("status") != "verified" else "not_executed",
            },
        )
        for key in cases
    }
    scenarios = {}
    for scenario in dict.fromkeys(value["scenario"] for value in cases.values()):
        statuses = [
            controls[key]["status"] for key, value in cases.items() if value["scenario"] == scenario
        ]
        scenarios[scenario] = (
            "passed"
            if all(s == "passed" for s in statuses)
            else next(s for s in ("failed", "error", "blocked", "not_executed") if s in statuses)
        )
    shutdown: list[Any] = []
    event_hashes: set[str] = set()
    for path in evidence.rglob("evidence.jsonl"):
        if sha(path) in event_hashes:
            continue
        event_hashes.add(sha(path))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        ends = [row for row in rows if row.get("scenario") == "shutdown"]
        if len(ends) != 1 or not all(
            ends[0].get(k) is True for k in ("proxy_closed", "postgres_removed")
        ):
            problems.append("stack_cleanup_missing_or_incomplete")
        shutdown += ends
    if not shutdown:
        problems.append("stack_evidence_missing")
    p0 = planned_cells(
        read(ROOT / "evaluation/scenarios/p0/matrix.json"),
        read(ROOT / "evaluation/scenarios/p0/ux03-oracle.json"),
    )
    unknown_scenarios = set(scenarios) - set(p0) - NON_SCENARIO_CONTROLS
    if unknown_scenarios:
        problems.append("unregistered_scenario_or_control")
    # P0's counter does not admit blocked/error: keep those explicit, never relabel passed.
    attributable = {
        key: value
        for key, value in scenarios.items()
        if key in p0 and value in {"passed", "failed"}
    }
    valid = (
        not problems
        and all(s == "passed" for s in scenarios.values())
        and junit_counts["passed"] == len(expected)
        and recorder.get("exit_code") == 0
    )
    attributed = {
        key: "blocked" if value == "passed" and problems else value
        for key, value in scenarios.items()
        if key in p0
    }
    expanded_statuses = {key: attributed.get(key, "not_executed") for key in p0}
    return {
        "schema": "exp01-results/1",
        "validated": valid,
        "manifest": pre,
        "issues": sorted(set(problems)),
        "controls": controls,
        "scenarios": scenarios,
        "attributed_scenarios": attributed,
        "non_scenario_controls": {
            key: value for key, value in scenarios.items() if key in NON_SCENARIO_CONTROLS
        },
        "junit": junit_counts,
        "recorder": recorder,
        "control_counts": dict(Counter(row["status"] for row in controls.values())),
        "minimum_plan_counts": dict(Counter(expanded_statuses.values())),
        "p0_compatible_projection": count_plan(p0, attributable if not problems else {}),
        "projection_note": "P0 collapses blocked/error; minimum_plan_counts retains EXP-01 states",
        "attribution_status": "verified" if not problems else "blocked",
        "stack_shutdown": shutdown,
        "scope": plan["scope"],
        "measured_modes": {
            key: sorted({value["mode"] for value in cases.values() if value["scenario"] == key})
            for key in scenarios
        },
        "limitations": plan["limitations"],
        "ux03_executed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--protocol", type=Path, default=PLAN)
    parser.add_argument("--scenario")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--junit", type=Path)
    parser.add_argument("--outcomes", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.preflight:
        result = preflight(args.protocol)
        valid = result["status"] == "verified"
    else:
        assert args.evidence and args.junit and args.outcomes and args.manifest
        result = report(
            args.evidence,
            args.junit,
            args.outcomes,
            args.manifest,
            plan_path=args.protocol,
            scenario=args.scenario,
        )
        valid = result["validated"]
        logs = list(args.evidence.rglob("*.log"))
        if logs:
            # Services imply installed locked dependencies; setup-only reporting stays stdlib.
            from .redaction import redact

            for log in logs:
                original = log.read_text(errors="replace")
                sanitized = redact(original)
                if sanitized != original:
                    log.write_text(sanitized)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if not args.preflight:
        assert args.evidence
        files = sorted(
            {
                p.resolve()
                for p in args.evidence.rglob("*")
                if p.is_file() and p.suffix in {".json", ".jsonl", ".log"}
            }
            | {
                p.resolve()
                for p in (args.output, args.junit, args.outcomes, args.manifest)
                if p and p.is_file()
            }
        )
        args.output.with_name("exp01-SHA256SUMS").write_text(
            "".join(
                sha(p) + "  " + p.relative_to(args.output.parent).as_posix() + "\n" for p in files
            )
        )
    print(
        json.dumps(
            {
                "validated": valid,
                "scenarios": result.get("scenarios"),
                "issues": result.get("issues", []),
            }
        )
    )
    if not valid:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
