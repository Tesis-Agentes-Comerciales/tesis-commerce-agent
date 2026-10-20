"""Structured-output transports. Only OpenAIResponsesTransport can reach a provider.

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
from typing import Any, Literal, Protocol

import httpx
from openai import APITimeoutError, OpenAI, OpenAIError
from openai.types.shared_params import Reasoning
from pydantic import BaseModel, SecretStr, ValidationError

from ..ports import InfrastructureError
from .config import ReasoningEffort
from .cost import TokenPrices, estimated_tokens
from .trace import TokenUsage, TransportId

FailureKind = Literal["refusal", "incomplete", "schema_invalid"]


class TransportError(InfrastructureError):
    """Provider or network failure. Carries the error class name, never response bodies."""


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


def _int(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def usage_from_body(body: object) -> TokenUsage | None:
    """Usage as reported in the provider's JSON body, read before any output parsing."""
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    inputs = usage.get("input_tokens_details")
    outputs = usage.get("output_tokens_details")
    return TokenUsage(
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cached_input_tokens=_int(inputs.get("cached_tokens")) if isinstance(inputs, dict) else 0,
        reasoning_tokens=_int(outputs.get("reasoning_tokens")) if isinstance(outputs, dict) else 0,
    )


class OpenAIResponsesTransport:
    """OpenAI Responses API with structured outputs, as in the conversation router."""

    transport_id: TransportId = "openai-responses"

    def __init__(self, *, api_key: SecretStr, http_client: httpx.Client | None = None) -> None:
        # max_retries=0: an SDK retry would be a second, uncounted observation and would
        # stretch the deadline. http_client exists for offline tests with a mock transport.
        self._client = OpenAI(
            api_key=api_key.get_secret_value(), max_retries=0, http_client=http_client
        )

    def parse[T: BaseModel](self, request: LlmRequest, schema: type[T]) -> LlmResult[T]:
        options: dict[str, Any] = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.reasoning_effort is not None:
            options["reasoning"] = Reasoning(effort=request.reasoning_effort)
        try:
            raw = self._client.responses.with_raw_response.parse(
                model=request.model,
                instructions=request.instructions,
                input=request.input_text,
                text_format=schema,
                store=False,
                max_output_tokens=request.max_output_tokens,
                timeout=request.timeout_seconds,
                **options,
            )
        except APITimeoutError:
            raise TransportTimeout("Provider request timed out") from None
        except OpenAIError as error:
            raise TransportError(type(error).__name__) from None
        try:
            body = raw.http_response.json()
        except ValueError:
            body = None
        usage = usage_from_body(body)
        model = body.get("model") if isinstance(body, dict) else None
        response_model = model if isinstance(model, str) else None
        try:
            response = raw.parse()
        except ValidationError:
            # Typically JSON cut by max_output_tokens: the model's answer, not the network.
            return LlmResult(None, "schema_invalid", usage, response_model)
        except OpenAIError as error:
            raise TransportError(type(error).__name__) from None
        if any(
            getattr(part, "type", None) == "refusal"
            for item in response.output
            for part in getattr(item, "content", ())
        ):
            return LlmResult(None, "refusal", usage, response_model)
        if response.status == "incomplete":
            return LlmResult(None, "incomplete", usage, response_model)
        parsed = response.output_parsed
        if parsed is None:
            return LlmResult(None, "schema_invalid", usage, response_model)
        return LlmResult(parsed, None, usage, response_model)

    def close(self) -> None:
        self._client.close()


class BudgetedTransport:
    """Refuses further provider calls once spend could pass the cap (fail closed).

    Each call is admitted only if the reported spend so far plus that call's worst case
    (estimated input, ``max_output_tokens`` output) stays within ``max_usd``. A refused call
    is an infrastructure error, so the episode is blocked and excluded from the metrics.
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
    """Deterministic offline transport for tests and --dry-run. It opens no connection.

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
