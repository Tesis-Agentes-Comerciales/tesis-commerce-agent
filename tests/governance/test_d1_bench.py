"""Smoke test of scripts/_support/d1_bench.py, the setups of scripts/measure_d1_enforce.py.

The bench keeps its own copy of the enforce integration setup (no test imports), so nothing else
would notice when PurchaseService or the D1 gate change under it. Each mode must still reach
human confirmation with one checkout creation, as measure() asserts.
"""

from pathlib import Path

import pytest

from scripts._support.d1_bench import baseline, enforce, start


def test_enforce_setup_reaches_confirmation_through_the_gate(tmp_path: Path) -> None:
    service, gateway, gate = enforce(tmp_path / "enforce.sqlite")

    result = start(service)

    assert result.stage == "awaiting_confirmation"
    assert [name for name, _ in gateway.calls] == ["create", "get", "update", "get"]
    assert gate.timings


@pytest.mark.parametrize("shadow", [False, True], ids=["off", "shadow"])
def test_baseline_setups_reach_confirmation(tmp_path: Path, shadow: bool) -> None:
    service, gateway, observer = baseline(tmp_path / "baseline.sqlite", shadow=shadow)

    result = start(service)

    assert result.stage == "awaiting_confirmation"
    assert sum(name == "create" for name, _ in gateway.calls) == 1
    assert (observer is not None) is shadow
    assert observer is None or observer.diagnostics.status == "observed"
