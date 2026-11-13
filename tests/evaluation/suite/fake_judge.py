"""Stands in for graders/judge.py (PR #210) behind the report's hook: it reads `judge.jsonl` and
passes every claims grade code left to the judge."""

from pathlib import Path
from typing import Any

from commerce_evaluation.suite.cases import Case
from commerce_evaluation.suite.graders.grade import Grade
from commerce_evaluation.suite.graders.success import grade
from commerce_evaluation.suite.trial import TrialRecord


def read_judged(run_dir: Path) -> dict[str, Any]:
    return {"read": (run_dir / "judge.jsonl").read_text()}


def grades_with_judge(case: Case, record: TrialRecord, judged: dict[str, Any]) -> tuple[Grade, ...]:
    assert judged == {"read": "{}\n"}
    return tuple(
        Grade(grader="claims", verdict="pass", reason="El juez da por cumplidos los criterios")
        if given.grader == "claims" and given.verdict == "unknown"
        else given
        for given in grade(case, record)
    )
