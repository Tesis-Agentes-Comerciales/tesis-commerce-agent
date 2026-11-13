"""Structured-output transports: the port and an offline fake.

Contract shared by every transport:
- one ``parse`` call is one request and one observation: no retries, no fallback model;
- provider and network failures raise ``TransportError`` (infrastructure, never a verdict);
- a call the run cannot pay for raises ``FundsExhausted``, which no retry can fix;
- an exceeded request timeout raises ``TransportTimeout`` (a ``TimeoutError``);
- answers that are refused, truncated or off-schema come back as ``failure`` with whatever
  usage the provider reported, so their cost is still counted.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from time import sleep
from typing import Literal, Protocol

from pydantic import BaseModel, ValidationError

from .cost import TokenUsage, estimated_tokens

FailureKind = Literal["refusal", "incomplete", "schema_invalid"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
TransportId = Literal["fake", "anthropic"]


class TransportError(RuntimeError):
    """Provider, network or budget failure: infrastructure, never an observation of the model.

    Carries the error class name, never response bodies.
    """


class FundsExhausted(TransportError):
    """The run cannot pay for the call: its ``--max-usd`` cap or the account's credit balance.

    Retrying never helps, so the runner stops instead of repeating the trial.
    """


class TransportTimeout(TimeoutError):
    """The provider did not answer within the request timeout."""


@dataclass(frozen=True)
class LlmRequest:
    model: str
    # The system prompt, and the single user message after it.
    instructions: str
    input_text: str
    # None keeps the model's default effort.
    effort: Effort | None
    max_output_tokens: int
    timeout_seconds: float


@dataclass(frozen=True)
class LlmResult[T: BaseModel]:
    parsed: T | None
    failure: FailureKind | None
    usage: TokenUsage | None
    response_model: str | None


class StructuredLLM(Protocol):
    transport_id: TransportId

    def parse[T: BaseModel](self, request: LlmRequest, schema: type[T]) -> LlmResult[T]: ...


@dataclass(frozen=True)
class FakeReply:
    """One scripted answer: an output object, a failure kind or an exception to raise."""

    output: Mapping[str, object] | None = None
    failure: Literal["refusal", "incomplete"] | None = None
    raises: BaseException | None = None
    delay_seconds: float = 0.0


class FakeTransport:
    """Deterministic offline transport for tests and dry runs. It opens no connection.

    Usage is estimated from text length so dry runs exercise cost accounting; it is labelled
    as a fake transport everywhere it is reported.
    """

    transport_id: TransportId = "fake"

    def __init__(self, responder: Callable[[LlmRequest], FakeReply]):
        self._responder = responder
        self._lock = Lock()
        self.requests: list[LlmRequest] = []

    def parse[T: BaseModel](self, request: LlmRequest, schema: type[T]) -> LlmResult[T]:
        with self._lock:
            self.requests.append(request)
        reply = self._responder(request)
        if reply.delay_seconds > request.timeout_seconds:
            # Behave like an HTTP client: give up at the request timeout.
            sleep(request.timeout_seconds)
            raise TransportTimeout("Fake provider timeout")
        if reply.delay_seconds:
            sleep(reply.delay_seconds)
        if reply.raises is not None:
            raise reply.raises
        text = "" if reply.output is None else json.dumps(reply.output, sort_keys=True)
        usage = TokenUsage(
            input_tokens=estimated_tokens(request.instructions + request.input_text),
            output_tokens=estimated_tokens(text),
            cache_read_tokens=0,
            cache_write_tokens=0,
            reasoning_tokens=0,
        )
        if reply.failure is not None or reply.output is None:
            return LlmResult(None, reply.failure or "schema_invalid", usage, request.model)
        try:
            parsed = schema.model_validate_json(text)
        except ValidationError:
            return LlmResult(None, "schema_invalid", usage, request.model)
        return LlmResult(parsed, None, usage, request.model)
