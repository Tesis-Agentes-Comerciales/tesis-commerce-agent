"""UX09-HNP and UX10-HNP-c (docs/design/p0-ux08-ux10.md §3.1, D4, §4.2).

A revocation or kill switch that lands while the selector or the intent reviewer is answering ends
the task before `start`'s POST: the lease renewal before the first effect compares the claim's
authority. One that lands right after the POST, or a checkout the merchant returned with two units,
ends the task with that checkout canceled at the merchant, and `task_finished` records what
happened to it. A purchase D1 left `uncertain` is not canceled: D1's reconciliation owns it.

The real SDK over the scripted transport of tests/support/model_server.py, whose `held` answer
waits while the test revokes or turns the switch on, and the executor's bench on real SQLite. The
B2-HNP checkout with two units runs over the real D1-HNP gate (test_hnp_executor_d1). No model is
called.
"""

import sqlite3
import threading
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from commerce_agent.acp.models import CheckoutSession
from commerce_agent.application.hnp_selection import ModelHnpSelector
from commerce_agent.delegation.models import CheckoutCancel, Step, TaskFinish
from commerce_agent.llm.delegated_choice import OpenAIDelegatedChooser
from commerce_agent.llm.intent_review import ModelHnpIntentReviewer
from commerce_agent.persistence.stage_timings_sqlite import SqliteStageTimings
from tests.delegation.test_hnp_executor_d1 import bench as d1_bench
from tests.governance.test_d1_hnp_gate import hnp_service, settings_for
from tests.governance.test_hp_continuation import StoreMerchant
from tests.governance.test_runtime_dispatch import runtime
from tests.support.hnp_executor import Bench, Effects, Gate, Merchant
from tests.support.model_server import Held, ModelServer, held, shortlisted_choice, structured

API_KEY = "sk-test-ux09-hnp-0123456789abcdef"
NOTHING = Effects(creates=0, signatures=0, commits=0, cancels=0)
CANCELED = Effects(creates=1, signatures=0, commits=0, cancels=1)
CHOICE_OK = structured(shortlisted_choice)
REVIEW_CONFIRMED = structured(lambda body: {"decision": "confirmed", "reason": "matches_request"})
ENDED = {"revoke": ("canceled", "revoked_by_owner"), "kill": ("canceled", "kill_switch")}
# What the ledger writes from the claim on, besides `choice_reviewed`.
TAIL = {
    "revoke": ["task_claimed", "revoked", "task_finished"],
    "kill": ["task_claimed", "kill_switch_blocked", "revoked", "task_finished"],
}


def timings(path: Path) -> SqliteStageTimings:
    timer = SqliteStageTimings(path)
    timer.initialize()
    timer.initialize_model_tokens()
    return timer


def model_selector(path: Path) -> ModelHnpSelector:
    chooser = OpenAIDelegatedChooser(api_key=SecretStr(API_KEY), model="test-model")
    return ModelHnpSelector(chooser, timings(path))


def model_reviewer(path: Path) -> ModelHnpIntentReviewer:
    return ModelHnpIntentReviewer(
        api_key=SecretStr(API_KEY), model="test-model", timer=timings(path)
    )


def stop(bench: Bench, cause: str) -> Callable[[], object]:
    """The owner's revocation (`POST /delegations/{id}/revoke`), the operator's kill switch, the
    grant's expiry or a version that moved under the claim with no cause of its own."""
    return {
        "revoke": bench.revoke,
        "kill": lambda: bench.ledger.set_kill_switch(True),
        "expire": lambda: bench.clock.advance(3_601),
        "bump": bench.bump_grant,
    }[cause]


def run_held(bench: Bench, answer: Held, cause: str) -> Step:
    """The task runs in its own thread; while `answer` is held the test stops it, then lets the
    model answer."""
    ran: dict[str, Step | BaseException] = {}

    def target() -> None:
        try:
            ran["step"] = bench.run()
        except BaseException as error:
            ran["step"] = error

    thread = threading.Thread(target=target)
    thread.start()
    try:
        assert answer.entered.wait(10)
        stop(bench, cause)()
    finally:
        answer.release()
        thread.join(10)
    assert not thread.is_alive()
    step = ran["step"]
    assert isinstance(step, Step), step
    return step


def finished(bench: Bench) -> Mapping[str, Any]:
    (body,) = [
        e.body for e in bench.ledger.events(bench.grant_id) if e.event_type == "task_finished"
    ]
    return body


def operations(bench: Bench) -> list[str]:
    """The operation keys of the run: a cancel's key is written before it is sent."""
    with closing(sqlite3.connect(bench.path)) as connection:
        rows = connection.execute(
            "SELECT operation FROM operation_keys WHERE run_id = ?", (bench.run_id(),)
        ).fetchall()
    return [row[0] for row in rows]


def after_claim(bench: Bench) -> list[str]:
    events = bench.events()
    return [event for event in events[events.index("task_claimed") :] if event != "choice_reviewed"]


# UX09-HNP-sel and -sel-kill: the selector is answering


@pytest.mark.parametrize("cause", ["revoke", "kill"])
@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_a_stop_while_the_selector_answers_ends_the_task_before_any_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, cause: str
) -> None:
    choice = held(CHOICE_OK)
    ModelServer(choice).install(monkeypatch)
    bench = Bench(tmp_path, mode=mode, selector=model_selector(tmp_path / "agent.db"))  # type: ignore[arg-type]

    step = run_held(bench, choice, cause)

    assert (step.status, bench.ended()) == ("closed", ENDED[cause])
    assert bench.effects() == NOTHING
    assert bench.purchase() is None
    assert bench.execution().reservation is None
    assert after_claim(bench) == TAIL[cause]
    assert "checkout_cancel" not in finished(bench)
    if isinstance(bench.gate, Gate):
        assert bench.gate.calls == []


@pytest.mark.parametrize(
    ("cause", "ended"),
    [("expire", ("canceled", "grant_expired")), ("bump", ("failed", "state_conflict"))],
)
@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_the_renewal_before_the_post_fails_closed_on_any_lost_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, cause: str, ended: tuple[str, str]
) -> None:
    """§3.1 compares the whole authority (`_blocking`), so an expiry or a moved grant during the
    model call also ends the task before the POST, not at UoW-1 after it."""
    choice = held(CHOICE_OK)
    ModelServer(choice).install(monkeypatch)
    bench = Bench(tmp_path, mode=mode, selector=model_selector(tmp_path / "agent.db"))  # type: ignore[arg-type]

    step = run_held(bench, choice, cause)

    assert (step.status, bench.ended()) == ("closed", ended)
    assert bench.effects() == NOTHING
    assert bench.purchase() is None


# UX09-HNP-rev: B2-HNP's intent reviewer is answering


@pytest.mark.parametrize("cause", ["revoke", "kill"])
def test_a_stop_while_the_reviewer_answers_ends_the_task_before_any_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cause: str
) -> None:
    review = held(REVIEW_CONFIRMED)
    ModelServer(CHOICE_OK, review).install(monkeypatch)
    path = tmp_path / "agent.db"
    gate = Gate()
    bench = Bench(
        tmp_path,
        mode="enforce",
        gate=gate,
        selector=model_selector(path),
        reviewer=model_reviewer(path),
    )

    step = run_held(bench, review, cause)

    assert (step.status, bench.ended()) == ("closed", ENDED[cause])
    assert bench.effects() == NOTHING
    assert gate.calls == []
    # The review is recorded, and nothing comes after it: the purchase the reviewer opened has no
    # checkout and closes with the task.
    assert "choice_reviewed" in bench.events()
    assert after_claim(bench) == TAIL[cause]
    assert bench.purchase() == ("canceled", None)
    assert bench.store.purchase(bench.run_id())["checkout_id"] is None  # type: ignore[index]


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_the_control_without_a_stop_reaches_the_commitment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    """UX09-HNP-control: the same script, nobody stops it."""
    ModelServer(CHOICE_OK).install(monkeypatch)
    bench = Bench(tmp_path, mode=mode, selector=model_selector(tmp_path / "agent.db"))  # type: ignore[arg-type]

    assert bench.run().status == "applied"

    assert bench.effects() == Effects(creates=1, signatures=1, commits=1, cancels=0)


# UX09-HNP-post: the stop lands right after the POST (D4)


@pytest.mark.parametrize("cause", ["revoke", "kill"])
@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_a_stop_right_after_the_post_cancels_the_checkout_at_the_merchant(
    tmp_path: Path, mode: str, cause: str
) -> None:
    bench = Bench(tmp_path, mode=mode)  # type: ignore[arg-type]
    bench.merchant.on_create = stop(bench, cause)

    assert bench.run().status == "closed"

    assert bench.ended() == ENDED[cause]
    assert bench.effects() == CANCELED
    assert bench.execution().reservation is None
    assert bench.purchase() == ("canceled", None)
    assert finished(bench)["checkout_cancel"] == {"checkout_id": "cs_1", "result": "canceled"}
    assert "checkout.cancel.stopped" in operations(bench)


class RevokingStore(StoreMerchant):
    """The merchant of the real D1-HNP harness; the owner revokes once its checkout exists."""

    on_create: Callable[[], object] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = super().__call__(request)
        if (request.method, request.url.path) == ("POST", "/checkout_sessions") and self.on_create:
            self.on_create()
        return response


def test_in_b2_a_revocation_after_d1s_post_cancels_through_the_acp_client(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    merchant = RevokingStore()
    built = runtime(settings, merchant)
    try:
        run = d1_bench(tmp_path, built, hnp_service(settings, built))
        merchant.on_create = run.revoke
        step = run.run()
    finally:
        built.close()

    assert (step.status, run.ended()) == ("closed", ENDED["revoke"])
    assert merchant.creations() == 1
    assert merchant.effects() == [("POST", "/checkout_sessions/cs_api_1/cancel")]
    assert run.purchase() == ("canceled", None)
    assert finished(run)["checkout_cancel"] == {"checkout_id": "cs_api_1", "result": "canceled"}


def test_a_cancel_the_merchant_refuses_is_recorded_and_keeps_the_terminal(tmp_path: Path) -> None:
    """At best effort and once: a cancel that fails is not retried, and the task keeps its
    outcome."""

    class Refusing(Merchant):
        def cancel_checkout(self, checkout_id, *, idempotency_key, reason_code=None):
            self.calls.append(("cancel", idempotency_key))
            raise RuntimeError("merchant unavailable")

    bench = Bench(tmp_path, merchant=Refusing())
    bench.merchant.on_create = bench.revoke

    assert bench.run().status == "closed"

    assert bench.ended() == ENDED["revoke"]
    assert bench.effects() == CANCELED  # one attempt
    assert finished(bench)["checkout_cancel"] == {"checkout_id": "cs_1", "result": "cancel_failed"}


# UX10-HNP-c: the merchant answers two units under a single-use grant (D4)


class TwoUnits(Merchant):
    """The Bench merchant, answering quantity 2 when creating or after the details update."""

    def __init__(self, when: str) -> None:
        super().__init__()
        self.when = when

    @staticmethod
    def _two(checkout: CheckoutSession) -> CheckoutSession:
        line = {**checkout.line_items[0], "quantity": 2}
        return checkout.model_copy(update={"line_items": [line]})

    def create_checkout(self, variant_id, *, currency, idempotency_key):
        created = super().create_checkout(
            variant_id, currency=currency, idempotency_key=idempotency_key
        )
        return self._two(created) if self.when == "create" else created

    def update_checkout(self, checkout_id, update, *, idempotency_key):
        updated = super().update_checkout(checkout_id, update, idempotency_key=idempotency_key)
        return self._two(updated) if self.when == "update" else updated

    def get_checkout(self, checkout_id):
        current = super().get_checkout(checkout_id)
        return self._two(current) if self.when == "update" else current


@pytest.mark.parametrize("when", ["create", "update"])
def test_in_b0_a_checkout_with_two_units_fails_and_is_canceled(tmp_path: Path, when: str) -> None:
    bench = Bench(tmp_path, merchant=TwoUnits(when))

    assert bench.run().status == "closed"

    assert bench.ended() == ("failed", "checkout_failed")
    assert bench.purchase() == ("failed", "checkout_mismatch")
    ex = bench.execution()
    assert (ex.grant.state, ex.grant.revoked_reason, ex.reservation) == ("revoked", "failed", None)
    assert bench.effects() == CANCELED
    assert finished(bench)["checkout_cancel"] == {"checkout_id": "cs_1", "result": "canceled"}
    assert "checkout.cancel.mismatch" in operations(bench)


class TwoUnitsStore(StoreMerchant):
    """The merchant of the real D1-HNP harness, answering quantity 2 like `TwoUnits`."""

    when = "create"
    updated = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = super().__call__(request)
        call = (request.method, request.url.path)
        self.updated = self.updated or call == ("POST", "/checkout_sessions/cs_api_1")
        created = call == ("POST", "/checkout_sessions")
        later = self.updated and call[1] == "/checkout_sessions/cs_api_1"
        if (created if self.when == "create" else later) and response.status_code < 300:
            body = response.json()
            body["line_items"][0]["quantity"] = 2
            return httpx.Response(response.status_code, json=body)
        return response


@pytest.mark.parametrize(("when", "checkout_id"), [("create", None), ("update", "cs_api_1")])
def test_in_b2_a_checkout_with_two_units_is_left_to_d1_reconciliation(
    tmp_path: Path, when: str, checkout_id: str | None
) -> None:
    """D1-HNP leaves the purchase `uncertain`: canceling it here would race D1's reconciliation,
    so nothing is canceled and the terminal says so."""
    settings = settings_for(tmp_path)
    merchant = TwoUnitsStore()
    merchant.when = when
    built = runtime(settings, merchant)
    try:
        run = d1_bench(tmp_path, built, hnp_service(settings, built))
        step = run.run()
    finally:
        built.close()

    assert (step.status, run.ended()) == ("closed", ("failed", "checkout_failed"))
    assert run.purchase() == ("uncertain", None)
    assert merchant.creations() == 1
    assert merchant.effects() == []
    assert run.execution().reservation is None
    assert finished(run)["checkout_cancel"] == {
        "checkout_id": checkout_id,
        "result": "left_to_d1_reconciliation",
    }


# The ledger keeps the rule


@pytest.mark.parametrize(
    ("outcome", "cancel"),
    [
        ("grant_expired", CheckoutCancel("cs_1", "canceled")),
        ("policy_denied", CheckoutCancel("cs_1", "cancel_failed")),
        ("revoked_by_owner", CheckoutCancel(None, "canceled")),
    ],
    ids=["expiry", "deny", "no-id"],
)
def test_only_a_stop_or_a_mismatch_records_its_checkouts_cancel(
    tmp_path: Path, outcome: str, cancel: CheckoutCancel
) -> None:
    """D4's scope (p0-ux08-ux10.md §3.3): no other terminal says it canceled anything, and only a
    checkout D1 kept no identity of goes without its id."""
    bench = Bench(tmp_path)
    claimed = bench.service.claim(bench.task_id)
    assert claimed.execution is not None and claimed.execution.task.lease_id is not None
    state = "canceled" if outcome in ("grant_expired", "revoked_by_owner") else "failed"
    request = TaskFinish(
        bench.task_id,
        claimed.execution.task.lease_id,
        state,  # type: ignore[arg-type]
        outcome,  # type: ignore[arg-type]
        checkout_cancel=cancel,
    )

    with pytest.raises(ValueError, match="checkout's cancel"):
        bench.ledger.finish_task(request)

    assert bench.ended() == ("running", None)
