"""No paid calls: HTTP transports and responses below are synthetic guard controls."""

from dataclasses import replace
from decimal import Decimal
from typing import Any

import httpx
import pytest

from commerce_evaluation.exp06 import (
    AttemptBudget,
    Authorization,
    SpendingBlocked,
    classify,
    messages,
    protocol,
    protocol_hash,
    review_template,
)


def authorization(cap="9"):
    return Authorization(protocol()["model"], Decimal(cap), protocol_hash(), "smoke", "unit-only")


def request(**changes):
    return httpx.Request(
        "POST",
        "https://api.openai.com/v1/responses",
        json={"model": protocol()["model"], "store": False, **changes},
    )


def test_authorization_required_before_transport_exists():
    with pytest.raises(SpendingBlocked, match="missing"):
        AttemptBudget(None)
    for changes in [
        {"model": "alias"},
        {"protocol_sha256": "changed"},
        {"stage": "other"},
        {"human_reference": ""},
        {"max_usd": Decimal("NaN")},
        {"max_usd": Decimal("0")},
    ]:
        with pytest.raises(SpendingBlocked, match="does_not_match"):
            AttemptBudget(replace(authorization(), **changes))


def test_infrastructure_refuses_real_transport_without_sending():
    with httpx.Client() as client, pytest.raises(SpendingBlocked, match="MockTransport"):
        AttemptBudget(None, infrastructure_only=True).attach(client)


@pytest.mark.parametrize(
    "payload",
    [
        {"model": "wrong"},
        {"store": True},
        {"stream": True},
        {"tools": [{"type": "web_search"}]},
    ],
)
def test_unregistered_requests_never_reach_transport(payload):
    sent = []

    def send(req):
        sent.append(req)
        return httpx.Response(503)

    budget = AttemptBudget(authorization())
    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        budget.attach(client)
        with pytest.raises(SpendingBlocked):
            client.send(request(**payload))
    assert sent == [] and budget.attempts == 0


def test_unknown_usage_reserves_retries_and_stops_before_cap():
    budget = AttemptBudget(authorization("0.89"))
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(503))) as client:
        budget.attach(client)
        client.send(request())
        client.send(request())
        with pytest.raises(SpendingBlocked, match="before_send"):
            client.send(request())
    assert budget.attempts == 2 and budget.reserved_or_spent == Decimal("0.890")
    budget.begin_episode()
    assert budget.reserved_or_spent == Decimal("0.890")


def test_usage_settlement_is_conservative_and_reasoning_not_added_twice():
    budget = AttemptBudget(authorization())
    body = {
        "model": protocol()["model"],
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "output_tokens_details": {"reasoning_tokens": 80},
        },
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))
    ) as client:
        budget.attach(client)
        client.send(request())
    assert budget.reserved_or_spent == Decimal("0.00045")


def test_timeout_keeps_reservation_and_counts_attempt():
    def timeout(req):
        raise httpx.ReadTimeout("synthetic", request=req)

    budget = AttemptBudget(authorization())
    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        budget.attach(client)
        with pytest.raises(httpx.ReadTimeout):
            client.send(request())
    assert budget.attempts == 1 and budget.reserved_or_spent == Decimal("0.445")


@pytest.mark.parametrize("case_id", ["UX01", "UX11", "UX12"])
def test_structural_success_never_claims_semantic_success(case_id):
    args: dict[str, Any] = {
        "status": 200,
        "body": {"action": "clarify", "purchase": None},
        "effects": {"checkouts": 0, "payments": 0},
        "ledger_complete": True,
    }
    assert classify(case_id, **args)["status"] == "pending_review"
    assert classify(case_id, **args, simulated=True)["status"] == "not_executed"
    args["status"] = 502
    assert classify(case_id, **args)["commercial_truth"] == "unknown"
    args["status"] = 200
    args["effects"] = {"tokens": 1}
    assert classify(case_id, **args)["status"] == "failed"
    args["effects"] = {"tokens": 0}
    args["body"] = {"action": "purchase"}
    assert classify(case_id, **args)["status"] == "failed"
    assert review_template(case_id)["status"] == "pending_review"
    assert messages(case_id)[-1]["content"] == protocol()["cases"][case_id]["prompt"]
