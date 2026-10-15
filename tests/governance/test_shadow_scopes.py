"""Validate the additive ledger extension before using it in PurchaseService."""

import sqlite3
from pathlib import Path

import pytest

from commerce_agent.governance.canonical import canonical_bytes
from commerce_agent.governance.evidence import EvidenceError
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from tests.governance.test_evidence import connect, make_case


def test_shadow_scopes_additive_rollback_and_historical_replay(tmp_path: Path) -> None:
    case = make_case(governance_mode="shadow")
    with connect(tmp_path / "scope.db") as connection:
        connection.execute("BEGIN IMMEDIATE")
        ledger = SqliteGovernanceEvidenceStore(connection)
        ledger.initialize()
        ledger.register_episode(case.scope, policy_bytes=case.policy, engine_bytes=case.sources)
        connection.commit()
        ledger.prepare_decision(case.record)
        connection.execute("BEGIN IMMEDIATE")
        ledger.record_decision(case.record, expected_state_version=1)
        connection.commit()
        original = connection.execute("SELECT * FROM governance_episodes").fetchall()
        connection.execute("BEGIN IMMEDIATE")
        ledger.initialize_shadow_scopes()
        connection.rollback()
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='governance_d1_shadow_scopes'"
            ).fetchall()
            == []
        )
        connection.execute("BEGIN IMMEDIATE")
        ledger.initialize_shadow_scopes()
        ledger.initialize_shadow_scopes()
        ref = case.data.feed_snapshot_ref.model_copy(update={"version": 2})
        data = case.data.model_copy(
            update={"decision_id": "decision-new", "feed_snapshot_ref": ref}
        )
        newer = make_case(**data.model_dump(mode="json"))
        scope = case.scope.model_copy(update={"source_refs": newer.scope.source_refs})
        ledger.register_shadow_scope(data.decision_id, scope)
        ledger.register_shadow_scope(data.decision_id, scope)
        connection.commit()
        record = newer.record.model_copy(update={"evidence_id": "evidence-new", "sequence": 2})
        ledger.prepare_decision(record)
        connection.execute("BEGIN IMMEDIATE")
        ledger.record_decision(record, expected_state_version=1)
        connection.commit()
        assert len(ledger.for_run(data.run_id)) == 2
        assert ledger.replay(record.evidence_id).outcome == record.decision.outcome
        assert ledger.get(case.record.evidence_id) == case.record
        assert ledger.replay(case.record.evidence_id).outcome == case.record.decision.outcome
        assert connection.execute("SELECT * FROM governance_episodes").fetchall() == original
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(EvidenceError):
            ledger.register_shadow_scope(case.record.decision.decision_id, scope)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM governance_d1_shadow_scopes")


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_ref", "another-owner"),
        ("context_version", 2),
        ("governance_mode", "enforce"),
        ("trace_id", "other-trace"),
    ],
)
def test_scopes_never_relax_episode_identity(tmp_path: Path, field: str, value: str | int) -> None:
    case = make_case(governance_mode="shadow")
    with connect(tmp_path / "scope.db") as connection:
        connection.execute("BEGIN IMMEDIATE")
        ledger = SqliteGovernanceEvidenceStore(connection)
        ledger.initialize()
        ledger.initialize_shadow_scopes()
        ledger.register_episode(case.scope, policy_bytes=case.policy, engine_bytes=case.sources)
        with pytest.raises(EvidenceError):
            ledger.register_shadow_scope(
                "new-decision", case.scope.model_copy(update={field: value})
            )
        assert (
            canonical_bytes(case.scope)
            == connection.execute("SELECT scope FROM governance_episodes").fetchone()[0].encode()
        )
