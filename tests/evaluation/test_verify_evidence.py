"""`verify-evidence`: the decision-consent-effect chain and H2 (hp-p2-evidence-chain, PR C).

Fixture databases come from real purchases through the app (tests/support/ap2_purchase.py):
only the merchant, the payment provider and the model are doubles. Manipulated chains are those
databases edited afterwards, as someone with the file could.
"""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import jwt
import pytest

from commerce_evaluation.demo_stack import OTHER_MERCHANT
from commerce_evaluation.verify_evidence import (
    LINKS,
    Link,
    h2_summary,
    is_complete,
    main,
    verify,
    verify_database,
)
from tests.evaluation.test_pilot_evidence import PSP_DELEGATIONS
from tests.governance.test_hp_continuation import ask
from tests.governance.test_stale_terms_pending import presented
from tests.support.ap2_purchase import Ap2Shop, ap2_shop

OK_B2 = dict.fromkeys(LINKS, "ok") | {"d2_decision": "not_applicable"}
DENIED = dict.fromkeys(LINKS, "not_applicable") | {"intent": "ok", "d1_decision": "ok"}


def statuses(run: dict[str, Any]) -> dict[str, str]:
    return {name: link["status"] for name, link in run["links"].items()}


def only_run(database: Path, **options: Any) -> dict[str, Any]:
    [run] = verify_database(database, **options)
    return run


def psp_database(path: Path, shop: Ap2Shop, database: Path, **changes: Any) -> Path:
    """The provider's `delegations` row for the shop's payment, as the real provider keeps it."""
    [delegation] = shop.payments.delegations
    with closing(sqlite3.connect(database)) as agent:
        [receipt] = agent.execute("SELECT payment_receipt_jwt FROM completion_results").fetchone()
    result = {
        "id": delegation["payment_id"],
        "status": "approved",
        "amount": delegation["max_amount"],
        "payment_receipt_jwt": receipt,
    }
    row = {"payment_mandate_hash": delegation["payment_mandate_hash"], **changes}
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(PSP_DELEGATIONS)
        connection.execute(
            "INSERT INTO delegations (key_hash, request_hash, token_hash, created_at, merchant_id, "
            "checkout_id, max_amount, currency, expires_at, outcome, result_json, "
            "payment_mandate_hash) VALUES ('key', 'request', 'token', '2026-09-10T14:05:00Z', "
            "'tesis_merchant', ?, ?, 'cop', '2026-09-10T15:05:00Z', 'approved', ?, ?)",
            (
                delegation["checkout_id"],
                delegation["max_amount"],
                json.dumps(result),
                row["payment_mandate_hash"],
            ),
        )
    return path


@pytest.fixture
def b2_purchase(tmp_path: Path) -> tuple[Path, Path]:
    """A completed B2 purchase whose order event arrived, and the provider's database."""
    database = tmp_path / "agent.db"
    with ap2_shop(database) as shop:
        run_id = shop.buy()
        shop.deliver_order_create(run_id)
    return database, psp_database(tmp_path / "payments.db", shop, database)


def test_a_b2_purchase_verifies_every_link_but_is_incomplete_without_d2(
    b2_purchase: tuple[Path, Path],
) -> None:
    database, payments = b2_purchase

    run = only_run(database, psp_database=payments)

    assert statuses(run) == OK_B2
    # A purchase with an order is complete only with its nine links ok: D2 does not exist yet.
    assert run["complete"] is False
    assert run["links"]["receipts"]["refs"]["psp"] == "verified"
    assert run["links"]["d1_decision"]["refs"]["replay"] == "reproduced"
    assert (
        run["links"]["approval"]["refs"]["freshness_digest"]
        == (run["links"]["freshness"]["refs"]["approve_digest"])
    )


def test_the_report_carries_no_token_mandate_receipt_or_buyer_data(
    b2_purchase: tuple[Path, Path],
) -> None:
    database, payments = b2_purchase

    report = json.dumps(verify([database], psp_database=payments))

    with closing(sqlite3.connect(database)) as connection:
        approval = connection.execute(
            "SELECT checkout_jwt, checkout_mandate, payment_mandate, actor_id FROM approvals"
        ).fetchone()
        receipts = connection.execute(
            "SELECT checkout_receipt_jwt, payment_receipt_jwt FROM completion_results"
        ).fetchone()
    checkout = jwt.decode(approval[0], options={"verify_signature": False})["checkout"]
    assert all(value not in report for value in [*approval, *receipts, "vt_", "eyJ"])
    assert checkout["buyer"]["email"] not in report


def test_without_the_order_webhook_the_chain_is_incomplete(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with ap2_shop(database) as shop:
        shop.buy()

    run = only_run(database)

    assert statuses(run) == OK_B2 | {"order_webhook": "missing"}
    assert run["complete"] is False


def test_a_b0_prime_purchase_misses_every_governed_link(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with ap2_shop(database, mode="off", flow="blocked") as shop:
        run_id = shop.buy()
        shop.deliver_order_create(run_id)

    run = only_run(database)

    # AP2 still signs mandates and receipts in B0', but nothing links them to a decision.
    assert statuses(run) == dict.fromkeys(LINKS, "missing") | {
        "d2_decision": "not_applicable",
        "order_webhook": "ok",
    }


def test_s04_blocked_at_approval_ends_complete_at_the_freshness_link(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with ap2_shop(database) as shop:
        run_id, terms = presented(shop.client, "turn-s04")
        shop.merchant.reprice(80_000)
        approval = shop.client.post(
            f"/purchases/{run_id}/approval",
            json={
                "expected_fingerprint": terms["fingerprint"],
                "checkout_jwt": terms["checkout_jwt"],
            },
        )
        assert approval.status_code == 409
        assert shop.client.post(f"/purchases/{run_id}/cancel").status_code == 200

    run = only_run(database)

    after = dict.fromkeys(("d2_decision", "approval", "mandates", "receipts", "order_webhook"))
    assert statuses(run) == dict.fromkeys(LINKS, "ok") | {name: "not_applicable" for name in after}
    assert run["links"]["freshness"]["refs"]["approve_decision"] == "block"
    assert run["complete"] is True


def test_a_d1_deny_verifies_the_intent_and_the_decision(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with ap2_shop(database, agent_d1_allowed_merchant_origins=OTHER_MERCHANT) as shop:
        turn = ask(shop.client, "turn-deny")
        assert turn.status_code == 409 and turn.json()["code"] == "d1_denied", turn.text

    run = only_run(database)

    # The purchase never chose a product: its D1 rows alone make both links apply.
    assert (run["stage"], run["order_id"]) == ("received", None)
    assert statuses(run) == DENIED
    assert run["links"]["d1_decision"]["refs"]["outcome"] == "deny"
    assert run["links"]["d1_decision"]["refs"]["replay"] == "reproduced"
    assert run["complete"] is True


@pytest.fixture
def denied(tmp_path: Path) -> Path:
    database = tmp_path / "denied.db"
    with ap2_shop(database, agent_d1_allowed_merchant_origins=OTHER_MERCHANT) as shop:
        assert ask(shop.client, "turn-deny").status_code == 409
    return database


DENY_TAMPERING: dict[str, tuple[Callable[[Path], None], str]] = {
    "deny turned into allow": (
        lambda db: edit(
            db,
            "governance_evidence",
            "UPDATE governance_evidence SET outcome = 'allow', "
            "body = json_set(body, '$.decision.outcome', 'allow')",
        ),
        "d1_decision",
    ),
    "intent of a deny": (
        lambda db: edit(
            db,
            "d1_provenance_sources",
            "UPDATE d1_provenance_sources SET public_body = CAST('{}' AS BLOB) "
            "WHERE kind = 'intent'",
        ),
        "intent",
    ),
    "checkout under a deny": (
        lambda db: edit(db, "purchases", "UPDATE purchases SET checkout_id = 'cs_bypass'"),
        "checkout_created",
    ),
}


@pytest.mark.parametrize("change", sorted(DENY_TAMPERING))
def test_a_manipulated_deny_is_a_mismatch(denied: Path, change: str) -> None:
    tamper, link = DENY_TAMPERING[change]

    tamper(denied)
    run = only_run(denied)

    assert run["links"][link]["status"] == "mismatch", run["links"][link]
    assert run["complete"] is False


def test_an_approved_b2_hp_v2_purchase_without_d2_misses_it(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with ap2_shop(database, agent_d1_policy_set_id="b2-hp-v2") as shop:
        run_id = shop.buy()
        shop.deliver_order_create(run_id)

    # b2-hp-v2 carries the D2 rules: approving left a D2 record, present but not yet verified.
    run = only_run(database)
    assert statuses(run) == OK_B2 | {"d2_decision": "missing"}
    assert run["links"]["d2_decision"]["refs"] == {
        "records": 1,
        "content_verified": False,
        "policy_set_id": "b2-hp-v2",
        "d2_required": True,
    }

    # Without that record (someone with the file deleted it) the approved purchase misses D2.
    edit(database, "governance_evidence", "DELETE FROM governance_evidence WHERE phase = 'D2'")
    run = only_run(database)

    assert statuses(run) == OK_B2 | {"d2_decision": "missing"}
    assert run["links"]["d2_decision"]["refs"] == {
        "policy_set_id": "b2-hp-v2",
        "d2_required": True,
    }
    assert run["complete"] is False


def test_a_d2_record_counts_as_unverified_until_its_link_checks_it(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        # The D2 schema is not on main yet: only the columns the verifier reads.
        connection.execute(
            "CREATE TABLE purchases (run_id TEXT, stage TEXT, selected_variant_id TEXT, "
            "checkout_id TEXT, order_id TEXT, created_at TEXT)"
        )
        connection.execute(
            "INSERT INTO purchases VALUES ('run-d2', 'received', NULL, NULL, NULL, 'now')"
        )
        connection.execute("CREATE TABLE governance_evidence (run_id TEXT, phase TEXT)")
        connection.execute("INSERT INTO governance_evidence VALUES ('run-d2', 'D2')")

    run = only_run(database)

    assert run["links"]["d2_decision"]["status"] == "missing"
    assert run["links"]["d2_decision"]["refs"]["content_verified"] is False


def test_a_creation_the_merchant_refused_has_no_checkout_receipts_or_order(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with ap2_shop(database) as shop:
        shop.merchant.refuse_creation = True
        turn = ask(shop.client, "turn-refused")
        assert turn.status_code == 409, turn.text

    run = only_run(database)

    assert run["stage"] == "failed"
    assert statuses(run) == dict.fromkeys(LINKS, "not_applicable") | {
        "intent": "ok",
        "d1_decision": "ok",
    }
    assert run["links"]["checkout_created"]["refs"]["code"] == "VERIFIED_NOT_CREATED"
    assert run["complete"] is True


def edit(database: Path, table: str, statement: str, *values: Any) -> None:
    """Someone with the file: drops the table's append-only triggers, then edits it."""
    with closing(sqlite3.connect(database)) as connection, connection:
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (table,)
        ).fetchall()
        for (name,) in triggers:
            connection.execute(f"DROP TRIGGER {name}")
        assert connection.execute(statement, values).rowcount >= 1


def body_edit(table: str, condition: str, path: str, value: str) -> Callable[[Path], None]:
    def apply(database: Path) -> None:
        edit(
            database,
            table,
            f"UPDATE {table} SET body = json_set(body, '{path}', ?) WHERE {condition}",
            value,
        )

    return apply


def forged_receipt(database: Path) -> None:
    with closing(sqlite3.connect(database)) as connection:
        [receipt] = connection.execute(
            "SELECT checkout_receipt_jwt FROM completion_results"
        ).fetchone()
    claims = jwt.decode(receipt, options={"verify_signature": False}) | {"order_id": "ord_other"}
    forged = jwt.encode(claims, "x" * 32, algorithm="HS256")
    edit(
        database,
        "completion_results",
        "UPDATE completion_results SET checkout_receipt_jwt = ?",
        forged,
    )


def swapped_checkout(database: Path) -> None:
    with closing(sqlite3.connect(database)) as connection:
        [signed] = connection.execute("SELECT checkout_jwt FROM approvals").fetchone()
    claims = jwt.decode(signed, options={"verify_signature": False})
    for row in claims["checkout"]["totals"]:
        row["amount"] = 1
    forged = jwt.encode(claims, "x" * 32, algorithm="HS256")
    edit(database, "approvals", "UPDATE approvals SET checkout_jwt = ?", forged)


TAMPERING: dict[str, tuple[Callable[[Path], None], str]] = {
    "intent body": (
        lambda db: edit(
            db,
            "d1_provenance_sources",
            "UPDATE d1_provenance_sources SET public_body = CAST('{}' AS BLOB) "
            "WHERE kind = 'intent'",
        ),
        "intent",
    ),
    "D1 decision body": (
        body_edit("governance_evidence", "1", "$.decision.decided_at", "2026-01-01T00:00:00.000Z"),
        "d1_decision",
    ),
    "approve freshness": (
        body_edit(
            "hp_evidence", "json_extract(body, '$.stage') = 'approve'", "$.outcome", "changed"
        ),
        "freshness",
    ),
    "approval rewritten": (
        lambda db: edit(
            db, "approvals", "UPDATE approvals SET approved_at = '2026-09-10T15:00:00Z'"
        ),
        "approval",
    ),
    "consent body": (
        body_edit("ap2_evidence", "record_kind = 'consent'", "$.checkout_hash", "other"),
        "approval",
    ),
    "signed checkout": (swapped_checkout, "mandates"),
    "merchant receipt": (forged_receipt, "receipts"),
    "effect body": (
        body_edit("ap2_evidence", "record_kind = 'effect'", "$.order_id", "ord_other"),
        "receipts",
    ),
    "order webhook": (
        lambda db: edit(
            db,
            "webhook_events",
            "UPDATE webhook_events SET payload = json_set(payload, '$.data.status', 'canceled')",
        ),
        "order_webhook",
    ),
}


@pytest.mark.parametrize("change", sorted(TAMPERING))
def test_a_manipulated_chain_is_a_mismatch_at_the_link_that_was_changed(
    b2_purchase: tuple[Path, Path], change: str
) -> None:
    database, _ = b2_purchase
    tamper, link = TAMPERING[change]

    tamper(database)
    run = only_run(database)

    assert run["links"][link]["status"] == "mismatch"
    assert all(
        run["links"][name]["status"] in {"ok", "not_applicable"}
        for name in LINKS[: LINKS.index(link)]
    )
    assert run["complete"] is False


def test_consent_effect_and_the_webhook_anchor_links_whose_rows_were_deleted(
    b2_purchase: tuple[Path, Path],
) -> None:
    database, _ = b2_purchase
    edit(database, "completion_results", "DELETE FROM completion_results")
    edit(database, "approvals", "DELETE FROM approvals")
    edit(
        database,
        "purchases",
        "UPDATE purchases SET order_id = NULL, stage = 'awaiting_confirmation'",
    )

    run = only_run(database)
    summary = verify([database])["h2"]

    # consent, effect and order_create still exist: the purchase was approved and has an order.
    assert run["order_id"] == "ord_cs_api_1"
    assert {name: statuses(run)[name] for name in LINKS[5:]} == {
        "approval": "missing",
        "mandates": "missing",
        "receipts": "missing",
        "order_webhook": "ok",
    }
    # The approve check is still there, and it allowed.
    assert run["links"]["freshness"]["status"] == "ok"
    assert run["complete"] is False
    assert summary["effects"] == {"n": 1, "complete": 0, "rate": 0.0}


def test_an_order_create_is_found_by_its_checkout_too(b2_purchase: tuple[Path, Path]) -> None:
    database, _ = b2_purchase
    edit(database, "completion_results", "DELETE FROM completion_results")
    edit(database, "purchases", "UPDATE purchases SET order_id = NULL")
    edit(database, "ap2_evidence", "DELETE FROM ap2_evidence WHERE record_kind = 'effect'")

    run = only_run(database)

    assert run["order_id"] == "ord_cs_api_1"
    assert statuses(run)["receipts"] == "missing"
    assert statuses(run)["order_webhook"] == "ok"


def test_mandates_without_total_or_currency_are_a_mismatch(
    b2_purchase: tuple[Path, Path],
) -> None:
    database, _ = b2_purchase
    with closing(sqlite3.connect(database)) as connection:
        [signed] = connection.execute("SELECT checkout_jwt FROM approvals").fetchone()
    claims = jwt.decode(signed, options={"verify_signature": False})
    claims["checkout"] = {
        key: value for key, value in claims["checkout"].items() if key not in {"totals", "currency"}
    }
    forged = jwt.encode(claims, "x" * 32, algorithm="HS256")
    edit(database, "approvals", "UPDATE approvals SET checkout_jwt = ?", forged)

    link = only_run(database)["links"]["mandates"]

    assert (link["status"], link["detail"]) == (
        "mismatch",
        "El checkout o el mandato de pago no tienen total y moneda",
    )


def test_a_purchase_with_an_order_is_complete_only_with_nine_links_ok() -> None:
    every: dict[str, Link] = dict.fromkeys(LINKS, Link("ok", ""))
    skipped = every | {"d2_decision": Link("not_applicable", "")}

    assert is_complete(every, effect=True) is True
    assert is_complete(skipped, effect=True) is False
    assert is_complete(skipped, effect=False) is True
    assert is_complete(every | {"freshness": Link("missing", "")}, effect=False) is False
    assert is_complete(every | {"intent": Link("mismatch", "")}, effect=False) is False
    assert is_complete({"intent": Link("ok", "")}, effect=False) is False


def test_a_payment_the_provider_kept_for_another_mandate_is_a_mismatch(
    tmp_path: Path, b2_purchase: tuple[Path, Path]
) -> None:
    database, _ = b2_purchase
    with ap2_shop(tmp_path / "other.db") as shop:
        shop.buy()
    other = psp_database(tmp_path / "other-payments.db", shop, tmp_path / "other.db")

    run = only_run(database, psp_database=other)

    assert run["links"]["receipts"]["status"] == "mismatch"


def test_h2_is_the_share_of_purchases_with_an_order_whose_chain_is_complete() -> None:
    def run(order_id: str | None, complete: bool, webhook: str) -> dict[str, Any]:
        links = {name: {"status": "ok"} for name in LINKS} | {"order_webhook": {"status": webhook}}
        return {"order_id": order_id, "complete": complete, "links": links}

    runs = [
        run("ord_1", True, "ok"),
        run("ord_2", False, "missing"),
        run(None, True, "not_applicable"),
    ]

    summary = h2_summary(runs)

    assert summary["effects"] == {"n": 2, "complete": 1, "rate": 0.5}
    assert summary["all_runs"] == {"n": 3, "complete": 2, "rate": 0.6667}
    assert summary["by_link"]["order_webhook"] == {
        "ok": 1,
        "missing": 1,
        "mismatch": 0,
        "not_applicable": 1,
    }
    assert h2_summary([])["effects"] == {"n": 0, "complete": 0, "rate": None}


def test_the_cli_writes_json_and_exits_by_completeness(
    tmp_path: Path,
    b2_purchase: tuple[Path, Path],
    denied: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, payments = b2_purchase
    [run_id] = [run["run_id"] for run in verify_database(database)]
    output = tmp_path / "verification.json"

    assert main(["--agent-db", str(denied), "--output", str(output)]) == 0
    assert main(["--agent-db", str(denied), "--agent-db", str(database)]) == 1
    assert main(["--agent-db", str(database), "--psp-db", str(payments), "--run", run_id]) == 1

    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["schema_version"] == "evidence-chain-verification/1"
    assert written["h2"]["effects"] == {"n": 0, "complete": 0, "rate": None}
    printed = json.loads(capsys.readouterr().out.split("\n}\n")[0] + "\n}")
    assert [run["complete"] for run in printed["runs"]] == [True, False]
    assert printed["h2"]["effects"] == {"n": 1, "complete": 0, "rate": 0.0}
    with pytest.raises(SystemExit) as unknown:
        main(["--agent-db", str(database), "--run", "run-unknown"])
    assert unknown.value.code == 2
    with pytest.raises(SystemExit):
        main(["--agent-db", str(tmp_path / "absent.db")])


def test_a_database_named_twice_is_verified_once(
    b2_purchase: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    database, _ = b2_purchase
    again = database.parent / "." / database.name

    assert len(verify([database, again, database])["runs"]) == 1
    main(["--agent-db", str(database), "--agent-db", str(again)])
    assert len(json.loads(capsys.readouterr().out)["runs"]) == 1
