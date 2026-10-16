"""Additive lookup evidence. No writes to D1/commercial tables; no HTTP in a write TX."""

import sqlite3
from collections.abc import Callable
from contextlib import closing, contextmanager
from datetime import datetime

from commerce_agent.acp.creation_lookup import CreationLookupClient
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.evidence import EvidenceError
from commerce_agent.governance.provenance import ProvenanceKeys, utc_milliseconds
from commerce_agent.governance.reconciliation import ReconciliationRequest
from commerce_agent.governance.remote_creation import (
    LocalCreation,
    Verification,
    VerificationAttempt,
    VerificationEvent,
    VerificationRequest,
)

from .d1_dispatch_sqlite import SqliteD1DispatchStore
from .d1_reconciliation_sqlite import SqliteD1Reconciler
from .post_create_sqlite import SqlitePostCreateStore

_TABLE = """CREATE TABLE IF NOT EXISTS d1_remote_creation_events (
    operation_id TEXT NOT NULL REFERENCES d1_checkout_operations(operation_id),
    version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>0),
    query_id TEXT NOT NULL,
    body BLOB NOT NULL CHECK(typeof(body)='blob'),
    digest TEXT NOT NULL CHECK(length(digest)=71),
    PRIMARY KEY(operation_id,version), UNIQUE(operation_id,query_id))"""
_HEAD = """CREATE TABLE IF NOT EXISTS d1_remote_creation_head (
    operation_id TEXT PRIMARY KEY NOT NULL,
    version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>0),
    digest TEXT NOT NULL CHECK(length(digest)=71),
    FOREIGN KEY(operation_id,version) REFERENCES d1_remote_creation_events(operation_id,version))"""


class SqliteRemoteCreationStore:
    """append returns a provisional event; only the coordinator reports durable commit."""

    def __init__(
        self, connection: sqlite3.Connection, keys: ProvenanceKeys, *, clock: Callable[[], datetime]
    ):
        self.connection, self.keys, self.clock = connection, keys, clock

    @contextmanager
    def _atomic(self):
        if (
            not self.connection.in_transaction
            or self.connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        ):
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        self.connection.execute("SAVEPOINT remote_creation")
        try:
            yield
        except BaseException:
            self.connection.execute("ROLLBACK TO remote_creation")
            raise
        finally:
            self.connection.execute("RELEASE remote_creation")

    def initialize(self) -> None:
        with self._atomic():
            ddl = SqliteD1DispatchStore(self.connection)._ddl
            ddl(_TABLE, "d1_remote_creation_events")
            ddl(_HEAD, "d1_remote_creation_head")
            for action in ("UPDATE", "DELETE"):
                name = f"d1_remote_creation_no_{action.lower()}"
                ddl(
                    f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} "
                    "ON d1_remote_creation_events "
                    "BEGIN SELECT RAISE(ABORT,'remote_creation_immutable'); END",
                    name,
                )
            name = "d1_remote_creation_no_replace"
            ddl(
                f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE INSERT ON d1_remote_creation_events "
                "WHEN EXISTS(SELECT 1 FROM d1_remote_creation_events "
                "WHERE operation_id=NEW.operation_id AND "
                "(version=NEW.version OR query_id=NEW.query_id)) "
                "BEGIN SELECT RAISE(ABORT,'remote_creation_immutable'); END",
                name,
            )
            ddl(
                "CREATE TRIGGER IF NOT EXISTS d1_remote_head_no_delete BEFORE DELETE "
                "ON d1_remote_creation_head "
                "BEGIN SELECT RAISE(ABORT,'remote_creation_immutable'); END",
                "d1_remote_head_no_delete",
            )
            ddl(
                "CREATE TRIGGER IF NOT EXISTS d1_remote_head_no_replace BEFORE INSERT "
                "ON d1_remote_creation_head WHEN EXISTS(SELECT 1 FROM d1_remote_creation_head "
                "WHERE operation_id=NEW.operation_id) "
                "BEGIN SELECT RAISE(ABORT,'remote_creation_immutable'); END",
                "d1_remote_head_no_replace",
            )
            for action in ("INSERT", "UPDATE"):
                name = f"d1_remote_head_validate_{action.lower()}"
                version = (
                    "NEW.version!=1"
                    if action == "INSERT"
                    else ("NEW.operation_id!=OLD.operation_id OR NEW.version!=OLD.version+1")
                )
                ddl(
                    f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} "
                    f"ON d1_remote_creation_head WHEN {version} OR NOT EXISTS("
                    "SELECT 1 FROM d1_remote_creation_events WHERE operation_id=NEW.operation_id "
                    "AND version=NEW.version AND digest=NEW.digest) "
                    "BEGIN SELECT RAISE(ABORT,'remote_creation_conflict'); END",
                    name,
                )

    @staticmethod
    def request_digest(request: VerificationRequest) -> str:
        # The owner is pseudonymized by D1; do not journal plaintext identity.
        return sha256_digest(b"commerce-remote-creation-query/v1\0" + canonical_bytes(request))

    @staticmethod
    def digest(event: VerificationEvent) -> str:
        return sha256_digest(b"commerce-remote-creation-event/v1\0" + canonical_bytes(event))

    def snapshot(self, request: VerificationRequest):
        request = parse_model(VerificationRequest, canonical_bytes(request))
        rec = ReconciliationRequest(
            schema_version="commerce-d1-reconciliation/1",
            request_id=request.query_id,
            operation_id=request.operation_id,
            run_id=request.run_id,
            owner_id=request.owner_id,
            expected_projection_version=0,
            observation=None,
        )
        # One caller-owned read snapshot; reuse all original D1 and historical checks.
        reconciler = SqliteD1Reconciler(self.connection, self.keys, clock=self.clock)
        recovered, envelope, reasons = reconciler._load(rec)
        if envelope is None or reasons:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        post = ()
        if reconciler._has("d1_post_create_events") or reconciler._has("d1_post_create_checkpoint"):
            post = SqlitePostCreateStore(self.connection, self.keys).history(
                request.run_id, request.owner_id
            )
        snapshot = LocalCreation(
            recovered=recovered,
            envelope_digest=envelope.digest(),
            post_create_history_digest=sha256_digest(
                canonical_bytes([event.model_dump(mode="json") for event in post])
            ),
            historical_checkout_id=recovered.historical_checkout_id
            or (post[0].checkout_id if post else None),
        )
        return snapshot, envelope

    def history(
        self, request: VerificationRequest, local: LocalCreation | None = None
    ) -> tuple[VerificationEvent, ...]:
        if not self.connection.in_transaction:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        local = local or self.snapshot(request)[0]
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('d1_remote_creation_events','d1_remote_creation_head')"
            )
        }
        if len(tables) == 1:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        if not tables:
            return ()
        rows = self.connection.execute(
            "SELECT version,query_id,body,digest FROM d1_remote_creation_events "
            "WHERE operation_id=? ORDER BY version",
            (request.operation_id,),
        ).fetchall()
        events = []
        previous = None
        try:
            for version, (stored_version, query_id, body, digest) in enumerate(rows, 1):
                event = parse_model(VerificationEvent, body)
                recovered = event.local.recovered
                original_request = request.model_copy(
                    update={
                        "query_id": event.query_id,
                        "expected_version": version - 1,
                    }
                )
                if (
                    stored_version != version
                    or event.version != version
                    or event.query_id != query_id
                    or canonical_bytes(event) != body
                    or self.digest(event) != digest
                    or event.previous_digest != previous
                    or event.request_digest != self.request_digest(original_request)
                    or recovered.operation_id != request.operation_id
                    or recovered.run_id != request.run_id
                    or recovered.owner_ref != local.recovered.owner_ref
                    or recovered.evidence_id != local.recovered.evidence_id
                    or recovered.evidence_digest != local.recovered.evidence_digest
                    or recovered.d1_input_digest != local.recovered.d1_input_digest
                    or event.local.envelope_digest != local.envelope_digest
                ):
                    raise ValueError("Invalid historical verification")
                events.append(event)
                previous = digest
            head = self.connection.execute(
                "SELECT version,digest FROM d1_remote_creation_head WHERE operation_id=?",
                (request.operation_id,),
            ).fetchone()
            if (tuple(head) if head else None) != ((len(events), previous) if events else None):
                raise ValueError("Invalid verification head")
        except Exception:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None
        return tuple(events)

    def append(
        self, request: VerificationRequest, local: LocalCreation, verification: Verification
    ) -> VerificationEvent:
        with self._atomic():
            self.initialize()
            # Also obtains the write lock for callers using deferred BEGIN.
            self.connection.execute("UPDATE d1_remote_creation_events SET version=version WHERE 0")
            current, _ = self.snapshot(request)
            history = self.history(request, current)
            digest = self.request_digest(request)
            for old in history:
                if old.query_id == request.query_id:
                    if (
                        old.request_digest != digest
                        or old.local != local
                        or old.verification != verification
                    ):
                        raise EvidenceError("GOV_STATE_CONFLICT")
                    return old
            if len(history) != request.expected_version or current != local:
                raise EvidenceError("GOV_STATE_CONFLICT")
            event = VerificationEvent(
                query_id=request.query_id,
                request_digest=digest,
                version=len(history) + 1,
                previous_digest=self.digest(history[-1]) if history else None,
                recorded_at=utc_milliseconds(self.clock()),
                local=local,
                verification=parse_model(Verification, canonical_bytes(verification)),
            )
            self.connection.execute(
                "INSERT INTO d1_remote_creation_events VALUES (?,?,?,?,?)",
                (
                    request.operation_id,
                    event.version,
                    event.query_id,
                    canonical_bytes(event),
                    self.digest(event),
                ),
            )
            if history:
                changed = self.connection.execute(
                    "UPDATE d1_remote_creation_head SET version=?,digest=? "
                    "WHERE operation_id=? AND version=? AND digest=?",
                    (
                        event.version,
                        self.digest(event),
                        request.operation_id,
                        request.expected_version,
                        self.digest(history[-1]),
                    ),
                ).rowcount
            else:
                changed = self.connection.execute(
                    "INSERT INTO d1_remote_creation_head VALUES (?,?,?)",
                    (request.operation_id, event.version, self.digest(event)),
                ).rowcount
            if changed != 1:
                raise EvidenceError("GOV_STATE_CONFLICT")
            return event


class RemoteCreationVerifier:
    """Explicit backend injection only. No Settings/runtime/tool integration or retries."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        keys: ProvenanceKeys,
        client: CreationLookupClient,
        *,
        clock: Callable[[], datetime],
    ):
        self.connect, self.keys, self.client, self.clock = connect, keys, client, clock

    def _store(self, connection):
        return SqliteRemoteCreationStore(connection, self.keys, clock=self.clock)

    def inspect(self, request: VerificationRequest) -> tuple[VerificationEvent, ...]:
        request = parse_model(VerificationRequest, canonical_bytes(request))
        with closing(self.connect()) as connection:
            connection.execute("BEGIN")
            try:
                return self._store(connection).history(request)
            finally:
                connection.rollback()

    def verify(self, request: VerificationRequest) -> VerificationAttempt:
        try:
            request = parse_model(VerificationRequest, canonical_bytes(request))
            with closing(self.connect()) as connection:
                connection.execute("BEGIN")
                try:
                    store = self._store(connection)
                    local, envelope = store.snapshot(request)
                    history = store.history(request, local)
                    for old in history:
                        if old.query_id == request.query_id:
                            if old.request_digest != store.request_digest(request):
                                raise EvidenceError("GOV_STATE_CONFLICT")
                            return VerificationAttempt(
                                verification=old.verification, durable=True, event=old
                            )
                    if len(history) != request.expected_version:
                        raise EvidenceError("GOV_STATE_CONFLICT")
                finally:
                    connection.rollback()
        except Exception as error:
            return self._failure(error, persistence=False)
        # No SQLite connection/transaction remains open during HTTP. No commercial client.
        verification = self.client.lookup(envelope, local.historical_checkout_id)
        try:
            with closing(self.connect()) as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    event = self._store(connection).append(request, local, verification)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            # A returned/provisional object or a commit() that silently did nothing
            # cannot be labelled durable. Reopen and verify the complete journal.
            if event not in self.inspect(request):
                raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
            return VerificationAttempt(verification=event.verification, durable=True, event=event)
        except Exception as error:
            return self._failure(error, persistence=True)

    @staticmethod
    def _failure(error: Exception, *, persistence: bool) -> VerificationAttempt:
        conflict = isinstance(error, EvidenceError) and str(error) == "GOV_STATE_CONFLICT"
        return VerificationAttempt(
            verification=Verification(
                outcome="cas_conflict"
                if conflict
                else "persistence_error"
                if persistence
                else "local_invalid",
                reason="LOCAL_STATE_CHANGED"
                if conflict
                else "LOCAL_PERSISTENCE_FAILED"
                if persistence
                else "LOCAL_INTEGRITY_FAILED",
            ),
            durable=False,
            event=None,
        )
