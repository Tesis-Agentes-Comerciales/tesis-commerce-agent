"""The regression map: each mapped task's coverage, and the outcome of each of its tests as the
plugin records it from a real pytest subprocess (eval-suite.md §4, §8)."""

import json
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation.suite import mapped

SAMPLE = """
import pytest

def test_ok():
    pass

def test_bad():
    assert False

def test_skipped():
    pytest.skip("sin stack")

@pytest.mark.parametrize("value", [1, 2])
def test_param(value):
    assert value == 1
"""


def test_coverage_comes_from_the_entry() -> None:
    assert mapped.coverage({"source": "CT1", "tests": ["t"], "why": "Cubre."}) == "covered"
    assert (
        mapped.coverage({"source": "CT1", "tests": ["t"], "why": "Parcial: un lado"}) == "partial"
    )
    assert mapped.coverage({"source": "CT1", "uncovered": "No hay cupones."}) == "uncovered"


def test_a_map_is_filtered_like_the_cases(tmp_path: Path) -> None:
    path = tmp_path / "map.json"
    path.write_text('{"a/presente": {"source": "X", "uncovered": "y"}, "b/delegado": {}}')

    assert list(mapped.load_map(path, "*/presente")) == ["a/presente"]


def test_the_plugin_records_each_outcome_of_a_real_pytest_run(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text(SAMPLE)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    entries = {
        "a/presente": {
            "source": "CT1",
            "tests": ["test_sample.py::test_ok", "test_sample.py::test_skipped"],
            "why": "Cubre.",
        },
        "b/delegado": {
            "source": "CT2",
            "tests": ["test_sample.py::test_bad", "test_sample.py::test_param"],
            "why": "Parcial: solo un lado.",
        },
    }

    tasks = mapped.run_mapped(entries, run_dir, cwd=tmp_path)

    outcomes = {test.id: test.outcome for task in tasks for test in task.tests}
    assert outcomes == {
        "test_sample.py::test_ok": "passed",
        "test_sample.py::test_skipped": "skipped",
        "test_sample.py::test_bad": "failed",
        # One of its parameters fails, so the test fails.
        "test_sample.py::test_param": "failed",
    }
    assert mapped.read_mapped(run_dir) == tasks


BROWSER = "tests/integration/test_live_browser_journeys.py::test_revoke"


@pytest.mark.parametrize(
    ("node", "installed", "reason"),
    [
        (True, (), "falta npm ci en auth/ y ui/"),
        (True, ("auth",), "falta npm ci en ui/"),
        (False, ("auth", "ui"), "falta node en el PATH"),
        (False, ("ui",), "falta node en el PATH; falta npm ci en auth/"),
        (True, ("auth", "ui"), None),
    ],
)
def test_a_browser_test_runs_only_with_node_and_npm_ci_and_otherwise_says_why(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    node: bool,
    installed: tuple[str, ...],
    reason: str | None,
) -> None:
    monkeypatch.setattr(mapped.shutil, "which", lambda name: f"/bin/{name}" if node else None)
    for name in installed:
        (tmp_path / name / "node_modules").mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    entries = {"a/delegado": {"source": "MA24", "tests": ["t.py::ok", BROWSER], "why": "Cubre."}}
    sent: list[str] = []

    def runner(command: list[str], **options: Any) -> None:
        sent.extend(command)
        rows = [{"id": test, "outcome": "passed"} for test in ("t.py::ok", BROWSER)]
        lines = "".join(json.dumps(row) + "\n" for row in rows if row["id"] in command)
        Path(options["env"][mapped.OUTCOMES_ENV]).write_text(lines)

    [task] = mapped.run_mapped(entries, run_dir, runner=runner, cwd=tmp_path)

    found = {test.id: (test.outcome, test.reason) for test in task.tests}
    if reason is None:
        assert found[BROWSER] == ("passed", None)
    else:
        assert found[BROWSER] == ("not_run", reason) and BROWSER not in sent
    assert found["t.py::ok"] == ("passed", None)
    assert mapped.read_mapped(run_dir) == [task]
