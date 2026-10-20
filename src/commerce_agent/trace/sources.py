"""Read-only access to what one purchase trace is built from (docs/design/trace-panel.md).

Queries name their columns: mandates, receipts, signatures and idempotency keys are only tested
for presence, never read.
"""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import SecretBytes

from commerce_agent.application.conversation import turn_run_id
from commerce_agent.config import Settings, validate_service_base_url
from commerce_agent.governance.evaluator import GovernanceError, verify_artifact
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.models import D1PolicyArtifact
from commerce_agent.governance.provenance import ProvenanceKeys, merchant_pseudonym
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore


@dataclass(frozen=True)
class TurnSnapshot:
    """The conversation turn that started the purchase, from its graph checkpoints."""

    started_at: str
    selected_at: str | None
    candidate: dict[str, Any] | None


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


def read_records(database: Path, graph_database: Path, run_id: str) -> PurchaseRecords | None:
    with closing(
        sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    ) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        purchase = _row(
            connection,
            "SELECT run_id, request_text, stage, checkout_id, order_id, created_at, updated_at "
            "FROM purchases WHERE run_id = ?",
            run_id,
        )
        if purchase is None:
            return None
        terms = _row(
            connection, "SELECT terms, updated_at FROM approval_terms WHERE run_id = ?", run_id
        )
        decisions = _decisions(connection, run_id)
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
            turn=_turn(graph_database, thread["thread_id"], run_id) if thread else None,
        )


def _row(connection: sqlite3.Connection, query: str, run_id: str) -> dict[str, Any] | None:
    cursor = connection.execute(query, (run_id,))
    row = cursor.fetchone()
    return (
        dict(zip((column[0] for column in cursor.description), row, strict=True)) if row else None
    )


def _decisions(connection: sqlite3.Connection, run_id: str) -> tuple[DecisionRecord, ...]:
    # Databases that never ran with governance have no ledger at all.
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'governance_evidence'"
    ).fetchone():
        return ()
    evidence = SqliteGovernanceEvidenceStore(connection).for_run(run_id)
    return tuple(item for item in evidence if isinstance(item, DecisionRecord))


def _policy(
    connection: sqlite3.Connection, run_id: str, decisions: tuple[DecisionRecord, ...]
) -> D1PolicyArtifact | None:
    """The run's policy artifact, only if it is the one the decisions were bound to."""
    if not decisions:
        return None
    row = connection.execute(
        "SELECT policy_bytes FROM governance_episodes WHERE run_id = ?", (run_id,)
    ).fetchone()
    try:
        return verify_artifact(row[0] if row else None, decisions[-1].decision.policy_binding)
    except GovernanceError:
        return None


def _turn(graph_database: Path, thread_id: str, run_id: str) -> TurnSnapshot | None:
    """Turn start and the checkpoint written right after `select_product` chose a variant."""
    if not graph_database.exists():
        return None
    try:
        uri = f"{graph_database.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, check_same_thread=False)) as connection:
            saver = SqliteSaver(connection, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
            config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
            checkpoints = [
                (item.checkpoint["ts"], item.checkpoint["channel_values"])
                for item in saver.list(config)
            ]
    except sqlite3.Error:
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
