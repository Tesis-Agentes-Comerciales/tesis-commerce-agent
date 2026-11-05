"""CT13: the merchant refuses to create the checkout of a delegated purchase.

The task still fails closed with `checkout_failed` and leaves no effect. When the merchant refused
the creation with a typed code and nothing was created, `task_finished` also names the refusal
(`merchant_refusal`) and the buyer's delegation card says why, in B0-HNP and in B2-HNP. A rejected
update, a failed close, a generic error or an untyped code name nothing.

The creation runs through the product's ACP client and, in B2-HNP, D1-HNP with its runtime dispatch,
over httpx.MockTransport (tests/support/hnp_chain.py); the paths after the creation use the
executor's bench. One SQLite file per test: no model, no network.
"""

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest

from commerce_agent.acp.client import ACPClientError
from commerce_agent.delegation.contract import HNP_MESSAGES
from commerce_agent.delegation.models import MerchantRefusal, TaskFinish
from commerce_agent.governance.enforce import RefusalCode
from commerce_agent.governance.runtime_dispatch import DEFINITIVE_CREATION_REFUSALS
from commerce_agent.trace import models as trace_models
from tests.governance.test_hp_continuation import StoreMerchant
from tests.governance.test_shadow_integration import OWNER
from tests.support.hnp_chain import HnpChain, hnp_chain
from tests.support.hnp_executor import AgentKey, Bench, Merchant

CREATE = ("POST", "/checkout_sessions")
DETAILS = ("POST", "/checkout_sessions/cs_api_1")


def finished(ledger: Any, grant_id: str) -> Mapping[str, Any]:
    (body,) = [
        event.body for event in ledger.events(grant_id) if event.event_type == "task_finished"
    ]
    return body


def effects(path: Path) -> dict[str, int]:
    """What a refused creation must not leave: a reservation, closed mandates, an outbox row (the
    payment) or a completion (the order)."""
    with closing(sqlite3.connect(path)) as connection:
        count = lambda sql: connection.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "reservations": count("SELECT COUNT(*) FROM grant_reservations"),
            "closed_mandates": count(
                "SELECT COUNT(*) FROM delegation_mandates WHERE kind LIKE 'closed_%'"
            ),
            "outbox": count("SELECT COUNT(*) FROM dispatch_outbox"),
            "completions": count("SELECT COUNT(*) FROM completion_results"),
        }


NO_EFFECT = {"reservations": 0, "closed_mandates": 0, "outbox": 0, "completions": 0}


def purchase(chain: HnpChain) -> tuple[str, str | None, str | None]:
    run_id = chain.ledger.execution(chain.task_id).task.run_id  # type: ignore[union-attr]
    with closing(sqlite3.connect(chain.path)) as connection:
        return connection.execute(
            "SELECT stage, failure_code, checkout_id FROM purchases WHERE run_id = ?", (run_id,)
        ).fetchone()


def card_message(chain: HnpChain) -> str | None:
    return chain.delegations.delegation(OWNER, chain.grant_id).task.message


# A typed refusal of the creation: recorded and explained

REFUSALS = [
    # (mode, the merchant's answer to the POST, the refusal the task records)
    ("off", "out_of_stock", "out_of_stock"),
    ("enforce", "out_of_stock", "out_of_stock"),
    ("off", "offer_expired", "offer_expired"),  # 409 OFFER_EXPIRED, as the merchant writes it
    ("enforce", "offer_expired", "offer_expired"),
    ("off", "offer_expired_lowercase", "offer_expired"),
    ("off", "invalid_item", "invalid_item"),
    ("enforce", "invalid_item", "invalid_item"),
]


@pytest.mark.parametrize(("mode", "fault", "refusal"), REFUSALS)
def test_a_typed_refusal_of_the_creation_is_recorded_and_explained(
    tmp_path: Path, mode: str, fault: str, refusal: str
) -> None:
    merchant = StoreMerchant(fault)
    with hnp_chain(tmp_path, mode=mode, merchant=merchant) as chain:  # type: ignore[arg-type]
        assert chain.run().status == "closed"

        run_id = chain.ledger.execution(chain.task_id).task.run_id  # type: ignore[union-attr]
        execution = chain.ledger.execution(chain.task_id)
        assert execution is not None
        # The terminal does not change: `checkout_failed`, fail closed, in both conditions.
        assert (execution.task.state, execution.task.outcome_code) == ("failed", "checkout_failed")
        assert (execution.grant.state, execution.grant.revoked_reason) == ("revoked", "failed")
        assert finished(chain.ledger, chain.grant_id) == {
            "state": "failed",
            "outcome_code": "checkout_failed",
            "run_id": run_id,
            "merchant_refusal": refusal,
        }
        # One POST and nothing after it: no reservation, mandate, payment or order.
        assert merchant.requests == [CREATE]
        assert effects(chain.path) == NO_EFFECT
        stage, _, checkout_id = purchase(chain)
        assert (stage, checkout_id) == ("failed", None)
        explained = HNP_MESSAGES[f"task.refused.{refusal}"]
        assert card_message(chain) == explained
        assert chain.delegations.task(chain.task_id).message == explained


# Nothing proves the cause: nothing is named

UNTYPED = ["client_error", "server_error", "route_not_found", "unrecoverable", "key_conflict"]


@pytest.mark.parametrize("mode", ["off", "enforce"])
@pytest.mark.parametrize("fault", UNTYPED)
def test_an_untyped_answer_to_the_creation_names_no_cause(
    tmp_path: Path, mode: str, fault: str
) -> None:
    """A 409 that is not a refusal, a 5xx, a 404 without `invalid_item` or a key conflict: the
    merchant may have created the checkout, so the card keeps the precaution."""
    merchant = StoreMerchant(fault)
    with hnp_chain(tmp_path, mode=mode, merchant=merchant) as chain:  # type: ignore[arg-type]
        chain.run()

        body = finished(chain.ledger, chain.grant_id)
        assert (body["state"], body["outcome_code"]) == ("failed", "checkout_failed")
        assert "merchant_refusal" not in body
        assert effects(chain.path) == NO_EFFECT
        assert card_message(chain) == HNP_MESSAGES["task.failed"]


class RefusesTheDetails(StoreMerchant):
    """Creates the checkout, then answers the details update with the typed 409 `out_of_stock`."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == DETAILS:
            self.requests.append(DETAILS)
            detail = {"code": "out_of_stock", "message": "Not available."}
            return httpx.Response(409, json={"detail": detail})
        return super().__call__(request)


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_a_typed_refusal_of_the_update_names_no_cause(tmp_path: Path, mode: str) -> None:
    """The same code on the update comes with a checkout already created: it proves nothing about
    the creation, so the task names no cause, even if the purchase keeps the merchant's code."""
    merchant = RefusesTheDetails()
    with hnp_chain(tmp_path, mode=mode, merchant=merchant) as chain:  # type: ignore[arg-type]
        chain.run()

        body = finished(chain.ledger, chain.grant_id)
        assert (body["state"], body["outcome_code"]) == ("failed", "checkout_failed")
        assert "merchant_refusal" not in body
        assert CREATE in merchant.requests and DETAILS in merchant.requests
        assert purchase(chain)[2] == "cs_api_1"
        assert effects(chain.path) == NO_EFFECT
        assert card_message(chain) == HNP_MESSAGES["task.failed"]


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_a_failed_close_names_no_cause(tmp_path: Path, mode: str) -> None:
    """The Agent Key fails after the checkout and the reservation: `checkout_failed`, no cause."""
    bench = Bench(tmp_path, mode=mode, closer=AgentKey(fail=True))  # type: ignore[arg-type]

    assert bench.run().status == "closed"

    assert bench.ended() == ("failed", "checkout_failed")
    body = finished(bench.ledger, bench.grant_id)
    assert "merchant_refusal" not in body
    assert bench.effects().commits == 0


class FailingCreation(Merchant):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def create_checkout(self, variant_id: str, *, currency: str, idempotency_key: str):
        self.calls.append(("create", idempotency_key))
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("merchant client bug"),
        ACPClientError("Out of stock", status_code=500, code="out_of_stock"),
        ACPClientError("Out of stock", code="out_of_stock"),
        ACPClientError("Unknown", status_code=409, code="something_new"),
    ],
    ids=["generic", "typed-code-wrong-status", "typed-code-no-status", "unknown-code"],
)
def test_a_generic_error_or_an_unknown_refusal_names_no_cause(
    tmp_path: Path, error: Exception
) -> None:
    bench = Bench(tmp_path, merchant=FailingCreation(error))

    assert bench.run().status == "closed"

    assert bench.ended() == ("failed", "checkout_failed")
    assert "merchant_refusal" not in finished(bench.ledger, bench.grant_id)
    assert bench.effects().commits == 0


# The ledger keeps the rule


def claimed_lease(bench: Bench) -> str:
    claimed = bench.service.claim(bench.task_id)
    assert claimed.execution is not None and claimed.execution.task.lease_id is not None
    return claimed.execution.task.lease_id


def test_only_checkout_failed_names_a_merchant_refusal(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    lease = claimed_lease(bench)

    with pytest.raises(ValueError, match="checkout_failed"):
        bench.ledger.finish_task(
            TaskFinish(
                bench.task_id, lease, "failed", "state_conflict", merchant_refusal="out_of_stock"
            )
        )
    with pytest.raises(ValueError, match="refusal"):
        bench.ledger.finish_task(
            TaskFinish(
                bench.task_id,
                lease,
                "failed",
                "checkout_failed",
                merchant_refusal="payment_declined",  # type: ignore[arg-type]
            )
        )

    assert bench.ended() == ("running", None)


def test_a_terminal_without_a_refusal_keeps_its_body(tmp_path: Path) -> None:
    """The key only appears with a refusal, so every other `task_finished` keeps its digest."""
    bench = Bench(tmp_path)
    lease = claimed_lease(bench)

    bench.ledger.finish_task(TaskFinish(bench.task_id, lease, "failed", "checkout_failed"))

    assert finished(bench.ledger, bench.grant_id) == {
        "state": "failed",
        "outcome_code": "checkout_failed",
        "run_id": bench.run_id(),
    }


def test_the_refusals_are_the_definitive_refusals_of_the_creation() -> None:
    """One list: what the runtime dispatch proves, the ledger keeps and the trace shows."""
    refusals = set(get_args(MerchantRefusal))
    assert refusals == set(get_args(RefusalCode)) == set(DEFINITIVE_CREATION_REFUSALS.values())
    assert refusals == set(get_args(trace_models.MerchantRefusal))
    assert {key for key in HNP_MESSAGES if key.startswith("task.refused.")} == {
        f"task.refused.{refusal}" for refusal in refusals
    }
