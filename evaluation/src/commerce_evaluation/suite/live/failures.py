"""Who a failure of the agent's model counts against, by the product's own kind (eval-suite.md §4).

The product names why each model call failed (`ModelFailureKind`,
src/commerce_agent/llm/model_failures.py). The executor reads that name where the product left
it, never deriving it again:

- presente: `model_failure` in the body of the Agent API's 502, 503 or 504 (api.py), the kind of
  the router's `IntentInterpretationError`;
- delegado: `failure_kind` in the task's chained `delegation_events`: `task_finished` for the
  selector (with `selection_unavailable`), `choice_reviewed` for the reviewer.

Not from the `model_requests` ledger: it keeps the HTTP status, the provider's code and (since
#217) its message, not the kind, and an answer outside the contract can end a request the ledger
calls `ok`. Its failed rows go into the record as the evidence.

`rejected` and `invalid_output` count against the agent (trial.AGENT_FAILURES): the trial is
graded, never repeated, and its trace keeps the error. Any other kind is the infrastructure's
(`api_5xx`, observe.infra_failure); `quota_exhausted` also stops the run (suite/run.py).
"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

from commerce_agent.llm.model_failures import ModelFailureKind
from commerce_evaluation.suite.trial import AGENT_FAILURES, ModelFailure, Row

# The ledger's columns that tell a failed request apart, all of them older than this module.
EVIDENCE = ("stage", "error_kind", "http_status", "provider_error_code", "retries")
# The provider's own words, redacted and cut by the product, when the ledger has the column: a
# database written before it lacks it, and then the row says None.
MESSAGE = "provider_error_message"


@dataclass(frozen=True)
class Seen:
    """A kind the product named, and where: "POST /conversation/turn 502", or the event."""

    kind: ModelFailureKind
    source: str


def kind_of(value: object) -> ModelFailureKind | None:
    """The kind, when `value` is one the product names; anything else names none."""
    return next((kind for kind in get_args(ModelFailureKind) if kind == value), None)


def task_failure(database: Path) -> Seen | None:
    """The kind of the last chained event that names one: the selector's or the reviewer's."""
    rows = _rows(
        database,
        "delegation_events",
        "SELECT event_type, json_extract(body, '$.failure_kind'), "
        "COALESCE(json_extract(body, '$.outcome_code'), json_extract(body, '$.reason')) "
        "FROM delegation_events WHERE event_type IN ('task_finished', 'choice_reviewed') "
        "ORDER BY rowid",
    )
    seen = [Seen(kind, f"{event} {end}") for event, named, end in rows if (kind := kind_of(named))]
    return seen[-1] if seen else None


def failed_requests(database: Path) -> tuple[Row, ...]:
    """The ledger's requests that failed, in order, with what tells them apart."""
    columns = _columns(database, "model_requests")
    message = MESSAGE if MESSAGE in columns else "NULL"
    rows = _rows(
        database,
        "model_requests",
        f"SELECT {', '.join(EVIDENCE)}, {message} FROM model_requests WHERE outcome = 'error' "
        "ORDER BY sequence",
    )
    return tuple(dict(zip((*EVIDENCE, MESSAGE), row, strict=True)) for row in rows)


def decided(seen: Seen | None, database: Path) -> ModelFailure | None:
    """The record's decision on the trial's model failure, with the ledger as its evidence."""
    if seen is None:
        return None
    return ModelFailure(
        kind=seen.kind,
        counts_against="agent" if seen.kind in AGENT_FAILURES else "infrastructure",
        source=seen.source,
        requests=failed_requests(database),
    )


def _columns(database: Path, table: str) -> set[str]:
    return {row[1] for row in _rows(database, table, f"PRAGMA table_info({table})")}


def _rows(database: Path, table: str, query: str) -> list[tuple[Any, ...]]:
    """The query's rows, or none while the trial's database or the table does not exist."""
    if not database.exists():
        return []
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone() is None:
            return []
        return db.execute(query).fetchall()
