"""Episode exports, offer changes, the 06D-C fixtures and the P0 catalog operations."""

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from commerce_evaluation.stack.merchant_control.core import BaseControl, module


class OfferControl(BaseControl):
    def exp01_export(self, command: dict[str, Any]) -> dict[str, Any]:
        """Export the whole existing episode; no synthesized commercial observations."""
        return module("commerce_lab.feed").export_feed(
            Path(command["feed_dir"]),
            run_id=command["run_id"],
            base_url=os.environ["ACP_API_BASE_URL"],
        )

    def exp01_observe(self, command: dict[str, Any]) -> dict[str, Any]:
        """Independent, read-only public facts; no bearer, JWT, keys or payment instruments."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT checkout_id,status,offer_id,quantity,idempotency_sha256 "
                "FROM checkout_sessions WHERE run_id=%s ORDER BY checkout_id",
                (command["run_id"],),
            ).fetchall()
            events = connection.execute(
                "SELECT event_type,count(*) FROM run_events WHERE run_id=%s "
                "GROUP BY event_type ORDER BY event_type",
                (command["run_id"],),
            ).fetchall()
            stock = connection.execute(
                "SELECT offer_id,revision,snapshot->>'availability',"
                "snapshot->>'available_quantity' FROM catalog_offers WHERE run_id=%s "
                "ORDER BY offer_id",
                (command["run_id"],),
            ).fetchall()
        return {
            "tables": self.snapshot({}),
            "checkouts": rows,
            "run_events": events,
            "stock": stock,
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
            if kind == "stock":
                value["revision"] += 1
                value["availability"] = "out_of_stock"
                value["available_quantity"] = 0
            if kind == "revision":
                value["revision"] += 1
            if kind == "digest":
                value["name"] += " changed"
            if kind == "shipping":
                value["pricing"]["shipping_total_minor"] += command.get("shipping_delta_minor", 1)
                value["pricing"]["total_minor"] += command.get("shipping_delta_minor", 1)
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
