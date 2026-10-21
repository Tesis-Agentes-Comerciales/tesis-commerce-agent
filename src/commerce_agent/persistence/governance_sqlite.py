"""Explicit, additive SQLite ledger; no writes to commercial purchase state.

    ledger.prepare_decision(evidence)  # evaluation/verification outside SQL transaction
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        ledger.record_decision(evidence, expected_state_version=version)
        # Application-owned local writes; any exception rolls everything back.

Initialize separately on an explicitly chosen database, never at runtime import.
No methods open connections, commit, roll back, evaluate commercial authority or
update purchase state. FK must already be enabled on the supplied connection.
"""

import sqlite3

from pydantic import TypeAdapter

from ..governance.canonical import canonical_bytes, parse_model, sha256_digest
from ..governance.evaluator import measure_reference_engine
from ..governance.evidence import (
    ApplicationFailure,
    DecisionEvidence,
    DecisionRecord,
    EvidenceError,
    EvidenceScope,
    parse_evidence,
    scope_input,
    validate_evidence,
)
from ..governance.evidence_artifacts import (
    ReferenceSourceArchive,
    replay_reference,
    verify_artifacts,
)
from ..governance.evidence_ports import LocalTransitionVerifier
from ..governance.models import Id, PolicyDecision

_TABLES = (
    """CREATE TABLE IF NOT EXISTS governance_schema (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        version INTEGER NOT NULL CHECK(version = 1))""",
    """CREATE TABLE IF NOT EXISTS governance_episodes (
        run_id TEXT PRIMARY KEY NOT NULL,
        scope TEXT NOT NULL CHECK(json_valid(scope)),
        policy_bytes BLOB, engine_bytes BLOB)""",
    """CREATE TABLE IF NOT EXISTS governance_evidence (
        evidence_id TEXT PRIMARY KEY NOT NULL,
        run_id TEXT NOT NULL REFERENCES governance_episodes(run_id),
        sequence INTEGER NOT NULL CHECK(typeof(sequence) = 'integer' AND sequence >= 1),
        decision_id TEXT NOT NULL,
        record_kind TEXT NOT NULL CHECK(record_kind IN
            ('decision_record','input_rejected','application_failure')),
        phase TEXT NOT NULL CHECK(phase IN ('D1','D2','D3')),
        governance_mode TEXT NOT NULL CHECK(governance_mode IN ('shadow','enforce')),
        policy_version INTEGER NOT NULL CHECK(policy_version >= 1),
        expected_state_version INTEGER NOT NULL CHECK(expected_state_version >= 1),
        input_digest TEXT,
        canonical_input TEXT,
        outcome TEXT CHECK(outcome IN ('allow','deny')),
        body TEXT NOT NULL CHECK(json_valid(body)),
        body_digest TEXT NOT NULL,
        UNIQUE(run_id, sequence),
        CHECK(evidence_id = json_extract(body, '$.evidence_id')),
        CHECK(sequence = json_extract(body, '$.sequence')),
        CHECK(record_kind = json_extract(body, '$.record_kind')),
        CHECK((record_kind = 'decision_record' AND canonical_input IS NOT NULL
               AND input_digest IS NOT NULL AND outcome IS NOT NULL)
           OR (record_kind != 'decision_record' AND canonical_input IS NULL
               AND input_digest IS NULL AND outcome IS NULL)))""",
    """CREATE UNIQUE INDEX IF NOT EXISTS governance_decision_identity
        ON governance_evidence(decision_id) WHERE record_kind = 'decision_record'""",
    """CREATE TRIGGER IF NOT EXISTS governance_sequence BEFORE INSERT ON governance_evidence
        WHEN NEW.sequence != COALESCE(
            (SELECT MAX(sequence) + 1 FROM governance_evidence WHERE run_id = NEW.run_id), 1)
        BEGIN SELECT RAISE(ABORT, 'governance_sequence_conflict'); END""",
    """CREATE TRIGGER IF NOT EXISTS governance_state_version BEFORE INSERT ON governance_evidence
        WHEN NEW.expected_state_version < COALESCE(
            (SELECT MAX(expected_state_version) FROM governance_evidence
             WHERE run_id = NEW.run_id), 1)
        BEGIN SELECT RAISE(ABORT, 'governance_state_conflict'); END""",
    """CREATE TRIGGER IF NOT EXISTS governance_decision_scope BEFORE INSERT ON governance_evidence
        WHEN EXISTS(SELECT 1 FROM governance_evidence
                    WHERE decision_id = NEW.decision_id AND run_id != NEW.run_id)
        BEGIN SELECT RAISE(ABORT, 'governance_decision_scope_conflict'); END""",
)

_COLUMNS = {
    "governance_schema": ("singleton", "version"),
    "governance_episodes": ("run_id", "scope", "policy_bytes", "engine_bytes"),
    "governance_evidence": (
        "evidence_id",
        "run_id",
        "sequence",
        "decision_id",
        "record_kind",
        "phase",
        "governance_mode",
        "policy_version",
        "expected_state_version",
        "input_digest",
        "canonical_input",
        "outcome",
        "body",
        "body_digest",
    ),
}


class SqliteGovernanceEvidenceStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._prepared: set[str] = set()
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")

    def _transaction(self) -> None:
        if (
            not self.connection.in_transaction
            or self.connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        ):
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")

    def initialize(self) -> None:
        """Additive DDL in caller transaction. No migration of existing business data."""
        self._transaction()
        try:
            for statement in _TABLES:
                self.connection.execute(statement)
            for table, expected in _COLUMNS.items():
                actual = tuple(
                    row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")
                )
                if actual != expected:
                    raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
            self.connection.execute(
                "INSERT INTO governance_schema(singleton, version) SELECT 1, 1 "
                "WHERE NOT EXISTS (SELECT 1 FROM governance_schema)"
            )
            versions = [
                row[0] for row in self.connection.execute("SELECT version FROM governance_schema")
            ]
            if versions != [1]:
                raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
            for table, key in (
                ("governance_schema", "singleton"),
                ("governance_episodes", "run_id"),
                ("governance_evidence", "evidence_id"),
            ):
                for action in ("UPDATE", "DELETE"):
                    self.connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                        f"BEFORE {action} ON {table} "
                        "BEGIN SELECT RAISE(ABORT, 'governance_append_only'); END"
                    )
                # Prevent INSERT OR REPLACE from bypassing deletion triggers even
                # when SQLite recursive_triggers is disabled on another connection.
                self.connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table} "
                    f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {key} = NEW.{key}) "
                    "BEGIN SELECT RAISE(ABORT, 'governance_identity_conflict'); END"
                )
        except EvidenceError:
            raise
        except sqlite3.Error:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None

    def register_episode(
        self,
        scope: EvidenceScope,
        *,
        policy_bytes: bytes | None,
        engine_bytes: bytes | None,
    ) -> None:
        self._transaction()
        try:
            scope = EvidenceScope.model_validate(scope)
            verify_artifacts(scope, policy_bytes, engine_bytes)
            if engine_bytes is not None:
                archive = parse_model(ReferenceSourceArchive, engine_bytes)
                if archive.engine_digest() != measure_reference_engine().artifact_digest:
                    raise EvidenceError("GOV_ARTIFACT_MISMATCH")
            body = canonical_bytes(scope).decode("utf-8")
            old = self.connection.execute(
                "SELECT scope, policy_bytes, engine_bytes FROM governance_episodes "
                "WHERE run_id = ?",
                (scope.run_id,),
            ).fetchone()
            if old is not None:
                if tuple(old) != (body, policy_bytes, engine_bytes):
                    raise EvidenceError("GOV_INPUT_INVALID")
                return
            self.connection.execute(
                "INSERT INTO governance_episodes(run_id,scope,policy_bytes,engine_bytes) "
                "VALUES (?,?,?,?)",
                (scope.run_id, body, policy_bytes, engine_bytes),
            )
        except EvidenceError:
            raise
        except Exception:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None

    def initialize_shadow_scopes(self) -> None:
        """Opt-in additive extension; legacy episode registration stays immutable."""
        self._transaction()
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS governance_d1_shadow_scopes (
            decision_id TEXT PRIMARY KEY NOT NULL,
            run_id TEXT NOT NULL REFERENCES governance_episodes(run_id),
            scope TEXT NOT NULL CHECK(json_valid(scope)))"""
        )
        columns = tuple(
            row[1]
            for row in self.connection.execute("PRAGMA table_info(governance_d1_shadow_scopes)")
        )
        if columns != ("decision_id", "run_id", "scope"):
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        for action in ("UPDATE", "DELETE"):
            self.connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS governance_d1_shadow_scopes_no_{action.lower()} "
                f"BEFORE {action} ON governance_d1_shadow_scopes "
                "BEGIN SELECT RAISE(ABORT, 'governance_append_only'); END"
            )
        self.connection.execute(
            """CREATE TRIGGER IF NOT EXISTS governance_d1_shadow_scopes_no_replace
            BEFORE INSERT ON governance_d1_shadow_scopes
            WHEN EXISTS(SELECT 1 FROM governance_d1_shadow_scopes WHERE decision_id=NEW.decision_id)
            BEGIN SELECT RAISE(ABORT, 'governance_identity_conflict'); END"""
        )

    def register_shadow_scope(self, decision_id: str, scope: EvidenceScope) -> None:
        self._transaction()
        try:
            TypeAdapter(Id).validate_python(decision_id, strict=True)
            scope = EvidenceScope.model_validate(scope)
            base, _, _ = self._episode(scope.run_id)
            if (
                scope.governance_mode != "shadow"
                or base.model_copy(update={"source_refs": scope.source_refs}) != scope
            ):
                raise EvidenceError("GOV_INPUT_INVALID")
            body = canonical_bytes(scope).decode()
            old = self.connection.execute(
                "SELECT run_id,scope FROM governance_d1_shadow_scopes WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if old is not None:
                if tuple(old) != (scope.run_id, body):
                    raise EvidenceError("GOV_INPUT_INVALID")
                return
            # A scope cannot be retrospectively attached to a legacy decision.
            if self.connection.execute(
                "SELECT 1 FROM governance_evidence WHERE decision_id=?", (decision_id,)
            ).fetchone():
                raise EvidenceError("GOV_INPUT_INVALID")
            self.connection.execute(
                "INSERT INTO governance_d1_shadow_scopes VALUES (?,?,?)",
                (decision_id, scope.run_id, body),
            )
        except EvidenceError:
            raise
        except Exception:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None

    def _episode(
        self, run_id: str, decision_id: str | None = None
    ) -> tuple[EvidenceScope, bytes | None, bytes | None]:
        row = self.connection.execute(
            "SELECT scope,policy_bytes,engine_bytes FROM governance_episodes WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise EvidenceError("GOV_INPUT_INVALID")
        scope = parse_model(EvidenceScope, row[0].encode("utf-8"))
        if scope.run_id != run_id:
            raise EvidenceError("GOV_INPUT_INVALID")
        if (
            decision_id is not None
            and self.connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='governance_d1_shadow_scopes'"
            ).fetchone()
        ):
            registered = self.connection.execute(
                "SELECT run_id,scope FROM governance_d1_shadow_scopes WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if registered is not None:
                specific = parse_model(EvidenceScope, registered[1].encode())
                if (
                    registered[0] != run_id
                    or specific.governance_mode != "shadow"
                    or (scope.model_copy(update={"source_refs": specific.source_refs}) != specific)
                ):
                    raise EvidenceError("GOV_INPUT_INVALID")
                scope = specific
        policy_bytes, engine_bytes = row[1], row[2]
        if decision_id is not None and self._has_table("governance_decision_scopes"):
            registered = self.connection.execute(
                "SELECT run_id,scope,policy_bytes,engine_bytes FROM governance_decision_scopes "
                "WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if registered is not None:
                specific = parse_model(EvidenceScope, registered[1].encode())
                if registered[0] != run_id or not _same_episode(scope, specific):
                    raise EvidenceError("GOV_INPUT_INVALID")
                scope, policy_bytes, engine_bytes = specific, registered[2], registered[3]
        verify_artifacts(scope, policy_bytes, engine_bytes)
        return scope, policy_bytes, engine_bytes

    def _has_table(self, name: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )

    def _admits_phase(self, phase: str) -> bool:
        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='governance_evidence'"
        ).fetchone()
        return row is None or f"'{phase}'" in row[0]

    def admits_phase_d2(self) -> bool:
        """False on a ledger created before D2: its `phase` column only admits D1."""
        return self._admits_phase("D2")

    def admits_phase_d3(self) -> bool:
        """False on a ledger created before D3-HNP: its `phase` column stops at D2."""
        return self._admits_phase("D3")

    def on_connection(self, connection: sqlite3.Connection) -> "SqliteGovernanceEvidenceStore":
        """This store's admissions over another connection, so a record prepared here can be
        written inside a unit of work that another store opened (the delegation ledger's hooks)."""
        store = SqliteGovernanceEvidenceStore(connection)
        store._prepared = self._prepared
        return store

    def initialize_decision_scopes(self) -> None:
        """Opt-in additive extension: the scope of one later-phase decision (D2 or D3).

        A D2 or D3 decision is evaluated under the policy and engine of its own moment, so it
        keeps their bytes here instead of reusing the episode's D1 registration.
        """
        self._transaction()
        try:
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS governance_decision_scopes (
                decision_id TEXT PRIMARY KEY NOT NULL,
                run_id TEXT NOT NULL REFERENCES governance_episodes(run_id),
                scope TEXT NOT NULL CHECK(json_valid(scope)),
                policy_bytes BLOB, engine_bytes BLOB)"""
            )
            columns = tuple(
                row[1]
                for row in self.connection.execute("PRAGMA table_info(governance_decision_scopes)")
            )
            if columns != ("decision_id", "run_id", "scope", "policy_bytes", "engine_bytes"):
                raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
            for action in ("UPDATE", "DELETE"):
                self.connection.execute(
                    "CREATE TRIGGER IF NOT EXISTS "
                    f"governance_decision_scopes_no_{action.lower()} "
                    f"BEFORE {action} ON governance_decision_scopes "
                    "BEGIN SELECT RAISE(ABORT, 'governance_append_only'); END"
                )
            self.connection.execute(
                """CREATE TRIGGER IF NOT EXISTS governance_decision_scopes_no_replace
                BEFORE INSERT ON governance_decision_scopes
                WHEN EXISTS(SELECT 1 FROM governance_decision_scopes
                            WHERE decision_id=NEW.decision_id)
                BEGIN SELECT RAISE(ABORT, 'governance_identity_conflict'); END"""
            )
        except EvidenceError:
            raise
        except sqlite3.Error:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None

    def register_decision_scope(
        self,
        decision_id: str,
        scope: EvidenceScope,
        *,
        policy_bytes: bytes | None,
        engine_bytes: bytes | None,
    ) -> None:
        """Bind a D2 or D3 decision to its scope before its evidence exists; never afterwards."""
        self._transaction()
        try:
            TypeAdapter(Id).validate_python(decision_id, strict=True)
            scope = EvidenceScope.model_validate(scope)
            base, _, _ = self._episode(scope.run_id)
            if scope.phase not in ("D2", "D3") or not _same_episode(base, scope):
                raise EvidenceError("GOV_INPUT_INVALID")
            verify_artifacts(scope, policy_bytes, engine_bytes)
            if engine_bytes is not None:
                archive = parse_model(ReferenceSourceArchive, engine_bytes)
                if archive.engine_digest() != measure_reference_engine().artifact_digest:
                    raise EvidenceError("GOV_ARTIFACT_MISMATCH")
            body = canonical_bytes(scope).decode()
            old = self.connection.execute(
                "SELECT run_id,scope,policy_bytes,engine_bytes FROM governance_decision_scopes "
                "WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if old is not None:
                if tuple(old) != (scope.run_id, body, policy_bytes, engine_bytes):
                    raise EvidenceError("GOV_INPUT_INVALID")
                return
            if self.connection.execute(
                "SELECT 1 FROM governance_evidence WHERE decision_id=?", (decision_id,)
            ).fetchone():
                raise EvidenceError("GOV_INPUT_INVALID")
            self.connection.execute(
                "INSERT INTO governance_decision_scopes VALUES (?,?,?,?,?)",
                (decision_id, scope.run_id, body, policy_bytes, engine_bytes),
            )
        except EvidenceError:
            raise
        except Exception:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None

    def record_decision(
        self,
        evidence: DecisionEvidence,
        *,
        expected_state_version: int,
        transition_verifier: LocalTransitionVerifier | None = None,
    ) -> None:
        self._transaction()
        try:
            evidence = validate_evidence(evidence)
            if type(expected_state_version) is not int or expected_state_version < 1:
                raise EvidenceError("GOV_STATE_CONFLICT")
            if isinstance(evidence, DecisionRecord):
                data = evidence.any_input()
                run_id, decision_id = data.run_id, data.decision_id
            else:
                run_id, decision_id = evidence.run_id, evidence.decision_id
            scope, _, _ = self._episode(run_id, decision_id)
            linked_runs = self.connection.execute(
                "SELECT DISTINCT run_id FROM governance_evidence WHERE decision_id = ?",
                (decision_id,),
            ).fetchall()
            if any(row[0] != run_id for row in linked_runs):
                raise EvidenceError("GOV_INPUT_INVALID")
            if isinstance(evidence, DecisionRecord):
                scope_input(scope, evidence.any_input())
                if (
                    any(ref not in scope.source_refs for ref in evidence.source_refs)
                    or (
                        evidence.policy_artifact_ref is not None
                        and evidence.policy_artifact_ref != scope.policy_artifact_ref
                    )
                    or (
                        evidence.engine_artifact_ref is not None
                        and evidence.engine_artifact_ref != scope.engine_artifact_ref
                    )
                ):
                    raise EvidenceError("GOV_INPUT_INVALID")
                if evidence.application.expected_state_version != expected_state_version:
                    raise EvidenceError("GOV_STATE_CONFLICT")
                if (
                    evidence.decision.origin == "policy"
                    and sha256_digest(canonical_bytes(evidence)) not in self._prepared
                ):
                    raise EvidenceError("GOV_OUTPUT_INVALID")
                if evidence.application.disposition == "transition_committed" and (
                    transition_verifier is None
                    or transition_verifier.verify(self.connection, run_id, evidence.application)
                    is not True
                ):
                    raise EvidenceError("GOV_STATE_CONFLICT")
            else:
                if evidence.trace_id != scope.trace_id:
                    raise EvidenceError("GOV_INPUT_INVALID")
                if isinstance(evidence, ApplicationFailure):
                    if evidence.expected_state_version != expected_state_version:
                        raise EvidenceError("GOV_STATE_CONFLICT")
                    parent = self.connection.execute(
                        "SELECT evidence_id FROM governance_evidence "
                        "WHERE run_id = ? AND decision_id = ? AND record_kind = 'decision_record'",
                        (run_id, decision_id),
                    ).fetchone()
                    # GOV_EVIDENCE_UNAVAILABLE can follow a decision whose write
                    # failed. No synthetic parent is created in that case.
                    if parent is None and evidence.reason_code != "GOV_EVIDENCE_UNAVAILABLE":
                        raise EvidenceError("GOV_INPUT_INVALID")
            body = canonical_bytes(evidence).decode("utf-8")
            old = self.connection.execute(
                "SELECT body,expected_state_version FROM governance_evidence WHERE evidence_id = ?",
                (evidence.evidence_id,),
            ).fetchone()
            if old is not None:
                if tuple(old) != (body, expected_state_version):
                    raise EvidenceError("GOV_STATE_CONFLICT")
                return
            record = evidence if isinstance(evidence, DecisionRecord) else None
            self.connection.execute(
                "INSERT INTO governance_evidence "
                "(evidence_id,run_id,sequence,decision_id,record_kind,phase,governance_mode,"
                "policy_version,expected_state_version,input_digest,canonical_input,outcome,"
                "body,body_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence.evidence_id,
                    run_id,
                    evidence.sequence,
                    decision_id,
                    evidence.record_kind,
                    scope.phase,
                    scope.governance_mode,
                    scope.policy_binding.version,
                    expected_state_version,
                    record.decision.input_digest if record else None,
                    record.canonical_input_bytes if record else None,
                    record.decision.outcome if record else None,
                    body,
                    sha256_digest(body.encode("utf-8")),
                ),
            )
        except EvidenceError:
            raise
        except sqlite3.IntegrityError:
            raise EvidenceError("GOV_STATE_CONFLICT") from None
        except Exception:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None

    def prepare_decision(self, evidence: DecisionRecord) -> None:
        """Verify reference result outside a transaction, then admit those exact bytes.

        Admission is local to this store instance and conveys no commercial
        authority. Append revalidates bindings and CAS expectations. Reopening a
        connection requires preparation again; archived code is never executed.
        """
        if self.connection.in_transaction:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        try:
            checked = validate_evidence(evidence)
            if not isinstance(checked, DecisionRecord):
                raise EvidenceError("GOV_INPUT_INVALID")
            scope, policy_bytes, engine_bytes = self._episode(
                checked.any_input().run_id, checked.decision.decision_id
            )
            scope_input(scope, checked.any_input())
            replay_reference(checked, scope, policy_bytes, engine_bytes)
            self._prepared.add(sha256_digest(canonical_bytes(checked)))
        except EvidenceError:
            raise
        except Exception:
            raise EvidenceError("GOV_OUTPUT_INVALID") from None

    def get(self, evidence_id: str) -> DecisionEvidence | None:
        try:
            cursor = self.connection.execute(
                "SELECT * FROM governance_evidence WHERE evidence_id = ?", (evidence_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            values = dict(zip((col[0] for col in cursor.description), tuple(row), strict=True))
            raw = values["body"].encode("utf-8")
            if sha256_digest(raw) != values["body_digest"]:
                raise EvidenceError("GOV_INPUT_INVALID")
            evidence = parse_evidence(raw)
            scope, _, _ = self._episode(values["run_id"], values["decision_id"])
            if isinstance(evidence, DecisionRecord):
                data = evidence.any_input()
                scope_input(scope, data)
                expected = (
                    data.run_id,
                    data.decision_id,
                    evidence.decision.input_digest,
                    evidence.canonical_input_bytes,
                    evidence.decision.outcome,
                )
                actual = tuple(
                    values[key]
                    for key in (
                        "run_id",
                        "decision_id",
                        "input_digest",
                        "canonical_input",
                        "outcome",
                    )
                )
                if actual != expected:
                    raise EvidenceError("GOV_INPUT_INVALID")
                if (
                    any(ref not in scope.source_refs for ref in evidence.source_refs)
                    or (
                        evidence.policy_artifact_ref is not None
                        and evidence.policy_artifact_ref != scope.policy_artifact_ref
                    )
                    or (
                        evidence.engine_artifact_ref is not None
                        and evidence.engine_artifact_ref != scope.engine_artifact_ref
                    )
                    or evidence.application.expected_state_version
                    != values["expected_state_version"]
                ):
                    raise EvidenceError("GOV_INPUT_INVALID")
            elif (
                evidence.run_id != scope.run_id
                or evidence.trace_id != scope.trace_id
                or evidence.decision_id != values["decision_id"]
            ):
                raise EvidenceError("GOV_INPUT_INVALID")
            if (
                evidence.evidence_id != values["evidence_id"]
                or evidence.sequence != values["sequence"]
                or scope.policy_binding.version != values["policy_version"]
                or scope.governance_mode != values["governance_mode"]
                or scope.phase != values["phase"]
            ):
                raise EvidenceError("GOV_INPUT_INVALID")
            return evidence
        except EvidenceError:
            raise
        except Exception:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None

    def for_run(self, run_id: str) -> tuple[DecisionEvidence, ...]:
        try:
            ids = [
                row[0]
                for row in self.connection.execute(
                    "SELECT evidence_id FROM governance_evidence "
                    "WHERE run_id = ? ORDER BY sequence",
                    (run_id,),
                )
            ]
            result = tuple(self.get(evidence_id) for evidence_id in ids)
            if any(item is None for item in result):
                raise EvidenceError("GOV_INPUT_INVALID")
            return tuple(item for item in result if item is not None)
        except EvidenceError:
            raise
        except Exception:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None

    def replay(self, evidence_id: str) -> PolicyDecision:
        try:
            evidence = self.get(evidence_id)
            if not isinstance(evidence, DecisionRecord):
                raise EvidenceError("GOV_FACTS_UNAVAILABLE")
            scope, policy_bytes, engine_bytes = self._episode(
                evidence.any_input().run_id, evidence.decision.decision_id
            )
            return replay_reference(evidence, scope, policy_bytes, engine_bytes)
        except EvidenceError:
            raise
        except Exception:
            raise EvidenceError("GOV_FACTS_UNAVAILABLE") from None


def _same_episode(base: EvidenceScope, scope: EvidenceScope) -> bool:
    """A decision scope belongs to the episode: same run, trace, owner and modes."""
    return (scope.run_id, scope.trace_id, scope.owner_ref, scope.purchase_mode) == (
        base.run_id,
        base.trace_id,
        base.owner_ref,
        base.purchase_mode,
    ) and scope.governance_mode == base.governance_mode
