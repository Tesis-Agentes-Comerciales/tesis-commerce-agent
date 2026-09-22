import json
import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    run_id: str
    checkout_id: str
    checkout_fingerprint: str
    actor_id: str
    approved_at: str
    signature: str
    consumed_at: str | None


class SqliteAgentStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS purchases (
                    run_id TEXT PRIMARY KEY,
                    request_text TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    selected_variant_id TEXT,
                    checkout_id TEXT UNIQUE,
                    order_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operation_keys (
                    run_id TEXT NOT NULL REFERENCES purchases(run_id) ON DELETE CASCADE,
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, operation)
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    run_id TEXT PRIMARY KEY REFERENCES purchases(run_id) ON DELETE CASCADE,
                    checkout_id TEXT NOT NULL,
                    checkout_fingerprint TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    consumed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    checkout_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS completion_results (
                    run_id TEXT PRIMARY KEY REFERENCES purchases(run_id) ON DELETE CASCADE,
                    checkout_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_turns (
                    turn_id TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    response TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS approval_terms (
                    run_id TEXT PRIMARY KEY REFERENCES purchases(run_id) ON DELETE CASCADE,
                    terms TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._initialize_webhook_events(connection)

    def create_purchase(self, run_id: str, request_text: str) -> None:
        now = _timestamp()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO purchases
                  (run_id, request_text, stage, created_at, updated_at)
                VALUES (?, ?, 'received', ?, ?)
                ON CONFLICT (run_id) DO NOTHING
                """,
                (run_id, request_text, now, now),
            )
            row = connection.execute(
                "SELECT request_text FROM purchases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row[0] != request_text:
                raise ValueError("Purchase run conflicts with another request")

    def claim_conversation_turn(self, turn_id: str, request_sha256: str) -> dict[str, Any] | None:
        now = _timestamp()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_turns
                  (turn_id, request_sha256, response, created_at, updated_at)
                VALUES (?, ?, NULL, ?, ?)
                ON CONFLICT (turn_id) DO NOTHING
                """,
                (turn_id, request_sha256, now, now),
            )
            row = connection.execute(
                "SELECT request_sha256, response FROM conversation_turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        if row is None or row["request_sha256"] != request_sha256:
            raise ValueError("Conversation turn conflicts with another request")
        return json.loads(row["response"]) if row["response"] is not None else None

    def save_conversation_turn(
        self, turn_id: str, request_sha256: str, response: dict[str, Any]
    ) -> None:
        serialized = json.dumps(response, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE conversation_turns
                SET response = ?, updated_at = ?
                WHERE turn_id = ? AND request_sha256 = ?
                """,
                (serialized, _timestamp(), turn_id, request_sha256),
            )
            if cursor.rowcount != 1:
                raise ValueError("Conversation turn was not claimed")

    def update_purchase(
        self,
        run_id: str,
        *,
        stage: str,
        selected_variant_id: str | None = None,
        checkout_id: str | None = None,
        order_id: str | None = None,
    ) -> None:
        assignments = ["stage = ?", "updated_at = ?"]
        values: list[object] = [stage, _timestamp()]
        for column, value in (
            ("selected_variant_id", selected_variant_id),
            ("checkout_id", checkout_id),
            ("order_id", order_id),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        values.append(run_id)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE purchases SET {', '.join(assignments)} WHERE run_id = ?", values
            )
            if cursor.rowcount != 1:
                raise KeyError("Purchase run does not exist")

    def pending_purchase(self, stages: Sequence[str]) -> dict[str, str | None] | None:
        """Oldest purchase still in one of ``stages``, without asking the client for a run ID."""
        if not stages:
            return None
        placeholders = ", ".join("?" for _ in stages)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT run_id, stage, order_id
                FROM purchases WHERE stage IN ({placeholders})
                ORDER BY created_at, run_id LIMIT 1
                """,
                tuple(stages),
            ).fetchone()
        return dict(row) if row is not None else None

    def save_approval_terms(self, run_id: str, terms: dict[str, Any]) -> None:
        """Keep the terms shown for a run, so an echoed approval card can be rebuilt server side."""
        serialized = json.dumps(terms, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approval_terms (run_id, terms, updated_at) VALUES (?, ?, ?)
                ON CONFLICT (run_id) DO UPDATE SET terms = excluded.terms,
                                                   updated_at = excluded.updated_at
                """,
                (run_id, serialized, _timestamp()),
            )

    def approval_terms(self, run_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not run_ids:
            return {}
        placeholders = ", ".join("?" for _ in run_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT run_id, terms FROM approval_terms WHERE run_id IN ({placeholders})",
                tuple(run_ids),
            ).fetchall()
        return {row["run_id"]: json.loads(row["terms"]) for row in rows}

    def purchase(self, run_id: str) -> dict[str, str | None] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, request_text, stage, selected_variant_id, checkout_id,
                       order_id, created_at, updated_at
                FROM purchases WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def claim_completion(self, run_id: str) -> bool:
        """Atomically reserve the only outbound completion attempt for a purchase."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE purchases
                SET stage = 'completing', updated_at = ?
                WHERE run_id = ? AND stage IN ('approved', 'uncertain')
                """,
                (_timestamp(), run_id),
            )
        return cursor.rowcount == 1

    def release_completion(self, run_id: str, *, uncertain: bool) -> None:
        stage = "uncertain" if uncertain else "approved"
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE purchases SET stage = ?, updated_at = ?
                WHERE run_id = ? AND stage = 'completing'
                """,
                (stage, _timestamp(), run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Purchase completion reservation is no longer active")

    def idempotency_key(self, run_id: str, operation: str) -> str:
        now = _timestamp()
        candidate = f"agent_{secrets.token_urlsafe(24)}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operation_keys (run_id, operation, idempotency_key, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (run_id, operation) DO NOTHING
                """,
                (run_id, operation, candidate, now),
            )
            row = connection.execute(
                "SELECT idempotency_key FROM operation_keys WHERE run_id = ? AND operation = ?",
                (run_id, operation),
            ).fetchone()
        if row is None:
            raise RuntimeError("Idempotency key could not be persisted")
        return str(row[0])

    def save_approval(self, record: ApprovalRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approvals
                  (run_id, checkout_id, checkout_fingerprint, actor_id,
                   approved_at, signature, consumed_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT (run_id) DO UPDATE SET
                  checkout_id = excluded.checkout_id,
                  checkout_fingerprint = excluded.checkout_fingerprint,
                  actor_id = excluded.actor_id,
                  approved_at = excluded.approved_at,
                  signature = excluded.signature,
                  consumed_at = NULL
                """,
                (
                    record.run_id,
                    record.checkout_id,
                    record.checkout_fingerprint,
                    record.actor_id,
                    record.approved_at,
                    record.signature,
                ),
            )

    def approval(self, run_id: str) -> ApprovalRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, checkout_id, checkout_fingerprint, actor_id,
                       approved_at, signature, consumed_at
                FROM approvals WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return ApprovalRecord(**dict(row)) if row is not None else None

    def record_completion(self, run_id: str, snapshot: dict[str, Any]) -> None:
        checkout_id = snapshot.get("id")
        order = snapshot.get("order")
        if not isinstance(checkout_id, str) or not isinstance(order, dict):
            raise ValueError("Completion snapshot is missing checkout or order")
        order_id = order.get("id")
        order_checkout_id = order.get("checkout_session_id")
        status = order.get("status")
        if (
            not isinstance(order_id, str)
            or order_checkout_id != checkout_id
            or (status is not None and not isinstance(status, str))
        ):
            raise ValueError("Completion order does not match checkout")
        serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        order_serialized = json.dumps(order, sort_keys=True, separators=(",", ":"))
        now = _timestamp()
        with self._connect() as connection:
            purchase = connection.execute(
                "SELECT checkout_id, order_id, stage FROM purchases WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if purchase is None:
                raise KeyError("Purchase run does not exist")
            existing = connection.execute(
                "SELECT snapshot FROM completion_results WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is not None:
                if existing[0] != serialized:
                    raise RuntimeError("Purchase already has a different completion result")
                return
            if purchase["checkout_id"] != checkout_id:
                raise RuntimeError("Completion checkout does not match purchase")
            if purchase["stage"] != "completing":
                raise RuntimeError("Purchase was not reserved for completion")
            approval = connection.execute(
                "SELECT consumed_at FROM approvals WHERE run_id = ?", (run_id,)
            ).fetchone()
            if approval is None or approval["consumed_at"] is not None:
                raise RuntimeError("Active approval is required to record completion")
            _project_order(
                connection,
                order_id=order_id,
                checkout_id=checkout_id,
                status=status or "created",
                snapshot=order_serialized,
                updated_at=now,
            )
            connection.execute(
                """
                INSERT INTO completion_results
                  (run_id, checkout_id, order_id, snapshot, completed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, checkout_id, order_id, serialized, now),
            )
            connection.execute(
                """
                UPDATE purchases
                SET stage = 'completed', order_id = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (order_id, now, run_id),
            )
            connection.execute(
                "UPDATE approvals SET consumed_at = ? WHERE run_id = ? AND consumed_at IS NULL",
                (now, run_id),
            )

    def completion_result(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT snapshot FROM completion_results WHERE run_id = ?", (run_id,)
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def record_webhook(
        self,
        *,
        request_id: str | None,
        event_type: str,
        order_id: str,
        payload_sha256: str,
        payload: dict[str, Any],
    ) -> bool:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        now = _timestamp()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO webhook_events
                  (payload_sha256, request_id, event_type, order_id, payload, received_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (payload_sha256) DO NOTHING
                """,
                (payload_sha256, request_id, event_type, order_id, serialized, now),
            )
            if cursor.rowcount == 0:
                return False
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError("Webhook data must be an object")
            checkout_id = data.get("checkout_session_id")
            status = data.get("status")
            if (
                payload.get("type") != event_type
                or data.get("id") != order_id
                or not isinstance(checkout_id, str)
                or not isinstance(status, str)
            ):
                raise ValueError("Webhook order is missing required identifiers")
            known = connection.execute(
                """
                SELECT 1 FROM purchases WHERE checkout_id = ?
                UNION ALL
                SELECT 1 FROM orders WHERE order_id = ? AND checkout_id = ?
                LIMIT 1
                """,
                (checkout_id, order_id, checkout_id),
            ).fetchone()
            if known is None:
                return True
            _project_order(
                connection,
                order_id=order_id,
                checkout_id=checkout_id,
                status=status,
                snapshot=serialized,
                updated_at=now,
            )
        return True

    def order(self, order_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT snapshot FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _initialize_webhook_events(connection: sqlite3.Connection) -> None:
        columns = connection.execute("PRAGMA table_info(webhook_events)").fetchall()
        legacy_request_id_primary_key = any(
            row[1] == "request_id" and row[5] == 1 for row in columns
        )
        if legacy_request_id_primary_key:
            connection.executescript(
                """
                ALTER TABLE webhook_events RENAME TO webhook_events_legacy;
                CREATE TABLE webhook_events (
                    payload_sha256 TEXT PRIMARY KEY,
                    request_id TEXT,
                    event_type TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO webhook_events
                  (payload_sha256, request_id, event_type, order_id, payload, received_at)
                SELECT payload_sha256, request_id, event_type, order_id, payload, received_at
                FROM webhook_events_legacy;
                DROP TABLE webhook_events_legacy;
                """
            )
            return
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS webhook_events (
                payload_sha256 TEXT PRIMARY KEY,
                request_id TEXT,
                event_type TEXT NOT NULL,
                order_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
            """
        )


_ORDER_STATUS_RANK = {
    "created": 0,
    "confirmed": 1,
    "manual_review": 2,
    "processing": 3,
    "shipped": 4,
    "completed": 5,
    "canceled": 5,
}
_TERMINAL_ORDER_STATUSES = {"completed", "canceled"}


def _can_advance_order(current: str, incoming: str) -> bool:
    if current == incoming:
        return True
    if current in _TERMINAL_ORDER_STATUSES:
        return False
    current_rank = _ORDER_STATUS_RANK.get(current)
    incoming_rank = _ORDER_STATUS_RANK.get(incoming)
    if current_rank is None:
        return incoming_rank is None
    if incoming_rank is None:
        return False
    return incoming_rank >= current_rank


def _project_order(
    connection: sqlite3.Connection,
    *,
    order_id: str,
    checkout_id: str,
    status: str,
    snapshot: str,
    updated_at: str,
) -> None:
    existing = connection.execute(
        "SELECT checkout_id, status FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    if existing is not None:
        if existing["checkout_id"] != checkout_id:
            raise RuntimeError("Order ID is already associated with a different checkout")
        if not _can_advance_order(str(existing["status"]), status):
            return
    connection.execute(
        """
        INSERT INTO orders (order_id, checkout_id, status, snapshot, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (order_id) DO UPDATE SET
          status = excluded.status,
          snapshot = excluded.snapshot,
          updated_at = excluded.updated_at
        """,
        (order_id, checkout_id, status, snapshot, updated_at),
    )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
