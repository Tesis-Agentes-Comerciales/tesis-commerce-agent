#!/usr/bin/env python3
"""Mutate one seeded merchant offer for a controlled evaluation stimulus."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
from commerce_lab.contracts import Offer
from commerce_lab.db import database_url
from commerce_lab.managed_catalog import changed_offer
from psycopg.types.json import Jsonb


def mutate_offer(run_id: str, offer_id: str, price_minor: int) -> dict[str, Any]:
    with psycopg.connect(database_url()) as connection, connection.transaction():
        row = connection.execute(
            """
            SELECT revision, snapshot
            FROM catalog_offers
            WHERE run_id = %s AND offer_id = %s
            FOR UPDATE
            """,
            (UUID(run_id), offer_id),
        ).fetchone()
        if row is None:
            raise ValueError("Offer does not exist in the seeded evaluation episode")
        revision, raw = row
        original = Offer.model_validate(raw)
        if original.revision != revision:
            raise ValueError("Catalog revision does not match its snapshot")
        updated = changed_offer(original, datetime.now(UTC), price_minor=price_minor)
        connection.execute(
            """
            UPDATE catalog_offers
            SET revision = %s, snapshot = %s
            WHERE run_id = %s AND offer_id = %s
            """,
            (
                updated.revision,
                Jsonb(updated.model_dump(mode="json")),
                UUID(run_id),
                offer_id,
            ),
        )
    return {
        "run_id": run_id,
        "offer_id": offer_id,
        "old_revision": original.revision,
        "new_revision": updated.revision,
        "old_price_minor": original.pricing.items_total_minor,
        "new_price_minor": updated.pricing.items_total_minor,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--offer-id", required=True)
    parser.add_argument("--price-minor", required=True, type=int)
    arguments = parser.parse_args()
    result = mutate_offer(arguments.run_id, arguments.offer_id, arguments.price_minor)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
