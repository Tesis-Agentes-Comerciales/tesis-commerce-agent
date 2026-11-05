"""Fail closed on missing cases, skips, setup failures or unverified cleanup."""

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from tests.support.p3_contract import CASES

PROTOCOL_CASES = {
    "PV-05-06-08": "test_protocol_contract_pv05_06_08",
    "PV-07": "test_protocol_contract_pv07",
    "PV-10": "test_protocol_contract_pv10",
}


def report(junit: Path, evidence: Path, *, protocol_gate: bool = False) -> dict:
    nodes = ET.parse(junit).getroot().findall(".//testcase") if junit.is_file() else []
    records = {}
    shutdown = []
    manifests = []
    event = "P3-PV-case" if protocol_gate else "P3-V01-case"
    tests = (
        PROTOCOL_CASES
        if protocol_gate
        else {case: f"test_offer_checkout_contract[{case}]" for case in CASES}
    )
    for path in sorted({p.resolve() for p in evidence.rglob("evidence.jsonl")}):
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("scenario") == "shutdown":
                shutdown.append(row)
            if row.get("scenario") == "P3-V01-manifest":
                manifests.append(row)
            if row.get("scenario") == event:
                key = row["case_id"]
                if key in records:
                    raise ValueError("Duplicate case evidence")
                records[key] = row
    results = []
    for case, test in tests.items():
        matching = [n for n in nodes if n.get("name") == test]
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
    result = {
        "schema": "p3-point5-report/1" if protocol_gate else "p3-v01-report/1",
        "p3_status": "BLOCKED",
        "collected": len(nodes),
        "counts": counts,
        "cases": results,
        "validated": counts["passed"] == len(tests) and len(nodes) == len(tests),
    }
    if protocol_gate:
        result["shutdown"] = shutdown
        result["sources"] = manifests
        result["validated"] &= (
            set(records) == set(tests)
            and len(manifests) == 1
            and len(shutdown) == 1
            and shutdown[0].get("proxy_closed") is True
            and shutdown[0].get("postgres_removed") is True
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--protocol-gate", action="store_true")
    args = parser.parse_args()
    result = report(args.junit, args.evidence, protocol_gate=args.protocol_gate)
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
    name = "p3-point5-SHA256SUMS" if args.protocol_gate else "p3-v01-SHA256SUMS"
    args.output.with_name(name).write_text(sums, encoding="utf-8")
    print(
        json.dumps({"validated": result["validated"], "counts": result["counts"], "p3": "BLOCKED"})
    )
    if not result["validated"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
