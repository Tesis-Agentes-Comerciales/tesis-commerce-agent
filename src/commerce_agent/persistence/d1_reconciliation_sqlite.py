"""Local inspection journal + CAS projection; no transport, replay or completion.

initialize/reconcile require a caller transaction. Returned events are provisional
until caller commit. All outcomes prohibit effects, including historical confirmation.
"""

import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import Literal

from ..governance.canonical import canonical_bytes, parse_model, sha256_digest
from ..governance.dispatch_envelope import CheckoutCreationEnvelope
from ..governance.evidence import DecisionRecord, EvidenceError
from ..governance.provenance import ProvenanceKeys, utc_milliseconds
from ..governance.reconciliation import (
    Reason,
    ReconciliationEvent,
    ReconciliationRequest,
    RecoveredOperation,
    ReplayAssessment,
)
from .d1_dispatch_sqlite import SqliteD1DispatchStore
from .d1_envelope_sqlite import SqliteD1EnvelopeStore
from .governance_sqlite import SqliteGovernanceEvidenceStore
from .provenance_sqlite import SqliteD1ProvenanceStore

_EVENT = """CREATE TABLE IF NOT EXISTS d1_reconciliation_events (
    event_id TEXT PRIMARY KEY NOT NULL,
    operation_id TEXT NOT NULL REFERENCES d1_checkout_operations(operation_id),
    request_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>0),
    request_digest TEXT NOT NULL CHECK(length(request_digest)=71),
    outcome TEXT NOT NULL CHECK(outcome IN ('blocked','historical_confirmation_only')),
    body BLOB NOT NULL CHECK(typeof(body)='blob'),
    body_digest TEXT NOT NULL CHECK(length(body_digest)=71),
    UNIQUE(operation_id,request_id), UNIQUE(operation_id,version))"""
_PROJECTION = """CREATE TABLE IF NOT EXISTS d1_reconciliation_projection (
    operation_id TEXT PRIMARY KEY NOT NULL REFERENCES d1_checkout_operations(operation_id),
    version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>0),
    last_event_id TEXT NOT NULL REFERENCES d1_reconciliation_events(event_id),
    outcome TEXT NOT NULL CHECK(outcome IN ('blocked','historical_confirmation_only')))"""


class SqliteD1Reconciler:
    def __init__(
        self,
        connection: sqlite3.Connection,
        keys: ProvenanceKeys,
        *,
        clock: Callable[[], datetime],
    ):
        self.connection, self.keys, self.clock = connection, keys, clock

    @contextmanager
    def _atomic(self):
        if (
            not self.connection.in_transaction
            or self.connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        ):
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        self.connection.execute("SAVEPOINT d1_reconciliation")
        try:
            yield
        except BaseException:
            self.connection.execute("ROLLBACK TO d1_reconciliation")
            raise
        finally:
            self.connection.execute("RELEASE d1_reconciliation")

    def initialize(self) -> None:
        with self._atomic():
            ddl = SqliteD1DispatchStore(self.connection)._ddl
            ddl(_EVENT, "d1_reconciliation_events")
            ddl(_PROJECTION, "d1_reconciliation_projection")
            for action in ("UPDATE", "DELETE"):
                name = f"d1_reconciliation_event_no_{action.lower()}"
                ddl(
                    f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} "
                    "ON d1_reconciliation_events "
                    "BEGIN SELECT RAISE(ABORT,'reconciliation_immutable'); END",
                    name,
                )
            for table, key in (
                ("d1_reconciliation_events", "event_id"),
                ("d1_reconciliation_projection", "operation_id"),
            ):
                name = f"{table}_no_replace"
                condition = (
                    f"{key}=NEW.{key}"
                    if table == "d1_reconciliation_projection"
                    else "event_id=NEW.event_id OR (operation_id=NEW.operation_id AND "
                    "(request_id=NEW.request_id OR version=NEW.version))"
                )
                ddl(
                    f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE INSERT ON {table} "
                    f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {condition}) "
                    "BEGIN SELECT RAISE(ABORT,'reconciliation_immutable'); END",
                    name,
                )
            ddl(
                "CREATE TRIGGER IF NOT EXISTS d1_projection_no_delete BEFORE DELETE "
                "ON d1_reconciliation_projection "
                "BEGIN SELECT RAISE(ABORT,'reconciliation_immutable'); END",
                "d1_projection_no_delete",
            )
            for action in ("INSERT", "UPDATE"):
                name = f"d1_projection_validate_{action.lower()}"
                version_check = (
                    "NEW.version!=1"
                    if action == "INSERT"
                    else "NEW.operation_id!=OLD.operation_id OR NEW.version!=OLD.version+1"
                )
                ddl(
                    f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} "
                    f"ON d1_reconciliation_projection WHEN {version_check} OR NOT EXISTS("
                    "SELECT 1 FROM d1_reconciliation_events WHERE event_id=NEW.last_event_id "
                    "AND operation_id=NEW.operation_id AND version=NEW.version "
                    "AND outcome=NEW.outcome) "
                    "BEGIN SELECT RAISE(ABORT,'reconciliation_projection_conflict'); END",
                    name,
                )

    def _has(self, name: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )

    def _scope(self, operation_id: str, run_id: str, owner_id: str):
        row = self.connection.execute(
            "SELECT o.evidence_id,o.evidence_digest,o.input_digest,o.merchant_ref,"
            "o.variant_id,o.currency,o.idempotency_key,o.state,p.stage,p.state_version,"
            "p.checkout_id,o.expected_version,o.resulting_version "
            "FROM d1_checkout_operations o JOIN purchases p ON p.run_id=o.run_id "
            "WHERE o.operation_id=? AND o.run_id=? AND p.owner_id=?",
            (operation_id, run_id, owner_id),
        ).fetchone()
        if row is None:
            raise EvidenceError("GOV_FACTS_UNAVAILABLE")
        return tuple(row)

    @staticmethod
    def _event_digest(event: ReconciliationEvent) -> str:
        return sha256_digest(b"commerce-d1-reconciliation-event/v1\0" + canonical_bytes(event))

    def _event(self, event_id: str) -> ReconciliationEvent:
        row = self.connection.execute(
            "SELECT operation_id,request_id,version,request_digest,outcome,body,body_digest "
            "FROM d1_reconciliation_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        try:
            if row is None:
                raise ValueError("Missing reconciliation event")
            event = parse_model(ReconciliationEvent, row[5])
            if (
                canonical_bytes(event) != row[5]
                or self._event_digest(event) != row[6]
                or event.event_id != event_id
                or tuple(row[:5])
                != (
                    event.recovered.operation_id,
                    event.request_id,
                    event.version,
                    event.request_digest,
                    event.outcome,
                )
            ):
                raise ValueError("Invalid reconciliation event")
            return event
        except Exception:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None

    def _version(self, operation_id: str) -> int:
        return len(self._validated_history(operation_id))

    def _validated_history(self, operation_id: str) -> tuple[ReconciliationEvent, ...]:
        """Validate every event and its contiguous position before trusting the projection."""
        if not self._has("d1_reconciliation_projection"):
            if (
                self._has("d1_reconciliation_events")
                and self.connection.execute(
                    "SELECT 1 FROM d1_reconciliation_events WHERE operation_id=?", (operation_id,)
                ).fetchone()
            ):
                raise EvidenceError("GOV_STATE_CONFLICT")
            return ()
        row = self.connection.execute(
            "SELECT version,last_event_id,outcome FROM d1_reconciliation_projection "
            "WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            if self.connection.execute(
                "SELECT 1 FROM d1_reconciliation_events WHERE operation_id=?", (operation_id,)
            ).fetchone():
                raise EvidenceError("GOV_STATE_CONFLICT")
            return ()
        rows = self.connection.execute(
            "SELECT event_id FROM d1_reconciliation_events WHERE operation_id=? ORDER BY version",
            (operation_id,),
        ).fetchall()
        events = []
        for version, (event_id,) in enumerate(rows, start=1):
            event = self._event(event_id)
            if event.recovered.operation_id != operation_id or event.version != version:
                raise EvidenceError("GOV_STATE_CONFLICT")
            events.append(event)
        if (
            not events
            or len(events) != row[0]
            or events[-1].event_id != row[1]
            or events[-1].outcome != row[2]
        ):
            raise EvidenceError("GOV_STATE_CONFLICT")
        return tuple(events)

    def _load(self, request: ReconciliationRequest):
        row = self._scope(request.operation_id, request.run_id, request.owner_id)
        reasons: list[Reason] = []
        owner_ref = "owner:unverifiable"
        authorized = False
        try:
            owner_ref = SqliteD1ProvenanceStore(self.connection, self.keys)._episode(
                request.run_id, request.owner_id
            )[0]
            record = SqliteGovernanceEvidenceStore(self.connection).get(row[0])
            if not isinstance(record, DecisionRecord):
                raise ValueError("Missing authorization")
            data = record.policy_input()
            authorized = (
                sha256_digest(canonical_bytes(record)) == row[1]
                and record.decision.input_digest == row[2]
                and record.decision.outcome == "allow"
                and record.decision.origin == "policy"
                and data.governance_mode == "enforce"
                and data.purchase_mode == "HP"
                and data.run_id == request.run_id
                and data.owner_ref == owner_ref
                and data.merchant_ref == row[3]
                and data.selected_variant_id == row[4]
                and record.application.transition_ref == request.operation_id
                and record.application.disposition == "transition_committed"
                and record.application.expected_state_version == row[11]
                and record.application.resulting_state_version == row[12]
            )
        except Exception:
            authorized = False
        if not authorized:
            reasons.append("AUTHORIZATION_UNVERIFIABLE")
        result = None
        if self._has("d1_checkout_results"):
            result = self.connection.execute(
                "SELECT state,checkout_id,code FROM d1_checkout_results WHERE operation_id=?",
                (request.operation_id,),
            ).fetchone()
        outcome = row[7] if result is None else result[0]
        if outcome == "claimed":
            outcome = "claimed_without_observation"
        historical_id = result[1] if result is not None else None
        if result is not None and (
            row[7] != "claimed"
            or tuple(result)
            not in (
                ("confirmed", historical_id, "CHECKOUT_VERIFIED"),
                ("uncertain", None, "DISPATCH_UNCERTAIN"),
                ("uncertain", None, "CHECKOUT_INVALID"),
                ("definitive_failure", None, "VERIFIED_NOT_CREATED"),
            )
        ):
            reasons.append("LOCAL_HISTORY_UNVERIFIABLE")
        if outcome == "confirmed" and (historical_id is None or historical_id != row[10]):
            reasons.append("LOCAL_CHECKOUT_CONFLICT")
        envelope: CheckoutCreationEnvelope | None = None
        envelope_status = "missing"
        if self._has("d1_checkout_envelopes"):
            try:
                envelope = SqliteD1EnvelopeStore(self.connection, self.keys).recover(
                    request.operation_id, run_id=request.run_id, owner_id=request.owner_id
                )
                if envelope is not None:
                    envelope_status = "available"
            except Exception:
                envelope_status = "unverifiable"
        if envelope_status != "available":
            reasons.append(
                "ENVELOPE_MISSING" if envelope_status == "missing" else "ENVELOPE_UNVERIFIABLE"
            )
        recovered = RecoveredOperation.model_validate(
            {
                "operation_id": request.operation_id,
                "run_id": request.run_id,
                "owner_ref": owner_ref,
                "local_outcome": outcome,
                "purchase_stage": row[8],
                "purchase_version": row[9],
                "historical_checkout_id": historical_id,
                "evidence_id": row[0],
                "evidence_digest": row[1],
                "d1_input_digest": row[2],
                "authorization": "historical_allow_only" if authorized else "unverifiable",
                "envelope_status": envelope_status,
                "envelope_digest": envelope.digest() if envelope else None,
                "binding_status": envelope.binding_status if envelope else "unavailable",
                "projection_version": self._version(request.operation_id),
            }
        )
        return recovered, envelope, reasons

    def inspect(self, request: ReconciliationRequest) -> RecoveredOperation:
        """Read-only local snapshot; does not initialize schema or grant permissions."""
        request = parse_model(ReconciliationRequest, canonical_bytes(request))
        self.connection.execute("SAVEPOINT d1_reconciliation_read")
        try:
            return self._load(request)[0]
        finally:
            self.connection.execute("RELEASE d1_reconciliation_read")

    @staticmethod
    def _assess(
        request: ReconciliationRequest,
        recovered: RecoveredOperation,
        envelope: CheckoutCreationEnvelope | None,
        reasons: list[Reason],
    ) -> tuple[Literal["blocked", "historical_confirmation_only"], tuple[Reason, ...]]:
        reasons.extend(("REMOTE_PROOF_UNAVAILABLE",))
        if recovered.binding_status == "backend_expected_unverified":
            reasons.append("EXPECTED_BINDING_NOT_AUTHENTICATED")
        if recovered.local_outcome == "claimed_without_observation":
            reasons.append("CLAIM_NOT_DELIVERY_PROOF")
        reasons.extend(("HISTORICAL_AUTHORIZATION_NOT_REPLAY_AUTHORITY", "RETENTION_UNPROVEN"))
        observation = request.observation
        if observation is not None and envelope is not None:
            expected = envelope.expected_remote_binding
            if observation.merchant_origin != envelope.merchant_origin or (
                expected is not None
                and (observation.remote_run_id, observation.remote_actor_id)
                != (expected.remote_run_id, expected.remote_actor_id)
            ):
                reasons.append("REMOTE_SCOPE_CONFLICT")
            key_digest = sha256_digest(envelope.idempotency_key.get_secret_value().encode())
            if observation.idempotency_key_digest != key_digest:
                reasons.append("REQUEST_KEY_CONFLICT")
            if observation.acp_fingerprint != envelope.body_digest:
                reasons.append("ACP_FINGERPRINT_CONFLICT")
            original, current = observation.original_creation, observation.current_checkout
            known = recovered.historical_checkout_id
            if (
                (original is not None and known is not None and original.checkout_id != known)
                or (current is not None and known is not None and current.checkout_id != known)
                or (
                    original is not None
                    and current is not None
                    and original.checkout_id != current.checkout_id
                )
            ):
                reasons.append("CHECKOUT_ID_CONFLICT")
            if (
                original is not None
                and current is not None
                and (
                    original.status != current.status
                    or original.snapshot_digest != current.snapshot_digest
                )
            ):
                reasons.append("HISTORICAL_CURRENT_DIFFER")
        reasons.append("CURRENT_STATE_NOT_CONTINUATION_AUTHORITY")
        invalid = {
            "AUTHORIZATION_UNVERIFIABLE",
            "LOCAL_CHECKOUT_CONFLICT",
            "LOCAL_HISTORY_UNVERIFIABLE",
            "REMOTE_SCOPE_CONFLICT",
            "REQUEST_KEY_CONFLICT",
            "ACP_FINGERPRINT_CONFLICT",
            "CHECKOUT_ID_CONFLICT",
        }
        outcome: Literal["blocked", "historical_confirmation_only"] = "blocked"
        if (
            recovered.local_outcome == "confirmed"
            and recovered.envelope_status == "available"
            and not invalid.intersection(reasons)
        ):
            outcome = "historical_confirmation_only"
            reasons.append("HISTORICAL_CONFIRMATION_ONLY")
        return outcome, tuple(reasons)

    def reconcile(self, request: ReconciliationRequest) -> ReconciliationEvent:
        request = parse_model(ReconciliationRequest, canonical_bytes(request))
        try:
            with self._atomic():
                # Acquire the write lock also for callers that used deferred BEGIN.
                self.connection.execute(
                    "UPDATE d1_reconciliation_projection SET version=version WHERE 0"
                )
                self._scope(request.operation_id, request.run_id, request.owner_id)
                digest = sha256_digest(
                    b"commerce-d1-reconciliation-request/v1\0" + canonical_bytes(request)
                )
                old = self.connection.execute(
                    "SELECT event_id FROM d1_reconciliation_events "
                    "WHERE operation_id=? AND request_id=?",
                    (request.operation_id, request.request_id),
                ).fetchone()
                if old is not None:
                    event = self._event(old[0])
                    if event.request_digest != digest:
                        raise EvidenceError("GOV_STATE_CONFLICT")
                    self._version(request.operation_id)
                    return event
                recovered, envelope, reasons = self._load(request)
                if recovered.projection_version != request.expected_projection_version:
                    raise EvidenceError("GOV_STATE_CONFLICT")
                outcome, reason_codes = self._assess(request, recovered, envelope, reasons)
                event = ReconciliationEvent(
                    schema_version="commerce-d1-reconciliation/1",
                    event_id="reconciliation:"
                    + sha256_digest(canonical_bytes([request.operation_id, request.request_id]))[
                        7:
                    ],
                    request_id=request.request_id,
                    request_digest=digest,
                    version=recovered.projection_version + 1,
                    recorded_at=utc_milliseconds(self.clock()),
                    source="local_sqlite_inspection/1",
                    recovered=recovered,
                    observation=request.observation,
                    outcome=outcome,
                    reasons=reason_codes,
                    replay=ReplayAssessment(),
                )
                self.connection.execute(
                    "INSERT INTO d1_reconciliation_events VALUES (?,?,?,?,?,?,?,?)",
                    (
                        event.event_id,
                        request.operation_id,
                        request.request_id,
                        event.version,
                        digest,
                        outcome,
                        canonical_bytes(event),
                        self._event_digest(event),
                    ),
                )
                if recovered.projection_version == 0:
                    changed = self.connection.execute(
                        "INSERT INTO d1_reconciliation_projection VALUES (?,?,?,?) "
                        "ON CONFLICT(operation_id) DO NOTHING",
                        (request.operation_id, event.version, event.event_id, outcome),
                    ).rowcount
                else:
                    changed = self.connection.execute(
                        "UPDATE d1_reconciliation_projection "
                        "SET version=?,last_event_id=?,outcome=? "
                        "WHERE operation_id=? AND version=?",
                        (
                            event.version,
                            event.event_id,
                            outcome,
                            request.operation_id,
                            request.expected_projection_version,
                        ),
                    ).rowcount
                if changed != 1:
                    raise EvidenceError("GOV_STATE_CONFLICT")
                return event
        except sqlite3.Error:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None

    def history(self, request: ReconciliationRequest) -> tuple[ReconciliationEvent, ...]:
        request = parse_model(ReconciliationRequest, canonical_bytes(request))
        self.connection.execute("SAVEPOINT d1_reconciliation_history")
        try:
            self._scope(request.operation_id, request.run_id, request.owner_id)
            return self._validated_history(request.operation_id)
        finally:
            self.connection.execute("RELEASE d1_reconciliation_history")
