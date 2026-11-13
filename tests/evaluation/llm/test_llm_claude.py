"""The Anthropic transports through the real SDK, answered by an in-process mock. No network."""

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx2
import pytest
from anthropic import Anthropic
from pydantic import BaseModel

from commerce_evaluation.llm.batch import BudgetedBatchTransport
from commerce_evaluation.llm.budget import BudgetedTransport, RunBudget
from commerce_evaluation.llm.claude import AnthropicBatchTransport, AnthropicTransport
from commerce_evaluation.llm.cost import CLAUDE_PRICES, TokenUsage
from commerce_evaluation.llm.transport import (
    FundsExhausted,
    LlmRequest,
    TransportError,
    TransportTimeout,
)

API = "https://api.anthropic.test"
BATCH = f"{API}/v1/messages/batches/msgbatch_1"
Handler = Callable[[httpx2.Request], httpx2.Response]


class Verdict(BaseModel):
    decision: Literal["accept", "reject"]


class Api:
    """Answers each request with the next scripted response and keeps what it received."""

    def __init__(self, *responses: httpx2.Response | Exception):
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(handler: Handler) -> Anthropic:
    transport = httpx2.MockTransport(handler)
    return Anthropic(
        api_key="test-key", base_url=API, http_client=httpx2.Client(transport=transport)
    )


def request(**overrides: Any) -> LlmRequest:
    values: dict[str, Any] = {
        "model": "claude-sonnet-5-5",
        "instructions": "You are the buyer. Answer as JSON.",
        "input_text": "Assistant: Shall I buy the blue one?",
        "effort": "low",
        "max_output_tokens": 1_024,
        "timeout_seconds": 5.0,
    }
    return LlmRequest(**{**values, **overrides})


def message(text: str, *, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "served-model",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": text},
        ],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 48,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 10,
            "output_tokens_details": {"thinking_tokens": 16},
        },
    }


def error(status: int, kind: str, text: str, code: str | None = None) -> httpx2.Response:
    body: dict[str, Any] = {"type": kind, "message": text}
    if code is not None:
        body["details"] = {"error_code": code}
    return httpx2.Response(status, json={"type": "error", "error": body})


USAGE = TokenUsage(
    input_tokens=120,
    output_tokens=48,
    cache_read_tokens=30,
    cache_write_tokens=10,
    reasoning_tokens=16,
)


def test_one_structured_call_sends_the_schema_and_effort_and_reads_usage_and_model():
    api = Api(httpx2.Response(200, json=message('{"decision": "accept"}')))

    result = AnthropicTransport(client(api)).parse(request(), Verdict)

    assert (result.parsed, result.failure) == (Verdict(decision="accept"), None)
    assert (result.usage, result.response_model) == (USAGE, "served-model")
    [sent] = api.requests
    body = json.loads(sent.content)
    assert sent.url.path == "/v1/messages"
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"] == "You are the buyer. Answer as JSON."
    assert body["messages"] == [{"role": "user", "content": "Assistant: Shall I buy the blue one?"}]
    assert body["max_tokens"] == 1_024
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["required"] == ["decision"]
    assert not {"temperature", "thinking", "stream"} & body.keys()


@pytest.mark.parametrize(
    ("text", "stop_reason", "failure"),
    [
        ('{"decision": "maybe"}', "end_turn", "schema_invalid"),
        ("I would rather not say.", "end_turn", "schema_invalid"),
        ("", "refusal", "refusal"),
        ('{"decis', "max_tokens", "incomplete"),
    ],
    ids=["off-schema", "not-json", "refusal", "truncated"],
)
def test_unusable_answers_come_back_as_failures_with_their_usage(
    text: str, stop_reason: str, failure: str
):
    api = Api(httpx2.Response(200, json=message(text, stop_reason=stop_reason)))

    result = AnthropicTransport(client(api)).parse(request(), Verdict)

    assert (result.parsed, result.failure, result.usage) == (None, failure, USAGE)


@pytest.mark.parametrize(
    ("response", "name"),
    [
        (
            error(429, "rate_limit_error", "Number of requests has exceeded your rate limit"),
            "RateLimitError",
        ),
        (error(529, "overloaded_error", "Overloaded"), "OverloadedError"),
        (httpx2.ConnectError("refused"), "APIConnectionError"),
    ],
    ids=["429", "529", "connection"],
)
def test_provider_failures_are_infrastructure_and_never_retried(
    response: httpx2.Response | Exception, name: str
):
    api = Api(response)

    with pytest.raises(TransportError) as raised:
        AnthropicTransport(client(api)).parse(request(), Verdict)

    assert str(raised.value) == name and not isinstance(raised.value, FundsExhausted)
    assert raised.value.__cause__ is None
    assert len(api.requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        error(400, "invalid_request_error", "Your credit balance is too low to access the API."),
        error(402, "billing_error", "Payment required."),
        error(429, "rate_limit_error", "Spend limit reached.", "enforced_spend_limit_reached"),
    ],
    ids=["credit-balance", "billing", "spend-cap"],
)
def test_a_spent_balance_stops_the_run_and_is_recorded(response: httpx2.Response, tmp_path: Path):
    api = Api(response)
    run_budget = RunBudget(max_usd=Decimal(1), run_dir=tmp_path)
    simulator = BudgetedTransport(
        AnthropicTransport(client(api)), budget=run_budget, role="simulator"
    )

    with pytest.raises(FundsExhausted, match="CreditBalanceExhausted"):
        simulator.parse(request(), Verdict)

    assert len(api.requests) == 1
    [row] = [json.loads(line) for line in run_budget.ledger_path.read_text().splitlines()]
    assert (row["transport"], row["error"], row["usage"]) == (
        "anthropic",
        "CreditBalanceExhausted",
        None,
    )


def test_an_exceeded_timeout_is_a_transport_timeout():
    api = Api(httpx2.ReadTimeout("slow"))

    with pytest.raises(TransportTimeout):
        AnthropicTransport(client(api)).parse(request(timeout_seconds=0.5), Verdict)

    assert len(api.requests) == 1


def test_a_budgeted_call_records_the_served_model_and_its_sonnet_price(tmp_path: Path):
    api = Api(httpx2.Response(200, json=message('{"decision": "reject"}')))
    run_budget = RunBudget(max_usd=Decimal(1), run_dir=tmp_path)
    simulator = BudgetedTransport(
        AnthropicTransport(client(api)), budget=run_budget, role="simulator"
    )

    simulator.parse(request(), Verdict)

    [row] = [json.loads(line) for line in run_budget.ledger_path.read_text().splitlines()]
    assert (row["model_requested"], row["model_returned"]) == ("claude-sonnet-5-5", "served-model")
    assert row["usage"] == USAGE.model_dump()
    assert row["usd"] == float(CLAUDE_PRICES["claude-sonnet-5-5"].cost(USAGE))
    assert run_budget.spent_usd == CLAUDE_PRICES["claude-sonnet-5-5"].cost(USAGE)


def batch(status: str) -> dict[str, Any]:
    ended = status == "ended"
    return {
        "id": "msgbatch_1",
        "type": "message_batch",
        "processing_status": status,
        "request_counts": {
            "processing": 0,
            "succeeded": 1,
            "errored": 2,
            "canceled": 0,
            "expired": 1,
        },
        "created_at": "2026-11-13T12:00:00Z",
        "expires_at": "2026-11-14T12:00:00Z",
        "ended_at": "2026-11-13T12:05:00Z" if ended else None,
        "results_url": f"{BATCH}/results" if ended else None,
    }


def results(*lines: dict[str, Any]) -> httpx2.Response:
    return httpx2.Response(200, content="\n".join(json.dumps(line) for line in lines).encode())


def errored(custom_id: str, kind: str) -> dict[str, Any]:
    failure = {"type": "error", "error": {"type": kind, "message": "Failed."}}
    return {"custom_id": custom_id, "result": {"type": "errored", "error": failure}}


def test_a_batch_is_submitted_polled_and_collected_by_custom_id(tmp_path: Path):
    api = Api(
        httpx2.Response(200, json=batch("in_progress")),
        httpx2.Response(200, json=batch("in_progress")),
        httpx2.Response(200, json=batch("ended")),
        # results() reads the batch again for its results URL.
        httpx2.Response(200, json=batch("ended")),
        # Out of order, as the API may return them; c5 never comes back.
        results(
            errored("c2", "overloaded_error"),
            {"custom_id": "c3", "result": {"type": "expired"}},
            {
                "custom_id": "c1",
                "result": {"type": "succeeded", "message": message('{"decision": "accept"}')},
            },
            errored("c4", "billing_error"),
        ),
    )
    waits: list[float] = []
    transport = AnthropicBatchTransport(client(api), poll_seconds=7.0, wait=waits.append)
    run_budget = RunBudget(max_usd=Decimal(1), run_dir=tmp_path)
    judge = BudgetedBatchTransport(transport, budget=run_budget, role="judge")
    items = {
        f"c{n}": request(model="claude-opus-5-5", input_text=f"Criterion {n}") for n in range(1, 6)
    }

    reply = judge.parse_batch(items, Verdict)

    assert reply.batch_id == "msgbatch_1" and waits == [7.0, 7.0]
    succeeded = reply.results["c1"]
    assert not isinstance(succeeded, TransportError)
    assert (succeeded.parsed, succeeded.usage) == (Verdict(decision="accept"), USAGE)
    failures = {key: value for key, value in reply.results.items() if key != "c1"}
    assert {key: (type(value).__name__, str(value)) for key, value in failures.items()} == {
        "c2": ("TransportError", "BatchItemErrored:overloaded_error"),
        "c3": ("TransportError", "BatchItemExpired"),
        "c4": ("FundsExhausted", "CreditBalanceExhausted"),
        "c5": ("TransportError", "BatchResultMissing"),
    }
    submitted = json.loads(api.requests[0].content)["requests"]
    assert [item["custom_id"] for item in submitted] == ["c1", "c2", "c3", "c4", "c5"]
    assert submitted[0]["params"]["output_config"]["format"]["type"] == "json_schema"
    rows = [json.loads(line) for line in run_budget.ledger_path.read_text().splitlines()]
    assert {(row["mode"], row["batch_id"]) for row in rows} == {("batch", "msgbatch_1")}
    assert rows[0]["usd"] == float(CLAUDE_PRICES["claude-opus-5-5"].batch().cost(USAGE))


def test_a_failed_batch_submission_is_not_retried():
    api = Api(error(429, "rate_limit_error", "Too many batches."))
    transport = AnthropicBatchTransport(client(api), wait=lambda _: None)

    with pytest.raises(TransportError, match="RateLimitError"):
        transport.parse_batch({"c1": request()}, Verdict)

    assert len(api.requests) == 1
