"""How the suite runs one trial; the live stack implements it (docs/design/eval-suite.md §2, §5)."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from commerce_evaluation.suite.cases import Case, Governance, Suite
from commerce_evaluation.suite.trial import TrialRecord


@dataclass(frozen=True)
class RunSettings:
    run_id: str
    run_dir: Path
    suite: Suite
    agent_model: str | None  # None runs the scripted reference agent (regression)
    agent_effort: str
    simulator_model: str | None  # None uses the scripted user


class SimulatorFailure(Exception):
    """The simulated user ended the trial on an unusable answer; it never counts against the
    agent."""


class TrialExecutor(AbstractContextManager["TrialExecutor"], Protocol):
    def run(self, case: Case, governance: Governance, trial: int, seed: int) -> TrialRecord: ...
