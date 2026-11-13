"""The regression map: tasks the scripted tests already cover instead of a case file (§4, §8).

`evaluation/data/suite/regression-map.json` names, for each task of `cambios-en-la-tienda`,
`cambios-en-el-permiso` and `fallas-tecnicas` that is not a case, the pytest ids that cover it
and why, or why none does. A `why` that starts with «Parcial» covers it in part.

Regression runs those ids in a subprocess with the live stack (`RUN_LIVE_STACK=1`) and never with
a live model: the variables that would call one are removed from its environment. A browser test
needs `npm ci` in `auth/` and `ui/`; without it, it is not run and its outcome says so. This module
is also the pytest plugin (`-p`) that writes each test's outcome to the file `OUTCOMES_ENV` names.
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from commerce_evaluation.paths import DATA, ROOT

MAP_FILE = DATA / "suite" / "regression-map.json"
# What the run directory keeps of the mapped tests.
MAPPED_FILE = "regression-map.json"
OUTCOMES_ENV = "EVAL_SUITE_OUTCOMES"
PARTIAL = "Parcial"
# Never a live model from regression (AGENTS.md).
LIVE_MODEL_SWITCHES = ("RUN_LIVE_LLM", "HNP_LIVE")
# The browser tests run auth/ and ui/ from their node_modules (tests/integration/live_browser.py).
BROWSER_TESTS = "tests/integration/test_live_browser"
NODE_PROJECTS = ("auth", "ui")

Coverage = Literal["covered", "partial", "uncovered"]
# A test's result; `not_run` when pytest did not report it (deselected, or a collection error).
TestOutcome = Literal["passed", "failed", "skipped", "not_run"]
Runner = Callable[..., object]


class MappedTest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    outcome: TestOutcome
    # Why it did not run, when the runner knows.
    reason: str | None = None


class MappedTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str
    source: str
    coverage: Coverage
    why: str
    tests: tuple[MappedTest, ...]


def load_map(path: Path = MAP_FILE, pattern: str | None = None) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return {
        task: entry for task, entry in entries.items() if pattern is None or fnmatch(task, pattern)
    }


def coverage(entry: dict[str, Any]) -> Coverage:
    if "uncovered" in entry:
        return "uncovered"
    return "partial" if str(entry["why"]).startswith(PARTIAL) else "covered"


def mapped_ids(entries: dict[str, dict[str, Any]]) -> list[str]:
    return list(
        dict.fromkeys(test for entry in entries.values() for test in entry.get("tests", ()))
    )


def run_mapped(
    entries: dict[str, dict[str, Any]],
    run_dir: Path,
    *,
    runner: Runner = subprocess.run,
    cwd: Path = ROOT,
) -> list[MappedTask]:
    """Runs every mapped test once and writes each task's coverage and results to the run."""
    missing = [f"{name}/" for name in NODE_PROJECTS if not (cwd / name / "node_modules").is_dir()]
    blocked = {
        test: f"falta npm ci en {' y '.join(missing)}"
        for test in mapped_ids(entries)
        if missing and test.startswith(BROWSER_TESTS)
    }
    ids = [test for test in mapped_ids(entries) if test not in blocked]
    outcomes_path = run_dir / "regression-map.outcomes.jsonl"
    outcomes_path.unlink(missing_ok=True)
    if ids:
        environment = {
            name: value for name, value in os.environ.items() if name not in LIVE_MODEL_SWITCHES
        }
        environment |= {"RUN_LIVE_STACK": "1", OUTCOMES_ENV: str(outcomes_path)}
        command = [sys.executable, "-m", "pytest", "-q", "-p", __name__, "-p", "no:cacheprovider"]
        runner([*command, *ids], cwd=cwd, env=environment, check=False)
    outcomes = read_outcomes(outcomes_path)
    tasks = [
        MappedTask(
            task=task,
            source=entry["source"],
            coverage=coverage(entry),
            why=entry.get("why") or entry.get("uncovered", ""),
            tests=tuple(
                MappedTest(id=test, outcome="not_run", reason=blocked[test])
                if test in blocked
                else MappedTest(id=test, outcome=_outcome(test, outcomes))
                for test in entry.get("tests", ())
            ),
        )
        for task, entry in entries.items()
    ]
    rows = [task.model_dump(mode="json") for task in tasks]
    text = json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
    (run_dir / MAPPED_FILE).write_text(text, encoding="utf-8")
    return tasks


def read_mapped(run_dir: Path) -> list[MappedTask] | None:
    path = run_dir / MAPPED_FILE
    if not path.exists():
        return None
    return [MappedTask.model_validate(row) for row in json.loads(path.read_text("utf-8"))]


def read_outcomes(path: Path) -> dict[str, TestOutcome]:
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]
    return {row["id"]: row["outcome"] for row in rows}


def _outcome(test: str, outcomes: dict[str, TestOutcome]) -> TestOutcome:
    """A parametrized id without its parameters stands for all of them: any failure fails it."""
    found = [
        outcome
        for node, outcome in outcomes.items()
        if node == test or (node.startswith(f"{test}[") and "[" not in test)
    ]
    for result in ("failed", "skipped", "passed"):
        if result in found:
            return result
    return "not_run"


def summary(coverages: Iterable[Coverage]) -> dict[Coverage, int]:
    counts: dict[Coverage, int] = {"covered": 0, "partial": 0, "uncovered": 0}
    for found in coverages:
        counts[found] += 1
    return counts


# The pytest plugin, loaded in the subprocess with `-p commerce_evaluation.suite.mapped`.


def pytest_runtest_logreport(report: Any) -> None:
    """A test fails if any phase fails, is skipped if its setup or call skips, and passes on
    its call; a later phase overrides an earlier one."""
    path = os.environ.get(OUTCOMES_ENV)
    if not path:
        return
    if report.failed:
        outcome = "failed"
    elif report.skipped:
        outcome = "skipped"
    elif report.passed and report.when == "call":
        outcome = "passed"
    else:
        return
    with open(path, "a", encoding="utf-8") as file:
        file.write(json.dumps({"id": report.nodeid, "outcome": outcome}) + "\n")
