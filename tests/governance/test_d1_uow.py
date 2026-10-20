"""Real temporary SQLite, actual snapshots and reference PDP; no remote integration."""

import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from typing import Literal

import pytest
from pydantic import SecretBytes, SecretStr, ValidationError

from commerce_agent.acp.feed import load_product_feed
from commerce_agent.domain import HardConstraints, PurchaseIntent, rank_candidates
from commerce_agent.governance.canonical import canonical_bytes, parse_model
from commerce_agent.governance.evaluator import (
    GovernanceError,
    ReferencePolicyEvaluator,
    measure_reference_engine,
)
from commerce_agent.governance.evidence import (
    ApplicationFailure,
    DecisionRecord,
    EvidenceApplication,
    EvidenceError,
    EvidenceScope,
)
from commerce_agent.governance.evidence_artifacts import capture_reference_sources
from commerce_agent.governance.models import (
    SCHEMA,
    EvaluationMetadata,
    ObjectRef,
    PolicyBinding,
    SelectionPolicyInput,
)
from commerce_agent.governance.provenance import (
    ClockProfile,
    D1Configuration,
    D1EnforceConfiguration,
    ProvenanceKeys,
    build_d1_policy,
)
from commerce_agent.governance.uow import D1Application, D1ApplicationError
from commerce_agent.persistence.d1_uow_sqlite import SqliteD1UnitOfWork
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.provenance_sqlite import SqliteD1ProvenanceStore
from commerce_agent.persistence.sqlite import SqliteAgentStore

OWNER = "synthetic-owner@example.test"
NOW = "2026-10-14T15:00:00.000Z"


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@dataclass
class Case:
    path: Path
    keys: ProvenanceKeys
    policy: bytes
    binding: PolicyBinding
    request: D1Application

    def uow(self, connection: sqlite3.Connection) -> SqliteD1UnitOfWork:
        return SqliteD1UnitOfWork(
            connection, keys=self.keys, policy_bytes=self.policy, policy_binding=self.binding
        )


def build(
    tmp_path: Path,
    *,
    deny: bool = False,
    merchant_denied: bool = False,
    mode: Literal["enforce", "shadow"] = "enforce",
) -> Case:
    path = tmp_path / "uow.sqlite"
    commercial = SqliteAgentStore(path)
    commercial.initialize()
    commercial.create_purchase("run-a", "synthetic private request", OWNER)
    keys = ProvenanceKeys(
        key_id="ephemeral",
        pseudonym_key=SecretBytes(secrets.token_bytes(32)),
        encryption_key=SecretBytes(secrets.token_bytes(32)),
    )
    configuration = parse_model(
        D1Configuration if mode == "shadow" else D1EnforceConfiguration,
        canonical_bytes(
            {
                "governance_mode": mode,
                "merchant_base_url": "http://127.0.0.1:4120",
                "policy_set_id": "uow-d1",
                "policy_revision": 1,
                "currency": "COP",
                "quantity": 1,
                "clock": ClockProfile(
                    authority_source="system_utc",
                    commercial_source="fixed_sandbox",
                    authority_provider=None,
                    commercial_provider=None,
                    fixed_commercial_at=NOW,
                ).model_dump(mode="json"),
            }
        ),
    )
    feed = load_product_feed(Path(__file__).parents[1] / "fixtures/feed")
    intent = PurchaseIntent(hard=HardConstraints(currency="COP"))
    with closing(connect(path)) as connection:
        vault = SqliteD1ProvenanceStore(connection, keys)
        ledger = SqliteGovernanceEvidenceStore(connection)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            vault.initialize()
            vault.register_episode("run-a", OWNER, experiment_run_id=None)
            if merchant_denied:
                policy_bytes, policy_binding = build_d1_policy(
                    policy_set_id="uow-d1", version=1, merchant_ref="merchant-denied"
                )
                context = vault.register_context(
                    "run-a",
                    OWNER,
                    configuration,
                    policy_bytes=policy_bytes,
                    policy_binding=policy_binding,
                )
            else:
                context = vault.register_context("run-a", OWNER, configuration)
            intent_ref = vault.register_intent("run-a", OWNER, intent)
            feed_ref = vault.register_feed("run-a", OWNER, feed)
            facts = vault.intent_facts("run-a", OWNER, intent_ref, context.ref)
            policy = vault.public_source("run-a", OWNER, context.data.policy_artifact_ref, "policy")
            ctx = context.data
            eligible = tuple(
                sorted(item.candidate.variant_id for item in rank_candidates(feed, intent).ranked)
            )
            assert eligible
            data = parse_model(
                SelectionPolicyInput,
                canonical_bytes(
                    {
                        "schema_version": SCHEMA,
                        "decision_id": "decision-a",
                        "run_id": "run-a",
                        "trace_id": ctx.trace_id,
                        "owner_ref": ctx.owner_ref,
                        "purchase_mode": "HP",
                        "governance_mode": mode,
                        "phase": "D1",
                        "action": "prepare_checkout",
                        "context_version": context.ref.version,
                        "expected_local_state_version": 1,
                        "expected_policy": ctx.expected_policy.model_dump(mode="json"),
                        "authority_now": NOW,
                        "commercial_now": NOW,
                        "clock_profile_ref": ctx.clock_profile_ref.model_dump(mode="json"),
                        "experiment_run_id": ctx.experiment_run_id,
                        "conversation_id": ctx.conversation_id,
                        "intent": facts.model_dump(mode="json"),
                        "feed_snapshot_ref": feed_ref.model_dump(mode="json"),
                        "eligibility_algorithm_ref": ctx.eligibility_algorithm_ref.model_dump(
                            mode="json"
                        ),
                        "eligible_variant_ids": eligible,
                        "selected_variant_id": "absent" if deny else eligible[0],
                        "merchant_ref": ctx.merchant_ref,
                    }
                ),
            )
            engine = measure_reference_engine()
            assert engine.artifact_digest is not None
            engine_ref = ObjectRef(
                schema_version=SCHEMA,
                id="reference-engine",
                version=1,
                digest=engine.artifact_digest,
            )
            refs = (
                intent_ref,
                feed_ref,
                ctx.clock_profile_ref,
                ctx.eligibility_algorithm_ref,
                context.ref,
            )
            scope = EvidenceScope(
                schema_version=SCHEMA,
                run_id="run-a",
                trace_id=ctx.trace_id,
                owner_ref=ctx.owner_ref,
                purchase_mode="HP",
                governance_mode=mode,
                phase="D1",
                context_version=context.ref.version,
                policy_binding=ctx.expected_policy,
                source_refs=refs,
                policy_artifact_ref=ctx.policy_artifact_ref,
                engine_artifact_ref=engine_ref,
            )  # type: ignore[arg-type]
            ledger.initialize()
            ledger.register_episode(
                scope, policy_bytes=policy, engine_bytes=capture_reference_sources()
            )
        decision = ReferencePolicyEvaluator(
            artifact_bytes=policy,
            binding=ctx.expected_policy,
            metadata=EvaluationMetadata(schema_version=SCHEMA, decided_at=NOW, latency_ms=1),
            identity=engine,
        ).evaluate(data)
        blocked = decision.outcome == "deny"
        record = DecisionRecord(
            schema_version=SCHEMA,
            record_kind="decision_record",
            evidence_id="evidence-a",
            sequence=1,
            recorded_at=NOW,
            canonicalization_profile="commerce-canonical-json/1",
            canonical_input_bytes=canonical_bytes(data).decode(),
            decision=decision,
            policy_artifact_ref=ctx.policy_artifact_ref,
            engine_artifact_ref=engine_ref,
            source_refs=refs,
            application=EvidenceApplication(
                schema_version=SCHEMA,
                disposition="observed_shadow"
                if mode == "shadow"
                else "blocked"
                if blocked
                else "transition_committed",
                transition_ref=None if blocked or mode == "shadow" else "operation-a",
                expected_state_version=1,
                resulting_state_version=None if blocked or mode == "shadow" else 2,
            ),
            replayability="full",
        )
        request = D1Application(
            schema_version=SCHEMA,
            evidence=record,
            context_ref=context.ref,
            operation_id="operation-a",
            idempotency_key=SecretStr("synthetic-stable-key"),
        )
        result = Case(path, keys, policy, ctx.expected_policy, request)
        uow = result.uow(connection)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            uow.initialize()
    return result


@pytest.fixture
def case(tmp_path: Path) -> Case:
    return build(tmp_path)


def state(connection: sqlite3.Connection) -> tuple:
    return (
        connection.execute(
            "SELECT stage,state_version,selected_variant_id FROM purchases"
        ).fetchone(),
        connection.execute("SELECT count(*) FROM governance_evidence").fetchone()[0],
        connection.execute("SELECT count(*) FROM d1_checkout_operations").fetchone()[0],
        connection.execute("SELECT count(*) FROM operation_keys").fetchone()[0],
    )


def test_allow_cas_ledger_reservation_and_reopen(case: Case) -> None:
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        uow.prepare(case.request)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            result = uow.apply(case.request, owner_id=OWNER)
            assert result.transitioned and result.state_version == 2
            assert connection.in_transaction  # the adapter did not commit
            with closing(connect(case.path)) as observer:
                assert state(observer) == (("received", 1, None), 0, 0, 0)
        assert state(connection)[1:] == (1, 1, 1)
        assert uow.ledger.get("evidence-a") == case.request.evidence
        public_body = canonical_bytes(uow.ledger.get("evidence-a"))
        assert OWNER.encode() not in public_body
        assert b"synthetic-stable-key" not in public_body
        assert b"synthetic private request" not in public_body
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        assert uow.recovery() == (("operation-a", "reserved", None),)
        uow.prepare(case.request)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            retry = uow.apply(case.request, owner_id=OWNER)
        assert retry.reused and not retry.transitioned
        assert state(connection)[1:] == (1, 1, 1)
        assert (
            SqliteAgentStore(case.path).idempotency_key("run-a", "checkout.create")
            == "synthetic-stable-key"
        )


@pytest.mark.parametrize("policy", ["POL-01", "POL-02"])
def test_normative_deny_preserved(tmp_path: Path, policy: str) -> None:
    case = build(tmp_path, deny=policy == "POL-01", merchant_denied=policy == "POL-02")
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        uow.prepare(case.request)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            assert not uow.apply(case.request, owner_id=OWNER).transitioned
            assert not uow.claim("operation-a", claimant="worker-a", owner_id=OWNER)
        assert state(connection) == (("received", 1, None), 1, 0, 0)
        assert uow.ledger.get("evidence-a") == case.request.evidence


@pytest.mark.parametrize(
    "fault", ["ledger-unavailable", "after-update", "fake-transition", "after-reservation"]
)
def test_local_failure_rolls_back_even_when_caught(case: Case, fault: str) -> None:
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        uow.prepare(case.request)
        if fault == "ledger-unavailable":
            connection.execute(
                "CREATE TRIGGER unavailable BEFORE INSERT ON governance_evidence "
                "BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        if fault == "after-update":
            connection.execute(
                "CREATE TRIGGER interrupted AFTER UPDATE ON purchases "
                "BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        if fault == "fake-transition":
            connection.execute(
                "CREATE TRIGGER ignored BEFORE UPDATE ON purchases WHEN NEW.stage='selected' "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        before = state(connection)
        if fault == "after-reservation":
            with pytest.raises(RuntimeError), connection:
                connection.execute("BEGIN IMMEDIATE")
                uow.apply(case.request, owner_id=OWNER)
                assert state(connection)[2] == 1
                raise RuntimeError("crash before commit")
        else:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                with pytest.raises(D1ApplicationError) as caught:
                    uow.apply(case.request, owner_id=OWNER)
                assert caught.value.original.decision == case.request.evidence.decision
                assert state(connection) == before
        assert state(connection) == before


@pytest.mark.parametrize(
    "fault",
    ["stale", "owner", "run", "stage", "intent-changed", "feed-changed", "context", "unprepared"],
)
def test_state_and_binding_rejection(case: Case, fault: str) -> None:
    request = case.request
    commercial = SqliteAgentStore(case.path)
    if fault == "stale":
        commercial.update_purchase("run-a", stage="failed")
        commercial.update_purchase("run-a", stage="received")
    if fault == "stage":
        commercial.update_purchase("run-a", stage="selected")
    if fault == "run":
        commercial.create_purchase("run-b", "different", OWNER)
        data = request.evidence.policy_input().model_copy(update={"run_id": "run-b"})
        record = request.evidence.model_copy(
            update={"canonical_input_bytes": canonical_bytes(data).decode()}
        )
        request = request.model_copy(update={"evidence": record})
    if fault == "context":
        request = request.model_copy(
            update={"context_ref": request.context_ref.model_copy(update={"version": 2})}
        )
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        if fault != "unprepared" and fault != "run":
            uow.prepare(request)
        if fault in ("intent-changed", "feed-changed"):
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if fault == "intent-changed":
                    uow.vault.register_intent(
                        "run-a",
                        OWNER,
                        PurchaseIntent(
                            hard=HardConstraints(currency="COP", max_unit_amount_minor=1)
                        ),
                    )
                else:
                    data = request.evidence.policy_input()
                    feed = uow.vault.recover_feed("run-a", OWNER, data.feed_snapshot_ref)
                    feed.products[0].variants[0].title = "new snapshot"
                    uow.vault.register_feed("run-a", OWNER, feed)
        before = state(connection)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises((D1ApplicationError, ValidationError)):
                uow.apply(request, owner_id="wrong" if fault == "owner" else OWNER)
        assert state(connection) == before


def test_shadow_never_authorizes(tmp_path: Path) -> None:
    case = build(tmp_path, mode="shadow")
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        uow.ledger.prepare_decision(case.request.evidence)
        with pytest.raises(EvidenceError):
            uow.prepare(case.request)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            uow.ledger.record_decision(case.request.evidence, expected_state_version=1)
        assert uow.ledger.get("evidence-a") == case.request.evidence
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(D1ApplicationError):
                uow.apply(case.request, owner_id=OWNER)
        assert state(connection) == (("received", 1, None), 1, 0, 0)


@pytest.mark.parametrize("fault", ["output", "engine", "policy", "application", "key", "operation"])
def test_tampered_request_or_engine_fails_closed(case: Case, fault: str) -> None:
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        uow.prepare(case.request)
        record = case.request.evidence
        if fault == "output":
            record = record.model_copy(
                update={
                    "decision": record.decision.model_copy(update={"reason_codes": ("UNEXPECTED",)})
                }
            )
        if fault == "engine":
            record = record.model_copy(
                update={
                    "decision": record.decision.model_copy(
                        update={
                            "engine": record.decision.engine.model_copy(
                                update={"artifact_digest": "sha256:" + "0" * 64}
                            )
                        }
                    )
                }
            )
        if fault == "policy":
            record = record.model_copy(
                update={
                    "decision": record.decision.model_copy(
                        update={"policy_binding": case.binding.model_copy(update={"version": 2})}
                    )
                }
            )
        if fault == "application":
            record = record.model_copy(
                update={
                    "application": record.application.model_copy(
                        update={"resulting_state_version": 3}
                    )
                }
            )
        request = case.request.model_copy(update={"evidence": record})
        if fault == "key":
            request = request.model_copy(update={"idempotency_key": SecretStr("foreign-key")})
        if fault == "operation":
            request = request.model_copy(update={"operation_id": "operation-foreign"})
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises((EvidenceError, ValidationError)):
                uow.apply(request, owner_id=OWNER)
        assert state(connection) == (("received", 1, None), 0, 0, 0)


def test_concurrent_cas_and_claim_one_winner(case: Case) -> None:
    barrier = Barrier(2)

    def compete(index: int) -> tuple[bool, bool]:
        with closing(connect(case.path)) as connection:
            uow = case.uow(connection)
            uow.prepare(case.request)
            barrier.wait(timeout=30)
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                result = uow.apply(case.request, owner_id=OWNER)
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                claimed = uow.claim("operation-a", claimant=f"worker-{index}", owner_id=OWNER)
            return result.transitioned, claimed

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(compete, (1, 2)))
    assert sum(item[0] for item in results) == 1
    assert sum(item[1] for item in results) == 1
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        assert uow.recovery()[0][1] == "claimed"
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            assert not uow.claim("operation-a", claimant="restarted-worker", owner_id=OWNER)
        assert state(connection)[1:] == (1, 1, 1)


def test_initialization_idempotent_and_ddl_rollback(case: Case) -> None:
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        before = tuple(
            connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
            )
        )
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            uow.initialize()
            uow.initialize()
        assert (
            tuple(
                connection.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
                )
            )
            == before
        )
        connection.execute("DROP TABLE d1_checkout_operations")
        connection.execute("BEGIN IMMEDIATE")
        uow.initialize()
        connection.rollback()
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='d1_checkout_operations'"
        ).fetchone()
        connection.execute("CREATE TABLE d1_checkout_operations(operation_id TEXT)")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises((EvidenceError, sqlite3.Error)):
                uow.initialize()


def test_claim_rollback_integrity_and_effect_binding(case: Case) -> None:
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        uow.prepare(case.request)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            uow.apply(case.request, owner_id=OWNER)
        row = connection.execute(
            "SELECT effect,merchant_ref,variant_id,input_digest,idempotency_key "
            "FROM d1_checkout_operations"
        ).fetchone()
        data = case.request.evidence.policy_input()
        assert row == (
            "checkout.create",
            data.merchant_ref,
            data.selected_variant_id,
            case.request.evidence.decision.input_digest,
            "synthetic-stable-key",
        )
        connection.execute("BEGIN IMMEDIATE")
        assert uow.claim("operation-a", claimant="worker-a", owner_id=OWNER)
        connection.rollback()
        assert uow.recovery() == (("operation-a", "reserved", None),)
        for sql in (
            "DELETE FROM d1_checkout_operations",
            "UPDATE d1_checkout_operations SET variant_id='other'",
            "UPDATE d1_checkout_operations SET state='claimed',claimant='worker',"
            "input_digest='foreign'",
            "INSERT OR REPLACE INTO d1_checkout_operations SELECT * FROM d1_checkout_operations",
            "UPDATE purchases SET stage='failed'",
        ):
            with pytest.raises(sqlite3.IntegrityError), connection:
                connection.execute(sql)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_diagnostics_preserve_original_allow_without_transition(case: Case) -> None:
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        blocked = case.request.evidence.model_copy(
            update={
                "application": EvidenceApplication(
                    schema_version=SCHEMA,
                    disposition="blocked",
                    transition_ref=None,
                    expected_state_version=1,
                    resulting_state_version=None,
                )
            }
        )
        uow.ledger.prepare_decision(blocked)
        failure = ApplicationFailure(
            schema_version=SCHEMA,
            record_kind="application_failure",
            evidence_id="failure-a",
            sequence=2,
            recorded_at=NOW,
            run_id="run-a",
            trace_id=blocked.policy_input().trace_id,
            decision_id=blocked.decision.decision_id,
            reason_code="GOV_STATE_CONFLICT",
            expected_state_version=1,
            observed_state_version=1,
            replayability="not_evaluable",
        )
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(EvidenceError):
                uow.record_failure(
                    blocked,
                    failure.model_copy(update={"observed_state_version": 999}),
                    owner_id=OWNER,
                )
            assert state(connection)[1] == 0
            uow.record_failure(blocked, failure, owner_id=OWNER)
        assert uow.ledger.get("evidence-a").decision == case.request.evidence.decision  # type: ignore[union-attr]
        assert uow.ledger.get("failure-a") == failure
        assert state(connection) == (("received", 1, None), 2, 0, 0)


def test_no_network_or_runtime_authorization_surface() -> None:
    # Structural scope check only; this is NOT a commercial integration test.
    import ast

    root = Path(__file__).parents[2] / "src/commerce_agent"
    module = ast.parse((root / "persistence/d1_uow_sqlite.py").read_text())
    imports = [
        ast.unparse(node)
        for node in ast.walk(module)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not any(
        word in line
        for line in imports
        for word in ("httpx", "requests", "CheckoutGateway", "socket")
    )
    for path in ("runtime.py", "api.py", "application/purchase.py"):
        assert "d1_uow" not in (root / path).read_text()
    from commerce_agent.config import Settings

    # Runtime enforce is opt-in only, through a gate without dispatch (test_enforce_runtime).
    assert Settings.model_fields["agent_governance_mode"].default == "off"


def test_engine_exception_does_not_admit_or_transition(case: Case, monkeypatch: pytest.MonkeyPatch):
    def failed(*args, **kwargs):
        raise RuntimeError("synthetic engine unavailable")

    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        monkeypatch.setattr(ReferencePolicyEvaluator, "evaluate", failed)
        with pytest.raises(EvidenceError):
            uow.prepare(case.request)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(D1ApplicationError):
                uow.apply(case.request, owner_id=OWNER)
        assert state(connection) == (("received", 1, None), 0, 0, 0)


@pytest.mark.parametrize(
    "field", ["merchant_ref", "selected_variant_id", "context_version", "run_id"]
)
def test_coherent_reevaluated_input_cannot_transplant_authority(case: Case, field: str):
    request = case.request
    old = request.evidence
    data = old.policy_input()
    changes = {field: 2 if field == "context_version" else "foreign"}
    data = SelectionPolicyInput.model_validate(data.model_copy(update=changes))
    decision = ReferencePolicyEvaluator(
        artifact_bytes=case.policy,
        binding=case.binding,
        identity=measure_reference_engine(),
        metadata=EvaluationMetadata(schema_version=SCHEMA, decided_at=NOW, latency_ms=1),
    ).evaluate(data)
    app = old.application
    if decision.outcome == "deny":
        app = EvidenceApplication(
            schema_version=SCHEMA,
            disposition="blocked",
            transition_ref=None,
            expected_state_version=1,
            resulting_state_version=None,
        )
    record = DecisionRecord.model_validate(
        old.model_copy(
            update={
                "canonical_input_bytes": canonical_bytes(data).decode(),
                "decision": decision,
                "application": app,
            }
        )
    )
    request = request.model_copy(update={"evidence": record})
    if field == "run_id":
        SqliteAgentStore(case.path).create_purchase("foreign", "synthetic other purchase", OWNER)
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        if field in ("run_id", "context_version"):
            with pytest.raises(EvidenceError):
                uow.prepare(request)
        else:
            uow.prepare(request)
        before = state(connection)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if field == "selected_variant_id":
                # A genuine POL-01 denial is recorded as blocked, never authorized.
                assert not uow.apply(request, owner_id=OWNER).transitioned
                assert state(connection)[2:] == (0, 0)
            else:
                with pytest.raises(D1ApplicationError):
                    uow.apply(request, owner_id=OWNER)
                assert state(connection) == before


def test_configured_policy_binding_is_not_taken_from_input(case: Case):
    payload, binding = build_d1_policy(
        policy_set_id="other-policy", version=1, merchant_ref="other"
    )
    with closing(connect(case.path)) as connection:
        uow = SqliteD1UnitOfWork(
            connection, keys=case.keys, policy_bytes=payload, policy_binding=binding
        )
        with pytest.raises(EvidenceError):
            uow.prepare(case.request)
        with pytest.raises(GovernanceError):
            SqliteD1UnitOfWork(
                connection, keys=case.keys, policy_bytes=case.policy, policy_binding=binding
            )
        assert state(connection) == (("received", 1, None), 0, 0, 0)


def test_existing_key_conflict_and_new_operation_retry_roll_back(case: Case):
    old_key = SqliteAgentStore(case.path).idempotency_key("run-a", "checkout.create")
    assert old_key != case.request.idempotency_key.get_secret_value()
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        uow.prepare(case.request)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(D1ApplicationError):
                uow.apply(case.request, owner_id=OWNER)
        assert state(connection) == (("received", 1, None), 0, 0, 1)


def test_new_operation_cannot_replace_committed_effect(case: Case):
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        uow.prepare(case.request)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            uow.apply(case.request, owner_id=OWNER)
        record = case.request.evidence
        replacement = case.request.model_copy(
            update={
                "operation_id": "new-operation",
                "idempotency_key": SecretStr("new-key"),
                "evidence": record.model_copy(
                    update={
                        "evidence_id": "new-evidence",
                        "application": record.application.model_copy(
                            update={"transition_ref": "new-operation"}
                        ),
                    }
                ),
            }
        )
        uow.prepare(replacement)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(D1ApplicationError):
                uow.apply(replacement, owner_id=OWNER)
        assert state(connection)[1:] == (1, 1, 1)


def test_transaction_required_and_recovery_cannot_export_uncommitted_state(case: Case):
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        uow.prepare(case.request)
        with pytest.raises(D1ApplicationError):
            uow.apply(case.request, owner_id=OWNER)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(EvidenceError):
                uow.prepare(case.request)
            uow.apply(case.request, owner_id=OWNER)
            with pytest.raises(EvidenceError):
                uow.recovery()


def test_full_ddl_schema_definition_is_verified(case: Case):
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        original = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='d1_checkout_operations'"
        ).fetchone()[0]
        connection.execute("DROP TABLE d1_checkout_operations")
        # Identical columns with a weakened effect constraint must be rejected.
        connection.execute(original.replace("CHECK(effect='checkout.create')", "CHECK(1)"))
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(EvidenceError):
                uow.initialize()
            assert not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='d1_operation_no_delete'"
            ).fetchone()


def test_original_b0_migration_and_revision_guard_unchanged(case: Case):
    with closing(connect(case.path)) as connection:
        original = tuple(
            connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE name LIKE 'purchases_%' ORDER BY name"
            )
        )
    commercial = SqliteAgentStore(case.path)
    commercial.initialize()
    commercial.create_purchase("b0", "synthetic legacy", OWNER)
    assert commercial.compare_and_set_purchase("b0", 1, stage="selected").status == "applied"
    assert commercial.purchase_state_version("b0") == 2
    purchase = commercial.purchase("b0")
    assert purchase is not None and "state_version" not in purchase
    with closing(connect(case.path)) as connection:
        assert original == tuple(
            connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE name LIKE 'purchases_%' ORDER BY name"
            )
        )


def test_evaluation_outside_transaction_and_network_never_invoked(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
):
    import httpx

    network_calls = []

    def forbidden(*args, **kwargs):
        network_calls.append("forbidden")
        raise AssertionError("External I/O is outside this port")

    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    with closing(connect(case.path)) as connection:
        original = ReferencePolicyEvaluator.evaluate
        observations = []

        def observed(evaluator, data):
            observations.append(connection.in_transaction)
            assert not connection.in_transaction
            return original(evaluator, data)

        monkeypatch.setattr(ReferencePolicyEvaluator, "evaluate", observed)
        uow = case.uow(connection)
        uow.prepare(case.request)
        assert observations == [False]
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            uow.apply(case.request, owner_id=OWNER)
        assert observations == [False] and network_calls == []


def test_commit_failure_rolls_back_provisional_application(case: Case):
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        uow.prepare(case.request)
        connection.execute(
            "CREATE TRIGGER lost_ledger BEFORE INSERT ON governance_evidence "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
        with pytest.raises(sqlite3.IntegrityError), connection:
            connection.execute("BEGIN IMMEDIATE")
            assert uow.apply(case.request, owner_id=OWNER).transitioned
            # Deferred reservation FK makes commit fail when ledger insertion was lost.
        assert state(connection) == (("received", 1, None), 0, 0, 0)


def test_concurrent_different_identities_do_not_authorize_second_effect(case: Case):
    barrier = Barrier(2)

    def attempt(index: int) -> bool:
        original = case.request.evidence
        operation = f"operation-{index}"
        request = case.request.model_copy(
            update={
                "operation_id": operation,
                "idempotency_key": SecretStr(f"key-{index}"),
                "evidence": original.model_copy(
                    update={
                        "evidence_id": f"evidence-{index}",
                        "application": original.application.model_copy(
                            update={"transition_ref": operation}
                        ),
                    }
                ),
            }
        )
        with closing(connect(case.path)) as connection:
            uow = case.uow(connection)
            uow.prepare(request)
            barrier.wait(timeout=30)
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    return uow.apply(request, owner_id=OWNER).transitioned
                except D1ApplicationError:
                    return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(attempt, (1, 2))) == 1
    with closing(connect(case.path)) as connection:
        assert state(connection)[1:] == (1, 1, 1)


def test_claim_rechecks_owner_and_current_commercial_state(case: Case):
    with closing(connect(case.path)) as connection:
        uow = case.uow(connection)
        uow.prepare(case.request)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            uow.apply(case.request, owner_id=OWNER)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            assert not uow.claim("operation-a", claimant="worker", owner_id="foreign")
        SqliteAgentStore(case.path).update_purchase("run-a", stage="canceled")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            assert not uow.claim("operation-a", claimant="worker", owner_id=OWNER)
        assert uow.recovery() == (("operation-a", "reserved", None),)


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_episode_mode_cannot_be_changed(tmp_path: Path, mode: Literal["shadow", "enforce"]):
    case = build(tmp_path, mode=mode)
    configuration_type = D1EnforceConfiguration if mode == "shadow" else D1Configuration
    changed = configuration_type(
        governance_mode="enforce" if mode == "shadow" else "shadow",  # type: ignore[arg-type]
        merchant_base_url="http://127.0.0.1:4120",
        policy_set_id="uow-d1",
        policy_revision=1,
        currency="COP",
        quantity=1,
        clock=ClockProfile(
            authority_source="system_utc",
            commercial_source="fixed_sandbox",
            authority_provider=None,
            commercial_provider=None,
            fixed_commercial_at=NOW,
        ),
    )
    with closing(connect(case.path)) as connection:
        vault = SqliteD1ProvenanceStore(connection, case.keys)
        before = tuple(
            connection.execute("SELECT * FROM d1_provenance_sources ORDER BY source_id,version")
        )
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(GovernanceError, match=r"^GOV_INPUT_INVALID$"):
                vault.register_context("run-a", OWNER, changed)
        assert before == tuple(
            connection.execute("SELECT * FROM d1_provenance_sources ORDER BY source_id,version")
        )
