"""The core of the Merchant control worker: its guarded connection and base operations."""

import hashlib
import importlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

# Every connection must reach the disposable cluster this run created, by its unique name.
CLUSTER_NAME = os.environ["LIVE_STACK_CLUSTER"]
EFFECT_TABLES = (
    "checkout_sessions",
    "checkout_mutations",
    "orders",
    "checkout_completion_attempts",
    "guarded_checkout_creations",
    "rejected_guarded_checkout_attempts",
)


def module(name: str) -> Any:
    # Dynamic imports keep the Agent's type checker away from the Merchant's packages.
    return importlib.import_module(name)


def bootstrap() -> Any:
    settings = module("commerce_lab.settings")
    settings.Settings.model_config["env_file"] = None
    settings.get_settings.cache_clear()
    pg = module("psycopg")
    real_connect = pg.connect
    dsn = os.environ["DATABASE_URL"]
    expected = pg.conninfo.conninfo_to_dict(dsn)

    def checked(conninfo="", **kwargs):
        if conninfo != dsn:
            raise RuntimeError("LIVE_STACK_DSN_REJECTED")
        connection = real_connect(conninfo, **kwargs)
        try:
            row = connection.execute(
                "SELECT current_database(),current_user,current_setting('cluster_name')"
            ).fetchone()
            if tuple(row) != ("tesis_lab", expected["user"], CLUSTER_NAME):
                raise RuntimeError("LIVE_STACK_CLUSTER_REJECTED")
            connection.rollback()
        except BaseException:
            connection.close()
            raise
        return connection

    pg.connect = checked
    return pg


class BaseControl:
    """Provisioning, sessions, snapshots and effect counts; every operation shares these."""

    def __init__(self, pg: Any):
        self.pg = pg
        self.db = module("commerce_lab.db")
        self.context = module("commerce_lab.context")
        self.acp = module("commerce_lab.acp")
        self.checkout = module("commerce_lab.checkout")
        self.contracts = module("commerce_lab.contracts")
        self.sql = module("psycopg.sql")
        self.jsonb = module("psycopg.types.json").Jsonb
        self.pending: dict[str, Any] = {}
        self.ct19_originals: dict[tuple[str, str], dict[str, Any]] = {}

    def connection(self):
        return self.pg.connect(self.db.database_url())

    def first_offer(self, run_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT snapshot FROM catalog_offers WHERE run_id=%s ORDER BY offer_id LIMIT 1",
                (run_id,),
            ).fetchone()[0]

    def issue(self, run_id: str, actor_id: str) -> dict[str, Any]:
        token = self.context.issue_lab_session(run_id, actor_id)
        resolved = self.context.authenticate_lab_session(token)
        assert (resolved.run_id, resolved.actor_id) == (run_id, actor_id)
        return {
            "bearer": token,
            "run_id": run_id,
            "actor_id": actor_id,
            "session_sha256": hashlib.sha256(token.encode()).hexdigest(),
        }

    def prepare(self, _: dict[str, Any]) -> dict[str, Any]:
        with self.connection() as connection:
            tables = connection.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
            ).fetchone()[0]
        assert tables == 0, "The live stack requires a new empty database"
        first, second = self.db.migrate(), self.db.migrate()
        assert second["status"] == "already_applied"
        return {"first": first, "second": second}

    def identity(self, _: dict[str, Any]) -> dict[str, Any]:
        metadata = module("importlib.metadata")
        with self.connection() as connection:
            row = connection.execute(
                "SELECT current_database(),current_user,current_setting('cluster_name'),"
                "current_setting('server_version'),current_setting('data_directory')"
            ).fetchone()
        return {
            "database": row[0],
            "user": row[1],
            "cluster_name": row[2],
            "server_version": row[3],
            "data_directory": row[4],
            "python": sys.version.split()[0],
            **{
                name: metadata.version(name)
                for name in ("psycopg", "fastapi", "uvicorn", "httpx", "pydantic")
            },
        }

    def episode(self, command: dict[str, Any]) -> dict[str, Any]:
        run_id = str(self.db.seed(actor_id=command["actor"], variant="B2")["run_id"])
        return {"run_id": run_id, "actor_id": command["actor"], "offer": self.first_offer(run_id)}

    def session(self, command: dict[str, Any]) -> dict[str, Any]:
        return self.issue(command["run_id"], command["actor_id"])

    def purchase_episode(self, command: dict[str, Any]) -> dict[str, Any]:
        """A B0 episode, its session and its ACP feed, as a buyer agent receives them."""
        run_id = str(self.db.seed(actor_id=command["actor"], variant="B0")["run_id"])
        exported = module("commerce_lab.feed").export_feed(
            Path(command["feed_dir"]), run_id=run_id, base_url=os.environ["ACP_API_BASE_URL"]
        )
        return {**self.issue(run_id, command["actor"]), "products": exported["product_count"]}

    def dispatch_webhooks(self, command: dict[str, Any]) -> list[dict[str, Any]]:
        dispatcher = module("commerce_lab.webhook_delivery").WebhookDispatcher()
        return dispatcher.dispatch_due(limit=command["limit"])

    def order_update(self, command: dict[str, Any]) -> dict[str, Any]:
        """The Merchant moves an order forward (the `order-update` command of its CLI)."""
        context = self.contracts.ExecutionContext(
            run_id=command["run_id"],
            actor_id=command["actor_id"],
            request_id=str(uuid4()),
            received_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        result = self.checkout.CheckoutService(context).update_order_status(command["order_id"])
        assert result.ok is True
        return result.data.model_dump(mode="json")

    def session_state(self, command: dict[str, Any]) -> dict[str, Any]:
        # Halfway between issue and now: already past, yet after created_at (CHECK).
        column = {"expired": "expires_at", "revoked": "revoked_at"}[command["state"]]
        with self.connection() as connection:
            changed = connection.execute(
                self.sql.SQL(
                    "UPDATE lab_sessions SET {}=created_at+(now()-created_at)/2 "
                    "WHERE session_sha256=%s AND now()>created_at"
                ).format(self.sql.Identifier(column)),
                (command["session_sha256"],),
            ).rowcount
        assert changed == 1
        return {"state": command["state"]}

    def snapshot(self, _: dict[str, Any]) -> dict[str, Any]:
        result = {}
        with self.connection() as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            for (table,) in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
            ).fetchall():
                value, count = connection.execute(
                    self.sql.SQL(
                        "SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),"
                        "'[]'::jsonb),count(*) FROM {} t"
                    ).format(self.sql.Identifier(table))
                ).fetchone()
                body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                result[table] = {"count": count, "sha256": hashlib.sha256(body).hexdigest()}
        return result

    def effects(self, _: dict[str, Any]) -> dict[str, Any]:
        with self.connection() as connection:
            return {
                table: connection.execute(
                    self.sql.SQL("SELECT count(*) FROM {}").format(self.sql.Identifier(table))
                ).fetchone()[0]
                for table in EFFECT_TABLES
            }

    def rewrite_offer(self, run_id: str, offer_id: str, change) -> dict[str, Any]:
        with self.connection() as connection:
            value = connection.execute(
                "SELECT snapshot FROM catalog_offers WHERE run_id=%s AND offer_id=%s FOR UPDATE",
                (run_id, offer_id),
            ).fetchone()[0]
            change(value)
            module("commerce_lab.contracts").Offer.model_validate(value)
            count = connection.execute(
                "UPDATE catalog_offers SET snapshot=%s,revision=%s WHERE run_id=%s AND offer_id=%s",
                (self.jsonb(value), value["revision"], run_id, offer_id),
            ).rowcount
        assert count == 1
        return {"changed": count, "revision": value["revision"]}

    def observe_offer(self, command: dict[str, Any]) -> dict[str, Any]:
        """Read-only full snapshot through the isolated Merchant worker."""
        with self.connection() as connection:
            return connection.execute(
                "SELECT snapshot FROM catalog_offers WHERE run_id=%s AND offer_id=%s",
                (command["run_id"], command["offer_id"]),
            ).fetchone()[0]

    def close(self) -> None:
        for connection in self.pending.values():
            connection.rollback()
            connection.close()
