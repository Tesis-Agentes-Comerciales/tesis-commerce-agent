"""How long the agent's model, D1 and merchant calls take, for latency attribution.

The governance pilot reads this table to split a turn's latency by stage (evaluation/README.md).
A row holds the stage, when it started, how long it took and whether it raised: no identifiers,
amounts, prompts or payloads. A timing that cannot be written is logged and dropped; it never
fails the call it measured.
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns

_SCHEMA = """CREATE TABLE IF NOT EXISTS stage_timings (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL,
    started_at TEXT NOT NULL,
    elapsed_ms REAL NOT NULL CHECK(elapsed_ms >= 0),
    outcome TEXT NOT NULL CHECK(outcome IN ('ok', 'error')))"""


class SqliteStageTimings:
    def __init__(self, path: Path) -> None:
        self._path = path

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.execute(_SCHEMA)

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        started_at = datetime.now(UTC).isoformat()
        started = perf_counter_ns()
        outcome = "error"
        try:
            yield
            outcome = "ok"
        finally:
            self._record(stage, started_at, (perf_counter_ns() - started) / 1e6, outcome)

    def _record(self, stage: str, started_at: str, elapsed_ms: float, outcome: str) -> None:
        try:
            with closing(sqlite3.connect(self._path, timeout=5)) as connection, connection:
                connection.execute(
                    "INSERT INTO stage_timings (stage, started_at, elapsed_ms, outcome) "
                    "VALUES (?, ?, ?, ?)",
                    (stage, started_at, elapsed_ms, outcome),
                )
        except sqlite3.Error:
            logging.getLogger(__name__).warning("Stage timing not recorded: %s", stage)
