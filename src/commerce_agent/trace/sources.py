"""Read-only access to what one purchase trace is built from (docs/design/trace-panel.md).

Queries name their columns: mandates, receipts, signatures and idempotency keys are only tested
for presence, never read. The D1 creation envelope also holds an idempotency key: SQLite extracts
its `prepared_at` and nothing else leaves the database. Of an HNP grant, SQLite extracts the
consented total and merchant: its checkout details and mandates never leave it.
"""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import SecretBytes, TypeAdapter, ValidationError

from commerce_agent.application.conversation import turn_run_id
from commerce_agent.config import Settings, validate_service_base_url
from commerce_agent.delegation.models import DelegationEvent
from commerce_agent.delegation.models import chain_is_valid as delegation_chain_is_valid
from commerce_agent.governance.evaluator import GovernanceError, verify_artifact
from commerce_agent.governance.evidence import DecisionRecord, EvidenceError
from commerce_agent.governance.models import D1PolicyArtifact
from commerce_agent.governance.provenance import ProvenanceKeys, merchant_pseudonym
from commerce_agent.persistence.delegation_sqlite import read_events
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.hp_evidence_sqlite import HpEvidence, chain_is_valid, read_run


class FreshnessBody(TypedDict):
    """What the trace reads of a `freshness_check` body (OfferFreshnessGuard._body)."""

    stage: str
    governance_mode: str
    outcome: str
    decision: str
    changed_fields: list[str]
    checked_at: str
    compared: NotRequired[dict[str, dict[str, object]] | None]
    observed_revision: NotRequired[object]
    reason: NotRequired[object]


class RecoveryBody(TypedDict):
    """What the trace reads of a `governed_recovery` body (GovernedRecoveryRecord)."""

    kind: str
    cause: str
    variant_id: NotRequired[object]
    budget_minor: NotRequired[object]
    observed_unit_amount_minor: NotRequired[object]
    currency: NotRequired[object]


class ReservedBody(TypedDict):
    reservation_id: str
    amount_minor: int


class ReservationBody(TypedDict):
    reservation_id: str


class RevokedBody(TypedDict):
    revoked_reason: str
    effect: str


class FinishedBody(TypedDict):
    state: str
    outcome_code: str | None


class ReviewedBody(TypedDict):
    """The intent reviewer's verdict (hnp-intent-review.md §7); its prompt and model stay out."""

    decision: str
    reason: str


class ReceiptBody(TypedDict):
    """Which verifier issued a recorded AP2 receipt and its status; never the receipt itself."""

    kind: str
    status: str


# What the trace reads of each delegation event it shows (delegation_sqlite.py, spec §9.1).
DELEGATION_BODIES: dict[str, TypeAdapter[Any]] = {
    "reservation_reserved": TypeAdapter(ReservedBody),
    "reservation_released": TypeAdapter(ReservationBody),
    "reservation_uncertain": TypeAdapter(ReservationBody),
    "consumed": TypeAdapter(ReservationBody),
    "revoked": TypeAdapter(RevokedBody),
    "task_finished": TypeAdapter(FinishedBody),
    "receipt_recorded": TypeAdapter(ReceiptBody),
    "choice_reviewed": TypeAdapter(ReviewedBody),
}
HP_BODIES: dict[str, TypeAdapter[Any]] = {
    "freshness_check": TypeAdapter(FreshnessBody),
    "governed_recovery": TypeAdapter(RecoveryBody),
}
# One conversation thread's graph checkpoints, oldest first: (`ts`, channel values).
Checkpoints = list[tuple[str, dict[str, Any]]]


@dataclass(frozen=True)
class TurnSnapshot:
    """The conversation turn that started the purchase, from its graph checkpoints."""

    started_at: str
    selected_at: str | None
    candidate: dict[str, Any] | None


@dataclass(frozen=True)
class Delegation:
    """The grant of an HNP purchase (docs/design/hnp-governed.md §4.3, §9.1)."""

    grant_id: str
    total_max_minor: int
    expires_at: str
    merchant_ref: str
    # The grant's whole chain, verified: one task per grant, so every event is this run's.
    events: tuple[DelegationEvent, ...]


@dataclass(frozen=True)
class PurchaseRecords:
    purchase: dict[str, Any]
    operations: dict[str, str]
    terms: dict[str, Any] | None
    terms_shown_at: str | None
    approval: dict[str, Any] | None
    completion: dict[str, Any] | None
    order_events: tuple[tuple[str, str], ...]
    decisions: tuple[DecisionRecord, ...]
    policy: D1PolicyArtifact | None
    turn: TurnSnapshot | None
    # Application controls of the governed HP flow: offer freshness and governed recovery.
    hp_evidence: tuple[HpEvidence, ...] = ()
    # The D1 enforce path keeps no `checkout.create` key: its creation envelope has the time.
    checkout_prepared_at: str | None = None
    # D2 decisions, one per evaluated approval (b2-hp-v2), oldest first.
    authorizations: tuple[DecisionRecord, ...] = ()
    # Human Not Present: the delegation and the D3 decisions (b2-hnp-v1).
    delegation: Delegation | None = None
    dispatches: tuple[DecisionRecord, ...] = ()


def merchant_origins(settings: Settings) -> dict[str, str]:
    """Pseudonym of each configured merchant origin, so POL-02 can show origins, not refs."""
    pseudonym_hex, encryption_hex = settings.agent_d1_hmac_key_hex, settings.agent_d1_aes_key_hex
    if settings.agent_d1_key_id is None or pseudonym_hex is None or encryption_hex is None:
        return {}
    try:
        keys = ProvenanceKeys(
            key_id=settings.agent_d1_key_id,
            pseudonym_key=SecretBytes(bytes.fromhex(pseudonym_hex.get_secret_value())),
            encryption_key=SecretBytes(bytes.fromhex(encryption_hex.get_secret_value())),
        )
    except ValueError:
        return {}
    allowed = (settings.agent_d1_allowed_merchant_origins or "").split(",")
    origins: dict[str, str] = {}
    for candidate in (settings.merchant_base_url, *allowed):
        try:
            origin = validate_service_base_url(candidate.strip())
        except ValueError:
            continue
        origins[merchant_pseudonym(keys, origin)] = origin
    return origins


class GraphCheckpoints:
    """Read-only checkpoints of each conversation thread, deserialized once per instance.

    The history builds one per page: purchases of the same conversation share its thread.
    """

    def __init__(self, graph_database: Path) -> None:
        self._database = graph_database
        self._threads: dict[str, Checkpoints | None] = {}

    def thread(self, thread_id: str) -> Checkpoints | None:
        if thread_id not in self._threads:
            self._threads[thread_id] = self._read(thread_id)
        return self._threads[thread_id]

    def _read(self, thread_id: str) -> Checkpoints | None:
        if not self._database.exists():
            return None
        try:
            uri = f"{self._database.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, check_same_thread=False)) as connection:
                saver = SqliteSaver(
                    connection, serde=JsonPlusSerializer(allowed_msgpack_modules=[])
                )
                config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
                return [
                    (item.checkpoint["ts"], item.checkpoint["channel_values"])
                    for item in saver.list(config)
                ]
        except sqlite3.Error as error:
            if transient(error):
                raise
            return None


# SQLITE_BUSY and SQLITE_LOCKED, extended codes included.
_LOCK_CODES = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


def transient(error: BaseException) -> bool:
    """Another connection held a lock past the timeout (`database is locked`): reading again later
    can work, unlike evidence that does not verify."""
    code = getattr(error, "sqlite_errorcode", None)
    return (
        isinstance(error, sqlite3.OperationalError)
        and isinstance(code, int)
        and code & 0xFF in _LOCK_CODES
    )


def read_records(
    database: Path,
    graph_database: Path,
    run_id: str,
    *,
    checkpoints: GraphCheckpoints | None = None,
) -> PurchaseRecords | None:
    graph = checkpoints or GraphCheckpoints(graph_database)
    with closing(
        sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    ) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        # Added by SqliteAgentStore.initialize; a database it has not migrated yet has none.
        columns = {row[1] for row in connection.execute("PRAGMA table_info(purchases)")}
        optional = ", ".join(
            name if name in columns else f"NULL AS {name}"
            for name in ("failure_code", "purchase_mode", "grant_id")
        )
        purchase = _row(
            connection,
            "SELECT run_id, request_text, stage, checkout_id, order_id, created_at, updated_at, "
            f"{optional} FROM purchases WHERE run_id = ?",
            run_id,
        )
        if purchase is None:
            return None
        terms = _row(
            connection, "SELECT terms, updated_at FROM approval_terms WHERE run_id = ?", run_id
        )
        ledger = _ledger(connection, run_id)
        decisions = tuple(item for item in ledger if item.decision.phase == "D1")
        thread = _row(
            connection, "SELECT thread_id FROM purchase_graph_threads WHERE run_id = ?", run_id
        )
        return PurchaseRecords(
            purchase=purchase,
            operations=dict(
                connection.execute(
                    "SELECT operation, created_at FROM operation_keys WHERE run_id = ?", (run_id,)
                ).fetchall()
            ),
            terms=json.loads(terms["terms"]) if terms else None,
            terms_shown_at=terms["updated_at"] if terms else None,
            approval=_row(
                connection,
                "SELECT approved_at, checkout_mandate IS NOT NULL AS checkout, "
                "payment_mandate IS NOT NULL AS payment FROM approvals WHERE run_id = ?",
                run_id,
            ),
            completion=_row(
                connection,
                "SELECT order_id, completed_at, checkout_receipt_jwt IS NOT NULL AS checkout, "
                "payment_receipt_jwt IS NOT NULL AS payment FROM completion_results "
                "WHERE run_id = ?",
                run_id,
            ),
            order_events=tuple(
                connection.execute(
                    "SELECT event_type, received_at FROM webhook_events WHERE order_id = ? "
                    "ORDER BY received_at, rowid",
                    (purchase["order_id"],),
                ).fetchall()
            ),
            decisions=decisions,
            policy=_policy(connection, run_id, decisions),
            turn=_turn(graph.thread(thread["thread_id"]), run_id) if thread else None,
            hp_evidence=_hp_evidence(connection, run_id),
            checkout_prepared_at=_checkout_prepared_at(connection, run_id),
            authorizations=tuple(item for item in ledger if item.decision.phase == "D2"),
            delegation=_delegation(connection, purchase["grant_id"]),
            dispatches=tuple(item for item in ledger if item.decision.phase == "D3"),
        )


def _row(connection: sqlite3.Connection, query: str, run_id: str) -> dict[str, Any] | None:
    cursor = connection.execute(query, (run_id,))
    row = cursor.fetchone()
    return (
        dict(zip((column[0] for column in cursor.description), row, strict=True)) if row else None
    )


def _ledger(connection: sqlite3.Connection, run_id: str) -> tuple[DecisionRecord, ...]:
    """The run's evaluated decisions, D1 and D2, in ledger order."""
    # Databases that never ran with governance have no ledger at all.
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'governance_evidence'"
    ).fetchone():
        return ()
    evidence = SqliteGovernanceEvidenceStore(connection).for_run(run_id)
    return tuple(item for item in evidence if isinstance(item, DecisionRecord))


def _hp_evidence(connection: sqlite3.Connection, run_id: str) -> tuple[HpEvidence, ...]:
    """The run's hp_evidence chain; a broken link or a body the trace cannot read makes the
    whole trace unavailable."""
    # Created only when a governed HP control is configured (off databases never have it).
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'hp_evidence'"
    ).fetchone():
        return ()
    try:
        records = read_run(connection, run_id)
    except (sqlite3.Error, ValueError) as error:
        if transient(error):
            raise
        raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None
    if not chain_is_valid(run_id, records):
        raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
    try:
        for record in records:
            HP_BODIES[record.record_kind].validate_python(record.body)
    except (KeyError, ValidationError):
        raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None
    return records


def _delegation(connection: sqlite3.Connection, grant_id: str | None) -> Delegation | None:
    """An HNP purchase's grant and its event chain; a broken link or a body the trace cannot
    read makes the whole trace unavailable, as with hp_evidence."""
    if grant_id is None:
        return None
    try:
        grant = connection.execute(
            "SELECT expires_at, json_extract(scope_json, '$.scope.total_max_minor'), "
            "json_extract(scope_json, '$.scope.merchant_refs[0]') "
            "FROM delegation_grants WHERE grant_id = ?",
            (grant_id,),
        ).fetchone()
        events = read_events(connection, grant_id) if grant is not None else ()
    except (sqlite3.Error, ValueError) as error:
        if transient(error):
            raise
        raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None
    if grant is None or not delegation_chain_is_valid(grant_id, events):
        raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
    try:
        for event in events:
            if event.event_type in DELEGATION_BODIES:
                DELEGATION_BODIES[event.event_type].validate_python(dict(event.body))
    except ValidationError:
        raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None
    return Delegation(grant_id, grant[1], grant[0], grant[2], events)


def _checkout_prepared_at(connection: sqlite3.Connection, run_id: str) -> str | None:
    """When the D1 enforce path prepared the checkout creation, right before calling the merchant.

    `prepared_at` is the time of the decision that allowed it (enforce.py), and the envelope's
    other fields never leave SQLite.
    """
    found = connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('d1_checkout_operations', 'd1_checkout_envelopes')"
    ).fetchone()
    if found[0] != 2:
        return None
    try:
        row = connection.execute(
            "SELECT json_extract(CAST(e.canonical_envelope AS TEXT), '$.prepared_at') "
            "FROM d1_checkout_envelopes e JOIN d1_checkout_operations o "
            "ON o.operation_id = e.operation_id WHERE o.run_id = ?",
            (run_id,),
        ).fetchone()
    except sqlite3.Error as error:
        if transient(error):
            raise
        raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None
    return row[0] if row is not None and isinstance(row[0], str) else None


def _policy(
    connection: sqlite3.Connection, run_id: str, decisions: tuple[DecisionRecord, ...]
) -> D1PolicyArtifact | None:
    """The run's policy artifact; one the decisions were not bound to fails verification."""
    if not decisions:
        return None
    row = connection.execute(
        "SELECT policy_bytes FROM governance_episodes WHERE run_id = ?", (run_id,)
    ).fetchone()
    try:
        return verify_artifact(row[0] if row else None, decisions[-1].decision.policy_binding)
    except GovernanceError:
        raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None


def _turn(checkpoints: Checkpoints | None, run_id: str) -> TurnSnapshot | None:
    """Turn start and the checkpoint written right after `select_product` chose a variant."""
    if checkpoints is None:
        return None
    turn = sorted(
        (
            (datetime.fromisoformat(ts), values)
            for ts, values in checkpoints
            if isinstance(values.get("turn_id"), str) and turn_run_id(values["turn_id"]) == run_id
        ),
        key=lambda item: item[0],
    )
    if not turn:
        return None
    # One thread per conversation: a turn's first checkpoints still carry the previous turn's
    # selection until the graph adopts the new turn as active.
    chosen = next(
        (
            (at, values)
            for at, values in turn
            if values.get("selected_variant_id")
            and values.get("active_turn_id") == values["turn_id"]
        ),
        None,
    )
    candidate = None
    if chosen is not None:
        candidate = next(
            (
                item["candidate"]
                for item in chosen[1].get("ranked_candidates", [])
                if item["candidate"]["variant_id"] == chosen[1]["selected_variant_id"]
            ),
            None,
        )
    return TurnSnapshot(
        started_at=turn[0][0].isoformat(),
        selected_at=chosen[0].isoformat() if chosen else None,
        candidate=candidate,
    )
