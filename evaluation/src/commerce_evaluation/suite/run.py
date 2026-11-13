"""The run: every trial of a suite, each kept as it completes (docs/design/eval-suite.md §2, §7).

A run directory holds what a report needs and nothing it has to recompute:

- `manifest.json`: the commit, the settings, each case file's hash, and each session's start, end
  and status. Written before the first trial.
- `cases.jsonl`: the cases as they were run, so a later correction to the catalog never changes
  how an old run is graded.
- `trials.jsonl`: one record per trial kept, appended as it completes. A crash loses at most the
  trial in flight, and running the same run id again skips every trial already kept.
- `infra.jsonl`: the attempts the infrastructure broke and that were repeated (§2). The last
  attempt is kept in `trials.jsonl` even when it broke too; the reduction leaves it out.
- `excluded.jsonl`: the trials the simulated user ruined twice. They never count against the agent.
- `llm-usage.jsonl`: every LLM call, written by the run's budget (llm/budget.py).

A spent budget (`FundsExhausted`) stops the run cleanly: everything kept stays, and the same run
id with a higher `--max-usd` resumes it.
"""

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

from commerce_evaluation.llm.transport import FundsExhausted
from commerce_evaluation.paths import ROOT
from commerce_evaluation.suite.cases import CASE, Case, Governance, Suite
from commerce_evaluation.suite.executor import RunSettings, SimulatorFailure, TrialExecutor
from commerce_evaluation.suite.reduce import is_infra_failure
from commerce_evaluation.suite.trial import TrialRecord

MANIFEST_FILE = "manifest.json"
CASES_FILE = "cases.jsonl"
TRIALS_FILE = "trials.jsonl"
INFRA_FILE = "infra.jsonl"
EXCLUDED_FILE = "excluded.jsonl"

# Trials per case and governance (§2): regression is scripted, so one says everything.
K: dict[Suite, int] = {"regression": 1, "capability": 5, "adversarial": 5}
# §2: a trial the infrastructure broke is repeated up to twice more.
INFRA_RETRIES = 2
# A simulated user that answered off-schema or refused gets one more chance, then the trial is
# excluded: the simulator's noise must not be charged to the agent.
SIMULATOR_RETRIES = 1
DEFAULT_SEED = 1

Status = Literal["running", "complete", "funds_exhausted", "interrupted"]
Say = Callable[[str], None]


class RunMismatch(ValueError):
    """The run id names a run made with other settings or other cases: it cannot be resumed."""


@dataclass(frozen=True)
class Planned:
    case: Case
    governance: Governance
    trial: int
    seed: int

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.case.id, self.governance, self.trial)

    def __str__(self) -> str:
        return f"{self.case.id} en {self.governance}, intento {self.trial}"


@dataclass(frozen=True)
class Plan:
    settings: RunSettings
    cases: tuple[Case, ...]
    k: int
    seed: int
    max_usd: Decimal | None
    # Where the case files are, to hash them into the manifest.
    catalog: Path

    def trials(self) -> list[Planned]:
        """Each case, under each of its governances, k times, with a seed of its own."""
        return [
            Planned(case, governance, trial, trial_seed(self.seed, case.id, governance, trial))
            for case in self.cases
            for governance in case.environment.governance
            for trial in range(1, self.k + 1)
        ]


@dataclass
class Outcome:
    status: Status = "running"
    # Trials kept or excluded by earlier sessions of the run, and kept by this one.
    resumed: int = 0
    kept: int = 0
    infra_retries: int = 0
    excluded: int = 0


def select(cases: Iterable[Case], suite: Suite, pattern: str | None = None) -> tuple[Case, ...]:
    """The cases that declare `suite`, and whose id matches `pattern` when one is given."""
    return tuple(
        case
        for case in cases
        if suite in case.suites and (pattern is None or fnmatch(case.id, pattern))
    )


def trial_seed(base: int, case_id: str, governance: str, trial: int) -> int:
    """Stable for a trial whatever else the run holds: adding a case moves no other seed."""
    digest = hashlib.sha256(f"{base}:{case_id}:{governance}:{trial}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def execute(plan: Plan, executor: TrialExecutor, *, say: Say = print) -> Outcome:
    """Runs every planned trial not yet in the run directory, keeping each as it ends."""
    outcome = Outcome(resumed=_start(plan))
    left = pending(plan)
    if outcome.resumed:
        say(f"Se retoma la corrida: {outcome.resumed} intentos ya hechos, faltan {len(left)}.")
    outcome.status = "interrupted"
    try:
        if left:
            with executor:
                for index, planned in enumerate(left, start=1):
                    say(f"[{index}/{len(left)}] {planned}")
                    _trial(plan.settings, planned, executor, outcome, say)
        outcome.status = "complete"
    except FundsExhausted:
        outcome.status = "funds_exhausted"
        say("Se acabó el presupuesto: la corrida se detuvo y lo hecho quedó guardado.")
    finally:
        _finish(plan.settings.run_dir, outcome.status)
    return outcome


def _trial(
    settings: RunSettings,
    planned: Planned,
    executor: TrialExecutor,
    outcome: Outcome,
    say: Say,
) -> None:
    """One trial, repeated while the infrastructure or the simulated user is to blame."""
    run_dir, case = settings.run_dir, planned.case
    infra_left, simulator_left, attempt = INFRA_RETRIES, SIMULATOR_RETRIES, 0
    while True:
        attempt += 1
        try:
            record = executor.run(case, planned.governance, planned.trial, planned.seed)
        except SimulatorFailure as failure:
            if simulator_left:
                simulator_left -= 1
                say(f"  El usuario simulado falló ({failure}); se repite una vez.")
                continue
            case_id, governance, trial = planned.key
            row = {"case_id": case_id, "governance": governance, "trial": trial}
            row |= {"seed": planned.seed, "attempts": attempt, "reason": str(failure) or "?"}
            append_jsonl(run_dir / EXCLUDED_FILE, json.dumps(row, ensure_ascii=False))
            outcome.excluded += 1
            say("  Excluido: el usuario simulado volvió a fallar. No cuenta contra el agente.")
            return
        _check(settings, planned, record)
        if record.infra_failure is not None and is_infra_failure(case, record) and infra_left:
            infra_left -= 1
            discarded = {"attempt": attempt, "record": record.model_dump(mode="json")}
            append_jsonl(run_dir / INFRA_FILE, json.dumps(discarded, ensure_ascii=False))
            outcome.infra_retries += 1
            say(f"  Falla de infraestructura ({record.infra_failure.cause}); se repite.")
            continue
        append_jsonl(run_dir / TRIALS_FILE, record.model_dump_json())
        outcome.kept += 1
        return


def _check(settings: RunSettings, planned: Planned, record: TrialRecord) -> None:
    got = (record.case_id, record.governance, record.trial, record.suite)
    wanted = (planned.case.id, planned.governance, planned.trial, settings.suite)
    if got != wanted:
        raise ValueError(f"The executor returned the record of {got} for {wanted}")


# The manifest.


def _start(plan: Plan) -> int:
    """Writes the manifest, or checks a resumed run is the same run; returns how many trials
    the run already holds."""
    settings = plan.settings
    path = settings.run_dir / MANIFEST_FILE
    fixed = _fixed(plan)
    session = {"started_at": _now(), "ended_at": None, "status": "running", "git": git_state()}
    session["max_usd"] = None if plan.max_usd is None else str(plan.max_usd)
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if changed := sorted(name for name in fixed if manifest.get(name) != fixed[name]):
            raise RunMismatch(
                f"La corrida {settings.run_id} ya existe con otro valor de "
                f"{', '.join(changed)}: usa otro --run-id"
            )
        for name in (TRIALS_FILE, INFRA_FILE, EXCLUDED_FILE):
            _repair(settings.run_dir / name)
        manifest["sessions"].append(session)
    else:
        settings.run_dir.mkdir(parents=True, exist_ok=True)
        cases = "".join(CASE.dump_json(case).decode() + "\n" for case in plan.cases)
        (settings.run_dir / CASES_FILE).write_text(cases, encoding="utf-8")
        manifest = {**fixed, "started_at": session["started_at"], "sessions": [session]}
    manifest |= {"ended_at": None, "status": "running", "max_usd": session["max_usd"]}
    _write_manifest(path, manifest)
    return len(_done(settings.run_dir))


def _fixed(plan: Plan) -> dict[str, Any]:
    """What a resumed run must share with the run it resumes."""
    settings = plan.settings
    return {
        "run_id": settings.run_id,
        "suite": settings.suite,
        "k": plan.k,
        "seed": plan.seed,
        "agent": {"model": settings.agent_model, "effort": settings.agent_effort},
        "simulator": {"model": settings.simulator_model},
        "cases": {case.id: case_hash(plan.catalog, case) for case in plan.cases},
    }


def _finish(run_dir: Path, status: Status) -> None:
    path = run_dir / MANIFEST_FILE
    manifest = json.loads(path.read_text(encoding="utf-8"))
    ended = _now()
    manifest["sessions"][-1] |= {"ended_at": ended, "status": status}
    manifest |= {"ended_at": ended, "status": status}
    _write_manifest(path, manifest)


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# JSON lines.


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


def _repair(path: Path) -> None:
    """Cuts a last line left without its newline, so the trial it held runs again."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        path.write_text(text[: text.rfind("\n") + 1], encoding="utf-8")


def read_trials(run_dir: Path) -> list[TrialRecord]:
    return [
        TrialRecord.model_validate_json(json.dumps(row))
        for row in read_jsonl(run_dir / TRIALS_FILE)
    ]


def read_cases(run_dir: Path) -> dict[str, Case]:
    path = run_dir / CASES_FILE
    cases = [CASE.validate_json(line) for line in path.read_text("utf-8").splitlines() if line]
    return {case.id: case for case in cases}


def pending(plan: Plan) -> list[Planned]:
    """The trials a run of `plan` would still execute: all of them in a new run directory."""
    done = _done(plan.settings.run_dir)
    return [planned for planned in plan.trials() if planned.key not in done]


def _done(run_dir: Path) -> set[tuple[str, str, int]]:
    """Trials kept or excluded: running the run id again skips them."""
    rows = [*read_jsonl(run_dir / TRIALS_FILE), *read_jsonl(run_dir / EXCLUDED_FILE)]
    return {(row["case_id"], row["governance"], row["trial"]) for row in rows}
