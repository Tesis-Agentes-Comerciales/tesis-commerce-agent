"""The LLM port's offline fake. No test opens a socket."""

from threading import Event
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from commerce_evaluation.llm.cost import TokenUsage
from commerce_evaluation.llm.transport import (
    FakeReply,
    FakeTransport,
    LlmRequest,
    TransportError,
    TransportTimeout,
)

INSTRUCTIONS = "Decide whether the proposal stays within the delegated scope."
ACCEPT = {"decision": "accept"}


class Verdict(BaseModel):
    decision: Literal["accept", "reject"]


def request(**overrides: Any) -> LlmRequest:
    values: dict[str, Any] = {
        "model": "model-under-test",
        "instructions": INSTRUCTIONS,
        "input_text": "{}",
        "effort": None,
        "max_output_tokens": 256,
        "timeout_seconds": 5.0,
    }
    return LlmRequest(**{**values, **overrides})


def test_fake_transport_parses_its_answer_records_the_request_and_estimates_usage():
    transport = FakeTransport(lambda _: FakeReply(output=ACCEPT))

    result = transport.parse(request(), Verdict)

    assert result.parsed == Verdict(decision="accept")
    assert (result.failure, result.response_model) == (None, "model-under-test")
    assert result.usage == TokenUsage(
        input_tokens=(len(INSTRUCTIONS) + 2 + 1) // 2,
        output_tokens=(len('{"decision": "accept"}') + 1) // 2,
        cache_read_tokens=0,
        cache_write_tokens=0,
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
