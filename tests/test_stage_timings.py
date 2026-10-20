"""The agent times its model, D1 and merchant calls in agent.db, for latency attribution.

The governance pilot splits a turn's latency with these rows (evaluation/README.md). Real
SQLite, the scripted router and the ACP merchant doubles; no model and no network.
"""

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from commerce_agent.application.stage_timing import TimedConversationRouter
from commerce_agent.persistence.stage_timings_sqlite import SqliteStageTimings
from tests.assistant_support import PURCHASE_REQUEST, ScriptedRouter, build_app, checkout_details
from tests.governance.test_runtime_dispatch import Merchant, governed, runtime
from tests.governance.test_shadow_integration import start


def stages(database: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(
            "SELECT stage, outcome FROM stage_timings ORDER BY sequence"
        ).fetchall()


def test_a_measured_call_records_its_stage_start_duration_and_outcome(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    timings = SqliteStageTimings(database)
    timings.initialize()

    with timings.measure("model.route"):
        pass
    with (
        pytest.raises(RuntimeError, match="merchant down"),
        timings.measure("merchant.create_checkout"),
    ):
        raise RuntimeError("merchant down")

    with closing(sqlite3.connect(database)) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(stage_timings)")]
        rows = connection.execute(
            "SELECT started_at, elapsed_ms FROM stage_timings ORDER BY sequence"
        ).fetchall()
    # Only the stage and its timing: no run, checkout, amount or payload.
    assert columns == ["sequence", "stage", "started_at", "elapsed_ms", "outcome"]
    assert stages(database) == [("model.route", "ok"), ("merchant.create_checkout", "error")]
    assert all(datetime.fromisoformat(started).tzinfo is not None for started, _ in rows)
    assert all(elapsed >= 0 for _, elapsed in rows)


def test_a_timing_that_cannot_be_written_never_fails_the_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    timings = SqliteStageTimings(tmp_path / "agent.db")  # never initialized: no table

    with timings.measure("model.route"):
        answer = "answered"

    assert answer == "answered"
    assert "Stage timing not recorded: model.route" in caplog.text


def test_a_purchase_turn_times_the_model_and_every_merchant_call(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"

    with TestClient(build_app(database)) as client:
        turn = client.post(
            "/conversation/turn",
            json={
                "turn_id": "turn-timed",
                "messages": [{"role": "user", "content": PURCHASE_REQUEST}],
                "details": checkout_details(),
            },
        )
        assert turn.status_code == 200, turn.text
        run_id = turn.json()["purchase"]["run_id"]
        assert client.post(f"/purchases/{run_id}/cancel", json={}).status_code == 200

    assert stages(database) == [
        ("model.route", "ok"),
        ("model.choose_candidate", "ok"),
        ("merchant.create_checkout", "ok"),
        ("merchant.update_checkout", "ok"),
        ("merchant.get_checkout", "ok"),
        # The approval terms read the checkout the turn prepared.
        ("merchant.get_checkout", "ok"),
        ("merchant.get_checkout", "ok"),
        ("merchant.cancel_checkout", "ok"),
    ]


def test_a_governed_checkout_times_the_d1_gate_around_the_merchant_post(tmp_path: Path) -> None:
    settings = governed(tmp_path / "agent.db")
    built = runtime(settings, Merchant())
    try:
        start(built.purchases)
    finally:
        built.close()

    assert [stage for stage, _ in stages(settings.agent_database_path)] == [
        "d1.capture",
        "merchant.create_checkout",
        "merchant.get_checkout",
        "d1.create_checkout",
        "merchant.update_checkout",
        "merchant.get_checkout",
    ]


class ClosingRouter(ScriptedRouter):
    closed = False

    def close(self) -> None:
        self.closed = True


def test_the_timed_router_closes_the_router_it_wraps(tmp_path: Path) -> None:
    timings = SqliteStageTimings(tmp_path / "agent.db")
    router = ClosingRouter()

    TimedConversationRouter(router, timings).close()
    TimedConversationRouter(ScriptedRouter(), timings).close()

    assert router.closed is True
