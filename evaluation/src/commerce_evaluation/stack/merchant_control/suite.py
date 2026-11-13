"""The evaluation suite's operations: the case's events on an offer and a trial's final state
(docs/design/eval-suite.md §4)."""

import os
from pathlib import Path
from typing import Any

from commerce_evaluation.stack.merchant_control.core import BaseControl, module


class SuiteControl(BaseControl):
    def suite_offer(self, command: dict[str, Any]) -> dict[str, Any]:
        """One offer event, in a new revision: `stock_out` sells the offer out. With `feed_dir`
        the episode's feed is exported again, so the Agent reads the change. `catalog_text` is
        not one: it changes the feed alone (suite/live/events.py)."""
        if command["kind"] != "stock_out":
            raise ValueError("Unknown offer event")

        def change(value: dict[str, Any]) -> None:
            value["revision"] += 1
            value["availability"] = "out_of_stock"
            value["available_quantity"] = 0

        changed = self.rewrite_offer(command["run_id"], command["variant"], change)
        if command.get("feed_dir") is None:
            return changed
        exported = module("commerce_lab.feed").export_feed(
            Path(command["feed_dir"]),
            run_id=command["run_id"],
            base_url=os.environ["ACP_API_BASE_URL"],
        )
        return {**changed, "products": exported["product_count"]}

    def suite_state(self, command: dict[str, Any]) -> dict[str, Any]:
        """The episode's checkouts and orders as the effects and outcome graders read them: each
        names its variant (the offer id), and an order its totals, shipping included or not."""
        with self.connection() as connection:
            checkouts = connection.execute(
                "SELECT checkout_id,status,offer_id FROM checkout_sessions WHERE run_id=%s "
                "ORDER BY created_at,checkout_id",
                (command["run_id"],),
            ).fetchall()
            orders = connection.execute(
                "SELECT order_id,checkout_id,snapshot FROM orders WHERE run_id=%s "
                "ORDER BY created_at,order_id",
                (command["run_id"],),
            ).fetchall()
        return {
            "checkouts": [
                {"id": str(row[0]), "status": row[1], "variant_id": row[2]} for row in checkouts
            ],
            "orders": [
                {
                    "id": str(row[0]),
                    "checkout_id": str(row[1]),
                    "variant_id": row[2]["offer_id"],
                    "status": row[2]["status"],
                    "total_minor": row[2]["total"],
                    "items_total_minor": row[2]["subtotal"],
                }
                for row in orders
            ],
        }
