"""One immutable envelope per existing D1 reservation, caller-owned transaction."""

import sqlite3

from ..governance.canonical import canonical_bytes, parse_model, sha256_digest
from ..governance.dispatch_envelope import CheckoutCreationEnvelope
from ..governance.evidence import DecisionRecord, EvidenceError
from ..governance.provenance import D1EnforceConfiguration, ProvenanceKeys
from .d1_dispatch_sqlite import SqliteD1DispatchStore
from .governance_sqlite import SqliteGovernanceEvidenceStore
from .provenance_sqlite import SqliteD1ProvenanceStore

_TABLE = """CREATE TABLE IF NOT EXISTS d1_checkout_envelopes (
    operation_id TEXT PRIMARY KEY NOT NULL REFERENCES d1_checkout_operations(operation_id),
    canonical_envelope BLOB NOT NULL CHECK(typeof(canonical_envelope)='blob'),
    envelope_digest TEXT NOT NULL CHECK(length(envelope_digest)=71))"""


class SqliteD1EnvelopeStore:
    def __init__(self, connection: sqlite3.Connection, keys: ProvenanceKeys):
        self.connection = connection
        self.keys = keys

    def initialize(self) -> None:
        if not self.connection.in_transaction:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        self.connection.execute("SAVEPOINT d1_envelope_schema")
        try:
            self._initialize()
        except Exception:
            self.connection.execute("ROLLBACK TO d1_envelope_schema")
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None
        finally:
            self.connection.execute("RELEASE d1_envelope_schema")

    def _initialize(self) -> None:
        # Reuse the strict DDL verifier; no changes to existing reservation tables.
        ddl = SqliteD1DispatchStore(self.connection)._ddl
        ddl(_TABLE, "d1_checkout_envelopes")
        for action in ("UPDATE", "DELETE"):
            name = f"d1_envelope_no_{action.lower()}"
            ddl(
                f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} ON d1_checkout_envelopes "
                "BEGIN SELECT RAISE(ABORT,'d1_envelope_immutable'); END",
                name,
            )
        ddl(
            "CREATE TRIGGER IF NOT EXISTS d1_envelope_no_replace BEFORE INSERT "
            "ON d1_checkout_envelopes WHEN EXISTS(SELECT 1 FROM d1_checkout_envelopes "
            "WHERE operation_id=NEW.operation_id) "
            "BEGIN SELECT RAISE(ABORT,'d1_envelope_immutable'); END",
            "d1_envelope_no_replace",
        )

    def _verify(self, envelope: CheckoutCreationEnvelope) -> None:
        row = self.connection.execute(
            "SELECT o.run_id,p.owner_id,o.evidence_id,o.evidence_digest,o.input_digest,"
            "o.merchant_ref,o.variant_id,o.currency,o.quantity,o.idempotency_key,"
            "i.operation_id,i.evidence_id,i.idempotency_key,i.context_ref "
            "FROM d1_checkout_operations o JOIN purchases p ON p.run_id=o.run_id "
            "JOIN d1_enforce_inputs i ON i.run_id=o.run_id WHERE o.operation_id=?",
            (envelope.operation_id,),
        ).fetchone()
        body = envelope.body()
        if row is None or tuple(row) != (
            envelope.run_id,
            envelope.owner_id,
            envelope.evidence_id,
            envelope.evidence_digest,
            envelope.d1_input_digest,
            envelope.merchant_ref,
            body["line_items"][0]["id"],
            body["currency"].upper(),
            1,
            envelope.idempotency_key.get_secret_value(),
            envelope.operation_id,
            envelope.evidence_id,
            envelope.idempotency_key.get_secret_value(),
            canonical_bytes(envelope.context_ref),
        ):
            raise EvidenceError("GOV_STATE_CONFLICT")
        record = SqliteGovernanceEvidenceStore(self.connection).get(envelope.evidence_id)
        if (
            not isinstance(record, DecisionRecord)
            or record.decision.decision_id != envelope.decision_id
            or record.decision.outcome != "allow"
            or record.decision.governance_mode != "enforce"
            or record.decision.origin != "policy"
            or record.application.transition_ref != envelope.operation_id
            or record.application.disposition != "transition_committed"
            or sha256_digest(canonical_bytes(record)) != envelope.evidence_digest
            or record.recorded_at != envelope.prepared_at
        ):
            raise EvidenceError("GOV_STATE_CONFLICT")
        vault = SqliteD1ProvenanceStore(self.connection, self.keys)
        context = vault.recover_context(envelope.run_id, envelope.owner_id, envelope.context_ref)
        configuration = parse_model(
            D1EnforceConfiguration,
            vault._get(
                envelope.run_id, envelope.owner_id, context.data.commercial_ref, "commercial"
            )[0],
        )
        if configuration.merchant_base_url != envelope.merchant_origin:
            raise EvidenceError("GOV_STATE_CONFLICT")

    def persist(self, envelope: CheckoutCreationEnvelope) -> None:
        if not self.connection.in_transaction:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        envelope = parse_model(CheckoutCreationEnvelope, envelope.storage_bytes())
        self._verify(envelope)
        existing = self.connection.execute(
            "SELECT canonical_envelope,envelope_digest FROM d1_checkout_envelopes "
            "WHERE operation_id=?",
            (envelope.operation_id,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (envelope.storage_bytes(), envelope.digest()):
                raise EvidenceError("GOV_STATE_CONFLICT")
            return
        state = self.connection.execute(
            "SELECT state FROM d1_checkout_operations WHERE operation_id=?",
            (envelope.operation_id,),
        ).fetchone()
        if state is None or state[0] != "reserved":
            # A claimed legacy operation has no recoverable original wire intent.
            raise EvidenceError("GOV_STATE_CONFLICT")
        self.connection.execute(
            "INSERT INTO d1_checkout_envelopes VALUES (?,?,?)",
            (envelope.operation_id, envelope.storage_bytes(), envelope.digest()),
        )

    def recover(
        self, operation_id: str, *, run_id: str, owner_id: str
    ) -> CheckoutCreationEnvelope | None:
        # Provenance recovery requires a transaction. A read savepoint supplies
        # one snapshot and is released before any transport can be invoked.
        self.connection.execute("SAVEPOINT d1_envelope_read")
        try:
            return self._recover(operation_id, run_id=run_id, owner_id=owner_id)
        finally:
            self.connection.execute("RELEASE d1_envelope_read")

    def _recover(
        self, operation_id: str, *, run_id: str, owner_id: str
    ) -> CheckoutCreationEnvelope | None:
        row = self.connection.execute(
            "SELECT e.canonical_envelope,e.envelope_digest FROM d1_checkout_envelopes e "
            "JOIN d1_checkout_operations o ON o.operation_id=e.operation_id "
            "JOIN purchases p ON p.run_id=o.run_id "
            "WHERE e.operation_id=? AND o.run_id=? AND p.owner_id=?",
            (operation_id, run_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        try:
            envelope = parse_model(CheckoutCreationEnvelope, row[0])
            if envelope.storage_bytes() != row[0] or envelope.digest() != row[1]:
                raise ValueError("Invalid envelope digest")
            self._verify(envelope)
            return envelope
        except Exception:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None
