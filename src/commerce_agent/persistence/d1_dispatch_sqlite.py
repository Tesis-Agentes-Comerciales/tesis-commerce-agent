"""Opt-in append-only D1 input identity and dispatch observations, caller transaction.

The original 06A reservation and its triggers are unchanged. A claimed operation
without a result is uncertain after restart; no method here authorizes resending.
"""

import sqlite3
from typing import Literal

from ..governance.evidence import EvidenceError

_INPUT = """CREATE TABLE IF NOT EXISTS d1_enforce_inputs (
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES purchases(run_id),
    canonical_input BLOB NOT NULL,
    context_ref BLOB NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    evidence_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE)"""
_RESULT = """CREATE TABLE IF NOT EXISTS d1_checkout_results (
    operation_id TEXT PRIMARY KEY NOT NULL REFERENCES d1_checkout_operations(operation_id),
    state TEXT NOT NULL CHECK(state IN ('confirmed','uncertain','definitive_failure')),
    checkout_id TEXT UNIQUE,
    code TEXT NOT NULL CHECK(code IN
        ('CHECKOUT_VERIFIED','DISPATCH_UNCERTAIN','CHECKOUT_INVALID','VERIFIED_NOT_CREATED')),
    CHECK((state='confirmed' AND checkout_id IS NOT NULL AND code='CHECKOUT_VERIFIED') OR
          (state='uncertain' AND checkout_id IS NULL AND code IN
              ('DISPATCH_UNCERTAIN','CHECKOUT_INVALID')) OR
          (state='definitive_failure' AND checkout_id IS NULL AND code='VERIFIED_NOT_CREATED')))"""


class SqliteD1DispatchStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def initialize(self) -> None:
        if not self.connection.in_transaction:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        self.connection.execute("SAVEPOINT d1_dispatch_schema")
        try:
            for statement in (_INPUT, _RESULT):
                table = statement.split()[5]
                self._ddl(statement, table)
                for action in ("UPDATE", "DELETE"):
                    name = f"{table}_no_{action.lower()}"
                    self._ddl(
                        f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'d1_dispatch_immutable'); END",
                        name,
                    )
                key = "run_id" if table == "d1_enforce_inputs" else "operation_id"
                name = f"{table}_no_replace"
                self._ddl(
                    f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE INSERT ON {table} "
                    f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {key}=NEW.{key}) "
                    "BEGIN SELECT RAISE(ABORT,'d1_dispatch_immutable'); END",
                    name,
                )
            self.connection.execute("RELEASE d1_dispatch_schema")
        except Exception:
            self.connection.execute("ROLLBACK TO d1_dispatch_schema")
            self.connection.execute("RELEASE d1_dispatch_schema")
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE") from None

    def _ddl(self, statement: str, name: str) -> None:
        self.connection.execute(statement)
        actual = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()

        def normalize(sql: str) -> str:
            return " ".join(sql.replace(" IF NOT EXISTS", "").split())

        if actual is None or normalize(actual[0]) != normalize(statement):
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")

    def observe(
        self,
        operation_id: str,
        *,
        state: Literal["confirmed", "uncertain"],
        checkout_id: str | None,
        code: Literal["CHECKOUT_VERIFIED", "DISPATCH_UNCERTAIN", "CHECKOUT_INVALID"],
    ) -> None:
        if not self.connection.in_transaction:
            raise EvidenceError("GOV_EVIDENCE_UNAVAILABLE")
        # No definitive_failure API: no remote proof of absence is available yet.
        row = self.connection.execute(
            "SELECT state FROM d1_checkout_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None or row[0] != "claimed":
            raise EvidenceError("GOV_STATE_CONFLICT")
        self.connection.execute(
            "INSERT INTO d1_checkout_results VALUES (?,?,?,?)",
            (operation_id, state, checkout_id, code),
        )

    def status(self, run_id: str) -> tuple[str, str | None, str] | None:
        row = self.connection.execute(
            "SELECT o.state,r.state,r.checkout_id,o.operation_id "
            "FROM d1_checkout_operations o LEFT JOIN d1_checkout_results r "
            "ON r.operation_id=o.operation_id WHERE o.run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return row[1] or ("uncertain" if row[0] == "claimed" else "reserved"), row[2], row[3]
