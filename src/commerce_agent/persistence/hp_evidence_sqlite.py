"""Append-only Human Present evidence, chained by digest within each run.

D1 keeps `governance_evidence`; this table holds the application controls of the governed HP
flow (docs/design/b2-governed-pilot.md). It is created only when one of them is configured, so
`off` databases never see it. Bodies carry no tokens, mandates or buyer data.
"""

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..governance.canonical import canonical_bytes, sha256_digest

HpRecordKind = Literal["freshness_check", "governed_recovery"]

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS hp_evidence (
        run_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK(typeof(sequence) = 'integer' AND sequence >= 1),
        record_kind TEXT NOT NULL CHECK(record_kind IN ('freshness_check','governed_recovery')),
        body TEXT NOT NULL CHECK(json_valid(body)),
        previous_digest TEXT CHECK((sequence = 1) = (previous_digest IS NULL)),
        digest TEXT NOT NULL UNIQUE,
        PRIMARY KEY(run_id, sequence))""",
    # Also stops INSERT OR REPLACE: a replaced row is never the next link of its run.
    """CREATE TRIGGER IF NOT EXISTS hp_evidence_chain BEFORE INSERT ON hp_evidence
        WHEN NEW.sequence != COALESCE(
                (SELECT MAX(sequence) + 1 FROM hp_evidence WHERE run_id = NEW.run_id), 1)
            OR NEW.previous_digest IS NOT (SELECT digest FROM hp_evidence
                WHERE run_id = NEW.run_id ORDER BY sequence DESC LIMIT 1)
        BEGIN SELECT RAISE(ABORT, 'hp_evidence_chain_conflict'); END""",
    """CREATE TRIGGER IF NOT EXISTS hp_evidence_no_update BEFORE UPDATE ON hp_evidence
        BEGIN SELECT RAISE(ABORT, 'hp_evidence_append_only'); END""",
    """CREATE TRIGGER IF NOT EXISTS hp_evidence_no_delete BEFORE DELETE ON hp_evidence
        BEGIN SELECT RAISE(ABORT, 'hp_evidence_append_only'); END""",
)


@dataclass(frozen=True, slots=True)
class HpEvidence:
    run_id: str
    sequence: int
    record_kind: HpRecordKind
    body: dict[str, Any]
    previous_digest: str | None
    digest: str


class SqliteHpEvidenceStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            for statement in _SCHEMA:
                connection.execute(statement)

    def append(
        self, run_id: str, record_kind: HpRecordKind, body: Mapping[str, object]
    ) -> HpEvidence:
        text = canonical_bytes(dict(body)).decode()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            last = connection.execute(
                "SELECT sequence, digest FROM hp_evidence WHERE run_id = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            sequence, previous = (1, None) if last is None else (last[0] + 1, last[1])
            digest = _digest(run_id, sequence, record_kind, text, previous)
            connection.execute(
                "INSERT INTO hp_evidence VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, sequence, record_kind, text, previous, digest),
            )
        return HpEvidence(run_id, sequence, record_kind, json.loads(text), previous, digest)

    def for_run(self, run_id: str) -> tuple[HpEvidence, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT run_id, sequence, record_kind, body, previous_digest, digest "
                "FROM hp_evidence WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return tuple(
            HpEvidence(row[0], row[1], row[2], json.loads(row[3]), row[4], row[5]) for row in rows
        )

    def verify_chain(self, run_id: str) -> bool:
        previous = None
        for expected, record in enumerate(self.for_run(run_id), start=1):
            text = canonical_bytes(record.body).decode()
            if (
                record.sequence != expected
                or record.previous_digest != previous
                or record.digest
                != _digest(run_id, record.sequence, record.record_kind, text, previous)
            ):
                return False
            previous = record.digest
        return True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection


def _digest(run_id: str, sequence: int, record_kind: str, body: str, previous: str | None) -> str:
    link = [run_id, sequence, record_kind, body, previous]
    return sha256_digest(b"hp-evidence/v1\0" + canonical_bytes(link))
