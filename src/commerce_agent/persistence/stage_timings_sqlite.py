"""How long the agent's model, D1 and merchant calls take, for latency attribution.

The governance pilot reads this table to split a turn's latency by stage (evaluation/README.md).
A row holds the stage, when it started, how long it took and whether it raised: no identifiers,
amounts, prompts or payloads. A timing that cannot be written is logged and dropped; it never
fails the call it measured.

The HNP model selector also reports the tokens of its call (docs/design/hnp-governed.md, HNP-09a).
Those two columns exist only after `initialize_model_tokens`, which the HNP runtime runs, so the
Human Present table keeps its shape.
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns

_SCHEMA = """CREATE TABLE IF NOT EXISTS stage_timings (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL,
    started_at TEXT NOT NULL,
    elapsed_ms REAL NOT NULL CHECK(elapsed_ms >= 0),
    outcome TEXT NOT NULL CHECK(outcome IN ('ok', 'error')))"""
_TOKEN_COLUMNS = ("input_tokens", "output_tokens")


@dataclass
class ModelUsage:
    """Tokens a measured model call reports; the call fills them in before the timing is written."""

    input_tokens: int | None = None
    output_tokens: int | None = None


class SqliteStageTimings:
    def __init__(self, path: Path) -> None:
        self._path = path

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.execute(_SCHEMA)

    def initialize_model_tokens(self) -> None:
        """Add the token columns of a measured model call, once."""
        self.initialize()
        with closing(sqlite3.connect(self._path)) as connection, connection:
            present = {row[1] for row in connection.execute("PRAGMA table_info(stage_timings)")}
            for column in _TOKEN_COLUMNS:
                if column not in present:
                    connection.execute(
                        f"ALTER TABLE stage_timings ADD COLUMN {column} INTEGER "
                        f"CHECK({column} IS NULL OR {column} >= 0)"
                    )

    @contextmanager
    def measure(self, stage: str, usage: ModelUsage | None = None) -> Iterator[None]:
        started_at = datetime.now(UTC).isoformat()
        started = perf_counter_ns()
        outcome = "error"
        try:
            yield
            outcome = "ok"
        finally:
            self._record(stage, started_at, (perf_counter_ns() - started) / 1e6, outcome, usage)

    def _record(
        self,
        stage: str,
        started_at: str,
        elapsed_ms: float,
        outcome: str,
        usage: ModelUsage | None,
    ) -> None:
        columns = ["stage", "started_at", "elapsed_ms", "outcome"]
        values: list[object] = [stage, started_at, elapsed_ms, outcome]
        if usage is not None:
            columns.extend(_TOKEN_COLUMNS)
            values.extend((usage.input_tokens, usage.output_tokens))
        try:
            with closing(sqlite3.connect(self._path, timeout=5)) as connection, connection:
                connection.execute(
                    f"INSERT INTO stage_timings ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' * len(values))})",
                    values,
                )
        except sqlite3.Error:
            logging.getLogger(__name__).warning("Stage timing not recorded: %s", stage)
