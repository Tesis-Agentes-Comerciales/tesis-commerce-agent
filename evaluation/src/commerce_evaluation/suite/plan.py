"""The trials a run plans (docs/design/eval-suite.md §2): which cases, under which governance,
how many times, in which order and with which seed, and which of them are still to do.

Round by round: every trial 1 first, then every trial 2, so a run cut short is not biased toward
the first groups of the catalog. A trial whose last record the infrastructure broke is still to
do: resuming the run repeats it.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from fnmatch import fnmatch
from pathlib import Path

from commerce_evaluation.suite.cases import Case, Governance, Suite
from commerce_evaluation.suite.executor import RunSettings
from commerce_evaluation.suite.reduce import is_infra_failure
from commerce_evaluation.suite.rundir import EXCLUDED_FILE, Key, read_jsonl, read_trials

# Trials per case and governance (§2): regression is scripted, so one says everything.
K: dict[Suite, int] = {"regression": 1, "capability": 5, "adversarial": 5}
DEFAULT_SEED = 1


@dataclass(frozen=True)
class Planned:
    case: Case
    governance: Governance
    trial: int
    seed: int

    @property
    def key(self) -> Key:
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
        """Round by round: each case under each of its governances, with a seed of its own."""
        return [
            Planned(case, governance, trial, trial_seed(self.seed, case.id, governance, trial))
            for trial in range(1, self.k + 1)
            for case in self.cases
            for governance in case.environment.governance
        ]


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


def pending(plan: Plan) -> list[Planned]:
    """The trials a run of `plan` would still execute: all of them in a new run directory."""
    finished = done(plan)
    return [planned for planned in plan.trials() if planned.key not in finished]


def done(plan: Plan) -> set[Key]:
    """Trials kept or excluded; a trial whose last record the infrastructure broke runs again."""
    run_dir = plan.settings.run_dir
    cases = {case.id: case for case in plan.cases}
    kept = {
        (record.case_id, record.governance, record.trial)
        for record in read_trials(run_dir).kept
        if record.case_id not in cases or not is_infra_failure(cases[record.case_id], record)
    }
    excluded = read_jsonl(run_dir / EXCLUDED_FILE)
    return kept | {(row["case_id"], row["governance"], row["trial"]) for row in excluded}
