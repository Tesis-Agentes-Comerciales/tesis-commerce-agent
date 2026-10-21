"""Real temporary SQLite + reference evaluator; no commercial integration."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from commerce_agent.governance.canonical import canonical_bytes, parse_model
from commerce_agent.governance.evaluator import (
    D1PolicyBoundary,
    ReferencePolicyEvaluator,
    measure_reference_engine,
    parse_input,
)
from commerce_agent.governance.evidence import (
    ApplicationFailure,
    DecisionEvidence,
    DecisionRecord,
    EvidenceApplication,
    EvidenceError,
    EvidenceScope,
    InputRejected,
    parse_evidence,
)
from commerce_agent.governance.evidence_artifacts import (
    capture_reference_sources,
    logical_result,
)
from commerce_agent.governance.hnp_policy import build_hnp_policy
from commerce_agent.governance.models import SCHEMA, EvaluationMetadata, SelectionPolicyInput
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.sqlite import SqliteAgentStore

FIXTURES = Path(__file__).parent / "fixtures"


@contextmanager
def connect(path: Path):
    connection = sqlite3.connect(path, timeout=5)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


@dataclass
class Case:
    data: SelectionPolicyInput
    policy: bytes
    sources: bytes
    scope: EvidenceScope
    record: DecisionRecord


def make_case(**changes: Any) -> Case:
    payload = json.loads((FIXTURES / "d1-input.json").read_bytes())
    payload.update(changes)
    policy = (FIXTURES / "d1-policy.json").read_bytes()
    if payload["purchase_mode"] == "HNP":
        # An HNP decision evaluates only b2-hnp-v1 (C02), whose D1 row adds POL-11.
        policy, binding = build_hnp_policy(merchant_ref=payload["merchant_ref"])
        payload["expected_policy"] = binding.model_dump(mode="json")
        payload["selected_unit_minor"] = payload["intent"]["max_unit_minor"]
    data = parse_input(canonical_bytes(payload))
    identity = measure_reference_engine()
    metadata = EvaluationMetadata(
        schema_version=SCHEMA, decided_at="2026-10-14T15:00:00.005Z", latency_ms=5
    )
    decision = ReferencePolicyEvaluator(
        artifact_bytes=policy, binding=data.expected_policy, identity=identity, metadata=metadata
    ).evaluate(data)
    refs = (
        data.intent.intent_ref,
        data.feed_snapshot_ref,
        data.eligibility_algorithm_ref,
        data.clock_profile_ref,
    )
    scope = parse_model(
        EvidenceScope,
        canonical_bytes(
            {
                "schema_version": SCHEMA,
                "run_id": data.run_id,
                "trace_id": data.trace_id,
                "owner_ref": data.owner_ref,
                "purchase_mode": data.purchase_mode,
                "governance_mode": data.governance_mode,
                "phase": "D1",
                "context_version": 1,
                "policy_binding": data.expected_policy.model_dump(mode="json"),
                "source_refs": [ref.model_dump(mode="json") for ref in refs],
                "policy_artifact_ref": {
                    "schema_version": SCHEMA,
                    "id": "artifact-policy-1",
                    "version": 1,
                    "digest": data.expected_policy.artifact_digest,
                },
                "engine_artifact_ref": {
                    "schema_version": SCHEMA,
                    "id": "artifact-engine-1",
                    "version": 1,
                    "digest": identity.artifact_digest,
                },
            }
        ),
    )
    record = DecisionRecord(
        schema_version=SCHEMA,
        record_kind="decision_record",
        evidence_id="evidence-1",
        sequence=1,
        recorded_at="2026-10-14T15:00:00.010Z",
        canonicalization_profile="commerce-canonical-json/1",
        canonical_input_bytes=canonical_bytes(data).decode(),
        decision=decision,
        policy_artifact_ref=scope.policy_artifact_ref,
        engine_artifact_ref=scope.engine_artifact_ref,
        source_refs=refs,
        application=EvidenceApplication(
            schema_version=SCHEMA,
            disposition="observed_shadow" if data.governance_mode == "shadow" else "blocked",
            transition_ref=None,
            expected_state_version=data.expected_local_state_version,
            resulting_state_version=None,
        ),
        replayability="full",
    )
    return Case(data, policy, capture_reference_sources(), scope, record)


@pytest.fixture
def case() -> Case:
    return make_case()


@pytest.fixture
def ledger(tmp_path: Path, case: Case):
    with connect(tmp_path / "evidence.sqlite") as connection:
        store = SqliteGovernanceEvidenceStore(connection)
        connection.execute("BEGIN IMMEDIATE")
        store.initialize()
        store.register_episode(case.scope, policy_bytes=case.policy, engine_bytes=case.sources)
        connection.commit()
        yield store


def append(
    store: SqliteGovernanceEvidenceStore, evidence: DecisionEvidence, state: int = 1
) -> None:
    if isinstance(evidence, DecisionRecord) and evidence.decision.origin == "policy":
        store.prepare_decision(evidence)
    with store.connection:
        store.connection.execute("BEGIN IMMEDIATE")
        store.record_decision(evidence, expected_state_version=state)


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
@pytest.mark.parametrize("purchase_mode", ["HP", "HNP"])
@pytest.mark.parametrize("selected", ["variant-1", "variant-absent"])
def test_write_read_replay(tmp_path: Path, mode: str, purchase_mode: str, selected: str) -> None:
    case = make_case(
        governance_mode=mode, purchase_mode=purchase_mode, selected_variant_id=selected
    )
    path = tmp_path / "roundtrip.sqlite"
    with connect(path) as connection:
        store = SqliteGovernanceEvidenceStore(connection)
        connection.execute("BEGIN IMMEDIATE")
        store.initialize()
        store.register_episode(case.scope, policy_bytes=case.policy, engine_bytes=case.sources)
        connection.commit()
        store.prepare_decision(case.record)
        connection.execute("BEGIN IMMEDIATE")
        store.record_decision(case.record, expected_state_version=1)
    with connect(path) as connection:
        store = SqliteGovernanceEvidenceStore(connection)
        assert store.get("evidence-1") == case.record
        assert store.for_run(case.data.run_id) == (case.record,)
        assert logical_result(store.replay("evidence-1")) == logical_result(case.record.decision)
        archived = connection.execute(
            "SELECT policy_bytes,engine_bytes FROM governance_episodes"
        ).fetchone()
        assert archived == (case.policy, case.sources)


def test_c09_identity_idempotency_and_conflict(ledger: SqliteGovernanceEvidenceStore, case: Case):
    append(ledger, case.record)
    append(ledger, case.record)
    other = case.record.model_copy(update={"recorded_at": "2026-10-14T15:00:00.011Z"})
    with pytest.raises(EvidenceError, match=r"^GOV_STATE_CONFLICT$"):
        append(ledger, other)
    assert ledger.for_run(case.data.run_id) == (case.record,)


@pytest.mark.parametrize("sequence", [0, 2, 4, True, "1"])
def test_c09_bad_sequence(ledger: SqliteGovernanceEvidenceStore, case: Case, sequence: Any):
    bad = case.record.model_copy(update={"sequence": sequence})
    with pytest.raises(EvidenceError):
        append(ledger, bad)
    assert ledger.for_run(case.data.run_id) == ()


def test_decision_id_unique(ledger: SqliteGovernanceEvidenceStore, case: Case):
    append(ledger, case.record)
    duplicate = case.record.model_copy(update={"evidence_id": "evidence-2", "sequence": 2})
    with pytest.raises(EvidenceError, match=r"^GOV_STATE_CONFLICT$"):
        append(ledger, duplicate)


@pytest.mark.parametrize(
    "table", ["governance_schema", "governance_episodes", "governance_evidence"]
)
@pytest.mark.parametrize("operation", ["update", "delete", "replace"])
def test_sql_append_only(
    ledger: SqliteGovernanceEvidenceStore, case: Case, table: str, operation: str
):
    append(ledger, case.record)
    with pytest.raises(sqlite3.IntegrityError), ledger.connection:
        if operation == "update":
            column = {
                "governance_schema": "version",
                "governance_episodes": "run_id",
                "governance_evidence": "evidence_id",
            }[table]
            ledger.connection.execute(f"UPDATE {table} SET {column}={column}")
        elif operation == "delete":
            ledger.connection.execute(f"DELETE FROM {table}")
        else:
            ledger.connection.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}")
    assert ledger.get(case.record.evidence_id) == case.record
    assert not hasattr(ledger, "delete") and not hasattr(ledger, "update")


def test_c11_rollback_after_local_write(ledger: SqliteGovernanceEvidenceStore, case: Case):
    ledger.connection.execute("CREATE TABLE local_state(value INTEGER)")
    ledger.prepare_decision(case.record)
    with pytest.raises(RuntimeError, match="local failure"), ledger.connection:
        ledger.connection.execute("BEGIN IMMEDIATE")
        ledger.record_decision(case.record, expected_state_version=1)
        ledger.connection.execute("INSERT INTO local_state VALUES (2)")
        raise RuntimeError("local failure")
    assert ledger.get(case.record.evidence_id) is None
    assert ledger.connection.execute("SELECT * FROM local_state").fetchall() == []
    append(ledger, case.record)
    assert ledger.get(case.record.evidence_id) == case.record


class TransitionVerifier:
    def verify(self, connection: sqlite3.Connection, run_id: str, application: EvidenceApplication):
        row = connection.execute("SELECT run_id,version,transition_id FROM local_state").fetchone()
        return row == (run_id, application.resulting_state_version, application.transition_ref)


def committed(case: Case) -> DecisionRecord:
    return case.record.model_copy(
        update={
            "application": EvidenceApplication(
                schema_version=SCHEMA,
                disposition="transition_committed",
                transition_ref="transition-1",
                expected_state_version=1,
                resulting_state_version=2,
            )
        }
    )


def test_c11_same_transaction_commit(ledger: SqliteGovernanceEvidenceStore, case: Case):
    connection = ledger.connection
    connection.execute("CREATE TABLE local_state(run_id TEXT,version INTEGER,transition_id TEXT)")
    ledger.prepare_decision(committed(case))
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO local_state VALUES (?,2,'transition-1')", (case.data.run_id,)
        )
        ledger.record_decision(
            committed(case), expected_state_version=1, transition_verifier=TransitionVerifier()
        )
    assert ledger.get(case.record.evidence_id) == committed(case)
    assert connection.execute("SELECT version FROM local_state").fetchone()[0] == 2


def test_transition_cannot_be_claimed_without_local_verification(
    ledger: SqliteGovernanceEvidenceStore,
    case: Case,
):
    with pytest.raises(EvidenceError, match=r"^GOV_STATE_CONFLICT$"):
        append(ledger, committed(case))
    assert ledger.get(case.record.evidence_id) is None


def test_c11_failed_evidence_rolls_back_local_transition(
    ledger: SqliteGovernanceEvidenceStore,
    case: Case,
):
    connection = ledger.connection
    connection.execute("CREATE TABLE local_state(version INTEGER)")
    connection.execute("""CREATE TRIGGER fail_write BEFORE INSERT ON governance_evidence
                        BEGIN SELECT RAISE(ABORT,'synthetic_write_failure'); END""")
    allow_before = canonical_bytes(case.record.decision)
    ledger.prepare_decision(case.record)
    with pytest.raises(EvidenceError), connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO local_state VALUES (2)")
        ledger.record_decision(case.record, expected_state_version=1)
    assert connection.execute("SELECT * FROM local_state").fetchall() == []
    assert ledger.get(case.record.evidence_id) is None
    assert canonical_bytes(case.record.decision) == allow_before
    connection.execute("DROP TRIGGER fail_write")
    failure = ApplicationFailure(
        schema_version=SCHEMA,
        record_kind="application_failure",
        evidence_id="write-failed",
        sequence=1,
        recorded_at=case.record.recorded_at,
        run_id=case.data.run_id,
        trace_id=case.data.trace_id,
        decision_id=case.data.decision_id,
        reason_code="GOV_EVIDENCE_UNAVAILABLE",
        expected_state_version=1,
        observed_state_version=None,
        replayability="not_evaluable",
    )
    append(ledger, failure)
    assert ledger.get(failure.evidence_id) == failure


@pytest.mark.parametrize("failure_kind", ["state", "evidence"])
def test_application_failure_keeps_original_decision(
    ledger: SqliteGovernanceEvidenceStore,
    case: Case,
    failure_kind: str,
):
    append(ledger, case.record)
    failure = ApplicationFailure(
        schema_version=SCHEMA,
        record_kind="application_failure",
        evidence_id="application-failed",
        sequence=2,
        recorded_at=case.record.recorded_at,
        run_id=case.data.run_id,
        trace_id=case.data.trace_id,
        decision_id=case.data.decision_id,
        reason_code="GOV_STATE_CONFLICT" if failure_kind == "state" else "GOV_EVIDENCE_UNAVAILABLE",
        expected_state_version=1,
        observed_state_version=2,
        replayability="not_evaluable",
    )
    append(ledger, failure)
    assert ledger.for_run(case.data.run_id) == (case.record, failure)
    assert ledger.get(case.record.evidence_id) == case.record


@pytest.mark.parametrize(
    "field",
    [
        "owner_ref",
        "trace_id",
        "run_id",
        "context_version",
        "feed_snapshot_ref",
        "policy_version",
    ],
)
def test_foreign_episode_or_version(
    ledger: SqliteGovernanceEvidenceStore,
    case: Case,
    field: str,
):
    payload = case.data.model_dump(mode="json")
    if field == "context_version":
        payload[field] = 2
    elif field == "feed_snapshot_ref":
        payload[field]["id"] = "foreign-feed"
    elif field == "policy_version":
        payload["expected_policy"]["version"] = 2
    else:
        payload[field] = "foreign-id"
    # Even if an attacker updates the digest and output correlation, configured scope wins.
    data = parse_input(canonical_bytes(payload))
    from commerce_agent.governance.canonical import input_digest

    decision = case.record.decision.model_copy(
        update={
            "input_digest": input_digest(data),
            "policy_binding": data.expected_policy,
        }
    )
    bad = case.record.model_copy(
        update={
            "canonical_input_bytes": canonical_bytes(data).decode(),
            "decision": decision,
            "source_refs": (
                data.intent.intent_ref,
                data.feed_snapshot_ref,
                data.eligibility_algorithm_ref,
                data.clock_profile_ref,
            ),
        }
    )
    with pytest.raises(EvidenceError):
        append(ledger, bad)
    assert ledger.for_run(case.data.run_id) == ()


@pytest.mark.parametrize(
    "mutation",
    [
        "digest",
        "decision_id",
        "allow_fail",
        "missing_rule",
        "fake_allow",
        "noncanonical",
        "source_missing",
        "source_duplicate",
        "source_foreign",
        "artifact_id",
        "artifact_digest",
        "state",
        "schema",
        "replayability",
        "missing_artifact",
    ],
)
def test_invalid_record(
    ledger: SqliteGovernanceEvidenceStore,
    case: Case,
    mutation: str,
):
    body = case.record.model_dump(mode="json")
    if mutation == "digest":
        body["decision"]["input_digest"] = "sha256:" + "0" * 64
    elif mutation == "decision_id":
        body["decision"]["decision_id"] = "foreign-decision"
    elif mutation == "allow_fail":
        body["decision"]["rule_results"][0]["result"] = "fail"
    elif mutation == "missing_rule":
        body["decision"]["rule_results"].pop()
    elif mutation == "fake_allow":
        from commerce_agent.governance.canonical import input_digest

        payload = case.data.model_dump(mode="json")
        payload["selected_variant_id"] = "not-eligible"
        data = parse_input(canonical_bytes(payload))
        body["canonical_input_bytes"] = canonical_bytes(data).decode()
        body["decision"]["input_digest"] = input_digest(data)
    elif mutation == "noncanonical":
        body["canonical_input_bytes"] += " "
    elif mutation == "source_missing":
        body["source_refs"].pop()
    elif mutation == "source_duplicate":
        body["source_refs"] *= 2
    elif mutation == "source_foreign":
        foreign = dict(body["source_refs"][0])
        foreign["id"] = "foreign-source"
        body["source_refs"].append(foreign)
    elif mutation == "artifact_id":
        body["policy_artifact_ref"]["id"] = "foreign-artifact"
    elif mutation == "artifact_digest":
        body["engine_artifact_ref"]["digest"] = "sha256:" + "0" * 64
    elif mutation == "state":
        body["application"]["expected_state_version"] = 2
    elif mutation == "schema":
        body["schema_version"] = "commerce-policy/2.0"
    elif mutation == "replayability":
        body["replayability"] = "policy_not_evaluated"
    elif mutation == "missing_artifact":
        body["engine_artifact_ref"] = None
    with pytest.raises(EvidenceError):
        bad = parse_evidence(canonical_bytes(body))
        append(ledger, bad)
    assert ledger.for_run(case.data.run_id) == ()


@pytest.mark.parametrize(
    "secret_field",
    ["jwt", "bearer", "address", "prompt", "llm_response", "payment_mandate", "private_key"],
)
def test_c10_secrets_never_persist(
    ledger: SqliteGovernanceEvidenceStore,
    case: Case,
    secret_field: str,
    caplog: pytest.LogCaptureFixture,
):
    marker = "SYNTHETIC-SENSITIVE-VALUE"
    body = case.record.model_dump(mode="json")
    body[secret_field] = marker
    with pytest.raises(EvidenceError) as exc:
        parse_evidence(canonical_bytes(body))
    assert marker not in str(exc.value) and marker not in caplog.text
    rejected = InputRejected(
        schema_version=SCHEMA,
        record_kind="input_rejected",
        evidence_id="rejected-1",
        sequence=1,
        recorded_at=case.record.recorded_at,
        run_id=case.data.run_id,
        trace_id=case.data.trace_id,
        decision_id=case.data.decision_id,
        reason_code="GOV_INPUT_INVALID",
        safe_field_paths=("intent",),
        replayability="not_evaluable",
    )
    append(ledger, rejected)
    assert ledger.get("rejected-1") == rejected
    serialized = ledger.connection.execute("SELECT body FROM governance_evidence").fetchone()[0]
    assert marker not in serialized and secret_field not in json.loads(serialized)
    assert "canonical_input_bytes" not in serialized and "input_digest" not in serialized


@pytest.mark.parametrize("path", ["jwt", "attacker-prompt", "SYNTHETIC-SECRET"])
def test_c10_safe_diagnostic_path_catalog(case: Case, path: str):
    body = {
        "schema_version": SCHEMA,
        "record_kind": "input_rejected",
        "evidence_id": "bad",
        "sequence": 1,
        "recorded_at": case.record.recorded_at,
        "run_id": case.data.run_id,
        "trace_id": case.data.trace_id,
        "decision_id": case.data.decision_id,
        "reason_code": "GOV_INPUT_INVALID",
        "safe_field_paths": [path],
        "replayability": "not_evaluable",
    }
    with pytest.raises(EvidenceError, match=r"^GOV_INPUT_INVALID$"):
        parse_evidence(canonical_bytes(body))


@pytest.mark.parametrize(
    "payload",
    [b"not-json", b'{"record_kind":"input_rejected","record_kind":"decision_record"}', b"{}"],
)
def test_invalid_evidence_json(payload: bytes):
    with pytest.raises(EvidenceError, match=r"^GOV_INPUT_INVALID$"):
        parse_evidence(payload)


def test_rejects_no_transaction_and_no_fk(
    tmp_path: Path, ledger: SqliteGovernanceEvidenceStore, case: Case
):
    with sqlite3.connect(tmp_path / "no-fk.sqlite") as connection, pytest.raises(EvidenceError):
        SqliteGovernanceEvidenceStore(connection)
    with pytest.raises(EvidenceError):
        ledger.record_decision(case.record, expected_state_version=1)
    with pytest.raises(EvidenceError):
        ledger.initialize()
    assert not ledger.connection.in_transaction


def test_version_regression(ledger: SqliteGovernanceEvidenceStore, case: Case):
    with pytest.raises(EvidenceError, match=r"^GOV_STATE_CONFLICT$"):
        append(ledger, case.record, 2)
    append(ledger, case.record)
    rejected = InputRejected(
        schema_version=SCHEMA,
        record_kind="input_rejected",
        evidence_id="rejected",
        sequence=2,
        recorded_at=case.record.recorded_at,
        run_id=case.data.run_id,
        trace_id=case.data.trace_id,
        decision_id="decision-next",
        reason_code="GOV_INPUT_INVALID",
        safe_field_paths=(),
        replayability="not_evaluable",
    )
    append(ledger, rejected, 2)
    regressed = rejected.model_copy(update={"evidence_id": "regressed", "sequence": 3})
    with pytest.raises(EvidenceError, match=r"^GOV_STATE_CONFLICT$"):
        append(ledger, regressed, 1)


def test_episode_registration_cannot_change(ledger: SqliteGovernanceEvidenceStore, case: Case):
    with ledger.connection:
        ledger.connection.execute("BEGIN IMMEDIATE")
        ledger.register_episode(case.scope, policy_bytes=case.policy, engine_bytes=case.sources)
    with pytest.raises(EvidenceError), ledger.connection:
        ledger.connection.execute("BEGIN IMMEDIATE")
        ledger.register_episode(
            case.scope.model_copy(update={"owner_ref": "other-owner"}),
            policy_bytes=case.policy,
            engine_bytes=case.sources,
        )


@pytest.mark.parametrize("artifact", ["policy", "engine"])
def test_corrupt_artifact_registration(tmp_path: Path, case: Case, artifact: str):
    with connect(tmp_path / "corrupt.sqlite") as connection:
        store = SqliteGovernanceEvidenceStore(connection)
        with pytest.raises(EvidenceError), connection:
            connection.execute("BEGIN IMMEDIATE")
            store.initialize()
            store.register_episode(
                case.scope,
                policy_bytes=b"{}" if artifact == "policy" else case.policy,
                engine_bytes=b"{}" if artifact == "engine" else case.sources,
            )
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'governance_%'"
        ).fetchall()


def test_runtime_guard_not_replayable(tmp_path: Path, case: Case):
    scope = case.scope.model_copy(update={"policy_artifact_ref": None, "engine_artifact_ref": None})
    port = D1PolicyBoundary(
        binding=case.data.expected_policy,
        artifact_bytes=None,
        engine_identity=measure_reference_engine(),
        metadata=EvaluationMetadata(
            schema_version=SCHEMA, decided_at=case.record.recorded_at, latency_ms=0
        ),
        evaluator_factory=lambda: (_ for _ in ()).throw(AssertionError("must not evaluate")),
    )
    record = case.record.model_copy(
        update={
            "decision": port.evaluate(case.data),
            "policy_artifact_ref": None,
            "engine_artifact_ref": None,
            "replayability": "policy_not_evaluated",
        }
    )
    with connect(tmp_path / "guard.sqlite") as connection:
        store = SqliteGovernanceEvidenceStore(connection)
        connection.execute("BEGIN IMMEDIATE")
        store.initialize()
        store.register_episode(scope, policy_bytes=None, engine_bytes=None)
        store.record_decision(record, expected_state_version=1)
        assert store.get(record.evidence_id) == record
        with pytest.raises(EvidenceError, match=r"^GOV_FACTS_UNAVAILABLE$"):
            store.replay(record.evidence_id)


def test_existing_sqlite_compatibility(tmp_path: Path, case: Case):
    path = tmp_path / "existing.sqlite"
    commercial = SqliteAgentStore(path)
    commercial.initialize()
    commercial.create_purchase(case.data.run_id, "synthetic request")
    before = commercial.purchase(case.data.run_id)
    with connect(path) as connection:
        schema_before = connection.execute(
            "SELECT name,sql FROM sqlite_master "
            "WHERE tbl_name NOT LIKE 'governance_%' ORDER BY name"
        ).fetchall()
        store = SqliteGovernanceEvidenceStore(connection)
        connection.execute("BEGIN IMMEDIATE")
        store.initialize()
        store.register_episode(case.scope, policy_bytes=case.policy, engine_bytes=case.sources)
        connection.commit()
        store.prepare_decision(case.record)
        connection.execute("BEGIN IMMEDIATE")
        store.record_decision(case.record, expected_state_version=1)
        connection.commit()
        assert (
            connection.execute(
                "SELECT name,sql FROM sqlite_master "
                "WHERE tbl_name NOT LIKE 'governance_%' ORDER BY name"
            ).fetchall()
            == schema_before
        )
        connection.execute("BEGIN IMMEDIATE")
        store.initialize()
        connection.commit()
    commercial.initialize()
    assert commercial.purchase(case.data.run_id) == before
    commercial.update_purchase(case.data.run_id, stage="selected", selected_variant_id="variant-1")
    after = commercial.purchase(case.data.run_id)
    assert after is not None and after["stage"] == "selected"
    with connect(path) as connection:
        assert SqliteGovernanceEvidenceStore(connection).get(case.record.evidence_id) == case.record


def test_incompatible_schema_rolls_back(tmp_path: Path):
    with connect(tmp_path / "incompatible.sqlite") as connection:
        connection.execute(
            "CREATE TABLE governance_schema(singleton INTEGER PRIMARY KEY,version INTEGER)"
        )
        connection.execute("INSERT INTO governance_schema VALUES (1,2)")
        connection.commit()
        with pytest.raises(EvidenceError), connection:
            connection.execute("BEGIN IMMEDIATE")
            SqliteGovernanceEvidenceStore(connection).initialize()
        assert connection.execute("SELECT version FROM governance_schema").fetchone() == (2,)
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='governance_evidence'"
        ).fetchall()


@pytest.mark.parametrize("collision", ["sequence", "evidence_id"])
def test_c09_concurrent_conflict(tmp_path: Path, case: Case, collision: str):
    path = tmp_path / "concurrent.sqlite"
    with connect(path) as connection:
        store = SqliteGovernanceEvidenceStore(connection)
        connection.execute("BEGIN IMMEDIATE")
        store.initialize()
        store.register_episode(case.scope, policy_bytes=case.policy, engine_bytes=case.sources)

    def worker(index: int) -> bool:
        payload = case.data.model_dump(mode="json")
        payload["decision_id"] = f"decision-{index}"
        other = make_case(**payload).record.model_copy(
            update={"evidence_id": "same-id" if collision == "evidence_id" else f"evidence-{index}"}
        )
        with connect(path) as connection:
            store = SqliteGovernanceEvidenceStore(connection)
            try:
                append(store, other)
                return True
            except EvidenceError as exc:
                assert exc.code == "GOV_STATE_CONFLICT"
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, [1, 2])) == [False, True]
    with connect(path) as connection:
        store = SqliteGovernanceEvidenceStore(connection)
        assert len(store.for_run(case.data.run_id)) == 1
        next_record = make_case(decision_id="next-decision").record.model_copy(
            update={"evidence_id": "next-evidence", "sequence": 2}
        )
        append(store, next_record)
        assert [item.sequence for item in store.for_run(case.data.run_id)] == [1, 2]


def test_read_detects_corruption(ledger: SqliteGovernanceEvidenceStore, case: Case):
    append(ledger, case.record)
    # Models protect normal API/SQL writes. This simulates privileged corruption;
    # it is detection, not a claim of tamper resistance against an administrator.
    with ledger.connection:
        ledger.connection.execute("DROP TRIGGER governance_evidence_no_update")
        ledger.connection.execute("UPDATE governance_evidence SET body_digest='sha256:corrupt'")
    with pytest.raises(EvidenceError, match=r"^GOV_INPUT_INVALID$"):
        ledger.get(case.record.evidence_id)


def test_readonly_write_failure_recovery(tmp_path: Path, case: Case):
    path = tmp_path / "readonly.sqlite"
    with connect(path) as connection:
        store = SqliteGovernanceEvidenceStore(connection)
        connection.execute("BEGIN IMMEDIATE")
        store.initialize()
        store.register_episode(case.scope, policy_bytes=case.policy, engine_bytes=case.sources)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        store = SqliteGovernanceEvidenceStore(connection)
        store.prepare_decision(case.record)
        with pytest.raises(EvidenceError, match=r"^GOV_EVIDENCE_UNAVAILABLE$"), connection:
            connection.execute("BEGIN")
            store.record_decision(case.record, expected_state_version=1)
    with connect(path) as connection:
        store = SqliteGovernanceEvidenceStore(connection)
        assert store.get(case.record.evidence_id) is None
        append(store, case.record)
        assert store.get(case.record.evidence_id) == case.record


def test_engine_verification_is_outside_sql_transaction(
    ledger: SqliteGovernanceEvidenceStore,
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
):
    original = ReferencePolicyEvaluator.evaluate
    calls = []

    def checked_evaluate(self, data):
        assert not ledger.connection.in_transaction
        calls.append(data.decision_id)
        return original(self, data)

    monkeypatch.setattr(ReferencePolicyEvaluator, "evaluate", checked_evaluate)
    ledger.prepare_decision(case.record)
    assert calls == [case.data.decision_id]
    with ledger.connection:
        ledger.connection.execute("BEGIN IMMEDIATE")
        ledger.record_decision(case.record, expected_state_version=1)
    assert calls == [case.data.decision_id]


def test_unprepared_policy_output_cannot_be_written(
    ledger: SqliteGovernanceEvidenceStore,
    case: Case,
):
    with pytest.raises(EvidenceError, match=r"^GOV_OUTPUT_INVALID$"), ledger.connection:
        ledger.connection.execute("BEGIN IMMEDIATE")
        ledger.record_decision(case.record, expected_state_version=1)
    assert ledger.get(case.record.evidence_id) is None
    with pytest.raises(EvidenceError), ledger.connection:
        ledger.connection.execute("BEGIN IMMEDIATE")
        ledger.prepare_decision(case.record)


def test_preparation_does_not_admit_different_bytes(
    ledger: SqliteGovernanceEvidenceStore,
    case: Case,
):
    ledger.prepare_decision(case.record)
    changed = case.record.model_copy(update={"recorded_at": "2026-10-14T15:00:00.011Z"})
    with pytest.raises(EvidenceError, match=r"^GOV_OUTPUT_INVALID$"), ledger.connection:
        ledger.connection.execute("BEGIN IMMEDIATE")
        ledger.record_decision(changed, expected_state_version=1)


def test_sqlite_row_factory_compatible(tmp_path: Path, case: Case):
    with connect(tmp_path / "row.sqlite") as connection:
        connection.row_factory = sqlite3.Row
        store = SqliteGovernanceEvidenceStore(connection)
        connection.execute("BEGIN IMMEDIATE")
        store.initialize()
        store.register_episode(case.scope, policy_bytes=case.policy, engine_bytes=case.sources)
        connection.commit()
        append(store, case.record)
        assert store.get(case.record.evidence_id) == case.record


def test_c10_nested_sensitive_input(case: Case):
    body = case.record.model_dump(mode="json")
    payload = case.data.model_dump(mode="json")
    payload["intent"]["address"] = "SYNTHETIC-SENSITIVE-VALUE"
    body["canonical_input_bytes"] = canonical_bytes(payload).decode()
    with pytest.raises(EvidenceError) as exc:
        parse_evidence(canonical_bytes(body))
    assert str(exc.value) == "GOV_INPUT_INVALID"


@pytest.mark.parametrize("field", ["context_version", "policy_version"])
def test_constructed_scope_rejects_coercible_bool(
    tmp_path: Path,
    case: Case,
    field: str,
):
    if field == "context_version":
        scope = case.scope.model_copy(update={"context_version": True})
    else:
        scope = case.scope.model_copy(
            update={
                "policy_binding": case.scope.policy_binding.model_copy(update={"version": True})
            }
        )
    with connect(tmp_path / "coercion.sqlite") as connection:
        store = SqliteGovernanceEvidenceStore(connection)
        connection.execute("BEGIN IMMEDIATE")
        store.initialize()
        connection.commit()
        with pytest.raises(EvidenceError), connection:
            connection.execute("BEGIN IMMEDIATE")
            store.register_episode(scope, policy_bytes=case.policy, engine_bytes=case.sources)
        assert connection.execute("SELECT * FROM governance_episodes").fetchall() == []


def test_known_foreign_decision_cannot_be_used_for_application_failure(
    ledger: SqliteGovernanceEvidenceStore,
    case: Case,
):
    append(ledger, case.record)
    other = make_case(run_id="run-2", trace_id="trace-2", decision_id="decision-2")
    with ledger.connection:
        ledger.connection.execute("BEGIN IMMEDIATE")
        ledger.register_episode(other.scope, policy_bytes=other.policy, engine_bytes=other.sources)
    failure = ApplicationFailure(
        schema_version=SCHEMA,
        record_kind="application_failure",
        evidence_id="foreign-failure",
        sequence=1,
        recorded_at=case.record.recorded_at,
        run_id="run-2",
        trace_id="trace-2",
        decision_id=case.data.decision_id,
        reason_code="GOV_EVIDENCE_UNAVAILABLE",
        expected_state_version=1,
        observed_state_version=None,
        replayability="not_evaluable",
    )
    with pytest.raises(EvidenceError, match=r"^GOV_INPUT_INVALID$"):
        append(ledger, failure)
    assert ledger.for_run("run-2") == ()


def test_sql_foreign_key_rejects_unregistered_episode(
    ledger: SqliteGovernanceEvidenceStore,
    case: Case,
):
    from commerce_agent.governance.canonical import sha256_digest

    append(ledger, case.record)
    cursor = ledger.connection.execute("SELECT * FROM governance_evidence")
    row = dict(zip((col[0] for col in cursor.description), cursor.fetchone(), strict=True))
    fresh = make_case(run_id="unregistered-run", decision_id="unregistered-decision")
    record = fresh.record.model_copy(update={"evidence_id": "unregistered-evidence"})
    row["evidence_id"] = record.evidence_id
    row["run_id"] = fresh.data.run_id
    row["decision_id"] = fresh.data.decision_id
    row["input_digest"] = record.decision.input_digest
    row["canonical_input"] = record.canonical_input_bytes
    body = record.model_dump(mode="json")
    row["body"] = canonical_bytes(body).decode()
    row["body_digest"] = sha256_digest(row["body"].encode())
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), ledger.connection:
        ledger.connection.execute(
            "INSERT INTO governance_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(row.values()),
        )


@pytest.mark.parametrize("fault", ["shadow", "missing_reference", "no_version", "old_version"])
def test_inconsistent_transition_claims(case: Case, fault: str):
    body = committed(case).model_dump(mode="json")
    if fault == "shadow":
        data = case.data.model_dump(mode="json")
        data["governance_mode"] = "shadow"
        body["canonical_input_bytes"] = canonical_bytes(parse_input(canonical_bytes(data))).decode()
        body["decision"]["governance_mode"] = "shadow"
    elif fault == "missing_reference":
        body["application"]["transition_ref"] = None
    elif fault == "no_version":
        body["application"]["resulting_state_version"] = None
    else:
        body["application"]["resulting_state_version"] = 1
    with pytest.raises(EvidenceError, match=r"^GOV_INPUT_INVALID$"):
        parse_evidence(canonical_bytes(body))


@pytest.mark.parametrize("response", [False, 1, {"matches": True}])
def test_transition_verifier_requires_explicit_true(
    ledger: SqliteGovernanceEvidenceStore,
    case: Case,
    response: Any,
):
    class InvalidVerifier:
        def verify(self, connection, run_id, application) -> Any:
            return response

    record = committed(case)
    ledger.prepare_decision(record)
    with pytest.raises(EvidenceError, match=r"^GOV_STATE_CONFLICT$"), ledger.connection:
        ledger.connection.execute("BEGIN IMMEDIATE")
        ledger.record_decision(
            record, expected_state_version=1, transition_verifier=InvalidVerifier()
        )
    assert ledger.get(record.evidence_id) is None
