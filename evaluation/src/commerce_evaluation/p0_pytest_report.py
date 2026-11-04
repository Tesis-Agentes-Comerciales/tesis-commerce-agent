"""Reporting-only pytest plugin. It never selects or enables tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from commerce_evaluation.scenarios_p0 import OUTCOMES, count_plan, report_outcome

_PRIORITY = {"passed": 1, "skipped": 2, "xfail": 3, "xpass": 4, "failed": 5}


def pytest_addoption(parser: Any) -> None:
    parser.addoption("--p0-report", help="Write aggregate Python test outcomes (not scenarios)")


def pytest_configure(config: Any) -> None:
    destination = config.getoption("--p0-report")
    if destination:
        config.pluginmanager.register(OutcomeRecorder(Path(destination)), "p0-outcomes")


class OutcomeRecorder:
    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.collected: list[str] = []
        self.observed: dict[str, str] = {}
        self.collection_errors = 0
        self.deselected = 0
        self.junit_subtests = dict.fromkeys(("passed", "failed", "skipped"), 0)

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected = [item.nodeid for item in session.items]

    def pytest_deselected(self, items: list[Any]) -> None:
        self.deselected += len(items)

    def pytest_collectreport(self, report: Any) -> None:
        if report.failed:
            self.collection_errors += 1

    def pytest_runtest_logreport(self, report: Any) -> None:
        # pytest 9 counts subtest call reports in the JUnit header,
        # but emits them inside their parent's testcase, not as separate items.
        if report.when == "call" and hasattr(report, "context"):
            self.junit_subtests[report.outcome] += 1
        result = report_outcome(
            report.outcome,
            report.when,
            hasattr(report, "wasxfail"),
            report.outcome == "failed" and str(report.longrepr).startswith("[XPASS(strict)]"),
        )
        if result is not None:
            previous = self.observed.get(report.nodeid)
            if previous is None or _PRIORITY[result] > _PRIORITY[previous]:
                self.observed[report.nodeid] = result

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        # Reports may exist even when collection was interrupted.
        plan = sorted(set(self.collected) | self.observed.keys())
        counts = count_plan(plan, self.observed)
        assert set(counts) == set(OUTCOMES)
        payload = {
            "status": "completed" if exitstatus == 0 else "incomplete_or_failed",
            "exit_code": int(exitstatus),
            "counts": counts,
            "collected": len(self.collected),
            "deselected": self.deselected,
            "collection_errors": self.collection_errors,
            "junit_subtests": self.junit_subtests,
            "scope": "Python product and harness tests; not scenario execution",
        }
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
