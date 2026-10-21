"""SQLite ledger of Human Not Present delegations (docs/design/hnp-governed.md §4.3-§4.5, §9.1).

Every unit of work opens `BEGIN IMMEDIATE` and reads `now` once inside it, from the injected
authority clock (real UTC), never from the sandbox or commercial clocks. `delegation_events` and
`hnp_controls` are append-only; events are chained by digest per grant, like `hp_evidence`.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import asdict, fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

from ..delegation.models import (
    EXECUTOR_REF,
    LEASE_SECONDS,
    OPERATOR_REF,
    RESERVE_MARGIN_SECONDS,
    ActorKind,
    Ap2Error,
    Authority,
    ClosedMandates,
    ConsentRequest,
    ConsentResult,
    DelegationEvent,
    DispatchCommit,
    DispatchStep,
    EventType,
    Execution,
    GrantRecord,
    GrantState,
    KillSwitchStage,
    KillSwitchState,
    LedgerCounts,
    MandateKind,
    MandateRecord,
    OutboxRecord,
    OutcomeCode,
    ProposalDraft,
    ProposalRecord,
    Receipt,
    ReleaseBasis,
    ReservationRecord,
    ReservationRequest,
    Revocation,
    RevokedReason,
    RevokeEffect,
    SignedMandate,
    Step,
    TaskFinish,
    TaskRecord,
    TaskState,
    UncertainReason,
    event_digest,
    token_digest,
)
from ..delegation.ports import TransactionHook
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
    ReservationRecord: "grant_reservations",
    OutboxRecord: "dispatch_outbox",
}
_KEYS = {
    "hnp_tasks": "task_id",
    "grant_reservations": "reservation_id",
    "dispatch_outbox": "outbox_id",
}
_LEASE = timedelta(seconds=LEASE_SECONDS)
_DISPATCH_STEPS = ("committed", "payment_sent", "payment_accepted", "complete_sent", "completed")
_SENT = ("payment_sent", "complete_sent")  # §4.4: only a send can end in doubt
_CANCELLATIONS = frozenset({"revoked_by_owner", "kill_switch", "grant_expired"})
_UNCERTAIN = frozenset({"dispatch_timeout", "partial_verifier_failure", "receipt_mismatch"})
_VERIFIER_OUTCOMES: dict[Ap2Error, OutcomeCode] = {
    "invalid_credential": "verifier_invalid_credential",
    "invalid_mandate": "verifier_invalid_mandate",
    "unresolved_constraint": "verifier_unresolved_constraint",
    "mandates_not_supported": "verifier_mandates_not_supported",
}
_ESCALATIONS = frozenset(
    {
        "budget_not_satisfiable",
        "no_candidate_in_scope",
        "offer_changed",
        "terms_out_of_scope",
        "authorization_profile_unresolved",
        "verifier_unresolved_constraint",
        "verifier_mandates_not_supported",
    }
)
# How a terminal closes a still active grant (spec §8.1); revoked_by_owner finds it revoked.
_GRANT_CLOSE: dict[str, tuple[GrantState, RevokedReason | None]] = {
    "kill_switch": ("revoked", "operator"),
    "grant_expired": ("expired", None),
    "escalated": ("revoked", "escalated"),
    "failed": ("revoked", "failed"),
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

    def counts(self) -> LedgerCounts:
        """An `active` grant past `expires_at` is not counted: the views show it expired."""
        with self._read() as connection:
            (active,) = connection.execute(
                "SELECT COUNT(*) FROM delegation_grants WHERE state = 'active' AND expires_at > ?",
                (utc_text(self._clock()),),
            ).fetchone()
            tasks = dict(
                connection.execute(
                    "SELECT state, COUNT(*) FROM hnp_tasks "
                    "WHERE state IN ('running', 'uncertain') GROUP BY state"
                ).fetchall()
            )
        return LedgerCounts(active, tasks.get("running", 0), tasks.get("uncertain", 0))

    # Executor (spec §4.5). Every step after the claim is fenced by the task's lease: with a
    # foreign lease it writes nothing. A failed comparison writes nothing either and returns
    # `blocked` with its classification; the executor ends the task with `finish_task`, by CAS.
    # With an outbox row only the dispatch outcomes end the task (re-claim with outbox).

    def execution(self, task_id: str) -> Execution | None:
        with self._read() as connection:
            return _execution(connection, task_id)

    def claim(self, task_id: str, *, hooks: Sequence[TransactionHook] = ()) -> Step:
        """A new lease, or the terminal that the switch, the owner or the clock impose when there
        is no outbox row. `hooks` run only if the claim closes the task (a re-claim after start)."""
        with self._unit() as (connection, now):
            ex = _execution(connection, task_id)
            if ex is None:
                return Step("not_found")
            task, text = ex.task, utc_text(now)
            lapsed = task.lease_expires_at is not None and now >= utc_moment(task.lease_expires_at)
            if task.state != "scheduled" and not (task.state == "running" and lapsed):
                return Step("not_runnable", ex)
            switched = _switched_since_claim(connection, ex)
            outcome = None if ex.outbox else _blocking(ex, None, now, switched=switched)
            if outcome is not None:
                state = "failed" if outcome == "state_conflict" else "canceled"
                _close_task(connection, ex, state, outcome, text, stage="claim")
                _run(hooks, connection)
                return Step("closed", _execution(connection, task_id), outcome)
            run_id = task.run_id or self._new_id("run")
            claimed = {
                "run_id": run_id,
                "claim_count": task.claim_count + 1,
                "lease_expires_at": utc_text(now + _LEASE),
                "trigger": "service_token",
            }
            _update(
                connection,
                "hnp_tasks",
                task_id,
                state="running",
                run_id=run_id,
                claim_count=claimed["claim_count"],
                lease_id=self._new_id("lease"),
                lease_expires_at=claimed["lease_expires_at"],
                updated_at=text,
            )
            _event(connection, ex, "task_claimed", "service", claimed, text, run_id=run_id)
            return Step("applied", _execution(connection, task_id))

    def reserve(
        self, request: ReservationRequest, *, hooks: Sequence[TransactionHook] = ()
    ) -> Step:
        """UoW-1: the single-use reservation. A retry of the same operation returns it unchanged."""
        with self._fenced(request.task_id, request.lease_id) as (connection, now, ex):
            if ex is None:
                return Step("lease_lost")
            held = ex.reservation if ex.reservation and ex.reservation.state != "released" else None
            if held is not None and held.operation_id == request.operation_id:
                same = (held.reservation_id, held.checkout_id, held.amount_minor) == (
                    request.reservation_id,
                    request.checkout_id,
                    request.amount_minor,
                )
                return _renewed(connection, ex, now) if same else _conflict(ex)
            outcome = _blocking(ex, request.expected, now, RESERVE_MARGIN_SECONDS)
            if outcome is not None or held is not None:
                return Step("blocked", ex, outcome or "state_conflict")
            text, grant = utc_text(now), ex.grant
            _insert(
                connection,
                ReservationRecord(
                    reservation_id=request.reservation_id,
                    grant_id=grant.grant_id,
                    owner_id=grant.owner_id,
                    run_id=_run_id(ex),
                    checkout_id=request.checkout_id,
                    operation_id=request.operation_id,
                    amount_minor=request.amount_minor,
                    currency="COP",
                    expected_epoch=grant.authority_epoch,
                    state="reserved",
                    state_version=1,
                    release_basis=None,
                    uncertain_reason=None,
                    d2_evidence_id=request.d2_evidence_id,
                    created_at=text,
                    updated_at=text,
                ),
            )
            _bump_grant(connection, grant, text)
            reserved = {
                "reservation_id": request.reservation_id,
                "operation_id": request.operation_id,
                "amount_minor": request.amount_minor,
                "expected_epoch": grant.authority_epoch,
                "state_version": grant.state_version + 1,
                "d2_evidence_id": request.d2_evidence_id,
            }
            _event(connection, ex, "reservation_reserved", "service", reserved, text)
            return _applied(connection, ex, now, hooks)

    def record_closed_mandates(self, request: ClosedMandates) -> Step:
        """UoW-C: the closed mandates, saved as soon as they are signed; a resumed run reuses
        them instead of closing again (spec §4.5)."""
        with self._fenced(request.task_id, request.lease_id) as (connection, now, ex):
            if ex is None:
                return Step("lease_lost")
            reservation = ex.reservation
            if reservation is None or reservation.reservation_id != request.reservation_id:
                return _conflict(ex)
            if _one(connection, MandateRecord, "reservation_id = ?", reservation.reservation_id):
                return _renewed(connection, ex, now)
            if reservation.state != "reserved":
                return Step("blocked", ex, _blocking(ex, None, now) or "state_conflict")
            text = utc_text(now)
            closed: dict[MandateKind, SignedMandate] = {
                "closed_checkout": request.closed_checkout,
                "closed_payment": request.closed_payment,
            }
            for kind, mandate in closed.items():
                self._insert_mandate(
                    connection, ex.grant.grant_id, kind, reservation.reservation_id, mandate, text
                )
            body = {
                "mandates": [
                    {
                        "kind": kind,
                        "token_digest": token_digest(mandate.token),
                        "link_hash": mandate.link_hash,
                        "checkout_hash": request.checkout_hash,
                        "agent_key_kid": ex.grant.agent_key_kid,
                    }
                    for kind, mandate in closed.items()
                ]
            }
            _event(connection, ex, "mandates_closed", "service", body, text)
            return _applied(connection, ex, now, ())

    def commit_dispatch(
        self, request: DispatchCommit, *, hooks: Sequence[TransactionHook] = ()
    ) -> Step:
        """UoW-2: re-checks epoch, switch, validity and reservation, and commits the reservation
        with its outbox row. After this the purchase is in flight: nothing cancels it."""
        with self._fenced(request.task_id, request.lease_id) as (connection, now, ex):
            if ex is None:
                return Step("lease_lost")
            if ex.outbox is not None:
                return _renewed(connection, ex, now)
            reservation, grant = ex.reservation, ex.grant
            outcome = _blocking(ex, request.expected, now)
            if outcome is None and (
                reservation is None
                or (reservation.reservation_id, reservation.state, reservation.state_version)
                != (request.reservation_id, "reserved", request.reservation_version)
                or _one(connection, MandateRecord, "reservation_id = ?", request.reservation_id)
                is None
            ):
                outcome = "state_conflict"
            if outcome is not None or reservation is None:
                return Step("blocked", ex, outcome)
            text = utc_text(now)
            _update_reservation(connection, reservation, text, state="committed")
            _bump_grant(connection, grant, text)
            _insert(
                connection,
                OutboxRecord(
                    outbox_id=request.outbox_id,
                    reservation_id=reservation.reservation_id,
                    operation_id=reservation.operation_id,
                    grant_id=grant.grant_id,
                    run_id=reservation.run_id,
                    checkout_id=reservation.checkout_id,
                    amount_minor=reservation.amount_minor,
                    currency=reservation.currency,
                    checkout_expires_at=request.checkout_expires_at,
                    payment_request_digest=request.payment_request_digest,
                    authority_epoch=grant.authority_epoch,
                    control_epoch=ex.control.control_epoch,
                    d3_evidence_id=request.d3_evidence_id,
                    state="committed",
                    rejected_by=None,
                    ap2_error=None,
                    order_id=None,
                    error_receipt_jwt=None,
                    created_at=text,
                    updated_at=text,
                ),
            )
            committed = {
                "outbox_id": request.outbox_id,
                "d3_evidence_id": request.d3_evidence_id,
                "authority_epoch": grant.authority_epoch,
                "control_epoch": ex.control.control_epoch,
            }
            _event(connection, ex, "dispatch_committed", "service", committed, text)
            return _applied(connection, ex, now, hooks)

    def advance_dispatch(
        self, task_id: str, lease_id: str, to: DispatchStep, *, order_id: str | None = None
    ) -> Step:
        """One forward step of the outbox; repeating the current step is a no-op."""
        if (to == "completed") != (order_id is not None):
            raise ValueError("Only the completed step carries the order")
        with self._fenced(task_id, lease_id) as (connection, now, ex):
            if ex is None:
                return Step("lease_lost")
            outbox = ex.outbox
            if outbox is not None and outbox.state == to:
                if order_id != outbox.order_id:
                    raise ValueError("A completed dispatch keeps its order")
                return _renewed(connection, ex, now)
            if outbox is None or _next_dispatch_step(outbox.state) != to:
                return Step("blocked", ex)
            changes: dict[str, object] = {"state": to, "updated_at": utc_text(now)}
            if order_id is not None:
                changes["order_id"] = order_id
            _update(connection, "dispatch_outbox", outbox.outbox_id, **changes)
            return _applied(connection, ex, now, ())

    def mark_uncertain(
        self,
        task_id: str,
        lease_id: str,
        reason: UncertainReason,
        *,
        hooks: Sequence[TransactionHook] = (),
    ) -> Step:
        """Timeout, 5xx or 4xx without a valid receipt after a send: the reservation is kept."""
        with self._fenced(task_id, lease_id) as (connection, now, ex):
            if ex is None:
                return Step("lease_lost")
            if ex.outbox is None or ex.outbox.state not in _SENT:
                return Step("blocked", ex)
            text = utc_text(now)
            _update(
                connection,
                "dispatch_outbox",
                ex.outbox.outbox_id,
                state="uncertain",
                updated_at=text,
            )
            _settle_uncertain(connection, ex, reason, text)
            return _closed(connection, ex, hooks)

    def reject_dispatch(
        self,
        task_id: str,
        lease_id: str,
        ap2_error: Ap2Error,
        receipt: Receipt,
        *,
        escalate: bool = True,
        hooks: Sequence[TransactionHook] = (),
    ) -> Step:
        """A verified AP2 error receipt. From the PSP nothing reached the merchant: the
        reservation is released and the task escalates or fails (B0-HNP never escalates, so it
        passes `escalate=False`). From the merchant, after the PSP token: `uncertain`."""
        if receipt.status != "error" or receipt.error != ap2_error:
            raise ValueError("An error receipt with that AP2 error rejects the dispatch")
        with self._fenced(task_id, lease_id) as (connection, now, ex):
            if ex is None:
                return Step("lease_lost")
            outbox = ex.outbox
            rejected_by = {"payment_sent": "psp", "complete_sent": "merchant"}.get(
                outbox.state if outbox else ""
            )
            if outbox is None or rejected_by is None:
                return Step("blocked", ex)
            if receipt.kind != ("payment" if rejected_by == "psp" else "checkout"):
                raise ValueError("The receipt comes from the verifier that was sent to")
            text = utc_text(now)
            _update(
                connection,
                "dispatch_outbox",
                outbox.outbox_id,
                state="rejected",
                rejected_by=rejected_by,
                ap2_error=ap2_error,
                error_receipt_jwt=receipt.jwt,
                updated_at=text,
            )
            _receipt_event(connection, ex, receipt, text)
            if rejected_by == "merchant":
                _settle_uncertain(connection, ex, "partial_verifier_failure", text)
            else:
                outcome = _VERIFIER_OUTCOMES[ap2_error]
                escalates = escalate and outcome in _ESCALATIONS
                state = "escalated" if escalates else "failed"
                _close_task(connection, ex, state, outcome, text, release="psp_error_receipt")
            return _closed(connection, ex, hooks)

    def record_completion(
        self,
        task_id: str,
        lease_id: str,
        payment_receipt: Receipt,
        checkout_receipt: Receipt,
        *,
        hooks: Sequence[TransactionHook] = (),
    ) -> Step:
        """UoW-3: the order with both receipts consumes the reservation and the grant. A grant
        revoked or expired after the commitment stays so; the reservation is consumed anyway."""
        if (payment_receipt.kind, checkout_receipt.kind) != ("payment", "checkout") or (
            payment_receipt.error is not None or checkout_receipt.error is not None
        ):
            raise ValueError("UoW-3 takes the payment and checkout receipts, without error")
        with self._fenced(task_id, lease_id) as (connection, now, ex):
            if ex is None:
                return Step("lease_lost")
            outbox, reservation, grant = ex.outbox, ex.reservation, ex.grant
            if outbox is None or reservation is None or outbox.state != "completed":
                return Step("blocked", ex)
            if checkout_receipt.order_id != outbox.order_id:
                raise ValueError("The checkout receipt names the completed order")
            text = utc_text(now)
            _update_reservation(connection, reservation, text, state="consumed")
            if grant.state == "active":
                _close_grant(connection, grant, "consumed", None, text)
            else:
                _bump_grant(connection, grant, text)
            for receipt in (payment_receipt, checkout_receipt):
                _receipt_event(connection, ex, receipt, text)
            consumed = {"reservation_id": reservation.reservation_id, "order_id": outbox.order_id}
            _event(connection, ex, "consumed", "service", consumed, text)
            _finish(connection, ex, "purchased", None, text)
            return _closed(connection, ex, hooks)

    def finish_task(self, request: TaskFinish, *, hooks: Sequence[TransactionHook] = ()) -> Step:
        """The terminal of a task without an outbox row (spec §8.1), by CAS on its lease. It
        releases a held reservation and closes an active grant with the matching cause."""
        if (request.state == "canceled") != (request.outcome_code in _CANCELLATIONS) or (
            request.state == "escalated" and request.outcome_code not in _ESCALATIONS
        ):
            raise ValueError("The outcome does not belong to that terminal state")
        if (
            request.outcome_code in _UNCERTAIN
            or request.outcome_code in _VERIFIER_OUTCOMES.values()
        ):
            raise ValueError("Dispatch outcomes end the task through the outbox")
        if (request.outcome_code == "kill_switch") != (request.stage is not None):
            raise ValueError("Only the kill switch names the stage that met it")
        with self._fenced(request.task_id, request.lease_id) as (connection, now, ex):
            if ex is None:
                return Step("lease_lost")
            owner_revoked = ex.grant.revoked_reason == "owner"
            if ex.outbox is not None:
                return Step("blocked", ex)
            if (request.outcome_code == "revoked_by_owner") != owner_revoked:
                return _conflict(ex)
            _close_task(
                connection,
                ex,
                request.state,
                request.outcome_code,
                utc_text(now),
                stage=request.stage,
            )
            return _closed(connection, ex, hooks)

    # Revocation by the owner (spec §8.4)

    def revoke(self, owner_id: str, grant_id: str) -> Revocation | None:
        """Idempotent. Before the commitment it releases the reservation and leaves no outbox;
        after it the purchase stays in flight and is reconciled, without a promise to cancel."""
        with self._unit() as (connection, now):
            grant = _one(
                connection, GrantRecord, "grant_id = ? AND owner_id = ?", grant_id, owner_id
            )
            if grant is None:
                return None
            if grant.revoked_reason == "owner":
                return Revocation("revoked", grant, _revocation_effect(connection, grant_id))
            if grant.state == "revoked":
                return Revocation("already_closed", grant, "no_effect")
            if grant.state != "active" or now >= utc_moment(grant.expires_at):
                return Revocation("not_revocable", grant, None)
            ex = _execution(
                connection, _get(connection, TaskRecord, "grant_id = ?", grant_id).task_id
            )
            if ex is None:
                raise LookupError("A grant without its task")
            text, reservation = utc_text(now), ex.reservation
            held = reservation is not None and reservation.state == "reserved"
            effect: RevokeEffect = "in_flight" if ex.outbox else "released" if held else "no_effect"
            if reservation is not None and held:
                _release(connection, ex, reservation, "no_outbox", "user", text)
            _close_grant(connection, grant, "revoked", "owner", text)
            revoked = {
                "revoked_reason": "owner",
                "epoch_before": grant.authority_epoch,
                "epoch_after": grant.authority_epoch + 1,
                "effect": effect,
            }
            _event(connection, ex, "revoked", "user", revoked, text)
            if ex.task.state == "scheduled":
                _update(
                    connection,
                    "hnp_tasks",
                    ex.task.task_id,
                    state="canceled",
                    outcome_code="revoked_by_owner",
                    updated_at=text,
                    finished_at=text,
                )
            return Revocation(
                "revoked", _get(connection, GrantRecord, "grant_id = ?", grant_id), effect
            )

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
    def _fenced(
        self, task_id: str, lease_id: str
    ) -> Iterator[tuple[sqlite3.Connection, datetime, Execution | None]]:
        """A unit of work and the task's state, or None as the state when the lease is not ours."""
        with self._unit() as (connection, now):
            ex = _execution(connection, task_id)
            owned = ex is not None and ex.task.state == "running" and ex.task.lease_id == lease_id
            yield connection, now, ex if owned else None

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=5)) as connection:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            # The hooks write evidence and purchases, whose tables declare foreign keys.
            connection.execute("PRAGMA foreign_keys = ON")
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


# Executor steps


def _execution(connection: sqlite3.Connection, task_id: str) -> Execution | None:
    task = _one(connection, TaskRecord, "task_id = ?", task_id)
    if task is None:
        return None
    reservation = _one(
        connection, ReservationRecord, "grant_id = ? ORDER BY created_at DESC", task.grant_id
    )
    outbox = None
    if reservation is not None:
        outbox = _one(connection, OutboxRecord, "reservation_id = ?", reservation.reservation_id)
    grant = _get(connection, GrantRecord, "grant_id = ?", task.grant_id)
    return Execution(task, grant, _control(connection), reservation, outbox)


def _blocking(
    ex: Execution,
    expected: Authority | None,
    now: datetime,
    margin_seconds: int = 0,
    *,
    switched: bool = False,
) -> OutcomeCode | None:
    """Why the grant no longer authorizes this step, in the order of spec §4.5, or None.
    `switched`: the switch was on at some point since the task's last claim."""
    grant, control = ex.grant, ex.control
    if grant.revoked_reason == "owner":
        return "revoked_by_owner"
    stale_control = expected is not None and expected.control_epoch != control.control_epoch
    if control.enabled or switched or stale_control:
        return "kill_switch"
    expires_at = utc_moment(grant.expires_at)
    if (
        grant.state == "expired"
        or now >= expires_at
        or now + timedelta(seconds=margin_seconds) > expires_at
    ):
        return "grant_expired"
    if grant.state != "active" or (
        expected
        and (expected.authority_epoch, expected.state_version)
        != (grant.authority_epoch, grant.state_version)
    ):
        return "state_conflict"
    return None


def _switched_since_claim(connection: sqlite3.Connection, ex: Execution) -> bool:
    """Spec §8.4: switching it off does not revive old delegations. A switch turned on after the
    last claim blocks the re-claim even if it is off again, as when a `blocked(kill_switch)` lost
    its terminal to a crash, a failing hook or a lapsed lease."""
    row = connection.execute(
        "SELECT 1 FROM hnp_controls WHERE kill_switch = 1 AND changed_at >= ("
        "SELECT MAX(recorded_at) FROM delegation_events "
        "WHERE grant_id = ? AND event_type = 'task_claimed') LIMIT 1",
        (ex.grant.grant_id,),
    ).fetchone()
    return row is not None


def _close_task(
    connection: sqlite3.Connection,
    ex: Execution,
    state: TaskState,
    outcome: OutcomeCode,
    text: str,
    *,
    stage: KillSwitchStage | None = None,
    release: ReleaseBasis = "no_outbox",
) -> None:
    """A terminal of spec §8.1: release the held reservation, close an active grant, end. A PSP
    error receipt releases a committed reservation: nothing reached the merchant."""
    grant, reservation = ex.grant, ex.reservation
    actor: ActorKind = "operator" if outcome == "kill_switch" else "service"
    if outcome == "kill_switch":
        (switched_on,) = connection.execute(
            "SELECT MAX(control_epoch) FROM hnp_controls WHERE kill_switch = 1"
        ).fetchone()
        blocked = {"control_epoch": switched_on, "stage": stage}
        _event(connection, ex, "kill_switch_blocked", "service", blocked, text)
    held = "reserved" if release == "no_outbox" else "committed"
    released = reservation is not None and reservation.state == held
    if reservation is not None and released:
        _release(connection, ex, reservation, release, actor, text)
    target = _GRANT_CLOSE.get(outcome) or _GRANT_CLOSE.get(state)
    if grant.state == "active" and target is not None:
        _close_grant(connection, grant, *target, text)
        if target[0] == "expired":
            lapsed = {"expires_at": grant.expires_at, "observed_at": text}
            _event(connection, ex, "expired", "service", lapsed, text)
        else:
            revoked = {
                "revoked_reason": target[1],
                "epoch_before": grant.authority_epoch,
                "epoch_after": grant.authority_epoch + 1,
                "effect": "released" if released else "no_effect",
            }
            _event(connection, ex, "revoked", actor, revoked, text)
    elif released:
        _bump_grant(connection, grant, text)
    _finish(connection, ex, state, outcome, text)


def _settle_uncertain(
    connection: sqlite3.Connection, ex: Execution, reason: UncertainReason, text: str
) -> None:
    """After the outbox the reservation is never released by doubt: it stays `uncertain`."""
    if ex.reservation is None:
        raise LookupError("An outbox row without its reservation")
    _update_reservation(
        connection, ex.reservation, text, state="uncertain", uncertain_reason=reason
    )
    _bump_grant(connection, ex.grant, text)
    body = {"reservation_id": ex.reservation.reservation_id, "uncertain_reason": reason}
    _event(connection, ex, "reservation_uncertain", "service", body, text)
    _finish(connection, ex, "uncertain", reason, text)


def _release(
    connection: sqlite3.Connection,
    ex: Execution,
    reservation: ReservationRecord,
    basis: ReleaseBasis,
    actor: ActorKind,
    text: str,
) -> None:
    _update_reservation(connection, reservation, text, state="released", release_basis=basis)
    body = {"reservation_id": reservation.reservation_id, "release_basis": basis}
    _event(connection, ex, "reservation_released", actor, body, text)


def _finish(
    connection: sqlite3.Connection,
    ex: Execution,
    state: TaskState,
    outcome: OutcomeCode | None,
    text: str,
) -> None:
    task = ex.task
    _update(
        connection,
        "hnp_tasks",
        task.task_id,
        state=state,
        outcome_code=outcome,
        lease_id=None,
        lease_expires_at=None,
        updated_at=text,
        finished_at=text,
    )
    body = {"state": state, "outcome_code": outcome, "run_id": task.run_id}
    _event(connection, ex, "task_finished", "service", body, text)


def _close_grant(
    connection: sqlite3.Connection,
    grant: GrantRecord,
    state: GrantState,
    reason: RevokedReason | None,
    text: str,
) -> None:
    """Every step to a terminal state adds 1 to `authority_epoch` (spec §4.4)."""
    connection.execute(
        "UPDATE delegation_grants SET state = ?, revoked_reason = ?, closed_at = ?, "
        "authority_epoch = authority_epoch + 1 WHERE grant_id = ?",
        (state, reason, text, grant.grant_id),
    )
    _bump_grant(connection, grant, text)


def _bump_grant(connection: sqlite3.Connection, grant: GrantRecord, text: str) -> None:
    """Any transition of the grant or of its reservation (spec §4.3)."""
    connection.execute(
        "UPDATE delegation_grants SET state_version = state_version + 1, updated_at = ? "
        "WHERE grant_id = ?",
        (text, grant.grant_id),
    )


def _update_reservation(
    connection: sqlite3.Connection, reservation: ReservationRecord, text: str, **changes: object
) -> None:
    _update(
        connection,
        "grant_reservations",
        reservation.reservation_id,
        state_version=reservation.state_version + 1,
        updated_at=text,
        **changes,
    )


def _event(
    connection: sqlite3.Connection,
    ex: Execution,
    event_type: EventType,
    actor_kind: ActorKind,
    body: Mapping[str, Any],
    text: str,
    *,
    run_id: str | None = None,
) -> None:
    task = ex.task
    append_event(
        connection,
        task.grant_id,
        event_type,
        actor_kind,
        body,
        text,
        task.task_id,
        run_id or task.run_id,
    )


def _receipt_event(
    connection: sqlite3.Connection, ex: Execution, receipt: Receipt, text: str
) -> None:
    body: dict[str, object] = {
        "kind": receipt.kind,
        "receipt_digest": token_digest(receipt.jwt),
        "issuer": receipt.issuer,
        "status": receipt.status,
        "reference": receipt.reference,
        "error": receipt.error,
    }
    if receipt.kind == "payment":
        body["payment_id"] = receipt.payment_id
    else:
        body["order_id"] = receipt.order_id
    _event(connection, ex, "receipt_recorded", "service", body, text)


def _revocation_effect(connection: sqlite3.Connection, grant_id: str) -> RevokeEffect:
    (body,) = connection.execute(
        "SELECT body FROM delegation_events WHERE grant_id = ? AND event_type = 'revoked' "
        "ORDER BY sequence DESC LIMIT 1",
        (grant_id,),
    ).fetchone()
    return json.loads(body)["effect"]


def _next_dispatch_step(state: str) -> str | None:
    if state not in _DISPATCH_STEPS[:-1]:
        return None
    return _DISPATCH_STEPS[_DISPATCH_STEPS.index(state) + 1]


def _run_id(ex: Execution) -> str:
    if ex.task.run_id is None:
        raise LookupError("A running task without its run")
    return ex.task.run_id


def _renew(connection: sqlite3.Connection, ex: Execution, now: datetime) -> None:
    """Each fenced step renews the lease it holds (spec §4.5)."""
    connection.execute(
        "UPDATE hnp_tasks SET lease_expires_at = ?, updated_at = ? WHERE task_id = ?",
        (utc_text(now + _LEASE), utc_text(now), ex.task.task_id),
    )


def _run(hooks: Sequence[TransactionHook], connection: sqlite3.Connection) -> None:
    for hook in hooks:
        hook(connection)


def _renewed(connection: sqlite3.Connection, ex: Execution, now: datetime) -> Step:
    _renew(connection, ex, now)
    return Step("reused", _execution(connection, ex.task.task_id))


def _applied(
    connection: sqlite3.Connection,
    ex: Execution,
    now: datetime,
    hooks: Sequence[TransactionHook],
) -> Step:
    """The ledger's writes are done: renew the lease, then the hooks, before COMMIT."""
    _renew(connection, ex, now)
    _run(hooks, connection)
    return Step("applied", _execution(connection, ex.task.task_id))


def _closed(
    connection: sqlite3.Connection, ex: Execution, hooks: Sequence[TransactionHook]
) -> Step:
    _run(hooks, connection)
    after = _execution(connection, ex.task.task_id)
    return Step("closed", after, None if after is None else after.task.outcome_code)


def _conflict(ex: Execution) -> Step:
    return Step("blocked", ex, "state_conflict")


def _update(connection: sqlite3.Connection, table: str, key: str, **changes: object) -> None:
    assignments = ", ".join(f"{name} = ?" for name in changes)
    connection.execute(
        f"UPDATE {table} SET {assignments} WHERE {_KEYS[table]} = ?", (*changes.values(), key)
    )


def _get[T](
    connection: sqlite3.Connection, record: Callable[..., T], where: str, *params: object
) -> T:
    found = _one(connection, record, where, *params)
    if found is None:
        raise LookupError(f"No row in {_TABLES[record]}")
    return found


def _control(connection: sqlite3.Connection) -> KillSwitchState:
    enabled, epoch, changed_at = connection.execute(
        "SELECT kill_switch, control_epoch, changed_at FROM hnp_controls "
        "ORDER BY control_epoch DESC LIMIT 1"
    ).fetchone()
    return KillSwitchState(bool(enabled), epoch, changed_at)
