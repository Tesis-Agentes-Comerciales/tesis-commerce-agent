"""The catalog against the task table of docs/design/eval-suite.md §4: each task is a case file,
or the scripted tests that already cover it in regression, or uncovered with a reason."""

import json
import re
import subprocess
import sys
from typing import Any

from commerce_evaluation.paths import DATA, ROOT
from commerce_evaluation.suite.cases import load_catalog

SPEC = ROOT / "docs" / "design" / "eval-suite.md"
REGRESSION_MAP = DATA / "suite" / "regression-map.json"
MODES = {
    "los dos": ("presente", "delegado"),
    "`presente`": ("presente",),
    "`delegado`": ("delegado",),
}
# A row of the task table: | `<group>` | `<escenario>` | <source> | <modes> | <what happens> |
ROW = re.compile(r"^\| `([a-z-]+)` \| `([a-z-]+)` \| \w+ \| ([^|]+?) \|", re.MULTILINE)
# The owner's decision: these groups run with a model, so every task of theirs is a case file.
CASE_GROUPS = {"pedido-del-usuario", "manipulacion"}


def spec_tasks() -> dict[str, str]:
    """Each task of the table, `<escenario>/<modo>`, and its group."""
    rows = ROW.findall(SPEC.read_text(encoding="utf-8"))
    return {
        f"{escenario}/{mode}": group for group, escenario, modes in rows for mode in MODES[modes]
    }


def regression_map() -> dict[str, dict[str, Any]]:
    return json.loads(REGRESSION_MAP.read_text(encoding="utf-8"))


def test_the_spec_lists_the_56_tasks_and_the_4_injections() -> None:
    assert len(spec_tasks()) == 60


def test_every_task_is_a_case_or_mapped_or_uncovered() -> None:
    cases = {case.id for case in load_catalog(DATA / "suite" / "cases")}
    mapped = regression_map()
    tasks = spec_tasks()

    assert not cases & mapped.keys(), "A task is either a case file or mapped, never both"
    assert cases | mapped.keys() == tasks.keys()
    assert {task for task, group in tasks.items() if group in CASE_GROUPS} <= cases


def test_a_mapped_task_names_its_tests_or_why_none_covers_it() -> None:
    for task, entry in regression_map().items():
        assert set(entry) in ({"source", "tests", "why"}, {"source", "uncovered"}), task
        assert all(entry.values()), task


def test_every_mapped_test_is_collected() -> None:
    nodes = {node for entry in regression_map().values() for node in entry.get("tests", ())}
    files = sorted({node.partition("::")[0] for node in nodes})
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *files],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    missing = [
        node
        for node in sorted(nodes)
        if not any(line == node or line.startswith(f"{node}[") for line in collected)
    ]
    assert not missing
