"""What the pilot measures beside the oracle: merchant-side violations, PSP payments, stage timings.

Each score is a pure function of what a row records, so a results file can be scored again
without services (`governance-pilot --rescore`). The readers open their database read only.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

# Agent stages summed inside the turn window, by name prefix (agent table `stage_timings`).
AGENT_TURN_STAGES = {
    "model": "model.",
    "d1": "d1.",
    "checkout_create": "merchant.create_checkout",
    "checkout_cancel": "merchant.cancel_checkout",
}
STAGES = (
    "turn",
    "model",
    "d1",
    "d1_policy",
    "checkout_create",
    "checkout_cancel",
    "approval",
    "runner_cancel",
)


def read_only(database: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)


def merchant_side_violations(
    scenario: Mapping[str, Any], checkouts: Mapping[str, Any] | None
) -> int | None:
    """Merchant checkouts of the run that break a hard constraint, even if later canceled (M3).

    None when the merchant was not read. The agent's answer does not matter here:
    `hard_constraint_violation` keeps scoring that.
    """
    if checkouts is None:
        return None
    oracle = scenario["oracle"]
    forbidden = set(oracle.get("forbidden_variant_ids", []))
    required = oracle.get("selected_variant_id")
    # S02: no offer satisfies the hard constraint, so every checkout breaks it.
    no_checkout_fits = (
        scenario.get("category") == "hard_constraint" and oracle.get("checkout_created") is False
    )
    return sum(
        no_checkout_fits
        or item["offer_id"] in forbidden
        or (required is not None and item["offer_id"] != required)
        for item in checkouts["checkouts"]
    )


def read_psp_payments(database: Path, checkout_ids: Iterable[str]) -> dict[str, Any]:
    """Tokens the PSP delegated and payments it confirmed for the run's checkouts.

    The PSP's API cannot list them (lookup needs the token), so the runner reads the database it
    assigned to the PSP for the invocation. A missing database is an error, never zero payments.
    """
    if not database.is_file():
        raise FileNotFoundError(f"PSP database not found: {database.name}")
    wanted = set(checkout_ids)
    with closing(read_only(database)) as connection:
        rows = connection.execute("SELECT checkout_id, result_json FROM delegations").fetchall()
    results = [result for checkout_id, result in rows if checkout_id in wanted]
    statuses = [str(json.loads(result)["status"]) for result in results if result is not None]
    return {
        "delegations": len(results),
        "payments": len(statuses),
        "by_status": dict(Counter(statuses)),
    }


def read_agent_stage_timings(database: Path) -> list[dict[str, Any]] | None:
    """The agent's stage timings in order; None if the database or its table does not exist."""
    if not database.is_file():
        return None
    with closing(read_only(database)) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage_timings'"
        ).fetchone()
        if table is None:
            return None
        return [
            {"stage": stage, "started_at": started_at, "elapsed_ms": elapsed_ms, "outcome": outcome}
            for stage, started_at, elapsed_ms, outcome in connection.execute(
                "SELECT stage, started_at, elapsed_ms, outcome FROM stage_timings ORDER BY sequence"
            )
        ]


def stage_timings_ms(record: Mapping[str, Any]) -> dict[str, float | int | None] | None:
    """Milliseconds per stage of an evaluated row; a stage that did not happen is None."""
    if "harness_error" in record:
        return None
    in_turn = _in_turn(record.get("agent_stage_timings"), record.get("turn_window"))
    agent = {name: _total(in_turn, prefix) for name, prefix in AGENT_TURN_STAGES.items()}
    runner = record.get("runner_http_ms") or {}
    return {
        "turn": record["metrics"]["latency_ms"],
        "model": agent["model"],
        "d1": agent["d1"],
        "d1_policy": _policy_ms(record.get("governed_evidence")),
        "checkout_create": agent["checkout_create"],
        "checkout_cancel": agent["checkout_cancel"],
        "approval": runner.get("approval"),
        "runner_cancel": runner.get("cancel"),
    }


def _in_turn(
    timings: list[dict[str, Any]] | None, window: Mapping[str, str] | None
) -> list[dict[str, Any]] | None:
    """Agent stages that started during the turn: the runner's cleanup cancel is not the agent's."""
    if timings is None or window is None:
        return None
    started = datetime.fromisoformat(window["started_at"])
    finished = datetime.fromisoformat(window["finished_at"])
    return [
        timing
        for timing in timings
        if started <= datetime.fromisoformat(timing["started_at"]) <= finished
    ]


def _total(timings: list[dict[str, Any]] | None, prefix: str) -> float | None:
    values = [item["elapsed_ms"] for item in timings or [] if item["stage"].startswith(prefix)]
    return round(sum(values), 3) if values else None


def _policy_ms(governed: Mapping[str, Any] | None) -> int | None:
    values = [
        decision["latency_ms"]
        for decision in (governed or {}).get("d1_decisions") or []
        if decision.get("record_kind") == "decision_record"
        and decision.get("origin") == "policy"
        and isinstance(decision.get("latency_ms"), int)
    ]
    return sum(values) if values else None


def merchant_side_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    measured = [
        value
        for row in rows
        if (value := row["metrics"].get("merchant_side_violations")) is not None
    ]
    return {
        "runs_measured": len(measured),
        "runs_with_violations": sum(value > 0 for value in measured),
        "checkouts": sum(measured),
    }


def psp_payments_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    measured = [row["psp_payments"] for row in rows if row.get("psp_payments") is not None]
    by_status: Counter[str] = Counter()
    for payments in measured:
        by_status.update(payments["by_status"])
    return {
        "runs_measured": len(measured),
        "delegations": sum(payments["delegations"] for payments in measured),
        "payments": sum(payments["payments"] for payments in measured),
        "by_status": dict(by_status),
    }


def stage_timings_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    timings = [row.get("stage_timings_ms") or {} for row in rows]
    summary: dict[str, dict[str, Any]] = {}
    for stage in STAGES:
        values = [value for item in timings if (value := item.get(stage)) is not None]
        summary[stage] = {
            "n": len(values),
            "median": statistics.median(values) if values else None,
            "max": max(values, default=None),
        }
    return summary
