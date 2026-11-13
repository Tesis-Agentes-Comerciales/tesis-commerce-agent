"""The report of a run: grades, the reduction, cost and what is left to a person (§2, §5).

Everything comes from the run directory (suite/rundir.py): the cases as they were run, the last
record of each trial, the attempts repeated or excluded, and the usage ledgers of the run and of
the judge. Grading is code's (`graders.success.grade`); when the run has `judge.jsonl`, the
judge's verdicts on what the agent says replace code's `claims` grade wherever code left it to
the judge (`graders.judge.grades_with_judge`). A caller can pass any other `Grading` instead.

The report is `report.json`, for the thesis's tables, and `report.md`, to read.
"""

from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from commerce_evaluation.llm.budget import LEDGER_FILE, read_ledger
from commerce_evaluation.suite.cases import Case, CaseId, Governance, Grader, SuiteModel, Text
from commerce_evaluation.suite.graders.grade import Grade, Verdict
from commerce_evaluation.suite.graders.judge import JUDGE_FILE, grades_with_judge, read_judged
from commerce_evaluation.suite.graders.success import grade, strict_success
from commerce_evaluation.suite.mapped import MappedTask, read_mapped
from commerce_evaluation.suite.reduce import Reduction, Scored, is_infra_failure, reduce
from commerce_evaluation.suite.report_md import markdown
from commerce_evaluation.suite.rundir import (
    EXCLUDED_FILE,
    INFRA_FILE,
    JUDGE_BUDGET_DIR,
    Trials,
    read_cases,
    read_jsonl,
    read_manifest,
    read_trials,
    repair_jsonl,
)
from commerce_evaluation.suite.trial import TrialRecord

REPORT_JSON = "report.json"
REPORT_MD = "report.md"

# Every grade of one trial, in the order of the case's reward basis.
Grading = Callable[[Case, TrialRecord], tuple[Grade, ...]]
Tally = dict[Verdict, int]


class TaskGraders(SuiteModel):
    case_id: CaseId
    governance: Governance
    # Over the trials graded: those the infrastructure broke are left out.
    graders: dict[Grader, Tally]


class GovernanceGraders(SuiteModel):
    governance: Governance
    graders: dict[Grader, Tally]


class Review(SuiteModel):
    """A trial whose strict success is `unknown`: each grader that could not decide, and why."""

    case_id: CaseId
    governance: Governance
    trial: int
    run_id: Text
    unknown: dict[Grader, str]


class RoleCost(SuiteModel):
    role: Text
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    usd: float


class Report(SuiteModel):
    manifest: dict[str, JsonValue]
    judged: bool
    reduction: Reduction
    tasks: tuple[TaskGraders, ...]
    governances: tuple[GovernanceGraders, ...]
    cost: tuple[RoleCost, ...]
    usd: float
    # Attempts repeated: by the infrastructure (`infra`, with `attempt` None for a trial run
    # again after a resume) or by the simulated user (`simulator`).
    retries: tuple[dict[str, JsonValue], ...]
    excluded: tuple[dict[str, JsonValue], ...]
    # Every failure of the agent's model, in the attempts repeated and then in the trials kept:
    # its kind, whom it counts against and the ledger's failed requests (trial.ModelFailure).
    model_failures: tuple[dict[str, JsonValue], ...]
    review: tuple[Review, ...]
    # Regression only: the tasks the scripted tests cover, and how those tests ended.
    mapped: tuple[MappedTask, ...] | None


def judged_grading(run_dir: Path) -> Grading | None:
    """The judge's verdicts folded into `claims`, or None when the run was not judged."""
    if not (run_dir / JUDGE_FILE).exists():
        return None
    judged = read_judged(run_dir)
    return lambda case, record: grades_with_judge(case, record, judged)


def build(run_dir: Path, grading: Grading | None = None) -> Report:
    manifest = read_manifest(run_dir)
    judging = grading or judged_grading(run_dir)
    grader = judging or grade
    cases = read_cases(run_dir)
    scored: list[Scored] = []
    tasks: dict[tuple[str, Governance], dict[Grader, Counter[Verdict]]] = {}
    review: list[Review] = []
    trials = read_trials(run_dir)
    for record in trials.kept:
        case = cases[record.case_id]
        grades = grader(case, record)
        success = strict_success(case, grades)
        scored.append(Scored(case, record, success))
        tally = tasks.setdefault((case.id, record.governance), {})
        for name in case.reward_basis:
            tally.setdefault(name, Counter())
        if is_infra_failure(case, record):
            continue
        for given in grades:
            tally[given.grader][given.verdict] += 1
        if success == "unknown":
            unknown: dict[Grader, str] = {
                given.grader: given.reason for given in grades if given.verdict == "unknown"
            }
            review.append(
                Review(
                    case_id=case.id,
                    governance=record.governance,
                    trial=record.trial,
                    run_id=record.run_id,
                    unknown=unknown,
                )
            )
    cost = _cost(run_dir)
    infra = read_jsonl(run_dir / INFRA_FILE)
    return Report(
        manifest=manifest,
        judged=judging is not None,
        reduction=reduce(scored, int(manifest["k"])),
        tasks=tuple(
            TaskGraders(case_id=case_id, governance=governance, graders=_tallies(tally))
            for (case_id, governance), tally in tasks.items()
        ),
        governances=_by_governance(tasks),
        cost=cost,
        usd=float(sum((Decimal(str(role.usd)) for role in cost), Decimal(0))),
        retries=(
            *(_retry(row) for row in infra),
            *(
                _retry({"kind": "infra", "record": record.model_dump(mode="json")})
                for record in trials.superseded
            ),
        ),
        excluded=tuple(read_jsonl(run_dir / EXCLUDED_FILE)),
        model_failures=_model_failures(trials, infra),
        review=tuple(review),
        mapped=None if (mapped := read_mapped(run_dir)) is None else tuple(mapped),
    )


def write(run_dir: Path, grading: Grading | None = None) -> Report:
    """Builds the report and writes `report.json` and `report.md` into the run directory."""
    report = build(run_dir, grading)
    (run_dir / REPORT_JSON).write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (run_dir / REPORT_MD).write_text(markdown(report), encoding="utf-8")
    return report


def _tallies(tally: dict[Grader, Counter[Verdict]]) -> dict[Grader, Tally]:
    verdicts: tuple[Verdict, ...] = ("pass", "fail", "unknown")
    return {
        name: {verdict: counts[verdict] for verdict in verdicts} for name, counts in tally.items()
    }


def _by_governance(
    tasks: dict[tuple[str, Governance], dict[Grader, Counter[Verdict]]],
) -> tuple[GovernanceGraders, ...]:
    totals: dict[Governance, dict[Grader, Counter[Verdict]]] = {}
    for (_, governance), tally in tasks.items():
        into = totals.setdefault(governance, {})
        for name, counts in tally.items():
            into.setdefault(name, Counter()).update(counts)
    return tuple(
        GovernanceGraders(governance=governance, graders=_tallies(tally))
        for governance, tally in totals.items()
    )


def _cost(run_dir: Path) -> tuple[RoleCost, ...]:
    """The calls and spend per role of the run's ledger and the judge's, in the order each role
    first spent. A call the cap refused counts, at USD 0; one that failed without reporting
    usage counts its worst case, which stays reserved because it may have been billed."""
    roles: dict[str, list[Any]] = {}
    for ledger in (run_dir / LEDGER_FILE, run_dir / JUDGE_BUDGET_DIR / LEDGER_FILE):
        repair_jsonl(ledger)
        for row in read_ledger(ledger):
            roles.setdefault(row.role, []).append(row)
    return tuple(
        RoleCost(
            role=role,
            calls=len(rows),
            input_tokens=sum(row.usage.input_tokens for row in rows if row.usage),
            output_tokens=sum(row.usage.output_tokens for row in rows if row.usage),
            cache_read_tokens=sum(row.usage.cache_read_tokens for row in rows if row.usage),
            cache_write_tokens=sum(row.usage.cache_write_tokens for row in rows if row.usage),
            usd=float(sum((Decimal(str(row.usd)) for row in rows), Decimal(0))),
        )
        for role, rows in roles.items()
    )


def _model_failures(
    trials: Trials, infra: list[dict[str, Any]]
) -> tuple[dict[str, JsonValue], ...]:
    """Each record's model failure: `attempt` numbers a repeated one (None before a resume),
    and `kept` tells the trial's last record."""
    seen = [(row["record"], row.get("attempt"), False) for row in infra if row["kind"] == "infra"]
    seen += [(record.model_dump(mode="json"), None, False) for record in trials.superseded]
    seen += [
        (record.model_dump(mode="json"), None, True)
        for record in trials.kept
        if record.model_failure is not None
    ]
    return tuple(
        {
            **{name: record[name] for name in ("case_id", "governance", "trial")},
            "attempt": attempt,
            "kept": kept,
            **record["model_failure"],
        }
        for record, attempt, kept in seen
        if record.get("model_failure")
    )


def _retry(row: dict[str, Any]) -> dict[str, JsonValue]:
    """One repeated attempt, the same for both kinds: who, which attempt, and why."""
    if row["kind"] == "simulator":
        named = {name: row[name] for name in ("case_id", "governance", "trial", "attempt")}
        return {"kind": "simulator", **named, "cause": "simulator", "detail": row["reason"]}
    record = row["record"]
    failure = record.get("infra_failure") or {}
    return {
        "kind": "infra",
        "case_id": record["case_id"],
        "governance": record["governance"],
        "trial": record["trial"],
        "attempt": row.get("attempt"),
        "cause": failure.get("cause"),
        "detail": failure.get("detail"),
    }
