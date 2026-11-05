"""`conditional_decisions`: append-only record of conditional decisions.

docs/design/p0-conditional-intent.md §3.7. `SqliteAgentStore.initialize()` creates it in every
mode, Human Present and Not Present, so the oracle can always read it. One run (the turn's
`turn_run_id` or the HNP task's run) holds its `decision` rows and, after a checkout, its
`checkout_check` row; the oracle reads the last decision. Triggers refuse UPDATE, DELETE and any
insert that is not the run's next sequence, for every SQL writer. Bodies are canonical JSON
without buyer data, tokens or model text.
"""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

from ..governance.canonical import canonical_bytes

ConditionalKind = Literal["decision", "checkout_check"]
ConditionalOutcome = Literal[
    "branch",
    "none",
    "undetermined",
    "clarify",
    "exclusions_only",
    "checkout_matches",
    "offer_changed",
]
DECISION_OUTCOMES: tuple[ConditionalOutcome, ...] = (
    "branch",
    "none",
    "undetermined",
    "clarify",
    "exclusions_only",
)
CHECK_OUTCOMES: tuple[ConditionalOutcome, ...] = ("checkout_matches", "offer_changed")
# The outcomes that name a branch: the chosen one and, after it, its checkout.
WITH_BRANCH: tuple[ConditionalOutcome, ...] = ("branch", "checkout_matches", "offer_changed")


def _sql_list(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


_SCHEMA = (
    f"""CREATE TABLE IF NOT EXISTS conditional_decisions (
        run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 256),
        sequence INTEGER NOT NULL CHECK(typeof(sequence) = 'integer' AND sequence >= 1),
        kind TEXT NOT NULL CHECK(kind IN ('decision','checkout_check')),
        outcome TEXT NOT NULL CHECK(
            (kind = 'decision' AND outcome IN ({_sql_list(DECISION_OUTCOMES)}))
            OR (kind = 'checkout_check' AND outcome IN ({_sql_list(CHECK_OUTCOMES)}))),
        branch_index INTEGER CHECK(branch_index IS NULL
            OR (typeof(branch_index) = 'integer' AND branch_index BETWEEN 0 AND 2)),
        variant_id TEXT CHECK(variant_id IS NULL OR length(variant_id) BETWEEN 1 AND 128),
        body TEXT NOT NULL CHECK(json_valid(body)),
        recorded_at TEXT NOT NULL,
        PRIMARY KEY(run_id, sequence),
        CHECK((outcome IN ({_sql_list(WITH_BRANCH)}))
            = (branch_index IS NOT NULL AND variant_id IS NOT NULL)),
        CHECK((branch_index IS NULL) = (variant_id IS NULL)))""",
    # Also stops INSERT OR REPLACE, which would evict a row without firing the delete trigger.
    """CREATE TRIGGER IF NOT EXISTS conditional_decisions_sequence
        BEFORE INSERT ON conditional_decisions
        WHEN NEW.sequence IS NOT COALESCE(
            (SELECT MAX(sequence) + 1 FROM conditional_decisions WHERE run_id = NEW.run_id), 1)
        BEGIN SELECT RAISE(ABORT, 'conditional_decisions_sequence_conflict'); END""",
    """CREATE TRIGGER IF NOT EXISTS conditional_decisions_no_update
        BEFORE UPDATE ON conditional_decisions
        BEGIN SELECT RAISE(ABORT, 'conditional_decisions_append_only'); END""",
    """CREATE TRIGGER IF NOT EXISTS conditional_decisions_no_delete
        BEFORE DELETE ON conditional_decisions
        BEGIN SELECT RAISE(ABORT, 'conditional_decisions_append_only'); END""",
)
_COLUMNS = "run_id, sequence, kind, outcome, branch_index, variant_id, body, recorded_at"


@dataclass(frozen=True, slots=True)
class ConditionalEntry:
    """What to append; the store adds the run, the sequence and the time."""

    kind: ConditionalKind
    outcome: ConditionalOutcome
    body: Mapping[str, object]
    branch_index: int | None = None
    variant_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in get_args(ConditionalKind):
            raise ValueError("Unknown conditional record kind")
        if self.outcome not in get_args(ConditionalOutcome):
            raise ValueError("Unknown conditional outcome")
        index = self.branch_index
        if index is not None and (type(index) is not int or not 0 <= index <= 2):
            raise ValueError("A branch index is an integer from 0 to 2")


@dataclass(frozen=True, slots=True)
class ConditionalRecord:
    run_id: str
    sequence: int
    kind: ConditionalKind
    outcome: ConditionalOutcome
    branch_index: int | None
    variant_id: str | None
    body: dict[str, Any]
    recorded_at: str


def initialize_conditional_decisions(connection: sqlite3.Connection) -> None:
    """Idempotent: an older database gains the table and keeps every other row."""
    for statement in _SCHEMA:
        connection.execute(statement)


def append_conditional(
    connection: sqlite3.Connection, run_id: str, entry: ConditionalEntry, recorded_at: str
) -> ConditionalRecord:
    text = canonical_bytes(dict(entry.body)).decode()
    connection.execute("BEGIN IMMEDIATE")
    try:
        (sequence,) = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM conditional_decisions WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        connection.execute(
            f"INSERT INTO conditional_decisions ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                sequence,
                entry.kind,
                entry.outcome,
                entry.branch_index,
                entry.variant_id,
                text,
                recorded_at,
            ),
        )
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
    return ConditionalRecord(
        run_id,
        sequence,
        entry.kind,
        entry.outcome,
        entry.branch_index,
        entry.variant_id,
        json.loads(text),
        recorded_at,
    )


def read_conditional(connection: sqlite3.Connection, run_id: str) -> tuple[ConditionalRecord, ...]:
    rows = connection.execute(
        f"SELECT {_COLUMNS} FROM conditional_decisions WHERE run_id = ? ORDER BY sequence",
        (run_id,),
    ).fetchall()
    return tuple(
        ConditionalRecord(
            row[0], row[1], row[2], row[3], row[4], row[5], json.loads(row[6]), row[7]
        )
        for row in rows
    )
