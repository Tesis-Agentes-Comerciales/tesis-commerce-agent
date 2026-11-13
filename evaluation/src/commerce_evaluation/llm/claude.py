"""Claude through the Anthropic SDK: one structured call, or one Message Batch of them.

Both paths send the same Messages parameters: the instructions as the system prompt, one user
message, the effort, and the schema in ``output_config.format`` (``transform_schema`` adapts it
as ``messages.parse`` would). Thinking stays adaptive, the default on the 5.5 models. The answer
is validated here instead of by ``messages.parse``, which raises on an off-schema answer and
loses its usage; here it comes back as ``schema_invalid`` and its cost is counted.

The SDK never retries (``max_retries=0``): a hidden retry would be a request without a ledger
row. ``AnthropicTransport`` sends a request again itself, at most twice and after its
``retry-after``, only when the API turned it away unprocessed: a 429 ``rate_limit_error`` or a
529 ``overloaded_error`` that is not a spent balance. The call keeps one reservation and one
ledger row, with its ``attempts``. Creating a batch is never retried; polling one is.

Errors follow the product's classification (src/commerce_agent/llm/model_failures.py): a spent
credit balance or spend cap is ``FundsExhausted``, for a response and a batch item alike; every
other failure is a ``TransportError`` named after the SDK's error class. Usage the price table
does not cover (1-hour cache writes, US-only inference at 1.1x) fails closed as
``TransportError("UnpricedUsage")``.
"""

import logging
from collections.abc import Callable, Mapping
from math import isfinite
from time import sleep

from anthropic import (
    Anthropic,
    AnthropicError,
    APIStatusError,
    APITimeoutError,
    transform_schema,
)
from anthropic.types import Message, OutputConfigParam, Usage
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages import MessageBatchResult
from pydantic import BaseModel, ValidationError

from .batch import BatchReply, Submitted, check_custom_ids
from .cost import TokenUsage
from .transport import (
    FundsExhausted,
    LlmRequest,
    LlmResult,
    TransportError,
    TransportId,
    TransportTimeout,
)

_log = logging.getLogger(__name__)

# Anthropic's errors and rate-limits docs: the tier's monthly spend cap, and the start of the
# two 400s that name a spent balance in their message alone.
SPEND_CAP = "enforced_spend_limit_reached"
SPENT_MESSAGES = ("your credit balance is too low", "you have reached your specified")
# Turned away before any processing, so sending it again bills nothing twice.
BUSY = ("rate_limit_error", "overloaded_error")
MAX_ATTEMPTS = 3
DEFAULT_RETRY_SECONDS = 5.0
MAX_RETRY_SECONDS = 60.0
BATCH_POLL_SECONDS = 30.0


class AnthropicTransport:
    """``StructuredLLM`` over the Messages API."""

    transport_id: TransportId = "anthropic"

    def __init__(self, client: Anthropic, *, wait: Callable[[float], None] = sleep):
        self._client = client.with_options(max_retries=0)
        self._wait = wait

    def parse[T: BaseModel](self, request: LlmRequest, schema: type[T]) -> LlmResult[T]:
        params = message_params(request, schema)
        attempt = 1
        while True:
            try:
                message = self._client.messages.create(**params, timeout=request.timeout_seconds)
            except APIStatusError as error:
                if attempt == MAX_ATTEMPTS or not _busy(error):
                    raise transport_error(error, attempts=attempt) from None
                self._wait(_retry_after(error))
                attempt += 1
            except AnthropicError as error:
                raise transport_error(error, attempts=attempt) from None
            else:
                return read_message(message, schema, attempts=attempt)


class AnthropicBatchTransport:
    """``StructuredBatchLLM`` over Message Batches: submit, poll until ended, collect."""

    transport_id: TransportId = "anthropic"

    def __init__(
        self,
        client: Anthropic,
        *,
        poll_seconds: float = BATCH_POLL_SECONDS,
        wait: Callable[[float], None] = sleep,
    ):
        # Creating a batch is not idempotent, so only reads are retried.
        self._submit = client.with_options(max_retries=0)
        self._read = client
        self._poll_seconds = poll_seconds
        self._wait = wait

    def parse_batch[T: BaseModel](
        self,
        requests: Mapping[str, LlmRequest],
        schema: type[T],
        *,
        on_submitted: Submitted | None = None,
    ) -> BatchReply[T]:
        check_custom_ids(requests)
        try:
            batch = self._submit.messages.batches.create(
                requests=[
                    {"custom_id": custom_id, "params": message_params(request, schema)}
                    for custom_id, request in requests.items()
                ]
            )
            _log.info("Message Batch %s submitted with %d requests", batch.id, len(requests))
            if on_submitted is not None:
                on_submitted(batch.id)
            while batch.processing_status != "ended":
                self._wait(self._poll_seconds)
                batch = self._read.messages.batches.retrieve(batch.id)
            rows = {
                row.custom_id: row.result for row in self._read.messages.batches.results(batch.id)
            }
        except AnthropicError as error:
            raise transport_error(error) from None
        return BatchReply(
            batch.id, {custom_id: _item(rows.get(custom_id), schema) for custom_id in requests}
        )


def message_params(request: LlmRequest, schema: type[BaseModel]) -> MessageCreateParamsNonStreaming:
    output_config: OutputConfigParam = {
        "format": {"type": "json_schema", "schema": transform_schema(schema)}
    }
    if request.effort is not None:
        output_config["effort"] = request.effort
    return {
        "model": request.model,
        "max_tokens": request.max_output_tokens,
        "system": request.instructions,
        "messages": [{"role": "user", "content": request.input_text}],
        "output_config": output_config,
    }


def read_message[T: BaseModel](
    message: Message, schema: type[T], *, attempts: int = 1
) -> LlmResult[T]:
    usage = token_usage(message.usage)
    if message.stop_reason == "refusal":
        return LlmResult(None, "refusal", usage, message.model, attempts)
    if message.stop_reason in ("max_tokens", "model_context_window_exceeded"):
        return LlmResult(None, "incomplete", usage, message.model, attempts)
    text = next((block.text for block in message.content if block.type == "text"), None)
    if text is None:
        return LlmResult(None, "schema_invalid", usage, message.model, attempts)
    try:
        parsed = schema.model_validate_json(text)
    except ValidationError:
        return LlmResult(None, "schema_invalid", usage, message.model, attempts)
    return LlmResult(parsed, None, usage, message.model, attempts)


def token_usage(usage: Usage) -> TokenUsage:
    one_hour_writes = usage.cache_creation.ephemeral_1h_input_tokens if usage.cache_creation else 0
    if one_hour_writes or usage.inference_geo == "us":
        raise TransportError("UnpricedUsage")
    details = usage.output_tokens_details
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
        reasoning_tokens=0 if details is None else details.thinking_tokens,
    )


def transport_error(
    error: AnthropicError, *, attempts: int = 1
) -> TransportError | TransportTimeout:
    """The SDK error as a transport error: its class name, never its body."""
    if isinstance(error, APITimeoutError):
        return TransportTimeout("APITimeoutError")
    if isinstance(error, APIStatusError) and _spent(
        error.status_code, error.type, error.error_code, error.api_message
    ):
        return FundsExhausted("CreditBalanceExhausted", attempts=attempts)
    return TransportError(type(error).__name__, attempts=attempts)


def _spent(status: int | None, kind: str | None, code: str | None, message: str | None) -> bool:
    """A spent credit balance or spend cap, from a response or from a batch item's error."""
    if status == 402 or kind == "billing_error" or code == SPEND_CAP:
        return True
    rejected = status == 400 or kind == "invalid_request_error"
    return rejected and (message or "").casefold().startswith(SPENT_MESSAGES)


def _busy(error: APIStatusError) -> bool:
    spent = _spent(error.status_code, error.type, error.error_code, error.api_message)
    return error.type in BUSY and not spent


def _retry_after(error: APIStatusError) -> float:
    try:
        seconds = float(error.response.headers.get("retry-after", ""))
    except ValueError:
        return DEFAULT_RETRY_SECONDS
    return min(max(seconds, 0.0), MAX_RETRY_SECONDS) if isfinite(seconds) else DEFAULT_RETRY_SECONDS


def _item[T: BaseModel](
    result: MessageBatchResult | None, schema: type[T]
) -> LlmResult[T] | TransportError:
    if result is None:
        return TransportError("BatchResultMissing")
    if result.type == "succeeded":
        try:
            return read_message(result.message, schema)
        except TransportError as error:
            return error
    if result.type == "errored":
        error = result.error.error
        if _spent(None, error.type, _detail_code(error), error.message):
            return FundsExhausted("CreditBalanceExhausted")
        return TransportError(f"BatchItemErrored:{error.type}")
    return TransportError(f"BatchItem{result.type.capitalize()}")


def _detail_code(error: object) -> str | None:
    # `details` is not a declared field of a batch item's error; the SDK keeps it as sent.
    details = getattr(error, "details", None)
    code = details.get("error_code") if isinstance(details, dict) else None
    return code if isinstance(code, str) else None
