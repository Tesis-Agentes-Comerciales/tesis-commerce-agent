"""Post-create journal/checkpoint only. Caller transaction; no network or replay."""

from contextlib import contextmanager

from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.evidence import EvidenceError
from commerce_agent.governance.post_create import PostCreateEvent
from commerce_agent.governance.provenance import utc_milliseconds

from .d1_dispatch_sqlite import SqliteD1DispatchStore
from .d1_envelope_sqlite import SqliteD1EnvelopeStore
from .provenance_sqlite import SqliteD1ProvenanceStore

_EVENTS = """CREATE TABLE IF NOT EXISTS d1_post_create_events (
    operation_id TEXT NOT NULL REFERENCES d1_checkout_operations(operation_id),
    version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>0),
    body BLOB NOT NULL CHECK(typeof(body)='blob'),
    digest TEXT NOT NULL CHECK(length(digest)=71),
    PRIMARY KEY(operation_id,version))"""
_CHECKPOINT = """CREATE TABLE IF NOT EXISTS d1_post_create_checkpoint (
    operation_id TEXT PRIMARY KEY NOT NULL,
    version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    FOREIGN KEY(operation_id,version) REFERENCES d1_post_create_events(operation_id,version))"""


def _transition(previous: PostCreateEvent | None, phase, effect) -> bool:
    if previous is None:
        return phase == "creation_observed" and effect is None
    if previous.phase in {"uncertain", "canceled", "awaiting_confirmation"}:
        return False
    if phase == "uncertain":
        return effect is None
    if previous.phase == "creation_observed":
        return (phase, effect) == ("intent", "verify_creation_get")
    if previous.phase == "intent":
        return phase == "observed" and effect == previous.effect
    if previous.phase == "creation_confirmed":
        return phase == "intent" and effect in {"update_details", "cancel_budget"}
    if previous.phase == "observed":
        if previous.effect is None:
            return False
        return (phase, effect) in {
            "verify_creation_get": {("creation_confirmed", None)},
            "update_details": {("intent", "get_current")},
            "get_current": {("awaiting_confirmation", None), ("intent", "cancel_budget")},
            "cancel_budget": {("canceled", None)},
        }[previous.effect]
    return False


class SqlitePostCreateStore:
    def __init__(self, connection, keys):
        self.connection, self.keys = connection, keys

    @contextmanager
    def _atomic(self):
        if (
            not self.connection.in_transaction
            or self.connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        ):
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        self.connection.execute("SAVEPOINT post_create")
        try:
            yield
        except BaseException:
            self.connection.execute("ROLLBACK TO post_create")
            raise
        finally:
            self.connection.execute("RELEASE post_create")

    def initialize(self) -> None:
        with self._atomic():
            ddl = SqliteD1DispatchStore(self.connection)._ddl
            ddl(_EVENTS, "d1_post_create_events")
            ddl(_CHECKPOINT, "d1_post_create_checkpoint")
            for table in ("d1_post_create_events", "d1_post_create_checkpoint"):
                actions = ("UPDATE", "DELETE") if table.endswith("events") else ("DELETE",)
                for action in actions:
                    name = f"{table}_no_{action.lower()}"
                    ddl(
                        f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'post_create_immutable'); END",
                        name,
                    )
                name = f"{table}_no_replace"
                condition = "operation_id=NEW.operation_id"
                if table.endswith("events"):
                    condition += " AND version=NEW.version"
                ddl(
                    f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE INSERT ON {table} "
                    f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {condition}) "
                    "BEGIN SELECT RAISE(ABORT,'post_create_immutable'); END",
                    name,
                )
            for action in ("INSERT", "UPDATE"):
                name = f"d1_post_checkpoint_{action.lower()}"
                version = (
                    "NEW.version!=1"
                    if action == "INSERT"
                    else ("NEW.operation_id!=OLD.operation_id OR NEW.version!=OLD.version+1")
                )
                ddl(
                    f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} "
                    f"ON d1_post_create_checkpoint WHEN {version} OR NOT EXISTS("
                    "SELECT 1 FROM d1_post_create_events WHERE operation_id=NEW.operation_id "
                    "AND version=NEW.version AND digest=NEW.digest) "
                    "BEGIN SELECT RAISE(ABORT,'post_create_conflict'); END",
                    name,
                )

    def _scope(self, run_id, owner_id):
        row = self.connection.execute(
            "SELECT o.operation_id,p.stage,p.state_version,p.checkout_id,o.resulting_version "
            "FROM d1_checkout_operations o JOIN purchases p ON p.run_id=o.run_id "
            "WHERE o.run_id=? AND p.owner_id=? AND o.state='claimed'",
            (run_id, owner_id),
        ).fetchone()
        if row is None:
            raise EvidenceError("GOV_FACTS_UNAVAILABLE")
        envelope = SqliteD1EnvelopeStore(self.connection, self.keys).recover(
            row[0],
            run_id=run_id,
            owner_id=owner_id,
        )
        if envelope is None:
            raise EvidenceError("GOV_FACTS_UNAVAILABLE")
        owner_ref = SqliteD1ProvenanceStore(self.connection, self.keys)._episode(run_id, owner_id)[
            0
        ]
        return row, envelope, owner_ref

    @staticmethod
    def digest(event):
        return sha256_digest(b"commerce-post-create-event/v1\0" + canonical_bytes(event))

    def history(self, run_id: str, owner_id: str) -> tuple[PostCreateEvent, ...]:
        row, envelope, owner_ref = self._scope(run_id, owner_id)
        tables = {
            r[0]
            for r in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"d1_post_create_events", "d1_post_create_checkpoint"} <= tables:
            if {"d1_post_create_events", "d1_post_create_checkpoint"} & tables:
                raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
            return ()
        rows = self.connection.execute(
            "SELECT version,body,digest FROM d1_post_create_events "
            "WHERE operation_id=? ORDER BY version",
            (row[0],),
        ).fetchall()
        events: list[PostCreateEvent] = []
        previous_digest = None
        try:
            for version, (stored_version, body, digest) in enumerate(rows, 1):
                event = parse_model(PostCreateEvent, body)
                if (
                    canonical_bytes(event) != body
                    or self.digest(event) != digest
                    or event.version != version
                    or stored_version != version
                    or event.previous_digest != previous_digest
                    or event.operation_id != envelope.operation_id
                    or event.run_id != run_id
                    or event.owner_ref != owner_ref
                    or event.evidence_id != envelope.evidence_id
                    or event.evidence_digest != envelope.evidence_digest
                    or event.envelope_digest != envelope.digest()
                    or event.binding_status != envelope.binding_status
                    or (events and event.checkout_id != events[0].checkout_id)
                    or not _transition(events[-1] if events else None, event.phase, event.effect)
                ):
                    raise ValueError("Invalid post-create history")
                events.append(event)
                previous_digest = digest
            checkpoint = self.connection.execute(
                "SELECT version,digest FROM d1_post_create_checkpoint WHERE operation_id=?",
                (row[0],),
            ).fetchone()
            if (tuple(checkpoint) if checkpoint else None) != (
                (len(events), previous_digest) if events else None
            ):
                raise ValueError("Invalid checkpoint")
            if any(event.phase == "creation_confirmed" for event in events):
                creation = self.connection.execute(
                    "SELECT state,checkout_id,code FROM d1_checkout_results WHERE operation_id=?",
                    (row[0],),
                ).fetchone()
                if (
                    creation is None
                    or tuple(creation) != ("confirmed", events[0].checkout_id, "CHECKOUT_VERIFIED")
                    or row[3] != events[0].checkout_id
                ):
                    raise ValueError("Creation evidence changed")
        except Exception:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None
        return tuple(events)

    def append(
        self,
        run_id,
        owner_id,
        *,
        expected_version,
        phase,
        effect,
        recorded_at,
        checkout_id=None,
        snapshot=None,
        request_digest=None,
        key_digest=None,
    ) -> PostCreateEvent:
        with self._atomic():
            self.connection.execute("UPDATE d1_post_create_checkpoint SET version=version WHERE 0")
            row, envelope, owner_ref = self._scope(run_id, owner_id)
            history = self.history(run_id, owner_id)
            previous = history[-1] if history else None
            if len(history) != expected_version or not _transition(previous, phase, effect):
                raise EvidenceError("GOV_STATE_CONFLICT")
            effective_checkout_id = previous.checkout_id if previous else checkout_id
            if not isinstance(effective_checkout_id, str):
                raise EvidenceError("GOV_INPUT_INVALID")
            prepared = phase == "creation_confirmed" or any(
                event.phase == "creation_confirmed" for event in history
            )
            if (
                row[1] != ("checkout_prepared" if prepared else "selected")
                or row[2] != row[4] + int(prepared)
                or row[3] != (effective_checkout_id if prepared else None)
            ):
                raise EvidenceError("GOV_STATE_CONFLICT")
            event = PostCreateEvent(
                operation_id=envelope.operation_id,
                run_id=run_id,
                owner_ref=owner_ref,
                checkout_id=effective_checkout_id,
                version=len(history) + 1,
                phase=phase,
                effect=effect,
                request_digest=request_digest,
                key_digest=key_digest,
                snapshot_digest=sha256_digest(canonical_bytes(snapshot)) if snapshot else None,
                previous_digest=self.digest(previous) if previous else None,
                evidence_id=envelope.evidence_id,
                evidence_digest=envelope.evidence_digest,
                envelope_digest=envelope.digest(),
                recorded_at=utc_milliseconds(recorded_at),
                binding_status=envelope.binding_status,
            )
            if phase == "creation_confirmed":
                confirmed = self.connection.execute(
                    "SELECT state,checkout_id FROM d1_checkout_results WHERE operation_id=?",
                    (row[0],),
                ).fetchone()
                if confirmed is None or tuple(confirmed) != ("confirmed", event.checkout_id):
                    raise EvidenceError("GOV_STATE_CONFLICT")
            if phase in {"awaiting_confirmation", "canceled", "uncertain"}:
                if (
                    row[1] not in {"selected", "checkout_prepared"}
                    or row[2] != (row[4] + (1 if row[1] == "checkout_prepared" else 0))
                    or (row[3] is not None and row[3] != event.checkout_id)
                ):
                    raise EvidenceError("GOV_STATE_CONFLICT")
                changed = self.connection.execute(
                    "UPDATE purchases SET stage=?,state_version=state_version+1 "
                    "WHERE run_id=? AND owner_id=? AND state_version=? AND stage=?",
                    (phase, run_id, owner_id, row[2], row[1]),
                ).rowcount
                if changed != 1:
                    raise EvidenceError("GOV_STATE_CONFLICT")
            digest = self.digest(event)
            self.connection.execute(
                "INSERT INTO d1_post_create_events VALUES (?,?,?,?)",
                (row[0], event.version, canonical_bytes(event), digest),
            )
            if not history:
                self.connection.execute(
                    "INSERT INTO d1_post_create_checkpoint VALUES (?,?,?)",
                    (row[0], event.version, digest),
                )
            else:
                changed = self.connection.execute(
                    "UPDATE d1_post_create_checkpoint SET version=?,digest=? "
                    "WHERE operation_id=? AND version=?",
                    (event.version, digest, row[0], expected_version),
                ).rowcount
                if changed != 1:
                    raise EvidenceError("GOV_STATE_CONFLICT")
            return event
