"""Append-only Human Present evidence chained by digest (b2-governed-pilot, «Evidencia HP»)."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from commerce_agent.persistence.hp_evidence_sqlite import SqliteHpEvidenceStore


def store(tmp_path: Path) -> SqliteHpEvidenceStore:
    evidence = SqliteHpEvidenceStore(tmp_path / "agent.db")
    evidence.initialize()
    evidence.initialize()
    return evidence


def check(outcome: str) -> dict[str, object]:
    return {"checkout_id": "chk_1", "outcome": outcome, "observed_revision": 2}


def test_records_chain_per_run_and_verify(tmp_path: Path) -> None:
    evidence = store(tmp_path)

    first = evidence.append("run-a", "freshness_check", check("fresh"))
    evidence.append("run-b", "freshness_check", check("fresh"))
    second = evidence.append("run-a", "freshness_check", check("changed"))

    assert (first.sequence, first.previous_digest) == (1, None)
    assert (second.sequence, second.previous_digest) == (2, first.digest)
    assert [item.body["outcome"] for item in evidence.for_run("run-a")] == ["fresh", "changed"]
    assert evidence.verify_chain("run-a") and evidence.verify_chain("run-b")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE hp_evidence SET body = '{}'",
        "DELETE FROM hp_evidence",
        "INSERT OR REPLACE INTO hp_evidence SELECT * FROM hp_evidence",
    ],
)
def test_rows_cannot_be_rewritten(tmp_path: Path, statement: str) -> None:
    evidence = store(tmp_path)
    evidence.append("run-a", "freshness_check", check("fresh"))

    with (
        closing(sqlite3.connect(tmp_path / "agent.db")) as connection,
        pytest.raises(sqlite3.IntegrityError, match="hp_evidence_"),
    ):
        connection.execute(statement)

    assert evidence.verify_chain("run-a")


def test_replace_cannot_evict_a_row_through_its_digest(tmp_path: Path) -> None:
    evidence = store(tmp_path)
    evidence.append("run-a", "freshness_check", check("fresh"))
    last = evidence.append("run-a", "freshness_check", check("changed"))

    with (
        closing(sqlite3.connect(tmp_path / "agent.db")) as connection,
        pytest.raises(sqlite3.IntegrityError, match="hp_evidence_chain_conflict"),
    ):
        # A valid first link of another run whose digest collides with run-a's last record.
        connection.execute(
            "INSERT OR REPLACE INTO hp_evidence VALUES "
            "('run-x', 1, 'freshness_check', '{}', NULL, ?)",
            (last.digest,),
        )

    assert [item.body["outcome"] for item in evidence.for_run("run-a")] == ["fresh", "changed"]


def test_insert_outside_the_chain_is_rejected(tmp_path: Path) -> None:
    evidence = store(tmp_path)
    evidence.append("run-a", "freshness_check", check("fresh"))

    with (
        closing(sqlite3.connect(tmp_path / "agent.db")) as connection,
        pytest.raises(sqlite3.IntegrityError, match="hp_evidence_chain_conflict"),
    ):
        connection.execute(
            "INSERT INTO hp_evidence VALUES "
            "('run-a', 2, 'freshness_check', '{}', 'sha256:forged', 'sha256:other')"
        )


def test_tampered_body_breaks_the_chain(tmp_path: Path) -> None:
    evidence = store(tmp_path)
    evidence.append("run-a", "freshness_check", check("changed"))
    evidence.append("run-a", "freshness_check", check("changed"))

    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection, connection:
        connection.execute("DROP TRIGGER hp_evidence_no_update")
        connection.execute(
            "UPDATE hp_evidence SET body = json_set(body, '$.outcome', 'fresh') WHERE sequence = 1"
        )

    assert not evidence.verify_chain("run-a")
