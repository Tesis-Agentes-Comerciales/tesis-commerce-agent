"""SQLite ledger of Human Not Present delegations (docs/design/hnp-governed.md §4.3-§4.5, §9.1).

Every unit of work opens `BEGIN IMMEDIATE` and reads `now` once inside it, from the injected
authority clock (real UTC), never from the sandbox or commercial clocks. `delegation_events` and
`hnp_controls` are append-only; events are chained by digest per grant, like `hp_evidence`.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

from ..delegation.models import (
    EXECUTOR_REF,
    OPERATOR_REF,
    ActorKind,
    ConsentRequest,
    ConsentResult,
    DelegationEvent,
    EventType,
    GrantRecord,
    KillSwitchState,
    MandateKind,
    MandateRecord,
    OutcomeCode,
    ProposalDraft,
    ProposalRecord,
    SignedMandate,
    TaskRecord,
    event_digest,
    token_digest,
)
from ..governance.canonical import canonical_bytes

Clock = Callable[[], datetime]
IdFactory = Callable[[str], str]


def _sql_list(literal: Any) -> str:
    return ",".join(f"'{value}'" for value in get_args(literal))


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS delegation_proposals (
        proposal_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
        scope_digest TEXT NOT NULL,
        checkout_details_json TEXT NOT NULL,
        presented_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        grant_id TEXT UNIQUE)""",
    """CREATE TABLE IF NOT EXISTS delegation_grants (
        grant_id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL UNIQUE,
        owner_id TEXT NOT NULL,
        grant_version INTEGER NOT NULL CHECK(grant_version = 1),
        scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
        scope_digest TEXT NOT NULL,
        intent_json TEXT NOT NULL,
        checkout_details_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('active','consumed','revoked','expired')),
        revoked_reason TEXT CHECK(revoked_reason IN ('owner','operator','escalated','failed')),
        authority_epoch INTEGER NOT NULL CHECK(authority_epoch >= 1),
        state_version INTEGER NOT NULL CHECK(state_version >= 1),
        valid_from TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consented_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        closed_at TEXT,
        agent_key_kid TEXT NOT NULL,
        issuer_kid TEXT NOT NULL,
        CHECK((state = 'revoked') = (revoked_reason IS NOT NULL)))""",
    "CREATE INDEX IF NOT EXISTS delegation_grants_owner_state "
    "ON delegation_grants(owner_id, state)",
    """CREATE TABLE IF NOT EXISTS delegation_mandates (
        mandate_id TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(
            kind IN ('open_checkout','open_payment','closed_checkout','closed_payment')),
        reservation_id TEXT,
        token TEXT NOT NULL,
        token_digest TEXT NOT NULL,
        link_hash TEXT NOT NULL,
        public_jwk_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(grant_id, kind),
        CHECK((kind IN ('closed_checkout','closed_payment')) = (reservation_id IS NOT NULL)))""",
    """CREATE TABLE IF NOT EXISTS grant_reservations (
        reservation_id TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        checkout_id TEXT NOT NULL,
        operation_id TEXT NOT NULL UNIQUE,
        amount_minor INTEGER NOT NULL CHECK(typeof(amount_minor) = 'integer' AND amount_minor >= 0),
        currency TEXT NOT NULL CHECK(currency = 'COP'),
        expected_epoch INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(
            state IN ('reserved','committed','consumed','released','uncertain')),
        state_version INTEGER NOT NULL CHECK(state_version >= 1),
        release_basis TEXT CHECK(release_basis IN ('no_outbox','psp_error_receipt')),
        uncertain_reason TEXT CHECK(uncertain_reason IN (
            'dispatch_timeout','partial_verifier_failure','receipt_mismatch')),
        d2_evidence_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK((state = 'released') = (release_basis IS NOT NULL)),
        CHECK(state != 'uncertain' OR uncertain_reason IS NOT NULL),
        CHECK(uncertain_reason IS NULL OR state IN ('uncertain','consumed')))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS one_use_per_grant ON grant_reservations(grant_id) "
    "WHERE state IN ('reserved','committed','uncertain','consumed')",
    f"""CREATE TABLE IF NOT EXISTS hnp_tasks (
        task_id TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL UNIQUE,
        owner_id TEXT NOT NULL,
        run_id TEXT UNIQUE,
        state TEXT NOT NULL CHECK(state IN (
            'scheduled','running','purchased','escalated','failed','canceled','uncertain')),
        outcome_code TEXT CHECK(outcome_code IN ({_sql_list(OutcomeCode)})),
        claim_count INTEGER NOT NULL CHECK(claim_count >= 0),
        lease_id TEXT,
        lease_expires_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT,
        CHECK((state IN ('scheduled','running','purchased')) = (outcome_code IS NULL)),
        CHECK((state = 'running') = (lease_id IS NOT NULL)),
        CHECK((lease_id IS NULL) = (lease_expires_at IS NULL)))""",
    """CREATE TABLE IF NOT EXISTS dispatch_outbox (
        outbox_id TEXT PRIMARY KEY,
        reservation_id TEXT NOT NULL UNIQUE,
        operation_id TEXT NOT NULL UNIQUE,
        grant_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        checkout_id TEXT NOT NULL,
        amount_minor INTEGER NOT NULL CHECK(typeof(amount_minor) = 'integer' AND amount_minor >= 0),
        currency TEXT NOT NULL CHECK(currency = 'COP'),
        checkout_expires_at TEXT NOT NULL,
        payment_request_digest TEXT NOT NULL,
        authority_epoch INTEGER NOT NULL,
        control_epoch INTEGER NOT NULL,
        d3_evidence_id TEXT,
        state TEXT NOT NULL CHECK(state IN ('committed','payment_sent','payment_accepted',
            'complete_sent','completed','rejected','uncertain')),
        rejected_by TEXT CHECK(rejected_by IN ('psp','merchant')),
        ap2_error TEXT CHECK(ap2_error IN ('invalid_credential','invalid_mandate',
            'unresolved_constraint','mandates_not_supported')),
        order_id TEXT,
        error_receipt_jwt TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK((state = 'rejected') = (rejected_by IS NOT NULL)))""",
    f"""CREATE TABLE IF NOT EXISTS delegation_events (
        grant_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK(typeof(sequence) = 'integer' AND sequence >= 1),
        event_type TEXT NOT NULL CHECK(event_type IN ({_sql_list(EventType)})),
        actor_kind TEXT NOT NULL CHECK(actor_kind IN ('user','service','operator')),
        actor_ref TEXT NOT NULL,
        on_behalf_of TEXT NOT NULL,
        task_id TEXT,
        run_id TEXT,
        body TEXT NOT NULL CHECK(json_valid(body)),
        recorded_at TEXT NOT NULL,
        previous_digest TEXT CHECK((sequence = 1) = (previous_digest IS NULL)),
        digest TEXT NOT NULL UNIQUE,
        PRIMARY KEY(grant_id, sequence))""",
    # Also stops INSERT OR REPLACE, as in hp_evidence: a replaced row is never the next link.
    """CREATE TRIGGER IF NOT EXISTS delegation_events_chain BEFORE INSERT ON delegation_events
        WHEN NEW.sequence != COALESCE(
                (SELECT MAX(sequence) + 1 FROM delegation_events WHERE grant_id = NEW.grant_id), 1)
            OR NEW.previous_digest IS NOT (SELECT digest FROM delegation_events
                WHERE grant_id = NEW.grant_id ORDER BY sequence DESC LIMIT 1)
            OR EXISTS (SELECT 1 FROM delegation_events WHERE digest = NEW.digest)
        BEGIN SELECT RAISE(ABORT, 'delegation_events_chain_conflict'); END""",
    """CREATE TRIGGER IF NOT EXISTS delegation_events_no_update BEFORE UPDATE ON delegation_events
        BEGIN SELECT RAISE(ABORT, 'delegation_events_append_only'); END""",
    """CREATE TRIGGER IF NOT EXISTS delegation_events_no_delete BEFORE DELETE ON delegation_events
        BEGIN SELECT RAISE(ABORT, 'delegation_events_append_only'); END""",
    """CREATE TABLE IF NOT EXISTS hnp_controls (
        control_epoch INTEGER PRIMARY KEY CHECK(control_epoch >= 1),
        kill_switch INTEGER NOT NULL CHECK(kill_switch IN (0, 1)),
        actor_ref TEXT NOT NULL CHECK(actor_ref IN ('operator','bootstrap')),
        changed_at TEXT NOT NULL)""",
    """CREATE TRIGGER IF NOT EXISTS hnp_controls_next BEFORE INSERT ON hnp_controls
        WHEN NEW.control_epoch != COALESCE((SELECT MAX(control_epoch) + 1 FROM hnp_controls), 1)
        BEGIN SELECT RAISE(ABORT, 'hnp_controls_append_only'); END""",
    """CREATE TRIGGER IF NOT EXISTS hnp_controls_no_update BEFORE UPDATE ON hnp_controls
        BEGIN SELECT RAISE(ABORT, 'hnp_controls_append_only'); END""",
    """CREATE TRIGGER IF NOT EXISTS hnp_controls_no_delete BEFORE DELETE ON hnp_controls
        BEGIN SELECT RAISE(ABORT, 'hnp_controls_append_only'); END""",
)

# Each record maps 1:1 to its table: field names are the column names.
_TABLES: dict[Callable[..., Any], str] = {
    ProposalRecord: "delegation_proposals",
    GrantRecord: "delegation_grants",
    MandateRecord: "delegation_mandates",
    TaskRecord: "hnp_tasks",
}


def utc_text(moment: datetime) -> str:
    """`UtcTime` of policy-v1: milliseconds and `Z`, so text order is time order."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_moment(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class SqliteDelegationLedger:
    def __init__(
        self, path: Path, *, clock: Clock = _utc_now, id_factory: IdFactory = _new_id
    ) -> None:
        self._path = path
        self._clock = clock
        self._new_id = id_factory

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._unit() as (connection, now):
            for statement in _SCHEMA:
                connection.execute(statement)
            if connection.execute("SELECT 1 FROM hnp_controls").fetchone() is None:
                connection.execute(
                    "INSERT INTO hnp_controls VALUES (1, 0, 'bootstrap', ?)", (utc_text(now),)
                )

    # Proposal and consent

    def create_proposal(self, draft: ProposalDraft) -> ProposalRecord:
        if _consent_scope(draft.scope_json)["proposal_id"] != draft.proposal_id:
            raise ValueError("The consent scope belongs to another proposal")
        record = ProposalRecord(**asdict(draft), grant_id=None)
        with self._unit() as (connection, _):
            _insert(connection, record)
        return record

    def proposal(self, owner_id: str, proposal_id: str) -> ProposalRecord | None:
        with self._read() as connection:
            return _one(
                connection,
                ProposalRecord,
                "proposal_id = ? AND owner_id = ?",
                proposal_id,
                owner_id,
            )

    def consent(self, request: ConsentRequest) -> ConsentResult:
        """The consent unit of work; repeating it with the same digest returns the same grant."""
        with self._unit() as (connection, now):
            proposal = _one(
                connection,
                ProposalRecord,
                "proposal_id = ? AND owner_id = ?",
                request.proposal_id,
                request.owner_id,
            )
            if proposal is None:
                return ConsentResult("not_found")
            if proposal.scope_digest != request.scope_digest:
                return ConsentResult("scope_mismatch")
            if proposal.grant_id is not None:
                grant = _one(connection, GrantRecord, "grant_id = ?", proposal.grant_id)
                task = _one(connection, TaskRecord, "grant_id = ?", proposal.grant_id)
                return ConsentResult("existing", grant, task)
            if now >= utc_moment(proposal.expires_at):
                return ConsentResult("proposal_expired")
            grant, task = self._create_grant(connection, request, proposal, utc_text(now))
        return ConsentResult("created", grant, task)

    def _create_grant(
        self,
        connection: sqlite3.Connection,
        request: ConsentRequest,
        proposal: ProposalRecord,
        now: str,
    ) -> tuple[GrantRecord, TaskRecord]:
        scope = _consent_scope(proposal.scope_json)
        grant = GrantRecord(
            grant_id=self._new_id("dg"),
            proposal_id=proposal.proposal_id,
            owner_id=proposal.owner_id,
            grant_version=1,
            scope_json=proposal.scope_json,
            scope_digest=proposal.scope_digest,
            intent_json=request.intent_json,
            checkout_details_json=proposal.checkout_details_json,
            state="active",
            revoked_reason=None,
            authority_epoch=1,
            state_version=1,
            valid_from=now,
            expires_at=scope["expires_at"],
            consented_at=now,
            updated_at=now,
            closed_at=None,
            agent_key_kid=request.agent_key_kid,
            issuer_kid=request.issuer_kid,
        )
        task = TaskRecord(
            task_id=self._new_id("ht"),
            grant_id=grant.grant_id,
            owner_id=grant.owner_id,
            run_id=None,
            state="scheduled",
            outcome_code=None,
            claim_count=0,
            lease_id=None,
            lease_expires_at=None,
            created_at=now,
            updated_at=now,
            finished_at=None,
        )
        _insert(connection, grant)
        _insert(connection, task)
        opened: dict[MandateKind, SignedMandate] = {
            "open_checkout": request.open_checkout,
            "open_payment": request.open_payment,
        }
        for kind, mandate in opened.items():
            self._insert_mandate(connection, grant.grant_id, kind, None, mandate, now)
        connection.execute(
            "UPDATE delegation_proposals SET grant_id = ? WHERE proposal_id = ?",
            (grant.grant_id, proposal.proposal_id),
        )
        consented = {
            "proposal_id": proposal.proposal_id,
            "scope_digest": proposal.scope_digest,
            "presented_at": proposal.presented_at,
            "consented_at": now,
            "grant_version": 1,
            "authority_epoch": 1,
            "task_id": task.task_id,
        }
        issued = {
            "mandates": [
                {
                    "kind": kind,
                    "token_digest": token_digest(mandate.token),
                    "link_hash": mandate.link_hash,
                    "issuer_kid": request.issuer_kid,
                }
                for kind, mandate in opened.items()
            ],
            "cnf_kid": request.agent_key_kid,
            "exp": grant.expires_at,
            "payment_reference": request.open_checkout.link_hash,
        }
        context = (now, task.task_id, None)
        append_event(
            connection,
            grant.grant_id,
            "consented",
            "user",
            consented,
            *context,
            owner_ref=scope["owner_ref"],
        )
        append_event(connection, grant.grant_id, "mandates_issued", "service", issued, *context)
        return grant, task

    def _insert_mandate(
        self,
        connection: sqlite3.Connection,
        grant_id: str,
        kind: MandateKind,
        reservation_id: str | None,
        mandate: SignedMandate,
        now: str,
    ) -> None:
        record = MandateRecord(
            mandate_id=self._new_id("dm"),
            grant_id=grant_id,
            kind=kind,
            reservation_id=reservation_id,
            token=mandate.token,
            token_digest=token_digest(mandate.token),
            link_hash=mandate.link_hash,
            public_jwk_json=mandate.public_jwk_json,
            created_at=now,
        )
        _insert(connection, record)

    # Reads. Owner-scoped reads answer None for another owner, as for a missing row.

    def grant(self, owner_id: str, grant_id: str) -> GrantRecord | None:
        with self._read() as connection:
            return _one(
                connection, GrantRecord, "grant_id = ? AND owner_id = ?", grant_id, owner_id
            )

    def grants(self, owner_id: str) -> tuple[GrantRecord, ...]:
        with self._read() as connection:
            return _all(
                connection,
                GrantRecord,
                "owner_id = ? ORDER BY consented_at DESC, grant_id DESC",
                owner_id,
            )

    def task_for_grant(self, grant_id: str) -> TaskRecord | None:
        with self._read() as connection:
            return _one(connection, TaskRecord, "grant_id = ?", grant_id)

    def mandates(self, grant_id: str) -> tuple[MandateRecord, ...]:
        with self._read() as connection:
            return _all(connection, MandateRecord, "grant_id = ? ORDER BY kind", grant_id)

    def events(self, grant_id: str) -> tuple[DelegationEvent, ...]:
        with self._read() as connection:
            return read_events(connection, grant_id)

    # Kill switch (spec §8.4): one row per change; the highest control_epoch is the current one.

    def kill_switch(self) -> KillSwitchState:
        with self._read() as connection:
            return _control(connection)

    def set_kill_switch(self, enabled: bool) -> KillSwitchState:
        with self._unit() as (connection, now):
            current = _control(connection)
            if current.enabled == enabled:
                return current
            changed = KillSwitchState(enabled, current.control_epoch + 1, utc_text(now))
            connection.execute(
                "INSERT INTO hnp_controls VALUES (?, ?, ?, ?)",
                (changed.control_epoch, int(enabled), OPERATOR_REF, changed.changed_at),
            )
        return changed

    # Connections

    @contextmanager
    def _unit(self) -> Iterator[tuple[sqlite3.Connection, datetime]]:
        """One `BEGIN IMMEDIATE` transaction and the single `now` its checks and rows use."""
        with self._read() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection, self._clock()
            except BaseException:
                connection.rollback()
                raise
            connection.commit()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=5)) as connection:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection


def append_event(
    connection: sqlite3.Connection,
    grant_id: str,
    event_type: EventType,
    actor_kind: ActorKind,
    body: Mapping[str, Any],
    recorded_at: str,
    task_id: str | None,
    run_id: str | None,
    *,
    owner_ref: str | None = None,
) -> DelegationEvent:
    """Next link of the grant's chain. The first link names the owner; later ones inherit it."""
    last = connection.execute(
        "SELECT sequence, digest, on_behalf_of FROM delegation_events WHERE grant_id = ? "
        "ORDER BY sequence DESC LIMIT 1",
        (grant_id,),
    ).fetchone()
    sequence, previous, owner = (1, None, owner_ref) if last is None else (last[0] + 1, *last[1:])
    if owner is None:
        raise ValueError("The first delegation event must name its owner")
    actor_ref = {"user": owner, "service": EXECUTOR_REF, "operator": OPERATOR_REF}[actor_kind]
    text = canonical_bytes(dict(body)).decode()
    unsigned = DelegationEvent(
        grant_id=grant_id,
        sequence=sequence,
        event_type=event_type,
        actor_kind=actor_kind,
        actor_ref=actor_ref,
        on_behalf_of=owner,
        task_id=task_id,
        run_id=run_id,
        body=json.loads(text),
        recorded_at=recorded_at,
        previous_digest=previous,
        digest="",
    )
    event = replace(unsigned, digest=event_digest(unsigned.link()))
    row = {**event.link(), "body": text, "digest": event.digest}
    names = ", ".join(row)
    connection.execute(
        f"INSERT INTO delegation_events ({names}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )
    return event


def read_events(connection: sqlite3.Connection, grant_id: str) -> tuple[DelegationEvent, ...]:
    """A grant's events in order, from any connection (the evidence verifier reads read-only)."""
    names = [field.name for field in fields(DelegationEvent)]
    rows = connection.execute(
        f"SELECT {', '.join(names)} FROM delegation_events WHERE grant_id = ? ORDER BY sequence",
        (grant_id,),
    )
    return tuple(
        DelegationEvent(**{**dict(zip(names, row, strict=True)), "body": json.loads(row[8])})
        for row in rows
    )


def _consent_scope(scope_json: str) -> dict[str, Any]:
    """The fields the ledger reads from ConsentScope (§4.2); the bytes must be canonical."""
    scope = json.loads(scope_json)
    if canonical_bytes(scope).decode() != scope_json:
        raise ValueError("The consent scope is not canonical JSON")
    for name in ("proposal_id", "owner_ref", "expires_at"):
        if not isinstance(scope.get(name), str):
            raise ValueError(f"The consent scope lacks {name}")
    utc_moment(scope["expires_at"])
    return scope


def _insert(connection: sqlite3.Connection, record: Any) -> None:
    row = asdict(record)
    connection.execute(
        f"INSERT INTO {_TABLES[type(record)]} ({', '.join(row)}) "
        f"VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )


def _all[T](
    connection: sqlite3.Connection, record: Callable[..., T], where: str, *params: object
) -> tuple[T, ...]:
    names = ", ".join(field.name for field in fields(record))  # pyright: ignore[reportArgumentType]
    cursor = connection.execute(f"SELECT {names} FROM {_TABLES[record]} WHERE {where}", params)
    return tuple(record(*row) for row in cursor)


def _one[T](
    connection: sqlite3.Connection, record: Callable[..., T], where: str, *params: object
) -> T | None:
    rows = _all(connection, record, where, *params)
    return rows[0] if rows else None


def _control(connection: sqlite3.Connection) -> KillSwitchState:
    enabled, epoch, changed_at = connection.execute(
        "SELECT kill_switch, control_epoch, changed_at FROM hnp_controls "
        "ORDER BY control_epoch DESC LIMIT 1"
    ).fetchone()
    return KillSwitchState(bool(enabled), epoch, changed_at)
