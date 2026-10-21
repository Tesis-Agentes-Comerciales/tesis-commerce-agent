"""Append-only AP2 consent and effect evidence, chained by digest within each run.

`approvals` keeps the signed mandates and is rewritten in place; `completion_results` keeps the
receipts. This table links them, at most once per run and kind: `consent` when the approval is
saved and `effect` when the order is recorded (docs/design/hp-p2-evidence-chain.md). Bodies carry
hashes, identifiers and receipt references: never a mandate, a receipt, the delegated token or
buyer data. It is created where `hp_evidence` is, so `off` databases never see it.
"""

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..governance.canonical import canonical_bytes, sha256_digest

Ap2RecordKind = Literal["consent", "effect"]

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS ap2_evidence (
        run_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK(typeof(sequence) = 'integer' AND sequence >= 1),
        record_kind TEXT NOT NULL CHECK(record_kind IN ('consent','effect')),
        body TEXT NOT NULL CHECK(json_valid(body)),
        previous_digest TEXT CHECK((sequence = 1) = (previous_digest IS NULL)),
        digest TEXT NOT NULL UNIQUE,
        PRIMARY KEY(run_id, sequence),
        UNIQUE(run_id, record_kind))""",
    # Raised before any conflict resolution, so INSERT OR REPLACE cannot evict a record either:
    # not by a repeated sequence or digest, and not by a second record of the same kind.
    """CREATE TRIGGER IF NOT EXISTS ap2_evidence_chain BEFORE INSERT ON ap2_evidence
        WHEN NEW.sequence != COALESCE(
                (SELECT MAX(sequence) + 1 FROM ap2_evidence WHERE run_id = NEW.run_id), 1)
            OR NEW.previous_digest IS NOT (SELECT digest FROM ap2_evidence
                WHERE run_id = NEW.run_id ORDER BY sequence DESC LIMIT 1)
            OR EXISTS (SELECT 1 FROM ap2_evidence WHERE digest = NEW.digest)
            OR EXISTS (SELECT 1 FROM ap2_evidence
                WHERE run_id = NEW.run_id AND record_kind = NEW.record_kind)
        BEGIN SELECT RAISE(ABORT, 'ap2_evidence_chain_conflict'); END""",
    """CREATE TRIGGER IF NOT EXISTS ap2_evidence_no_update BEFORE UPDATE ON ap2_evidence
        BEGIN SELECT RAISE(ABORT, 'ap2_evidence_append_only'); END""",
    """CREATE TRIGGER IF NOT EXISTS ap2_evidence_no_delete BEFORE DELETE ON ap2_evidence
        BEGIN SELECT RAISE(ABORT, 'ap2_evidence_append_only'); END""",
)
_SELECT = "SELECT run_id, sequence, record_kind, body, previous_digest, digest FROM ap2_evidence"


@dataclass(frozen=True, slots=True)
class Ap2Evidence:
    run_id: str
    sequence: int
    record_kind: Ap2RecordKind
    body: dict[str, Any]
    previous_digest: str | None
    digest: str


class SqliteAp2EvidenceStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            for statement in _SCHEMA:
                connection.execute(statement)

    def append(
        self, run_id: str, record_kind: Ap2RecordKind, body: Mapping[str, object]
    ) -> Ap2Evidence:
        """The next link of the run. A second record of the same kind raises IntegrityError."""
        text = canonical_bytes(dict(body)).decode()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            last = connection.execute(
                "SELECT sequence, digest FROM ap2_evidence WHERE run_id = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            sequence, previous = (1, None) if last is None else (last[0] + 1, last[1])
            digest = record_digest(run_id, sequence, record_kind, text, previous)
            connection.execute(
                "INSERT INTO ap2_evidence VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, sequence, record_kind, text, previous, digest),
            )
        return Ap2Evidence(run_id, sequence, record_kind, json.loads(text), previous, digest)

    def for_run(self, run_id: str) -> tuple[Ap2Evidence, ...]:
        with closing(self._connect()) as connection:
            return read_run(connection, run_id)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection


def read_run(connection: sqlite3.Connection, run_id: str) -> tuple[Ap2Evidence, ...]:
    """A run's records in order, from any connection: the verifier reads read-only."""
    rows = connection.execute(f"{_SELECT} WHERE run_id = ? ORDER BY sequence", (run_id,)).fetchall()
    return tuple(
        Ap2Evidence(row[0], row[1], row[2], json.loads(row[3]), row[4], row[5]) for row in rows
    )


def chain_is_valid(run_id: str, records: tuple[Ap2Evidence, ...]) -> bool:
    previous = None
    for expected, record in enumerate(records, start=1):
        text = canonical_bytes(record.body).decode()
        if (
            record.run_id != run_id
            or record.sequence != expected
            or record.previous_digest != previous
            or record.digest
            != record_digest(run_id, record.sequence, record.record_kind, text, previous)
        ):
            return False
        previous = record.digest
    return True


def record_digest(
    run_id: str, sequence: int, record_kind: str, body: str, previous: str | None
) -> str:
    link = [run_id, sequence, record_kind, body, previous]
    return sha256_digest(b"ap2-evidence/v1\0" + canonical_bytes(link))
