"""HNP-09b: the dispatch of the committed outbox, its receipts and UoW-3 (hnp-governed.md §3.3
steps 11-12, §4.4, §4.5 and §8.1).

The executor runs as in HNP-09a with the product's mandates (`real_chains`); the dispatcher uses
the product's PSP and ACP clients over HTTP doubles of the PSP and the merchant
(tests/support/hnp_dispatch.py) that sign their receipts with test keys.
"""

import json
import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

import pytest

from commerce_agent.delegation.models import LEASE_SECONDS
from tests.support.hnp_dispatch import ORDER_ID, MerchantApi, Psp, dispatching
from tests.support.hnp_executor import Bench, Crash

Mode = Literal["off", "enforce"]
PAYMENT_KEY, COMPLETE_KEY = "op_1:payment", "op_1:complete"


def bench(
    tmp_path: Path, psp: Psp | None = None, merchant: MerchantApi | None = None, mode: Mode = "off"
) -> tuple[Bench, Psp, MerchantApi]:
    psp, merchant = psp or Psp(), merchant or MerchantApi()
    built = Bench(tmp_path, mode=mode, real_chains=True, dispatcher=dispatching(psp, merchant))
    return built, psp, merchant


def rows(built: Bench) -> dict[str, Any]:
    with closing(sqlite3.connect(built.path)) as connection:
        outbox = connection.execute(
            "SELECT state, rejected_by, ap2_error, order_id, error_receipt_jwt FROM dispatch_outbox"
        ).fetchone()
        reservation = connection.execute(
            "SELECT state, release_basis, uncertain_reason FROM grant_reservations"
        ).fetchone()
        grant = connection.execute("SELECT state, revoked_reason FROM delegation_grants").fetchone()
    return {"outbox": outbox, "reservation": reservation, "grant": grant}


def crash_when(
    monkeypatch: pytest.MonkeyPatch, target: object, name: str, when: Callable[..., bool]
) -> None:
    """The process dies on the first call to `target.name` that `when` matches."""
    original, crashed = getattr(target, name), []

    def crashing(*args: Any, **kwargs: Any) -> Any:
        if not crashed and when(*args, **kwargs):
            crashed.append(True)
            raise Crash
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, crashing)


def lapse(built: Bench) -> None:
    built.clock.advance(LEASE_SECONDS + 1)


# The purchase


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_a_dispatch_buys_with_both_receipts_in_completion_results_and_orders(
    tmp_path: Path, mode: Mode
) -> None:
    built, psp, merchant = bench(tmp_path, mode=mode)

    step = built.run()

    assert (step.status, built.ended()) == ("closed", ("purchased", None))
    assert rows(built) == {
        "outbox": ("completed", None, None, ORDER_ID, None),
        "reservation": ("consumed", None, None),
        "grant": ("consumed", None),
    }
    run_id = built.run_id()
    assert built.purchase() == ("completed", None)
    with closing(sqlite3.connect(built.path)) as connection:
        checkout_receipt, payment_receipt = connection.execute(
            "SELECT checkout_receipt_jwt, payment_receipt_jwt FROM completion_results "
            "WHERE run_id = ? AND order_id = ?",
            (run_id, ORDER_ID),
        ).fetchone()
        order = connection.execute(
            "SELECT checkout_id, status FROM orders WHERE order_id = ?", (ORDER_ID,)
        ).fetchone()
    assert order == ("cs_1", "confirmed")
    assert (checkout_receipt, payment_receipt) == (merchant.issued[0], psp.issued[0])
    events = built.events()
    assert events[-4:] == ["receipt_recorded", "receipt_recorded", "consumed", "task_finished"]
    assert built.effects().signatures == 1


def test_the_dispatch_sends_the_stored_chains_under_the_outbox_keys(tmp_path: Path) -> None:
    built, psp, merchant = bench(tmp_path)

    built.run()

    stored = {m.kind: m.token for m in built.ledger.mandates(built.grant_id)}
    [(key, body)] = psp.delegations
    assert key == PAYMENT_KEY
    assert body["ap2_payment_mandate"] == stored["closed_payment"]
    assert body["ap2_checkout_mandate"] == stored["closed_checkout"]
    assert body["allowance"]["checkout_session_id"] == "cs_1"
    [(complete_key, completion)] = merchant.completions
    assert complete_key == COMPLETE_KEY
    assert completion["checkout_mandate"] == stored["closed_checkout"]
    assert psp.receipts == [completion["payment_data"]["instrument"]["credential"]["token"]]


# A PSP error receipt: nothing reached the merchant (§8.1)


@pytest.mark.parametrize(
    ("mode", "code", "terminal", "purchase", "cancels"),
    [
        ("enforce", "unresolved_constraint", "escalated", ("canceled", None), 1),
        ("off", "unresolved_constraint", "failed", ("failed", "verifier_unresolved_constraint"), 0),
        ("enforce", "invalid_credential", "failed", ("failed", "verifier_invalid_credential"), 0),
    ],
)
def test_a_psp_error_receipt_releases_and_escalates_or_fails(
    tmp_path: Path,
    mode: Mode,
    code: str,
    terminal: str,
    purchase: tuple[str, str | None],
    cancels: int,
) -> None:
    built, _, merchant = bench(tmp_path, Psp(code), mode=mode)

    step = built.run()

    assert (step.status, built.ended()) == ("closed", (terminal, f"verifier_{code}"))
    found = rows(built)
    assert found["outbox"][:3] == ("rejected", "psp", code)
    assert found["outbox"][4] is not None  # the verified error receipt
    assert found["reservation"] == ("released", "psp_error_receipt", None)
    assert found["grant"] == ("revoked", "escalated" if terminal == "escalated" else "failed")
    assert built.purchase() == purchase
    assert (merchant.completions, len(merchant.cancels)) == ([], cancels)
    assert "receipt_recorded" in built.events()


def test_an_invalid_mandate_fails_and_alerts_the_operator(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    built, _, merchant = bench(tmp_path, Psp("invalid_mandate"), mode="enforce")

    with caplog.at_level(logging.ERROR, logger="commerce_agent.hnp.alerts"):
        built.run()

    assert built.ended() == ("failed", "verifier_invalid_mandate")
    assert built.purchase() == ("failed", "verifier_invalid_mandate")
    [alert] = [r for r in caplog.records if r.name == "commerce_agent.hnp.alerts"]
    assert "verifier_invalid_mandate" in alert.getMessage() and built.task_id in alert.getMessage()
    assert merchant.completions == []


# In doubt after a send: the reservation stays held (§4.4, §8.1)


def assert_in_doubt(built: Bench, reason: str) -> None:
    assert built.ended() == ("uncertain", reason)
    found = rows(built)
    assert found["reservation"] == ("uncertain", None, reason)
    assert found["grant"] == ("active", None)
    assert built.purchase() == ("uncertain", None)
    assert built.events()[-2:] == ["reservation_uncertain", "task_finished"]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ("timeout", "dispatch_timeout"),
        ("unavailable", "dispatch_timeout"),
        ("idempotency_conflict", "receipt_mismatch"),
        ("merchant_mismatch", "receipt_mismatch"),
        ("invalid_mandate:wrong_reference", "receipt_mismatch"),
    ],
)
def test_the_psp_without_a_valid_receipt_leaves_the_purchase_uncertain(
    tmp_path: Path, answer: str, reason: str
) -> None:
    built, _, merchant = bench(tmp_path, Psp(answer), mode="enforce")

    step = built.run()

    assert step.status == "closed"
    assert_in_doubt(built, reason)
    assert rows(built)["outbox"][:2] == ("uncertain", None)
    assert merchant.completions == []


@pytest.mark.parametrize(
    ("answer", "reason", "outbox"),
    [
        ("timeout", "dispatch_timeout", ("uncertain", None, None)),
        ("unavailable", "dispatch_timeout", ("uncertain", None, None)),
        ("no_receipt", "receipt_mismatch", ("uncertain", None, None)),
        ("wrong_reference", "receipt_mismatch", ("uncertain", None, None)),
        # After the PSP's token a merchant rejection is a partial failure, whatever its code.
        (
            "unresolved_constraint",
            "partial_verifier_failure",
            ("rejected", "merchant", "unresolved_constraint"),
        ),
    ],
)
def test_the_merchant_without_an_order_leaves_the_purchase_uncertain(
    tmp_path: Path, answer: str, reason: str, outbox: tuple[str, str | None, str | None]
) -> None:
    built, psp, merchant = bench(tmp_path, merchant=MerchantApi(answer), mode="enforce")

    built.run()

    assert_in_doubt(built, reason)
    assert rows(built)["outbox"][:3] == outbox
    assert len(psp.delegations) == 1 and merchant.cancels == []


@pytest.mark.parametrize(
    ("answer", "reason"), [("timeout", "dispatch_timeout"), ("wrong_reference", "receipt_mismatch")]
)
def test_a_payment_receipt_that_does_not_verify_leaves_the_purchase_uncertain(
    tmp_path: Path, answer: str, reason: str
) -> None:
    psp = Psp()
    psp.receipt_answers.append(answer)
    built, _, merchant = bench(tmp_path, psp)

    built.run()

    assert_in_doubt(built, reason)
    assert rows(built)["outbox"][0] == "uncertain"
    assert len(merchant.completions) == 1


# Crashes and re-claims: the same keys, never another close (§4.5)


def test_a_crash_after_the_reservation_never_closes(tmp_path: Path) -> None:
    built, psp, _ = bench(tmp_path)
    built.closer.before = lambda: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        built.run()
    built.closer.before = None
    lapse(built)

    built.run()

    assert built.ended() == ("failed", "state_conflict")
    assert (built.effects().signatures, psp.delegations) == (0, [])


@pytest.mark.parametrize(
    ("method", "when"),
    [
        pytest.param("commit_dispatch", lambda *a, **k: True, id="after-the-close"),
        pytest.param(
            "advance_dispatch", lambda task, lease, to, **k: to == "payment_sent", id="after-outbox"
        ),
        pytest.param(
            "advance_dispatch",
            lambda task, lease, to, **k: to == "payment_accepted",
            id="after-the-token",
        ),
        pytest.param(
            "advance_dispatch", lambda task, lease, to, **k: to == "completed", id="after-the-order"
        ),
        pytest.param("record_completion", lambda *a, **k: True, id="before-uow3"),
    ],
)
def test_a_crash_resumes_with_the_same_keys_and_one_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, when: Callable[..., bool]
) -> None:
    built, psp, merchant = bench(tmp_path, mode="enforce")
    crash_when(monkeypatch, built.ledger, method, when)
    with pytest.raises(Crash):
        built.run()
    lapse(built)

    step = built.run()

    assert (step.status, built.ended()) == ("closed", ("purchased", None))
    assert built.effects().signatures == 1 and built.effects().creates == 1
    assert (psp.keys(), merchant.keys()) == ({PAYMENT_KEY}, {COMPLETE_KEY})
    assert len({json_of(body) for _, body in psp.delegations}) == 1  # one request, replayed
    assert built.purchase() == ("completed", None)


def json_of(body: dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True)


def test_a_committed_dispatch_resumes_despite_revocation_kill_switch_and_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built, psp, merchant = bench(tmp_path, mode="enforce")
    crash_when(monkeypatch, built.ledger, "advance_dispatch", lambda *a, **k: True)
    with pytest.raises(Crash):
        built.run()
    built.revoke()
    built.ledger.set_kill_switch(True)
    built.clock.advance(3600 + LEASE_SECONDS)

    step = built.run()

    assert (step.status, built.ended()) == ("closed", ("purchased", None))
    assert rows(built)["grant"] == ("revoked", "owner")
    assert rows(built)["reservation"] == ("consumed", None, None)
    assert (psp.keys(), merchant.keys()) == ({PAYMENT_KEY}, {COMPLETE_KEY})


def test_a_request_that_no_longer_matches_its_commitment_is_never_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built, psp, merchant = bench(tmp_path)
    crash_when(monkeypatch, built.ledger, "advance_dispatch", lambda *a, **k: True)
    with pytest.raises(Crash):
        built.run()
    with closing(sqlite3.connect(built.path)) as connection, connection:
        connection.execute("UPDATE dispatch_outbox SET payment_request_digest = 'sha256:' || 'f'")
    lapse(built)

    built.run()

    assert_in_doubt(built, "receipt_mismatch")
    assert (psp.delegations, merchant.completions) == ([], [])


def test_a_completed_dispatch_never_becomes_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built, _, merchant = bench(tmp_path)
    crash_when(monkeypatch, built.ledger, "record_completion", lambda *a, **k: True)
    with pytest.raises(Crash):
        built.run()
    merchant.answers.append("timeout")
    lapse(built)

    waiting = built.run()

    assert waiting.status == "blocked"
    assert built.ended() == ("running", None)
    assert rows(built)["outbox"][0] == "completed"
    lapse(built)
    assert built.run().status == "closed"
    assert built.ended() == ("purchased", None)
    assert merchant.keys() == {COMPLETE_KEY} and len(merchant.completions) == 3
