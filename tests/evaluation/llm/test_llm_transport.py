"""The LLM port's offline transports: the budget guard and the fake. No test opens a socket."""

from decimal import Decimal
from threading import Event
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from commerce_evaluation.llm.cost import TokenPrices, TokenUsage
from commerce_evaluation.llm.transport import (
    BudgetedTransport,
    FakeReply,
    FakeTransport,
    LlmRequest,
    TransportError,
    TransportTimeout,
)

PRICES = TokenPrices(Decimal("1.5"), Decimal("6"))
INSTRUCTIONS = "Decide whether the proposal stays within the delegated scope."
ACCEPT = {"decision": "accept"}


class Verdict(BaseModel):
    decision: Literal["accept", "reject"]


def request(**overrides: Any) -> LlmRequest:
    values: dict[str, Any] = {
        "model": "model-under-test",
        "instructions": INSTRUCTIONS,
        "input_text": "{}",
        "temperature": None,
        "reasoning_effort": None,
        "max_output_tokens": 256,
        "timeout_seconds": 5.0,
    }
    return LlmRequest(**{**values, **overrides})


def worst_case(input_characters: int, max_output_tokens: int) -> Decimal:
    return PRICES.cost(
        TokenUsage(
            input_tokens=(input_characters + 2) // 3,
            output_tokens=max_output_tokens,
            cached_input_tokens=0,
            reasoning_tokens=0,
        )
    )


def test_budget_guard_refuses_calls_that_could_exceed_the_cap():
    inner = FakeTransport(lambda _: FakeReply(output=ACCEPT))
    small = request(input_text="x" * 300, max_output_tokens=1_000)
    worst = worst_case(len(INSTRUCTIONS) + 300, 1_000)
    guarded = BudgetedTransport(inner, prices=PRICES, max_usd=worst * 2)
    assert guarded.transport_id == "fake"
    for _ in range(3):
        guarded.parse(small, Verdict)
    # Actual usage is far below the worst case, so spend is reconciled downwards.
    assert guarded.spent_usd < worst
    exhausted = BudgetedTransport(inner, prices=PRICES, max_usd=worst / 2)
    with pytest.raises(TransportError, match="BudgetExhausted"):
        exhausted.parse(small, Verdict)


def test_a_failed_call_keeps_its_worst_case_reserved():
    failing = FakeTransport(lambda _: FakeReply(raises=TransportError("APIConnectionError")))
    guarded = BudgetedTransport(failing, prices=PRICES, max_usd=Decimal(1))

    with pytest.raises(TransportError, match="APIConnectionError"):
        guarded.parse(request(), Verdict)

    assert guarded.spent_usd == worst_case(len(INSTRUCTIONS) + 2, 256)


def test_fake_transport_parses_its_answer_records_the_request_and_estimates_usage():
    transport = FakeTransport(lambda _: FakeReply(output=ACCEPT))

    result = transport.parse(request(), Verdict)

    assert result.parsed == Verdict(decision="accept")
    assert (result.failure, result.response_model) == (None, "model-under-test")
    assert result.usage == TokenUsage(
        input_tokens=(len(INSTRUCTIONS) + 2 + 2) // 3,
        output_tokens=(len('{"decision": "accept"}') + 2) // 3,
        cached_input_tokens=0,
        reasoning_tokens=0,
    )
    assert transport.requests == [request()]


@pytest.mark.parametrize(
    ("reply", "failure"),
    [
        (FakeReply(failure="refusal"), "refusal"),
        (FakeReply(output=ACCEPT, failure="incomplete"), "incomplete"),
        (FakeReply(), "schema_invalid"),
        (FakeReply(output={"decision": "maybe"}), "schema_invalid"),
    ],
    ids=["refusal", "incomplete", "no-output", "off-schema"],
)
def test_fake_transport_reports_answer_failures_with_usage(reply: FakeReply, failure: str):
    result = FakeTransport(lambda _: reply).parse(request(), Verdict)

    assert result.parsed is None and result.failure == failure
    assert result.usage is not None and result.usage.input_tokens > 0


def test_fake_transport_raises_the_scripted_infrastructure_error():
    error = TransportError("APIConnectionError")

    with pytest.raises(TransportError) as raised:
        FakeTransport(lambda _: FakeReply(raises=error)).parse(request(), Verdict)

    assert raised.value is error


def test_fake_transport_honours_request_timeout():
    released = Event()

    def respond(_: LlmRequest) -> FakeReply:
        released.set()
        return FakeReply(output=ACCEPT, delay_seconds=10.0)

    with pytest.raises(TransportTimeout):
        FakeTransport(respond).parse(request(timeout_seconds=0.05), Verdict)
    assert released.is_set()
