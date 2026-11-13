"""The regression map: each mapped task's coverage, and the outcome of each of its tests as the
plugin records it from a real pytest subprocess (eval-suite.md §4, §8)."""

from pathlib import Path

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
