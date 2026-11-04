"""Merchant fixture control for the live stack. Run ONLY with the Merchant's interpreter.

The harness starts it through `uv run --project $MERCHANT_REPO`; no Agent process imports a
Merchant package. JSON lines on stdin/stdout carry synthetic fixture credentials in memory
only. Failures report the exception type, never values, rows, DSNs or tokens.
"""

import hashlib
import importlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
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


class Control:
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

    def internal_create(self, command: dict[str, Any]) -> dict[str, Any]:
        """Real Merchant transaction, without (unrecoverable) or before (pending) its record."""
        ctx = self.context.authenticate_lab_session(command["bearer"])
        adapter = self.acp.ACPCheckoutAdapter(ctx)
        connection = self.connection()
        try:
            offer = adapter._offer(command["variant"], connection=connection)
            result = self.checkout.CheckoutService(ctx).prepare_in_transaction(
                {
                    "offer_id": offer.id,
                    "expected_offer_revision": offer.revision,
                    "quantity": 1,
                    "delivery_context": offer.delivery_context.model_dump(mode="json"),
                    "idempotency_key": command["key"],
                },
                connection=connection,
            )
            assert result.ok is True
            if command["pending"]:
                original = adapter._response(result.data, offer)
                fingerprint = self.acp.creation_fingerprint(command["body"])
                key_hash = hashlib.sha256(command["key"].encode()).hexdigest()
                adapter._save_creation(connection, key_hash, fingerprint, result.data.id, original)
                # Held open: other sessions must not see it until commit_pending.
                self.pending[command["handle"]] = connection
            else:
                connection.commit()
                connection.close()
            return {"checkout_id": result.data.id}
        except BaseException:
            connection.rollback()
            connection.close()
            raise

    def commit_pending(self, command: dict[str, Any]) -> dict[str, Any]:
        connection = self.pending.pop(command["handle"])
        connection.commit()
        connection.close()
        return {"committed": True}

    def cancel_fixture(self, command: dict[str, Any]) -> dict[str, Any]:
        ctx = self.context.authenticate_lab_session(command["bearer"])
        result = self.checkout.CheckoutService(ctx).cancel(
            {"checkout_id": command["checkout_id"], "idempotency_key": command["key"]}
        )
        assert result.ok is True
        return {"status": result.data.status}

    def alter_offer_fixture(self, command: dict[str, Any]) -> dict[str, Any]:
        def change(value: dict[str, Any]) -> None:
            value["revision"] += 1
            value["name"] = "Changed after original creation"
            value["availability"] = "out_of_stock"
            value["available_quantity"] = 0
            value["pricing"]["items_total_minor"] += 100
            value["pricing"]["total_minor"] += 100

        return self.rewrite_offer(command["run_id"], command["variant"], change)

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

    def managed_catalog(self, _: dict[str, Any]) -> dict[str, Any]:
        """The published catalog P/Q observe, plus a same-IDs episode owned by another run."""
        catalog = module("commerce_lab.managed_catalog")
        catalog.import_current_catalog()
        context = catalog.managed_catalog_context()
        assert context is not None
        run_id, actor_id = str(context[0]), context[1]
        other = str(self.db.seed(actor_id="live-other-catalog", variant="B2")["run_id"])
        offer = self.first_offer(run_id)
        _, products = module("commerce_lab.feed").current_feed(
            base_url=os.environ["ACP_API_BASE_URL"]
        )
        return {
            "run_id": run_id,
            "actor_id": actor_id,
            "merchant_id": offer["merchant_id"],
            "offer": offer,
            "products": len(products),
            "credential": self.issue(run_id, actor_id),
            "other_credential": self.issue(other, "live-other-catalog"),
        }

    def edit_offer(self, command: dict[str, Any]) -> dict[str, Any]:
        """07B-B Merchant-side changes between the Agent's observation and its guarded POST."""
        kind = command["change"]

        def change(value: dict[str, Any]) -> None:
            if kind == "restore":
                value.clear()
                value.update(command["snapshot"])
            if kind == "revision":
                value["revision"] += 1
            if kind == "digest":
                value["name"] += " changed"
            if kind == "shipping":
                value["pricing"]["shipping_total_minor"] += 1
                value["pricing"]["total_minor"] += 1
            if kind == "expiry":
                observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
                value["expires_at"] = (observed + timedelta(seconds=1)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )

        return self.rewrite_offer(command["run_id"], command["offer_id"], change)

    def reprice_offer(self, command: dict[str, Any]) -> dict[str, Any]:
        """HN03 (hnp-scenarios.json): the item price changes after the consent, in a new revision,
        and the episode's feed is exported again, so a restarted Agent reads it."""

        def change(value: dict[str, Any]) -> None:
            pricing = value["pricing"]
            value["revision"] += 1
            pricing["items_total_minor"] = command["price_minor"]
            pricing["total_minor"] = command["price_minor"] + pricing["shipping_total_minor"]

        changed = self.rewrite_offer(command["run_id"], command["variant"], change)
        exported = module("commerce_lab.feed").export_feed(
            Path(command["feed_dir"]),
            run_id=command["run_id"],
            base_url=os.environ["ACP_API_BASE_URL"],
        )
        return {**changed, "products": exported["product_count"]}

    def projection(self, command: dict[str, Any]) -> dict[str, Any]:
        contracts = module("commerce_lab.contracts")
        feed = module("commerce_lab.feed")
        guarded = module("commerce_lab.contracts.guarded_creation")
        first = contracts.Offer.model_validate(command["offer"])
        changed = first.model_dump(mode="json")
        changed["revision"] += 1
        changed["pricing"]["shipping_total_minor"] += 1
        changed["pricing"]["total_minor"] += 1
        second = contracts.Offer.model_validate(changed)
        return {
            "same_feed": feed.build_feed([first]) == feed.build_feed([second]),
            "first_digest": guarded.offer_snapshot_sha256(first),
            "second_digest": guarded.offer_snapshot_sha256(second),
        }

    def p0_provenance(self, command: dict[str, Any]) -> dict[str, Any]:
        """Read the pinned Merchant's own generator, managed catalog and episode export."""
        fixtures = module("commerce_lab.fixtures.p0")
        root = Path(fixtures.__file__).resolve().parents[3]
        catalog = module("commerce_lab.managed_catalog")
        catalog.import_current_catalog()
        managed_run, _ = catalog.managed_catalog_context()
        feed = module("commerce_lab.feed")
        metadata, products = feed.current_feed(base_url=os.environ["ACP_API_BASE_URL"])
        with self.connection() as connection:
            offers = connection.execute(
                "SELECT snapshot FROM catalog_offers WHERE run_id=%s ORDER BY offer_id",
                (command["run_id"],),
            ).fetchall()
            managed = connection.execute(
                "SELECT snapshot FROM catalog_offers WHERE run_id=%s ORDER BY offer_id",
                (managed_run,),
            ).fetchall()
        sources = {}
        for name in ("catalog_snapshot.json", "p0.py", "generated.py", "specs.py", "__init__.py"):
            relative = "src/commerce_lab/fixtures/" + name
            sources[relative] = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in ("src/commerce_lab/managed_catalog.py", "src/commerce_lab/feed.py"):
            sources[relative] = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        snapshot = json.loads(
            (root / "src/commerce_lab/fixtures/catalog_snapshot.json").read_text(encoding="utf-8")
        )
        fixture_offers, fixture_hash, _ = module("commerce_lab.db.core").fixture_snapshot()
        exported = feed.export_feed(
            Path(command["feed_dir"]),
            run_id=command["run_id"],
            base_url=os.environ["ACP_API_BASE_URL"],
        )
        return {
            "sources_sha256": sources,
            "snapshot_products": len(snapshot["products"]),
            "fixture_sha256": fixture_hash,
            "episode_matches_fixture": [row[0] for row in offers] == fixture_offers,
            "managed_run_id": str(managed_run),
            "managed_offers": [row[0] for row in managed],
            "episode_offers": [row[0] for row in offers],
            "metadata": metadata,
            "products": products,
            "exported": exported,
        }

    def p0_managed_update(self, command: dict[str, Any]) -> dict[str, Any]:
        """Existing production catalog operation, guarded by this worker's disposable DSN."""
        updated = module("commerce_lab.managed_catalog").update_offer(
            command["offer_id"], price_minor=command.get("price_minor"), stock=command.get("stock")
        )
        return updated.model_dump(mode="json")

    def p0_episode_state(self, command: dict[str, Any]) -> dict[str, Any]:
        with self.connection() as connection:
            offers = connection.execute(
                "SELECT snapshot FROM catalog_offers WHERE run_id=%s ORDER BY offer_id",
                (command["run_id"],),
            ).fetchall()
            checkouts = connection.execute(
                "SELECT checkout_id,status,offer_id FROM checkout_sessions WHERE run_id=%s "
                "ORDER BY checkout_id",
                (command["run_id"],),
            ).fetchall()
            orders = connection.execute(
                "SELECT count(*) FROM orders WHERE run_id=%s", (command["run_id"],)
            ).fetchone()[0]
        return {
            "offers": [row[0] for row in offers],
            "checkouts": [
                {"id": str(row[0]), "status": row[1], "offer_id": row[2]} for row in checkouts
            ],
            "orders": orders,
        }

    def close(self) -> None:
        for connection in self.pending.values():
            connection.rollback()
            connection.close()

    def p3_state(self, command: dict[str, Any]) -> dict[str, Any]:
        """Public snapshots and table hashes, including events; never credentials or DB bytes."""
        run = command["run_id"]
        with self.connection() as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            clock, actor = connection.execute(
                "SELECT clock_at,actor_id FROM experiment_runs WHERE run_id=%s", (run,)
            ).fetchone()
            offers = connection.execute(
                "SELECT snapshot FROM catalog_offers WHERE run_id=%s ORDER BY offer_id", (run,)
            ).fetchall()
            checkouts = connection.execute(
                "SELECT snapshot FROM checkout_sessions WHERE run_id=%s ORDER BY checkout_id",
                (run,),
            ).fetchall()
            events = connection.execute(
                "SELECT event_type,scenario_at,payload FROM run_events "
                "WHERE run_id=%s ORDER BY event_id",
                (run,),
            ).fetchall()
            tables = {}
            global_effects = {}
            for (name,) in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
            ).fetchall():
                has_run = connection.execute(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                    "AND table_name=%s AND column_name='run_id'",
                    (name,),
                ).fetchone()
                query = self.sql.SQL(
                    "SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),"
                    "'[]'::jsonb) "
                    "FROM {} t"
                ).format(self.sql.Identifier(name))
                if has_run:
                    query += self.sql.SQL(" WHERE run_id=%s")
                rows = connection.execute(query, (run,) if has_run else ()).fetchone()[0]
                tables[name] = {
                    "count": len(rows),
                    "sha256": hashlib.sha256(
                        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "scope": "run" if has_run else "global",
                }
                if name not in {"catalog_offers", "run_events", "experiment_runs"}:
                    # Detect writes outside C/V too, without publishing rows or credentials.
                    global_effects[name] = connection.execute(
                        self.sql.SQL("SELECT count(*) FROM {}").format(self.sql.Identifier(name))
                    ).fetchone()[0]
            global_checkouts = connection.execute(
                "SELECT run_id,actor_id,checkout_id,status FROM checkout_sessions "
                "ORDER BY run_id,checkout_id"
            ).fetchall()
        return {
            "run_id": run,
            "actor_id": actor,
            "matches_full_fixture": [r[0] for r in offers] == self.db.fixture_snapshot()[0],
            "clock_at": clock.isoformat(),
            "offers": [r[0] for r in offers],
            "checkouts": [r[0] for r in checkouts],
            "events": [{"type": r[0], "at": r[1].isoformat(), "payload": r[2]} for r in events],
            "tables": tables,
            "global_effects": global_effects,
            "global_checkouts": [
                {"run_id": str(r[0]), "actor_id": r[1], "id": r[2], "status": r[3]}
                for r in global_checkouts
            ],
        }

    def p3_mutate(self, command: dict[str, Any]) -> dict[str, Any]:
        """Declared negative stimulus in one disposable run; no production Merchant change."""
        kind = command["kind"]
        if kind == "clock":
            with self.connection() as connection:
                raw = connection.execute(
                    "SELECT snapshot FROM catalog_offers WHERE run_id=%s AND offer_id=%s",
                    (command["run_id"], command["offer_id"]),
                ).fetchone()[0]
                at = datetime.fromisoformat(raw["expires_at"].replace("Z", "+00:00"))
                at += timedelta(seconds=command["offset"])
                connection.execute(
                    "UPDATE experiment_runs SET clock_at=%s WHERE run_id=%s",
                    (at, command["run_id"]),
                )
            return {"clock_at": at.isoformat()}

        def change(value):
            if kind == "revision":
                value["revision"] += 1
            elif kind in {"shipping", "price"}:
                field = "shipping_total_minor" if kind == "shipping" else "items_total_minor"
                value["pricing"][field] += 1
                value["pricing"]["total_minor"] += 1
                # Shipping deliberately preserves revision: digest must detect this stimulus.
                value["revision"] += int(kind == "price")
            elif kind == "stock":
                value.update(
                    availability="out_of_stock",
                    available_quantity=0,
                    revision=value["revision"] + 1,
                )
            elif kind == "inactive":
                value.update(product_status="inactive", revision=value["revision"] + 1)
            else:
                raise ValueError("Unknown P3 stimulus")

        result = self.rewrite_offer(command["run_id"], command["offer_id"], change)
        if command.get("feed_dir"):
            module("commerce_lab.feed").export_feed(
                Path(command["feed_dir"]),
                run_id=command["run_id"],
                base_url=os.environ["ACP_API_BASE_URL"],
            )
        return result


def main() -> None:
    control = Control(bootstrap())
    operations = {
        name: getattr(control, name)
        for name in (
            "prepare",
            "identity",
            "episode",
            "session",
            "purchase_episode",
            "dispatch_webhooks",
            "order_update",
            "session_state",
            "snapshot",
            "effects",
            "internal_create",
            "commit_pending",
            "cancel_fixture",
            "alter_offer_fixture",
            "managed_catalog",
            "edit_offer",
            "reprice_offer",
            "projection",
            "p0_provenance",
            "p0_managed_update",
            "p0_episode_state",
            "p3_state",
            "p3_mutate",
        )
    }
    try:
        for line in sys.stdin:
            command = json.loads(line)
            if command["operation"] == "shutdown":
                print(json.dumps({"ok": True, "data": None}), flush=True)
                return
            try:
                data = operations[command["operation"]](command)
                print(json.dumps({"ok": True, "data": data}), flush=True)
            except Exception as error:
                # Never reflect tokens, requests, DSNs, rows or exception text.
                print(json.dumps({"ok": False, "error_type": type(error).__name__}), flush=True)
    finally:
        control.close()


if __name__ == "__main__":
    main()
