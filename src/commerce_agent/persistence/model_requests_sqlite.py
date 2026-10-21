"""One row per request the agent sends to the model (docs/design/run-traceability.md §4.4).

`stage_timings` keeps one row per stage, without payloads. This table keeps what a stage asked the
model and what it answered, so a run can be explained after the fact: the run, the stage and the
request's order in it, the model, the four token counters, the SDK's retries, the latency, the
outcome, the input sent and the structured output or tool call received. `agent.db` is never
versioned. It never holds the request headers, so no API key or Authorization header.

`reasoning_summary` stays NULL: only a later flag fills it (§5, T5). A row that cannot be written
is logged and dropped; it never fails the request it describes.
"""

import logging
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

_SCHEMA = """CREATE TABLE IF NOT EXISTS model_requests (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    stage TEXT,
    request_index INTEGER NOT NULL CHECK(request_index >= 1),
    started_at TEXT NOT NULL,
    latency_ms REAL NOT NULL CHECK(latency_ms >= 0),
    outcome TEXT NOT NULL CHECK(outcome IN ('ok', 'error')),
    error_kind TEXT CHECK((outcome = 'error') = (error_kind IS NOT NULL)),
    model TEXT NOT NULL,
    response_model TEXT,
    reasoning_effort TEXT,
    text_format TEXT,
    input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens >= 0),
    cached_input_tokens INTEGER CHECK(cached_input_tokens IS NULL OR cached_input_tokens >= 0),
    cache_write_tokens INTEGER CHECK(cache_write_tokens IS NULL OR cache_write_tokens >= 0),
    output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens >= 0),
    reasoning_tokens INTEGER CHECK(reasoning_tokens IS NULL OR reasoning_tokens >= 0),
    retries INTEGER CHECK(retries IS NULL OR retries >= 0),
    request_sha256 TEXT,
    instructions_sha256 TEXT,
    catalog_vocabulary TEXT,
    input_json TEXT,
    output_json TEXT,
    reasoning_summary TEXT)"""
_INDEX = "CREATE INDEX IF NOT EXISTS model_requests_by_run ON model_requests(run_id, sequence)"

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelRequestRow:
    run_id: str | None
    stage: str | None
    # 1 for the first request of the stage; the router's catalog lookup makes a second one.
    request_index: int
    started_at: str
    latency_ms: float
    outcome: Literal["ok", "error"]
    error_kind: str | None
    # The model the agent asked for, and the snapshot that answered.
    model: str
    response_model: str | None
    reasoning_effort: str | None
    # The structured-output model the request forced (its JSON schema carries the shortlist).
    text_format: str | None
    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    # None when no HTTP attempt was observed (a test double instead of the SDK client).
    retries: int | None
    # The instructions and the input; the instructions alone. The router's instructions carry the
    # catalog vocabulary of the feed at that moment, which a scenario may mutate.
    request_sha256: str | None
    instructions_sha256: str | None
    catalog_vocabulary: str | None
    input_json: str | None
    output_json: str | None


class SqliteModelRequests:
    def __init__(self, path: Path) -> None:
        self._path = path

    def initialize(self) -> None:
        """Create the table on a new or an existing database; running it again changes nothing."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.execute(_SCHEMA)
            connection.execute(_INDEX)

    def record(self, row: ModelRequestRow) -> None:
        values = asdict(row)
        try:
            with closing(sqlite3.connect(self._path, timeout=5)) as connection, connection:
                connection.execute(
                    f"INSERT INTO model_requests ({', '.join(values)}) "
                    f"VALUES ({', '.join('?' * len(values))})",
                    tuple(values.values()),
                )
        except sqlite3.Error:
            _log.warning("Model request not recorded: %s", row.stage)
