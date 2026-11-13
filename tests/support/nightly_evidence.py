"""Pinned live-suite gate. Reports executions, never new P0 scenario observations."""

import argparse
import hashlib
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

from commerce_evaluation.scenarios_p0 import python_evidence

PLAN = Path(__file__).with_name("nightly-required-cases.json")
STATUSES = ("passed", "failed", "error", "skipped", "blocked", "not_executed")


def contract() -> dict[str, Any]:
    return json.loads(PLAN.read_text(encoding="utf-8"))


def expected_cases(scope: str) -> list[str]:
    scope = scope or "full"
    if scope not in {"full", "p0-catalog-bridge"}:
        raise ValueError("Unsupported nightly profile")
    return contract()[scope]


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def preflight(scope: str) -> dict[str, Any]:
    """No service starts here; refs, clean sources and HNP dependencies must be demonstrated."""
    plan = contract()
    result: dict[str, Any] = {
        "schema": "nightly-manifest/1",
        "scope": scope,
        "profile": "historical-siblings",
        "status": "blocked",
        "sources": {},
        "required_cases": expected_cases(scope),
        "scenario_cells_executed": 0,
    }
    try:
        if os.name != "posix":
            raise ValueError("POSIX runner required")
        if any(os.environ.get(key) == "1" for key in ("HNP_LIVE", "RUN_LIVE_LLM")):
            raise ValueError("Real model execution is forbidden")
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"):
            raise ValueError("Model credential is not permitted in the live job")
        for role, root, sha in (
            ("agent", Path.cwd(), os.environ["GITHUB_SHA"]),
            ("merchant", Path(os.environ["MERCHANT_REPO"]), plan["merchant_sha"]),
            ("payment", Path(os.environ["PAYMENT_REPO"]), plan["payment_sha"]),
        ):
            actual = git(root, "rev-parse", "HEAD")
            clean = not git(root, "status", "--porcelain=v1")
            result["sources"][role] = {
                "sha": actual,
                "clean": clean,
                "uv_lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
            }
            if actual != sha or not clean:
                raise ValueError("Pinned clean source required: " + role)
        for variable, path, marker in (
            ("MERCHANT_REPO", "src/commerce_lab/ap2_checkout.py", 'CHAIN_SEPARATOR = "~~"'),
            ("PAYMENT_REPO", "src/tesis_payment_provider/ap2.py", "def is_mandate_chain("),
        ):
            if marker not in (Path(os.environ[variable]) / path).read_text(encoding="utf-8"):
                raise ValueError("Required HNP dependency missing")
        result["status"] = "verified"
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        result["reason"] = str(error) if isinstance(error, ValueError) else type(error).__name__
    return result


def summarize(scope: str, junit: Path, outcomes: Path, evidence: Path) -> dict[str, Any]:
    scope = scope or "full"
    expected = expected_cases(scope)
    issues: list[str] = []
    nodes: list[ET.Element] = []
    try:
        nodes = ET.parse(junit).getroot().findall(".//testcase")
    except (OSError, ET.ParseError):
        issues.append("junit_missing_or_invalid")
    observed: dict[str, str] = {}
    for node in nodes:
        nodeid = node.get("classname", "").replace(".", "/") + ".py::" + node.get("name", "")
        if nodeid in observed:
            issues.append("duplicate_testcase")
        observed[nodeid] = (
            "error"
            if node.find("error") is not None
            else "failed"
            if node.find("failure") is not None
            else "skipped"
            if node.find("skipped") is not None
            else "passed"
        )
    if set(observed) - set(expected):
        issues.append("unexpected_testcases")
    results = [{"nodeid": node, "status": observed.get(node, "not_executed")} for node in expected]
    counts: dict[str, int] = dict.fromkeys(STATUSES, 0)
    counts.update(Counter(row["status"] for row in results))
    python = python_evidence(outcomes, junit)
    if not python.get("artifacts_valid") or python.get("exit_code") != 0:
        issues.append("python_evidence_incomplete_or_failed")
    if any(python.get("counts", {}).get(key, 0) for key in ("xfail", "xpass", "skipped")):
        issues.append("required_test_omitted_or_expected_failure")
    records: list[dict[str, Any]] = []
    files = sorted({p.resolve() for p in evidence.rglob("evidence.jsonl")})
    seen: set[str] = set()
    for path in files:
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:  # Actions materializes pytest's `current` symlink as duplicate bytes.
            continue
        seen.add(digest)
        try:
            rows = [json.loads(line) for line in data.decode().splitlines()]
        except (UnicodeError, json.JSONDecodeError):
            issues.append("evidence_invalid")
            continue
        if not any(r.get("scenario") == "bootstrap" for r in rows):
            issues.append("bootstrap_evidence_missing")
        endings = [r for r in rows if r.get("scenario") == "shutdown"]
        if len(endings) != 1 or not all(
            endings[0].get(k) is True for k in ("proxy_closed", "postgres_removed")
        ):
            issues.append("stack_cleanup_unverified")
        records.extend(rows)
    if not records:
        issues.append("live_evidence_missing")
    pin_rows = [r for r in records if r.get("scenario") == "P2-source-pins"]
    if (
        len(pin_rows) != 1
        or pin_rows[0].get("merchant_sha") != contract()["merchant_sha"]
        or (pin_rows[0].get("psp_sha") != contract()["payment_sha"])
    ):
        issues.append("p2_source_evidence_missing_or_incorrect")
    for case in ("P2-PROV-01", "P2-PROV-02", "P2-PROV-03"):
        matching = [r for r in records if r.get("scenario") == case]
        if len(matching) != 1 or matching[0].get("status") != "passed":
            issues.append(case + ":not_passed")
        elif case != "P2-PROV-01" and matching[0].get("cleanup_verified") is not True:
            issues.append(case + ":cleanup_unverified")
    return {
        "schema": "nightly-validation/1",
        "scope": scope,
        "profile": "historical-siblings",
        "validated": counts["passed"] == len(expected) and not issues,
        "collected": len(nodes),
        "required": len(expected),
        "counts": counts,
        "issues": sorted(set(issues)),
        "cases": results,
        "python": python,
        "scenario_cells_executed": 0,
        "ux03_validated": False,
        "p2_evidence": [
            {
                "case": r["scenario"],
                "status": r.get("status"),
                "cleanup_verified": r.get("cleanup_verified"),
            }
            for r in records
            if str(r.get("scenario", "")).startswith("P2-PROV-")
        ],
        "evidence_sha256": sorted(seen),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", default="full")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--junit", type=Path)
    parser.add_argument("--outcomes", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.preflight:
        result = preflight(args.scope)
        valid = result["status"] == "verified"
    else:
        assert args.junit and args.outcomes and args.evidence and args.manifest
        result = summarize(args.scope, args.junit, args.outcomes, args.evidence)
        try:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {"status": "not_executed"}
        result["manifest"] = manifest
        if manifest.get("status") != "verified" or manifest.get("scope") != args.scope:
            result["issues"].append("preflight_not_verified")
            result["validated"] = False
        result["validation_status"] = (
            "blocked"
            if manifest.get("status") != "verified"
            else "passed"
            if result["validated"]
            else "failed"
        )
        valid = result["validated"]
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if not args.preflight:
        paths = {
            p.resolve()
            for p in args.evidence.rglob("*")
            if p.is_file() and p.suffix in {".jsonl", ".log", ".png"}
        }
        paths |= {
            p.resolve()
            for p in (args.output, args.manifest, args.junit, args.outcomes)
            if p.is_file()
        }
        sums = "".join(
            hashlib.sha256(p.read_bytes()).hexdigest()
            + "  "
            + p.relative_to(args.output.parent).as_posix()
            + "\n"
            for p in sorted(paths)
        )
        args.output.with_name("nightly-SHA256SUMS").write_text(sums, encoding="utf-8")
    print(
        json.dumps(
            {
                "scope": args.scope,
                "validated": valid,
                "counts": result.get("counts"),
                "issues": result.get("issues", []),
                "scenario_cells_executed": 0,
            }
        )
    )
    if not valid:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
