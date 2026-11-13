"""Claude through the Anthropic SDK: one structured call, or one Message Batch of them.

Both paths send the same Messages parameters: the instructions as the system prompt, one user
message, the effort, and the schema in ``output_config.format`` (``transform_schema`` adapts it
as ``messages.parse`` would). Thinking stays adaptive, the default on the 5.5 models. The answer
is validated here instead of by ``messages.parse``, which raises on an off-schema answer and
loses its usage; here it comes back as ``schema_invalid`` and its cost is counted.

The client never retries (``max_retries=0``), except when polling a batch: one HTTP request is
one observation and one ledger row, and the runner retries trials after infrastructure failures.
Errors follow the product's classification (src/commerce_agent/llm/model_failures.py): a spent
credit balance or spend cap is ``FundsExhausted``; every other failure is a ``TransportError``
named after the SDK's error class.
"""

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
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

from .batch import BatchReply
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
BATCH_POLL_SECONDS = 30.0


class AnthropicTransport:
    """``StructuredLLM`` over the Messages API."""

    transport_id: TransportId = "anthropic"

    def __init__(self, client: Anthropic):
        self._client = client.with_options(max_retries=0)

    def parse[T: BaseModel](self, request: LlmRequest, schema: type[T]) -> LlmResult[T]:
        with provider_errors():
            message = self._client.messages.create(
                **message_params(request, schema), timeout=request.timeout_seconds
            )
        return read_message(message, schema)


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
        self, requests: Mapping[str, LlmRequest], schema: type[T]
    ) -> BatchReply[T]:
        with provider_errors():
            batch = self._submit.messages.batches.create(
                requests=[
                    {"custom_id": custom_id, "params": message_params(request, schema)}
                    for custom_id, request in requests.items()
                ]
            )
            # Logged before polling, so a batch whose results are never collected can be found.
            _log.info("Message Batch %s submitted with %d requests", batch.id, len(requests))
            while batch.processing_status != "ended":
                self._wait(self._poll_seconds)
                batch = self._read.messages.batches.retrieve(batch.id)
            rows = {
                row.custom_id: row.result for row in self._read.messages.batches.results(batch.id)
            }
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


def read_message[T: BaseModel](message: Message, schema: type[T]) -> LlmResult[T]:
    usage = token_usage(message.usage)
    if message.stop_reason == "refusal":
        return LlmResult(None, "refusal", usage, message.model)
    if message.stop_reason in ("max_tokens", "model_context_window_exceeded"):
        return LlmResult(None, "incomplete", usage, message.model)
    text = next((block.text for block in message.content if block.type == "text"), None)
    if text is None:
        return LlmResult(None, "schema_invalid", usage, message.model)
    try:
        parsed = schema.model_validate_json(text)
    except ValidationError:
        return LlmResult(None, "schema_invalid", usage, message.model)
    return LlmResult(parsed, None, usage, message.model)


def token_usage(usage: Usage) -> TokenUsage:
    details = usage.output_tokens_details
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
        reasoning_tokens=0 if details is None else details.thinking_tokens,
    )


@contextmanager
def provider_errors() -> Iterator[None]:
    """Turns SDK errors into transport errors that carry a class name, never a body."""
    try:
        yield
    except APITimeoutError:
        raise TransportTimeout("APITimeoutError") from None
    except APIStatusError as error:
        if _spent(error):
            raise FundsExhausted("CreditBalanceExhausted") from None
        raise TransportError(type(error).__name__) from None
    except AnthropicError as error:
        raise TransportError(type(error).__name__) from None


def _spent(error: APIStatusError) -> bool:
    if error.status_code == 402 or error.type == "billing_error" or error.error_code == SPEND_CAP:
        return True
    message = (error.api_message or "").casefold()
    return error.status_code == 400 and message.startswith(SPENT_MESSAGES)


def _item[T: BaseModel](
    result: MessageBatchResult | None, schema: type[T]
) -> LlmResult[T] | TransportError:
    if result is None:
        return TransportError("BatchResultMissing")
    if result.type == "succeeded":
        return read_message(result.message, schema)
    if result.type == "errored":
        kind = result.error.error.type
        if kind == "billing_error":
            return FundsExhausted("CreditBalanceExhausted")
        return TransportError(f"BatchItemErrored:{kind}")
    return TransportError(f"BatchItem{result.type.capitalize()}")
