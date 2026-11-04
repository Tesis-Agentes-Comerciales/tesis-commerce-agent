"""Fail closed on missing cases, skips, setup failures or unverified cleanup."""

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from tests.support.p3_contract import CASES


def report(junit: Path, evidence: Path) -> dict:
    nodes = ET.parse(junit).getroot().findall(".//testcase") if junit.is_file() else []
    records = {}
    for path in sorted({p.resolve() for p in evidence.rglob("evidence.jsonl")}):
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("scenario") == "P3-V01-case":
                key = row["case_id"]
                if key in records:
                    raise ValueError("Duplicate case evidence")
                records[key] = row
    results = []
    for case in CASES:
        matching = [n for n in nodes if n.get("name") == f"test_offer_checkout_contract[{case}]"]
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
    return {
        "schema": "p3-v01-report/1",
        "p3_status": "BLOCKED",
        "collected": len(nodes),
        "counts": counts,
        "cases": results,
        "validated": counts["passed"] == len(CASES) and len(nodes) == len(CASES),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = report(args.junit, args.evidence)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    paths = {p.resolve() for p in args.evidence.rglob("evidence.jsonl")}
    paths |= {p.resolve() for p in (args.junit, args.output) if p.is_file()}
    sums = "".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(args.output.parent)}\n"
        for p in sorted(paths)
    )
    args.output.with_name("p3-v01-SHA256SUMS").write_text(sums, encoding="utf-8")
    print(
        json.dumps({"validated": result["validated"], "counts": result["counts"], "p3": "BLOCKED"})
    )
    if not result["validated"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
