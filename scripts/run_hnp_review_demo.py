"""Offline fictitious demonstration. No runtime, credentials or operational DBs."""

import argparse
import json
from pathlib import Path

from commerce_agent.hnp_experiment.evaluation import (
    expectation_deviations,
    load_oracles,
    score,
    verify_expected,
)
from commerce_agent.hnp_experiment.evidence import (
    ExperimentalEvidence,
    load_entries,
    verify_entries,
)
from commerce_agent.hnp_experiment.harness import load_cases, run_case
from commerce_agent.hnp_experiment.models import Condition, wire_bytes

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/hnp_experiment/fixtures"


def run(output: Path, strict: bool = True) -> dict:
    """Strict (default) aborts on any oracle deviation; otherwise deviations are recorded."""
    output = output.resolve()
    if not output.is_relative_to((ROOT / ".local").resolve()):
        raise ValueError("Experimental evidence must stay under this worktree's .local directory")
    evidence = ExperimentalEvidence(output)
    cases = load_cases(FIXTURES / "cases.json")
    results = []
    for case in cases:
        for condition in ("A", "B"):
            typed_condition: Condition = condition
            result, sink = run_case(case, typed_condition, evidence)
            if result.effect_occurred != bool(sink.events):
                raise ValueError("Effect observation mismatch")
            results.append(result)
            assessment = result.record.assessment
            verdict = assessment.verdict if assessment is not None else "not_available"
            failures = (
                [c.check_id for c in assessment.checks if c.result != "pass"] if assessment else []
            )
            proposal = result.record.proposal
            print(
                json.dumps(
                    {
                        "case": case.case_id,
                        "condition": condition,
                        "proposal": proposal.draft.model_dump(mode="json") if proposal else None,
                        "proposal_digest": proposal.proposal_digest if proposal else None,
                        "validator": verdict,
                        "validator_nonpass": failures,
                        "validator_pass": [
                            c.check_id for c in assessment.checks if c.result == "pass"
                        ]
                        if assessment
                        else [],
                        "assessment_status": result.record.assessment_status,
                        "backend": result.effective_admission.model_dump(mode="json"),
                        "fictitious_effect": result.effect_occurred,
                        "review_digest": result.record_digest,
                    },
                    ensure_ascii=False,
                )
            )
    # Labels are loaded only after all role invocations have completed.
    oracles = load_oracles(FIXTURES / "oracle.json")
    if strict:
        verify_expected(results, oracles)
    else:
        for deviation in expectation_deviations(results, oracles):
            print(json.dumps({"deviation": deviation.model_dump(mode="json")}))
    verify_entries(evidence.entries)
    if evidence.path is None or load_entries(evidence.path) != evidence.entries:
        raise ValueError("Durable evidence readback mismatch")
    summary = score(results, oracles, strict=strict)
    import platform
    import sys
    from datetime import UTC, datetime

    from commerce_agent.governance.canonical import sha256_digest

    summary["environment"] = {
        "platform": platform.platform(),
        "python": sys.version,
        "recorded_at": datetime.now(UTC).isoformat(),
        "cases_sha256": sha256_digest((FIXTURES / "cases.json").read_bytes()),
        "oracle_sha256": sha256_digest((FIXTURES / "oracle.json").read_bytes()),
        "implementation_sha256": {
            p.name: sha256_digest(p.read_bytes())
            for p in sorted((ROOT / "src/commerce_agent/hnp_experiment").glob("*.py"))
        },
    }
    (output / "results.json").write_bytes(wire_bytes(results) + b"\n")
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/hnp01/demo")
    parser.add_argument(
        "--strict",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="abort on any oracle deviation (scripted reproducibility); --no-strict records them",
    )
    args = parser.parse_args()
    run(args.output, args.strict)
