"""The files of a run directory, and how they are read and written (docs/design/eval-suite.md §8).

- `manifest.json`: the commit, the settings, each case file's hash, and each session's start,
  end and status. Written before the first trial.
- `cases.jsonl`: the cases as they were run, so a later correction to the catalog never changes
  how an old run is graded.
- `trials.jsonl`: one record per trial, appended as it ends. A trial the infrastructure broke
  may appear again after a resume; the last record of a trial is the one that counts, and the
  earlier ones are reported with the repeated attempts (`read_trials`).
- `infra.jsonl`: the attempts that were repeated, with their `kind`: `infra` (the record of an
  attempt the infrastructure broke) or `simulator` (the simulated user failed and got another
  chance).
- `excluded.jsonl`: the trials the simulated user ruined twice. They never count against the agent.
- `llm-usage.jsonl` and `batches.jsonl`: the run's LLM calls (llm/budget.py). The judge keeps its
  own in `judge/`, under its own cap, and writes its verdicts to `judge.jsonl`.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, NamedTuple

from commerce_evaluation.paths import ROOT
from commerce_evaluation.suite.cases import CASE, Case
from commerce_evaluation.suite.trial import TrialRecord

MANIFEST_FILE = "manifest.json"
CASES_FILE = "cases.jsonl"
TRIALS_FILE = "trials.jsonl"
INFRA_FILE = "infra.jsonl"
EXCLUDED_FILE = "excluded.jsonl"
# The judge's ledger and batches, apart from the run's: it has its own --max-usd.
JUDGE_BUDGET_DIR = "judge"

Key = tuple[str, str, int]


class Trials(NamedTuple):
    # The last record of each trial, in the order the trials first appear.
    kept: list[TrialRecord]
    # Earlier records of a trial run again after a resume.
    superseded: list[TrialRecord]


def key_of(record: TrialRecord) -> Key:
    return (record.case_id, record.governance, record.trial)


def read_trials(run_dir: Path) -> Trials:
    last: dict[Key, TrialRecord] = {}
    superseded: list[TrialRecord] = []
    for row in read_jsonl(run_dir / TRIALS_FILE):
        record = TrialRecord.model_validate_json(json.dumps(row))
        if (earlier := last.get(key_of(record))) is not None:
            superseded.append(earlier)
        last[key_of(record)] = record
    return Trials(list(last.values()), superseded)


def read_cases(run_dir: Path) -> dict[str, Case]:
    lines = (run_dir / CASES_FILE).read_text("utf-8").splitlines()
    cases = [CASE.validate_json(line) for line in lines if line]
    return {case.id: case for case in cases}


def write_cases(run_dir: Path, cases: tuple[Case, ...]) -> None:
    text = "".join(CASE.dump_json(case).decode() + "\n" for case in cases)
    (run_dir / CASES_FILE).write_text(text, encoding="utf-8")


def read_manifest(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / MANIFEST_FILE).read_text("utf-8"))


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    """Replaces the manifest in one step, so a crash never leaves half of it."""
    path = run_dir / MANIFEST_FILE
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    temporary.replace(path)


def case_hash(catalog: Path, case: Case) -> str:
    path = catalog / case.group / f"{case.id}.json"
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def git_state(root: Path = ROOT) -> dict[str, Any]:
    """The commit and whether the tree differs from it, run results aside."""

    def git(*arguments: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", *arguments], cwd=root, capture_output=True, text=True, check=True
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        return done.stdout.strip()

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--", ".", ":(exclude)evaluation/results/suite")
    return {"commit": commit, "dirty": None if status is None else bool(status)}


def append_jsonl(path: Path, line: str) -> None:
    """Appends one line and forces it to disk before the next trial starts."""
    with path.open("a", encoding="utf-8") as file:
        file.write(line + "\n")
        file.flush()
        os.fsync(file.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every complete line: a crash mid-write leaves a last line without its newline."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").split("\n")[:-1]
    return [json.loads(line) for line in lines if line]


def repair_jsonl(path: Path) -> None:
    """Cuts a last line left without its newline, so what it held is redone or left out."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        path.write_text(text[: text.rfind("\n") + 1], encoding="utf-8")
