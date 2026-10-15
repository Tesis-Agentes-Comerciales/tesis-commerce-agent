"""Isolated D1 local atomicity experiment; deliberately not wired into runtime.

Caller owns BEGIN IMMEDIATE/commit/rollback and the supplied connection. A
savepoint prevents partial application even if the caller catches an exception.
The initial zero-row UPDATE obtains a write lock for callers using deferred BEGIN.
No evaluation or external I/O occurs inside apply. Source recovery is local SQL
and authenticated decryption; the reference replay is admitted before BEGIN.
"""

import json
import sqlite3
from contextlib import contextmanager
from typing import Literal

from ..domain import rank_candidates
from ..governance.canonical import canonical_bytes, sha256_digest
from ..governance.evaluator import GovernanceError, verify_artifact
from ..governance.evidence import (
    ApplicationFailure,
    DecisionRecord,
    EvidenceApplication,
    EvidenceError,
    scope_input,
    validate_evidence,
)
from ..governance.models import ObjectRef, PolicyBinding
from ..governance.provenance import ProvenanceKeys
from ..governance.provenance_artifacts import measure_d1_sources
from ..governance.shadow_artifacts import verify_loaded_selection
from ..governance.uow import (
    D1Application,
    D1ApplicationError,
    D1ApplicationResult,
)
from .governance_sqlite import SqliteGovernanceEvidenceStore
from .provenance_sqlite import Kind, SqliteD1ProvenanceStore

_TABLE = """CREATE TABLE IF NOT EXISTS d1_checkout_operations (
    operation_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL UNIQUE REFERENCES purchases(run_id),
    effect TEXT NOT NULL CHECK(effect='checkout.create'),
    evidence_id TEXT NOT NULL UNIQUE REFERENCES governance_evidence(evidence_id)
        DEFERRABLE INITIALLY DEFERRED,
    evidence_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    merchant_ref TEXT NOT NULL,
    variant_id TEXT NOT NULL,
    currency TEXT NOT NULL CHECK(currency='COP'),
    quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity=1),
    expected_version INTEGER NOT NULL
        CHECK(typeof(expected_version)='integer' AND expected_version>=1),
    resulting_version INTEGER NOT NULL CHECK(resulting_version=expected_version+1),
    idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key) BETWEEN 1 AND 128),
    state TEXT NOT NULL CHECK(state IN ('reserved','claimed')),
    claimant TEXT,
    CHECK((state='reserved' AND claimant IS NULL) OR (state='claimed' AND claimant IS NOT NULL)))"""

_IMMUTABLE = (
    "operation_id",
    "run_id",
    "effect",
    "evidence_id",
    "evidence_digest",
    "input_digest",
    "merchant_ref",
    "variant_id",
    "currency",
    "quantity",
    "expected_version",
    "resulting_version",
    "idempotency_key",
)
_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS d1_operation_no_delete BEFORE DELETE ON d1_checkout_operations "
    "BEGIN SELECT RAISE(ABORT, 'd1_operation_immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS d1_operation_no_replace BEFORE INSERT ON d1_checkout_operations "
    "WHEN NEW.state!='reserved' OR NEW.claimant IS NOT NULL OR EXISTS(SELECT 1 FROM "
    "d1_checkout_operations WHERE operation_id=NEW.operation_id OR run_id=NEW.run_id "
    "OR evidence_id=NEW.evidence_id OR idempotency_key=NEW.idempotency_key) "
    "BEGIN SELECT RAISE(ABORT, 'd1_operation_conflict'); END",
    "CREATE TRIGGER IF NOT EXISTS d1_operation_claim_only BEFORE UPDATE ON d1_checkout_operations "
    "WHEN OLD.state!='reserved' OR NEW.state!='claimed' OR NEW.claimant IS NULL OR "
    + " OR ".join(f"NEW.{field} IS NOT OLD.{field}" for field in _IMMUTABLE)
    + " BEGIN SELECT RAISE(ABORT, 'd1_operation_immutable'); END",
)


def _sql(statement: str) -> str:
    return " ".join(statement.replace(" IF NOT EXISTS", "").split())


class _Transition:
    """Private verifier: requires both the affected SQL row and its bound receipt."""

    def __init__(self, request: D1Application, affected: int):
        self.request = request
        self.affected = affected

    def verify(
        self, connection: sqlite3.Connection, run_id: str, application: EvidenceApplication
    ) -> bool:
        data = self.request.evidence.policy_input()
        row = connection.execute(
            "SELECT p.stage,p.selected_variant_id,p.state_version,o.expected_version,"
            "o.resulting_version,o.evidence_digest FROM purchases p "
            "JOIN d1_checkout_operations o ON o.run_id=p.run_id "
            "WHERE p.run_id=? AND o.operation_id=?",
            (run_id, application.transition_ref),
        ).fetchone()
        return (
            self.affected == 1
            and row is not None
            and tuple(row)
            == (
                "selected",
                data.selected_variant_id,
                application.resulting_state_version,
                application.expected_state_version,
                application.resulting_state_version,
                sha256_digest(canonical_bytes(self.request.evidence)),
            )
        )


class SqliteD1UnitOfWork:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        keys: ProvenanceKeys,
        policy_bytes: bytes,
        policy_binding: PolicyBinding,
    ):
        if connection.in_transaction:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        self.connection = connection
        self.ledger = SqliteGovernanceEvidenceStore(connection)
        self.vault = SqliteD1ProvenanceStore(connection, keys)
        verify_artifact(policy_bytes, policy_binding)
        self._binding = PolicyBinding.model_validate(policy_binding)
        self._sources = measure_d1_sources()
        verify_loaded_selection(json.loads(self._sources["algorithm"])["sources"], rank_candidates)
        self._admitted: set[tuple[str, str, str, str]] = set()

    @contextmanager
    def _atomic(self):
        if not self.connection.in_transaction:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        self.connection.execute("SAVEPOINT d1_uow")
        try:
            self.connection.execute("UPDATE purchases SET state_version=state_version WHERE 0")
            yield
            self.connection.execute("RELEASE d1_uow")
        except Exception:
            self.connection.execute("ROLLBACK TO d1_uow")
            self.connection.execute("RELEASE d1_uow")
            raise

    def initialize(self) -> None:
        """Explicit additive, rollbackable DDL; reject weakened or foreign schemas."""
        with self._atomic():
            for statement in (_TABLE, *_TRIGGERS):
                self.connection.execute(statement)
                words = statement.replace(" IF NOT EXISTS", "").split()
                name = words[2]
                existing = self.connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?", (name,)
                ).fetchone()
                if existing is None or _sql(existing[0]) != _sql(statement):
                    raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")

    @staticmethod
    def _key(request: D1Application) -> tuple[str, str, str, str]:
        return (
            sha256_digest(canonical_bytes(request.evidence)),
            canonical_bytes(request.context_ref).decode(),
            request.operation_id,
            request.idempotency_key.get_secret_value(),
        )

    def prepare(self, request: D1Application) -> None:
        request = D1Application.model_validate(request)
        if measure_d1_sources() != self._sources:
            raise EvidenceError("GOV_ARTIFACT_MISMATCH")
        record = request.evidence
        data = record.policy_input()
        if (
            data.governance_mode != "enforce"
            or data.purchase_mode != "HP"
            or data.expected_policy != self._binding
        ):
            raise EvidenceError("GOV_INPUT_INVALID")
        app = record.application
        allow = record.decision.outcome == "allow"
        if allow and (
            app.disposition != "transition_committed"
            or app.transition_ref != request.operation_id
            or app.resulting_state_version != data.expected_local_state_version + 1
        ):
            raise EvidenceError("GOV_STATE_CONFLICT")
        if not allow and app.disposition != "blocked":
            raise EvidenceError("GOV_INPUT_INVALID")
        self.ledger.prepare_decision(record)
        self._admitted.add(self._key(request))

    def _facts(self, request: D1Application, owner_id: str) -> None:
        data = request.evidence.policy_input()
        context = self.vault.recover_context(data.run_id, owner_id, request.context_ref)
        ctx = context.data
        facts = self.vault.intent_facts(
            data.run_id, owner_id, data.intent.intent_ref, request.context_ref
        )
        intent = self.vault.recover_intent(data.run_id, owner_id, data.intent.intent_ref)
        feed = self.vault.recover_feed(data.run_id, owner_id, data.feed_snapshot_ref)
        eligible = tuple(
            sorted(item.candidate.variant_id for item in rank_candidates(feed, intent).ranked)
        )
        if (
            ctx.governance_mode != "enforce"
            or request.evidence.policy_artifact_ref != ctx.policy_artifact_ref
            or data.intent != facts
            or data.context_version != context.ref.version
            or data.trace_id != ctx.trace_id
            or data.owner_ref != ctx.owner_ref
            or data.merchant_ref != ctx.merchant_ref
            or data.expected_policy != ctx.expected_policy
            or data.clock_profile_ref != ctx.clock_profile_ref
            or data.eligibility_algorithm_ref != ctx.eligibility_algorithm_ref
            or data.experiment_run_id != ctx.experiment_run_id
            or data.conversation_id != ctx.conversation_id
            or data.eligible_variant_ids != eligible
            or any(
                item.candidate.currency.upper() != "COP"
                for item in rank_candidates(feed, intent).ranked
            )
        ):
            raise EvidenceError("GOV_INPUT_INVALID")
        snapshots: tuple[tuple[ObjectRef, Kind], ...] = (
            (request.context_ref, "context"),
            (data.intent.intent_ref, "intent"),
            (data.feed_snapshot_ref, "feed"),
        )
        for ref, kind in snapshots:
            latest = self.connection.execute(
                "SELECT MAX(version) FROM d1_provenance_sources WHERE source_id=? AND run_id=?",
                (ref.id, data.run_id),
            ).fetchone()[0]
            if latest != ref.version:
                raise EvidenceError("GOV_STATE_CONFLICT")
            self.vault.public_source(data.run_id, owner_id, ref, kind)
        sources: tuple[tuple[ObjectRef, Literal["algorithm", "normalization"]], ...] = (
            (ctx.eligibility_algorithm_ref, "algorithm"),
            (ctx.normalization_ref, "normalization"),
        )
        for ref, kind in sources:
            if self.vault.public_source(data.run_id, owner_id, ref, kind) != self._sources[kind]:
                raise EvidenceError("GOV_ARTIFACT_MISMATCH")

    def apply(self, request: D1Application, *, owner_id: str) -> D1ApplicationResult:
        request = D1Application.model_validate(request)
        record = request.evidence
        data = record.policy_input()
        try:
            with self._atomic():
                if self._key(request) not in self._admitted:
                    raise EvidenceError("GOV_OUTPUT_INVALID")
                self._facts(request, owner_id)
                scope, _, _ = self.ledger._episode(data.run_id, data.decision_id)
                scope_input(scope, data)
                old = self.connection.execute(
                    "SELECT operation_id,evidence_id,evidence_digest,"
                    "idempotency_key,resulting_version "
                    "FROM d1_checkout_operations WHERE run_id=?",
                    (data.run_id,),
                ).fetchone()
                if old is not None:
                    if (
                        tuple(old)
                        != (
                            request.operation_id,
                            record.evidence_id,
                            self._key(request)[0],
                            request.idempotency_key.get_secret_value(),
                            record.application.resulting_state_version,
                        )
                        or self.ledger.get(record.evidence_id) != record
                    ):
                        raise EvidenceError("GOV_STATE_CONFLICT")
                    return D1ApplicationResult(False, True, request.operation_id, old[4])
                current = self.connection.execute(
                    "SELECT owner_id,stage,state_version,selected_variant_id,checkout_id,order_id "
                    "FROM purchases WHERE run_id=?",
                    (data.run_id,),
                ).fetchone()
                version = data.expected_local_state_version
                if current is None or tuple(current) != (
                    owner_id,
                    "received",
                    version,
                    None,
                    None,
                    None,
                ):
                    raise EvidenceError("GOV_STATE_CONFLICT")
                if record.decision.outcome == "deny":
                    self.ledger.record_decision(record, expected_state_version=version)
                    return D1ApplicationResult(False, False, None, version)
                affected = self.connection.execute(
                    "UPDATE purchases SET stage='selected',selected_variant_id=?,"
                    "state_version=state_version+1 "
                    "WHERE run_id=? AND owner_id=? AND stage='received' AND state_version=?",
                    (data.selected_variant_id, data.run_id, owner_id, version),
                ).rowcount
                self.connection.execute(
                    "INSERT INTO operation_keys(run_id,operation,idempotency_key,created_at) "
                    "VALUES (?,'checkout.create',?,?) "
                    "ON CONFLICT(run_id,operation) DO NOTHING",
                    (data.run_id, request.idempotency_key.get_secret_value(), record.recorded_at),
                )
                if (
                    self.connection.execute(
                        "SELECT idempotency_key FROM operation_keys WHERE run_id=? "
                        "AND operation='checkout.create'",
                        (data.run_id,),
                    ).fetchone()[0]
                    != request.idempotency_key.get_secret_value()
                ):
                    raise EvidenceError("GOV_STATE_CONFLICT")
                self.connection.execute(
                    "INSERT INTO d1_checkout_operations VALUES "
                    "(?,?, 'checkout.create',?,?,?,?,?,'COP',1,?,?,?,'reserved',NULL)",
                    (
                        request.operation_id,
                        data.run_id,
                        record.evidence_id,
                        self._key(request)[0],
                        record.decision.input_digest,
                        data.merchant_ref,
                        data.selected_variant_id,
                        version,
                        version + 1,
                        request.idempotency_key.get_secret_value(),
                    ),
                )
                self.ledger.record_decision(
                    record,
                    expected_state_version=version,
                    transition_verifier=_Transition(request, affected),
                )
                return D1ApplicationResult(True, False, request.operation_id, version + 1)
        except Exception as error:
            cause = (
                error
                if isinstance(error, EvidenceError)
                else EvidenceError(
                    "GOV_INPUT_INVALID"
                    if isinstance(error, GovernanceError)
                    else "GOV_EVIDENCE_UNAVAILABLE"
                )
            )
            raise D1ApplicationError(record, cause) from None

    def record_failure(
        self,
        original: DecisionRecord,
        diagnostic: ApplicationFailure,
        *,
        owner_id: str,
    ) -> None:
        """Separate caller-owned transaction after rollback; never rewrite the decision.

        Caller must prepare the blocked form outside SQL first. If the ledger is
        unavailable this also fails: no fictitious successful evidence is returned.
        """
        with self._atomic():
            checked = validate_evidence(original)
            if (
                not isinstance(checked, DecisionRecord)
                or checked.application.disposition != "blocked"
            ):
                raise EvidenceError("GOV_INPUT_INVALID")
            data = checked.policy_input()
            self.vault.public_source(data.run_id, owner_id, data.intent.intent_ref, "intent")
            current = self.connection.execute(
                "SELECT state_version FROM purchases WHERE run_id=?", (data.run_id,)
            ).fetchone()
            if (
                data.governance_mode != "enforce"
                or diagnostic.decision_id != checked.decision.decision_id
                or diagnostic.run_id != data.run_id
                or diagnostic.trace_id != data.trace_id
                or current is None
                or diagnostic.observed_state_version != current[0]
            ):
                raise EvidenceError("GOV_INPUT_INVALID")
            self.ledger.record_decision(
                checked, expected_state_version=checked.application.expected_state_version
            )
            self.ledger.record_decision(
                diagnostic, expected_state_version=checked.application.expected_state_version
            )

    def claim(self, operation_id: str, *, claimant: str, owner_id: str) -> bool:
        """Single local claim, provisional until caller commit. Never automatically release.

        A recovered claimed row is uncertain; it does not authorize a fresh send.
        This method performs no HTTP. A future worker must commit before invoking
        a gateway and reconcile with the same key after any ambiguous crash.
        """
        from pydantic import TypeAdapter

        from ..governance.models import Id

        TypeAdapter(Id).validate_python(operation_id, strict=True)
        TypeAdapter(Id).validate_python(claimant, strict=True)
        with self._atomic():
            row = self.connection.execute(
                "SELECT o.evidence_id,o.evidence_digest FROM d1_checkout_operations o "
                "JOIN purchases p ON p.run_id=o.run_id "
                "JOIN operation_keys k ON k.run_id=o.run_id AND k.operation=o.effect "
                "WHERE o.operation_id=? AND o.state='reserved' AND p.owner_id=? "
                "AND p.stage='selected' AND p.state_version=o.resulting_version "
                "AND p.selected_variant_id=o.variant_id AND p.checkout_id IS NULL "
                "AND p.order_id IS NULL AND k.idempotency_key=o.idempotency_key",
                (operation_id, owner_id),
            ).fetchone()
            if row is None:
                return False
            record = self.ledger.get(row[0])
            if (
                not isinstance(record, DecisionRecord)
                or sha256_digest(canonical_bytes(record)) != row[1]
                or record.decision.policy_binding != self._binding
                or record.decision.governance_mode != "enforce"
                or record.decision.outcome != "allow"
                or record.decision.origin != "policy"
                or record.application.disposition != "transition_committed"
                or record.application.transition_ref != operation_id
            ):
                raise EvidenceError("GOV_STATE_CONFLICT")
            return (
                self.connection.execute(
                    "UPDATE d1_checkout_operations SET state='claimed',claimant=? "
                    "WHERE operation_id=? AND state='reserved'",
                    (claimant, operation_id),
                ).rowcount
                == 1
            )

    def recovery(self) -> tuple[tuple[str, str, str | None], ...]:
        """Identifiers/status only, never a dispatch capability or a secret key export."""
        if self.connection.in_transaction:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        return tuple(
            tuple(row)
            for row in self.connection.execute(
                "SELECT operation_id,state,claimant FROM d1_checkout_operations "
                "ORDER BY operation_id"
            )
        )
