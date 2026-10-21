"""AP2 consent and effect evidence (docs/design/hp-p2-evidence-chain.md, PR B).

Purchases run through the app with the real approval, AP2 signing and completion services; only
the merchant, the payment provider and the model are doubles (tests/support/ap2_purchase.py).
"""

import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import jwt
import pytest
from pydantic import SecretStr

from commerce_agent.acp import ACPClient
from commerce_agent.ap2 import checkout_hash
from commerce_agent.application.ap2_evidence import build_ap2_evidence, text_digest
from commerce_agent.application.offer_freshness import build_offer_freshness
from commerce_agent.persistence.ap2_evidence_sqlite import (
    SqliteAp2EvidenceStore,
    chain_is_valid,
)
from commerce_agent.persistence.hp_evidence_sqlite import SqliteHpEvidenceStore
from tests.governance.test_shadow_integration import configuration
from tests.support.ap2_purchase import BUYER, PAYMENT_ID, PAYMENT_ISSUER, ap2_shop, sd_hash


def stored(database: Path, run_id: str) -> dict[str, Any]:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        approval = connection.execute("SELECT * FROM approvals WHERE run_id = ?", (run_id,))
        completion = connection.execute(
            "SELECT * FROM completion_results WHERE run_id = ?", (run_id,)
        )
        return {**dict(approval.fetchone()), **dict(completion.fetchone())}


def tables(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}


@pytest.mark.parametrize(("mode", "flow"), [("enforce", "governed"), ("shadow", "blocked")])
def test_an_ap2_purchase_links_its_consent_to_its_receipts(
    tmp_path: Path, mode: str, flow: str
) -> None:
    database = tmp_path / "agent.db"
    with ap2_shop(database, mode=mode, flow=flow) as shop:
        run_id = shop.buy()

    consent, effect = SqliteAp2EvidenceStore(database).for_run(run_id)
    raw = stored(database, run_id)
    approve = [
        record
        for record in SqliteHpEvidenceStore(database).for_run(run_id)
        if record.body["stage"] == "approve"
    ][-1]
    assert approve.body["decision"] == "allow"
    assert (consent.record_kind, effect.record_kind) == ("consent", "effect")
    assert chain_is_valid(run_id, (consent, effect))
    assert effect.previous_digest == consent.digest
    assert consent.body == {
        "run_id": run_id,
        "checkout_id": raw["checkout_id"],
        "approved_at": raw["approved_at"],
        "checkout_fingerprint": raw["checkout_fingerprint"],
        "actor_digest": text_digest(BUYER),
        "freshness_digest": approve.digest,
        "checkout_hash": checkout_hash(raw["checkout_jwt"]),
        "checkout_mandate_sd_hash": sd_hash(raw["checkout_mandate"]),
        "payment_mandate_sd_hash": sd_hash(raw["payment_mandate"]),
    }
    assert effect.body == {
        "run_id": run_id,
        "checkout_id": raw["checkout_id"],
        "order_id": raw["order_id"],
        "checkout_receipt": {
            "digest": text_digest(raw["checkout_receipt_jwt"]),
            "issuer": shop.settings.merchant_base_url,
            "status": "Success",
            "reference": consent.body["checkout_mandate_sd_hash"],
            "order_id": raw["order_id"],
        },
        "payment_receipt": {
            "digest": text_digest(raw["payment_receipt_jwt"]),
            "issuer": PAYMENT_ISSUER,
            "status": "Success",
            "reference": consent.body["payment_mandate_sd_hash"],
            "payment_id": PAYMENT_ID,
        },
    }
    assert shop.payments.delegations[0]["payment_mandate_hash"] == sd_hash(raw["payment_mandate"])


def test_the_evidence_keeps_no_mandate_receipt_token_or_buyer_data(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with ap2_shop(database) as shop:
        run_id = shop.buy()

    with closing(sqlite3.connect(database)) as connection:
        evidence = json.dumps(connection.execute("SELECT * FROM ap2_evidence").fetchall())
    raw = stored(database, run_id)
    checkout = jwt.decode(raw["checkout_jwt"], options={"verify_signature": False})["checkout"]
    buyer = [
        value
        for part in (checkout["buyer"], checkout["fulfillment_details"])
        for value in json.dumps(part).split('"')
        if len(value) > 3 and value not in {"name", "email", "phone_number", "address"}
    ]
    secrets = [
        raw[column]
        for column in (
            "checkout_jwt",
            "checkout_mandate",
            "payment_mandate",
            "checkout_receipt_jwt",
            "payment_receipt_jwt",
            "signature",
        )
    ]
    assert buyer and "@" in "".join(buyer)
    assert all(value not in evidence for value in [*secrets, *buyer, BUYER, "vt_"])


def test_off_purchases_leave_no_ap2_evidence_table(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with ap2_shop(database, mode="off", flow="blocked") as shop:
        shop.buy()

    assert "ap2_evidence" not in tables(database)


@pytest.mark.parametrize(
    ("mode", "flow", "exists"),
    [
        ("off", "blocked", False),
        ("enforce", "blocked", False),
        ("shadow", "blocked", True),
        ("enforce", "governed", True),
    ],
)
def test_the_recorder_exists_exactly_where_the_freshness_evidence_does(
    tmp_path: Path, mode: str, flow: str, exists: bool
) -> None:
    settings = configuration(tmp_path / "agent.db", mode=mode, agent_hp_flow=flow)
    client = ACPClient(settings.merchant_base_url, SecretStr("merchant-token"))

    recorder = build_ap2_evidence(settings)

    assert (recorder is not None) is exists
    assert (build_offer_freshness(settings, client) is not None) is exists
    assert ("ap2_evidence" in tables(tmp_path / "agent.db")) is exists


def test_an_unwritable_record_never_stops_the_purchase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def unavailable(*_: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteAp2EvidenceStore, "append", unavailable)
    database = tmp_path / "agent.db"
    with caplog.at_level(logging.WARNING), ap2_shop(database) as shop:
        run_id = shop.buy()

    assert stored(database, run_id)["order_id"]
    assert SqliteAp2EvidenceStore(database).for_run(run_id) == ()
    warnings = [record.getMessage() for record in caplog.records]
    assert "ap2_evidence_unavailable kind=consent error=OperationalError" in warnings
    assert "ap2_evidence_unavailable kind=effect error=OperationalError" in warnings


def test_the_store_is_append_only_with_one_record_per_kind(tmp_path: Path) -> None:
    store = SqliteAp2EvidenceStore(tmp_path / "agent.db")
    store.initialize()
    consent = store.append("run-1", "consent", {"checkout_id": "cs_1"})
    other_run = store.append("run-2", "consent", {"checkout_id": "cs_1"})

    with pytest.raises(sqlite3.IntegrityError, match="ap2_evidence_chain_conflict"):
        store.append("run-1", "consent", {"checkout_id": "cs_2"})
    effect = store.append("run-1", "effect", {"order_id": "ord_1"})
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection, connection:
        for statement in (
            "UPDATE ap2_evidence SET body = '{}'",
            "DELETE FROM ap2_evidence WHERE run_id = 'run-1'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="ap2_evidence_append_only"):
                connection.execute(statement)
        # A forged third link reusing a kind would otherwise evict the first one (UNIQUE).
        with pytest.raises(sqlite3.IntegrityError, match="ap2_evidence_chain_conflict"):
            connection.execute(
                "INSERT OR REPLACE INTO ap2_evidence VALUES ('run-1', 3, 'consent', '{}', ?, ?)",
                (effect.digest, "sha256:" + "f" * 64),
            )

    records = store.for_run("run-1")
    assert records == (consent, effect)
    assert chain_is_valid("run-1", records)
    assert other_run.sequence == 1 and other_run.digest != consent.digest
    assert not chain_is_valid("run-1", (consent, replace(effect, body={"order_id": "ord_2"})))
