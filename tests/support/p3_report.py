"""Fail closed on missing cases, skips, setup failures or unverified cleanup."""

import argparse
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from tests.support.p3_contract import (
    AGENT_BASE,
    CASES,
    INTEGRATED_AGENT_BASE,
    INTEGRATED_PRODUCT_TREE,
    MERCHANT,
    PSP,
)

PROTOCOL_CASES = {
    "PV-05-06-08": "test_protocol_contract_pv05_06_08",
    "PV-07": "test_protocol_contract_pv07",
    "PV-10": "test_protocol_contract_pv10",
}


def report(
    junit: Path,
    evidence: Path,
    expected_agent: str | None = None,
    *,
    protocol_gate: bool = False,
) -> dict:
    nodes = ET.parse(junit).getroot().findall(".//testcase") if junit.is_file() else []
    records = {}
    manifests, shutdowns, psp_shutdowns = [], [], []
    event = "P3-PV-case" if protocol_gate else "P3-V01-case"
    tests = (
        PROTOCOL_CASES
        if protocol_gate
        else {case: f"test_offer_checkout_contract[{case}]" for case in CASES}
    )
    for path in sorted({p.resolve() for p in evidence.rglob("evidence.jsonl")}):
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("scenario") == event:
                key = row["case_id"]
                if key in records:
                    raise ValueError("Duplicate case evidence")
                records[key] = row
            elif row.get("scenario") == "P3-V01-manifest":
                manifests.append(row)
            elif row.get("scenario") == "shutdown":
                shutdowns.append(row)
            elif row.get("scenario") == "P3-V01-psp-shutdown":
                psp_shutdowns.append(row)
    results = []
    for case, test_name in tests.items():
        matching = [n for n in nodes if n.get("name") == test_name]
        row = records.get(case, {})
        status = "not_executed"
        if matching:
            assert len(matching) == 1, "Duplicate JUnit case"
            node = matching[0]
            status = (
                "error"
                if node.find("error") is not None
                else "failed"
                if node.find("failure") is not None
                else "skipped"
                if node.find("skipped") is not None
                else "passed"
            )
            if status == "passed" and (
                row.get("status") != "passed" or not row.get("cleanup_verified")
            ):
                status = "blocked"
        results.append(
            {
                "case_id": case,
                "status": status,
                "evidence_status": row.get("status", "not_executed"),
                "commercial": row.get("commercial", "unknown"),
                "cleanup_verified": row.get("cleanup_verified", False),
            }
        )
    counts = {key: 0 for key in ("passed", "failed", "error", "skipped", "blocked", "not_executed")}
    counts.update(Counter(row["status"] for row in results))
    issues = []
    manifest = manifests[0] if len(manifests) == 1 else {}
    pins = manifest.get("pins", {})
    agent_sha = manifest.get("agent_sha", "")
    if not (
        re.fullmatch(r"[0-9a-f]{40}", agent_sha)
        and (expected_agent is None or agent_sha == expected_agent)
        and manifest.get("historical_agent_base") == AGENT_BASE
        and manifest.get("agent_base") == INTEGRATED_AGENT_BASE
        and manifest.get("product_tree")
        == manifest.get("base_product_tree")
        == INTEGRATED_PRODUCT_TREE
        and manifest.get("required_cases") == list(CASES)
        and manifest.get("llm") is False
        and manifest.get("payments") is False
        and all(
            pins.get(role, {}).get("sha") == sha
            and re.fullmatch(r"[0-9a-f]{64}", pins[role].get("lock_sha256", ""))
            for role, sha in (("MERCHANT_REPO", MERCHANT), ("PAYMENT_REPO", PSP))
        )
    ):
        issues.append("manifest_unverified")
    if (
        len(shutdowns) != 1
        or shutdowns[0].get("proxy_closed") is not True
        or shutdowns[0].get("postgres_removed") is not True
        or len(psp_shutdowns) != 1
        or psp_shutdowns[0].get("psp_stopped") is not True
    ):
        issues.append("stack_cleanup_unverified")
    if protocol_gate and set(records) != set(tests):
        issues.append("protocol_evidence_incomplete")
    result = {
        "schema": "p3-point5-report/1" if protocol_gate else "p3-v01-report/1",
        "p3_status": "BLOCKED",
        "collected": len(nodes),
        "counts": counts,
        "cases": results,
        "manifest": manifest,
        "issues": issues,
        "validated": counts["passed"] == len(tests) and len(nodes) == len(tests) and not issues,
    }
    if protocol_gate:
        result["shutdown"] = shutdowns
        result["sources"] = manifests
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--protocol-gate", action="store_true")
    args = parser.parse_args()
    result = report(
        args.junit, args.evidence, os.environ.get("GITHUB_SHA"), protocol_gate=args.protocol_gate
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    paths = {p.resolve() for p in args.evidence.rglob("evidence.jsonl")}
    paths |= {p.resolve() for p in (args.junit, args.output) if p.is_file()}
    if args.protocol_gate:
        for pattern in ("*.log", "*.jsonl"):
            paths |= {p.resolve() for p in args.evidence.rglob(pattern)}
    sums = "".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(args.output.parent)}\n"
        for p in sorted(paths)
    )
    hash_file = "p3-point5-SHA256SUMS" if args.protocol_gate else "p3-v01-SHA256SUMS"
    args.output.with_name(hash_file).write_text(sums, encoding="utf-8")
    print(
        json.dumps(
            {
                "validated": result["validated"],
                "counts": result["counts"],
                "issues": result["issues"],
                "p3": "BLOCKED",
            }
        )
    )
    if not result["validated"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
