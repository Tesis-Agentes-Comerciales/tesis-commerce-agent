"""Structured-output transports: the port, a spending cap and an offline fake.

Contract shared by every transport:
- one ``parse`` call is one request and one observation: no retries, no fallback model;
- provider and network failures raise ``TransportError`` (infrastructure, never a verdict);
- an exceeded request timeout raises ``TransportTimeout`` (a ``TimeoutError``);
- answers that are refused, truncated or off-schema come back as ``failure`` with whatever
  usage the provider reported, so their cost is still counted.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock
from time import sleep
from typing import Literal, Protocol

from pydantic import BaseModel, ValidationError

from .cost import TokenPrices, TokenUsage, estimated_tokens

FailureKind = Literal["refusal", "incomplete", "schema_invalid"]
ReasoningEffort = Literal["minimal", "low", "medium", "high"]
TransportId = Literal["fake"]


class TransportError(RuntimeError):
    """Provider, network or budget failure: infrastructure, never an observation of the model.

    Carries the error class name, never response bodies.
    """


class TransportTimeout(TimeoutError):
    """The provider did not answer within the request timeout."""


@dataclass(frozen=True)
class LlmRequest:
    model: str
    instructions: str
    input_text: str
    temperature: float | None
    reasoning_effort: ReasoningEffort | None
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


class BudgetedTransport:
    """Refuses further provider calls once spend could pass the cap (fail closed).

    Each call is admitted only if the reported spend so far plus that call's worst case
    (estimated input, ``max_output_tokens`` output) stays within ``max_usd``. A refused call
    is an infrastructure error, so it never counts against the model.
    """

    def __init__(self, inner: StructuredLLM, *, prices: TokenPrices, max_usd: Decimal):
        self.transport_id: TransportId = inner.transport_id
        self._inner = inner
        self._prices = prices
        self._max_usd = max_usd
        self._lock = Lock()
        self.spent_usd = Decimal(0)

    def parse[T: BaseModel](self, request: LlmRequest, schema: type[T]) -> LlmResult[T]:
        worst = self._prices.cost(
            TokenUsage(
                input_tokens=estimated_tokens(request.instructions + request.input_text),
                output_tokens=request.max_output_tokens,
                cached_input_tokens=0,
                reasoning_tokens=0,
            )
        )
        with self._lock:
            if self.spent_usd + worst > self._max_usd:
                raise TransportError("BudgetExhausted")
            # Reserve the worst case until the provider reports actual usage.
            self.spent_usd += worst
        # A failed or timed-out call keeps its worst case reserved: it may have been billed.
        result = self._inner.parse(request, schema)
        if result.usage is not None:
            with self._lock:
                self.spent_usd += self._prices.cost(result.usage) - worst
        return result


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
            cached_input_tokens=0,
            reasoning_tokens=0,
        )
        if reply.failure is not None or reply.output is None:
            return LlmResult(None, reply.failure or "schema_invalid", usage, request.model)
        try:
            parsed = schema.model_validate_json(text)
        except ValidationError:
            return LlmResult(None, "schema_invalid", usage, request.model)
        return LlmResult(parsed, None, usage, request.model)
