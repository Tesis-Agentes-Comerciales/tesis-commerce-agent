"""Offline PurchaseService measurement with test spies; never a deployment entrypoint.

uv run python scripts/measure_d1_enforce.py --output .local/gov06b/latency.json
Requires dev dependencies and local synthetic test fixtures. No real client is built.
"""

import argparse
import gc
import json
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.governance.test_enforce_integration import (  # noqa: E402
    MARKER,
    OWNER,
    HardConstraints,
    PurchaseIntent,
    baseline,
    details,
    make_case,
    start,
)


def measure(output: Path, repetitions: int) -> dict:
    output = output.resolve()
    if not output.is_relative_to(ROOT / ".local") or repetitions < 1:
        raise ValueError("Use a .local output path and positive repetitions")
    output.parent.mkdir(parents=True, exist_ok=True)
    runs = []
    with TemporaryDirectory(prefix="d1-bench-", dir=output.parent) as directory:
        temporary = Path(directory).resolve()
        assert temporary.is_relative_to(output.parent)
        for repetition in range(repetitions):
            # Rotate mode order so the paired comparison does not always run off first.
            modes = ["off", "shadow", "enforce"]
            modes = modes[repetition % 3 :] + modes[: repetition % 3]
            for mode in modes:
                # Legacy B0 connection contexts commit but rely on GC for close.
                # Collect between measurements, never inside the timed interval.
                gc.collect()
                path = temporary / f"{repetition}-{mode}.sqlite"
                began = perf_counter_ns()
                if mode == "enforce":
                    case = make_case(path)
                    setup_ms = (perf_counter_ns() - began) / 1e6
                    began = perf_counter_ns()
                    result = start(case)
                    phases = dict(case.gate.timings)
                    gateway = case.gateway
                else:
                    service, gateway, observer = baseline(path, shadow=mode == "shadow")
                    setup_ms = (perf_counter_ns() - began) / 1e6
                    began = perf_counter_ns()
                    result = service.start(
                        run_id="run-a",
                        owner_id=OWNER,
                        request=MARKER,
                        intent=PurchaseIntent(hard=HardConstraints(currency="COP")),
                        selected_variant_id="SON-01",
                        details=details(),
                    )
                    phases = (
                        {}
                        if observer is None
                        else {
                            "capture_ms": observer.diagnostics.capture_ms,
                            "evaluation_ms": observer.diagnostics.evaluation_ms,
                        }
                    )
                    # A degraded observer still completes B0; never report it as shadow cost.
                    assert observer is None or observer.diagnostics.status == "observed"
                service_ms = (perf_counter_ns() - began) / 1e6
                assert result.stage == "awaiting_confirmation"
                assert sum(name == "create" for name, _ in gateway.calls) == 1
                runs.append(
                    {
                        "repetition": repetition,
                        "mode": mode,
                        "setup_ms": setup_ms,
                        "service_ms": service_ms,
                        "phases": phases,
                        "gateway_calls": [name for name, _ in gateway.calls],
                    }
                )
        gc.collect()  # Release SQLite handles before Windows removes the isolated directory.
    summary = {}
    for mode in ("off", "shadow", "enforce"):
        values = [row["service_ms"] for row in runs if row["mode"] == mode]
        summary[mode] = {"median_ms": median(values), "min_ms": min(values), "max_ms": max(values)}
    paired = {}
    for mode in ("shadow", "enforce"):
        differences = []
        for repetition in range(repetitions):
            pair = {
                row["mode"]: row["service_ms"] for row in runs if row["repetition"] == repetition
            }
            differences.append(pair[mode] - pair["off"])
        paired[mode] = {"differences_ms": differences, "median_ms": median(differences)}
    result_data = {
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
            "sqlite": sqlite3.sqlite_version,
            "head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
        },
        "repetitions": repetitions,
        "runs": runs,
        "service_summary": summary,
        "paired_added": paired,
        "transport": "SpyGateway only; no HTTP transport constructed",
        "scope": "Synthetic offline spy; no remote recovery or production performance validation",
    }
    output.write_text(json.dumps(result_data, indent=2), encoding="utf-8")
    return result_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    result = measure(args.output, args.repetitions)
    print(
        json.dumps(
            {"service_summary": result["service_summary"], "paired_added": result["paired_added"]}
        )
    )
