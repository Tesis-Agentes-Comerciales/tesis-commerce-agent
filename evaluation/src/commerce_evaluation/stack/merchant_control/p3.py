"""P3: public run state and the declared negative stimuli of one disposable run."""

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from commerce_evaluation.stack.merchant_control.core import BaseControl, module


class P3Control(BaseControl):
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
            "matches_full_fixture": [r[0] for r in offers]
            == module("commerce_lab.db.core").fixture_snapshot()[0],
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
