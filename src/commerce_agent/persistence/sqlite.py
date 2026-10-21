import json
import re
import secrets
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

# Why a purchase failed, for its trace: a code, never a message or merchant text.
_FAILURE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def failure_code(value: object) -> str | None:
    """The value if it looks like an error code; anything else is not stored."""
    return value if isinstance(value, str) and _FAILURE_CODE.fullmatch(value) else None


@dataclass(frozen=True, slots=True)
class PurchaseCASResult:
    status: Literal["applied", "unchanged", "stale", "not_found"]
    state_version: int | None


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    run_id: str
    checkout_id: str
    checkout_fingerprint: str
    actor_id: str
    approved_at: str
    signature: str
    consumed_at: str | None
    checkout_jwt: str | None = None
    checkout_mandate: str | None = None
    payment_mandate: str | None = None
    # The D2 decision that authorized this approval (governance_evidence); None without D2.
    authorization_evidence_id: str | None = None


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
                    owner_id TEXT NOT NULL DEFAULT 'local-human',
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
                    consumed_at TEXT,
                    checkout_jwt TEXT,
                    checkout_mandate TEXT,
                    payment_mandate TEXT
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
                    completed_at TEXT NOT NULL,
                    checkout_receipt_jwt TEXT,
                    payment_receipt_jwt TEXT
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

                CREATE TABLE IF NOT EXISTS purchase_graph_threads (
                    run_id TEXT PRIMARY KEY REFERENCES purchases(run_id) ON DELETE CASCADE,
                    thread_id TEXT NOT NULL
                );
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(purchases)")}
            if "owner_id" not in columns:
                connection.execute(
                    "ALTER TABLE purchases ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local-human'"
                )
            # Additive and nullable: existing rows keep NULL, read as the generic failure.
            # Bookkeeping like updated_at, so the revision triggers do not cover it.
            if "failure_code" not in columns:
                connection.execute("ALTER TABLE purchases ADD COLUMN failure_code TEXT")
            approval_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(approvals)")
            }
            for column in (
                "checkout_jwt",
                "checkout_mandate",
                "payment_mandate",
                "authorization_evidence_id",
            ):
                if column not in approval_columns:
                    connection.execute(f"ALTER TABLE approvals ADD COLUMN {column} TEXT")
            completion_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(completion_results)")
            }
            for column in ("checkout_receipt_jwt", "payment_receipt_jwt"):
                if column not in completion_columns:
                    connection.execute(f"ALTER TABLE completion_results ADD COLUMN {column} TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS purchases_owner_stage ON purchases(owner_id, stage)"
            )
            self._initialize_webhook_events(connection)
            self._initialize_purchase_revision(connection)

    @staticmethod
    def _initialize_purchase_revision(connection: sqlite3.Connection) -> None:
        # This migration runs after legacy executescript migrations, which may commit.
        # Keep the additive column and its guards in one explicit transaction.
        connection.execute("BEGIN IMMEDIATE")
        columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(purchases)")}
        if "state_version" not in columns:
            connection.execute(
                "ALTER TABLE purchases ADD COLUMN state_version INTEGER NOT NULL DEFAULT 1 "
                "CHECK(typeof(state_version) = 'integer' AND state_version >= 1)"
            )
        else:
            column = columns["state_version"]
            if (column["type"].upper(), column["notnull"], column["dflt_value"]) != (
                "INTEGER",
                1,
                "1",
            ):
                raise RuntimeError("Incompatible purchase revision schema")
        if connection.execute(
            "SELECT 1 FROM purchases WHERE typeof(state_version) != 'integer' "
            "OR state_version < 1 LIMIT 1"
        ).fetchone():
            raise RuntimeError("Invalid purchase revision")
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS purchases_revision_insert
            BEFORE INSERT ON purchases WHEN NEW.state_version IS NOT 1
            BEGIN SELECT RAISE(ABORT, 'purchase_revision_initial'); END"""
        )
        # Fail closed for every SQL writer, including connections outside this store.
        # updated_at is bookkeeping; created_at and every business field are covered.
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS purchases_revision_update
            BEFORE UPDATE ON purchases
            WHEN typeof(NEW.state_version) != 'integer'
              OR NEW.state_version != OLD.state_version + CASE WHEN
                   NEW.run_id IS NOT OLD.run_id
                OR NEW.owner_id IS NOT OLD.owner_id
                OR NEW.request_text IS NOT OLD.request_text
                OR NEW.stage IS NOT OLD.stage
                OR NEW.selected_variant_id IS NOT OLD.selected_variant_id
                OR NEW.checkout_id IS NOT OLD.checkout_id
                OR NEW.order_id IS NOT OLD.order_id
                OR NEW.created_at IS NOT OLD.created_at
                THEN 1 ELSE 0 END
            BEGIN SELECT RAISE(ABORT, 'purchase_revision_mismatch'); END"""
        )

    def create_purchase(
        self, run_id: str, request_text: str, owner_id: str = "local-human"
    ) -> None:
        now = _timestamp()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO purchases
                  (run_id, owner_id, request_text, stage, created_at, updated_at)
                VALUES (?, ?, ?, 'received', ?, ?)
                ON CONFLICT (run_id) DO NOTHING
                """,
                (run_id, owner_id, request_text, now, now),
            )
            row = connection.execute(
                "SELECT owner_id, request_text FROM purchases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row[0] != owner_id or row[1] != request_text:
                raise ValueError("Purchase run conflicts with another request")

    def save_graph_thread(self, run_id: str, thread_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO purchase_graph_threads (run_id, thread_id) VALUES (?, ?) "
                "ON CONFLICT (run_id) DO NOTHING",
                (run_id, thread_id),
            )
            row = connection.execute(
                "SELECT thread_id FROM purchase_graph_threads WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row[0] != thread_id:
                raise ValueError("Purchase run belongs to another graph thread")

    def graph_thread(self, run_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT thread_id FROM purchase_graph_threads WHERE run_id = ?", (run_id,)
            ).fetchone()
        return str(row[0]) if row is not None else None

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
        failure: object = None,
    ) -> None:
        """`failure` is the error code of a `failed` stage, kept only if it looks like one."""
        assignments, values = self._purchase_update(
            stage, selected_variant_id, checkout_id, order_id
        )
        if (code := failure_code(failure)) is not None:
            assignments += ", failure_code = ?"
            values.append(code)
        values.append(run_id)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE purchases SET {assignments} WHERE run_id = ?", values
            )
            if cursor.rowcount != 1:
                raise KeyError("Purchase run does not exist")

    @staticmethod
    def _purchase_update(
        stage: str,
        selected_variant_id: str | None,
        checkout_id: str | None,
        order_id: str | None,
    ) -> tuple[str, list[object]]:
        fields: list[tuple[str, str]] = [("stage", stage)]
        for column, value in (
            ("selected_variant_id", selected_variant_id),
            ("checkout_id", checkout_id),
            ("order_id", order_id),
        ):
            if value is not None:
                fields.append((column, value))
        changed = " OR ".join(f"{column} IS NOT ?" for column, _ in fields)
        assignments = ", ".join(f"{column} = ?" for column, _ in fields)
        assignments += (
            f", state_version = state_version + CASE WHEN {changed} THEN 1 ELSE 0 END"
            ", updated_at = ?"
        )
        values: list[object] = [value for _, value in fields] * 2
        values.append(_timestamp())
        return assignments, values

    def purchase_state_version(self, run_id: str) -> int | None:
        """Read the persisted revision, without changing the legacy purchase projection."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT state_version FROM purchases WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row[0]) if row is not None else None

    def compare_and_set_purchase(
        self,
        run_id: str,
        expected_state_version: int,
        *,
        stage: str,
        selected_variant_id: str | None = None,
        checkout_id: str | None = None,
        order_id: str | None = None,
    ) -> PurchaseCASResult:
        """Local row CAS only; no authority, state-machine or external-effect validation.

        Owns its transaction. A future evidence UoW must share a connection instead
        of composing this independently committed operation with ledger writes.
        None retains optional fields, as in update_purchase.
        """
        if type(expected_state_version) is not int or not 1 <= expected_state_version < 2**63:
            raise ValueError("Expected purchase revision must be a positive SQLite integer")
        assignments, values = self._purchase_update(
            stage, selected_variant_id, checkout_id, order_id
        )
        values.extend((run_id, expected_state_version))
        with closing(self._connect()) as connection, connection:
            # Keep both the conditional write and result classification under the write lock.
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"UPDATE purchases SET {assignments} WHERE run_id = ? AND state_version = ?",
                values,
            )
            row = connection.execute(
                "SELECT state_version FROM purchases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return PurchaseCASResult("not_found", None)
            version = int(row[0])
            if cursor.rowcount == 0:
                return PurchaseCASResult("stale", version)
            return PurchaseCASResult(
                "unchanged" if version == expected_state_version else "applied", version
            )

    def pending_purchase(
        self, stages: Sequence[str], owner_id: str = "local-human"
    ) -> dict[str, str | None] | None:
        """Oldest purchase still in one of ``stages``, without asking the client for a run ID."""
        if not stages:
            return None
        placeholders = ", ".join("?" for _ in stages)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT run_id, stage, order_id
                FROM purchases WHERE owner_id = ? AND stage IN ({placeholders})
                ORDER BY created_at, run_id LIMIT 1
                """,
                (owner_id, *stages),
            ).fetchone()
        return dict(row) if row is not None else None

    def purchase_for_owner(self, run_id: str, owner_id: str) -> dict[str, str | None] | None:
        purchase = self.purchase(run_id)
        return purchase if purchase is not None and purchase["owner_id"] == owner_id else None

    def has_d1_operation(self, run_id: str) -> bool:
        """Read-only downgrade guard; ordinary off/shadow databases need no D1 schema."""
        with self._connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='d1_checkout_operations'"
                ).fetchone()
                is None
            ):
                return False
            return (
                connection.execute(
                    "SELECT 1 FROM d1_checkout_operations WHERE run_id=?", (run_id,)
                ).fetchone()
                is not None
            )

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
                SELECT run_id, owner_id, request_text, stage, selected_variant_id, checkout_id,
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
                SET stage = 'completing', updated_at = ?, state_version = state_version + 1
                WHERE run_id = ? AND stage IN ('approved', 'uncertain')
                """,
                (_timestamp(), run_id),
            )
        return cursor.rowcount == 1

    def release_completion(
        self,
        run_id: str,
        *,
        stage: Literal["approved", "uncertain", "failed"],
        failure: object = None,
    ) -> None:
        code = failure_code(failure) if stage == "failed" else None
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE purchases SET stage = ?, updated_at = ?, state_version = state_version + 1,
                    failure_code = COALESCE(?, failure_code)
                WHERE run_id = ? AND stage = 'completing'
                """,
                (stage, _timestamp(), code, run_id),
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
            write_approval(connection, record)

    def approval(self, run_id: str) -> ApprovalRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, checkout_id, checkout_fingerprint, actor_id,
                       approved_at, signature, consumed_at, checkout_jwt,
                       checkout_mandate, payment_mandate, authorization_evidence_id
                FROM approvals WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return ApprovalRecord(**dict(row)) if row is not None else None

    def record_completion(
        self,
        run_id: str,
        snapshot: dict[str, Any],
        *,
        checkout_receipt_jwt: str | None = None,
        payment_receipt_jwt: str | None = None,
    ) -> None:
        if (checkout_receipt_jwt is None) != (payment_receipt_jwt is None):
            raise ValueError("AP2 completion requires both receipts")
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
                  (run_id, checkout_id, order_id, snapshot, completed_at,
                   checkout_receipt_jwt, payment_receipt_jwt)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    checkout_id,
                    order_id,
                    serialized,
                    now,
                    checkout_receipt_jwt,
                    payment_receipt_jwt,
                ),
            )
            connection.execute(
                """
                UPDATE purchases
                SET stage = 'completed', order_id = ?, updated_at = ?,
                    state_version = state_version + CASE
                        WHEN stage IS NOT 'completed' OR order_id IS NOT ? THEN 1 ELSE 0 END
                WHERE run_id = ?
                """,
                (order_id, now, order_id, run_id),
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


def write_approval(connection: sqlite3.Connection, record: ApprovalRecord) -> None:
    """Write the run's active approval on the caller's connection and transaction.

    D2 writes it in the same transaction as the purchase transition and its evidence.
    """
    connection.execute(
        """
        INSERT INTO approvals
          (run_id, checkout_id, checkout_fingerprint, actor_id,
           approved_at, signature, consumed_at, checkout_jwt,
           checkout_mandate, payment_mandate, authorization_evidence_id)
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
        ON CONFLICT (run_id) DO UPDATE SET
          checkout_id = excluded.checkout_id,
          checkout_fingerprint = excluded.checkout_fingerprint,
          actor_id = excluded.actor_id,
          approved_at = excluded.approved_at,
          signature = excluded.signature,
          consumed_at = NULL,
          checkout_jwt = excluded.checkout_jwt,
          checkout_mandate = excluded.checkout_mandate,
          payment_mandate = excluded.payment_mandate,
          authorization_evidence_id = excluded.authorization_evidence_id
        """,
        (
            record.run_id,
            record.checkout_id,
            record.checkout_fingerprint,
            record.actor_id,
            record.approved_at,
            record.signature,
            record.checkout_jwt,
            record.checkout_mandate,
            record.payment_mandate,
            record.authorization_evidence_id,
        ),
    )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
