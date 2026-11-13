"""What the three model clients share: one Anthropic client and its structured request.

The HP router, the HNP selector and the HNP reviewer each ask for a structured output
(docs/design/eval-suite.md §6). `ClaudeClient` builds the SDK client with the recorder's HTTP
client (llm/model_requests.py), sends the request with its effort, validates the answer against
the response model, records it and turns any SDK or validation error into an
`IntentInterpretationError` with its failure kind (llm/model_failures.py).

- **Schema.** Every property of the response model is required in the schema sent, as the earlier
  provider's strict mode did: the API admits at most 24 optional properties per request, and the
  router's decision alone has more. A field with a default is then always written, often as null
  or []. `messages.parse` builds its schema from the model, optional fields and all, so the
  clients send theirs in `output_config.format` with `messages.create` (the SDK's way for a schema
  of one's own) and validate the answer against the model themselves, as `parse` would.
- **A schema never goes with a tool the model may call.** The API compiles the output schema and
  the tools the model may call into one grammar, and it rejects the router's decision schema next
  to `lookup_catalog` with `tool_choice` auto (400 `invalid_request_error`, "The compiled grammar
  is too large"; seen on Haiku 5.5 on 13-nov-2026, with or without `strict`). So `offer_tools`
  lets the model call a tool and asks for no schema, and `parse` sends any tools with
  `tool_choice` none: the model sees them, and the prefix stays the one `offer_tools` sent, but it
  cannot call them. The router makes the two requests of a turn this way (conversation_router.py).
- **Cache.** The instructions go in `system` with a cache breakpoint, so the stable prefix (the
  tools and the instructions) is read from the cache on the next request that repeats it; a
  different `tool_choice` leaves that prefix cached. Below the model's minimum (512 tokens on
  Haiku 5.5 and Sonnet 5.5) the API skips it silently, at no cost.
- **Thinking.** Haiku 5.5 and Sonnet 5.5 think adaptively by default and return the thinking empty
  unless `display: "summarized"` is asked for, which only the router does, for its summaries.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic, AnthropicError, transform_schema
from anthropic.types import Message, MessageParam, OutputConfigParam, ToolChoiceParam, ToolParam
from pydantic import BaseModel, SecretStr, ValidationError

from commerce_agent.config import ModelEffort
from commerce_agent.llm.model_failures import IntentInterpretationError, classify_model_failure
from commerce_agent.llm.model_requests import ModelRequestRecorder, ModelRequestSink

# Room for the adaptive thinking and the answer: the docs' default for a request that is not
# streamed. Only the tokens produced are billed.
MAX_TOKENS = 16_000


def _all_required(node: Any) -> Any:
    if isinstance(node, list):
        return [_all_required(item) for item in node]
    if not isinstance(node, dict):
        return node
    copy = {key: _all_required(value) for key, value in node.items()}
    if copy.get("type") == "object" and isinstance(copy.get("properties"), dict):
        copy["required"] = list(copy["properties"])
    return copy


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """`model`'s JSON schema with every property required, as the API admits it."""
    return transform_schema(_all_required(model.model_json_schema()))


@dataclass(frozen=True)
class Reply[ParsedT: BaseModel]:
    message: Message
    # The first text block validated against the response model; None without a text block.
    parsed: ParsedT | None


class ClaudeClient:
    """One Anthropic client with its recorder; the SDK retries a failed request once."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        effort: ModelEffort,
        timeout_seconds: float,
        model_requests: ModelRequestSink | None,
    ) -> None:
        if not model.strip():
            raise ValueError("The model must not be empty")
        self.model = model
        self.effort: ModelEffort = effort
        # Without a ledger (studio, tests) nothing is recorded, but the SDK still gets the
        # recorder's client, with its hook that refuses to retry a spent balance.
        self._recorder = ModelRequestRecorder(model_requests, model=model, reasoning_effort=effort)
        self._client = Anthropic(
            api_key=api_key.get_secret_value(),
            timeout=timeout_seconds,
            max_retries=1,
            http_client=self._recorder.http_client(),
        )

    def parse[ParsedT: BaseModel](
        self,
        *,
        instructions: str,
        model_input: str | list[MessageParam],
        output: type[ParsedT],
        failure: str,
        catalog_vocabulary: str | None = None,
        tools: list[ToolParam] | None = None,
        summarize_thinking: bool = False,
    ) -> Reply[ParsedT]:
        """One structured request. A text `model_input` is the JSON payload of one user message.

        `tools` go with `tool_choice` none: the model sees them but cannot call them. The ledger
        keeps `model_input` as given: the conversation, or the payload text.
        """

        def validate(message: Message) -> ParsedT | None:
            # A tool call is the answer: any text before it is not the structured output.
            if any(block.type == "tool_use" for block in message.content):
                return None
            texts = [block.text for block in message.content if block.type == "text"]
            # A cut or refused answer fails here as one outside the schema.
            return output.model_validate_json(texts[0]) if texts else None

        message, parsed = self._send(
            instructions=instructions,
            model_input=model_input,
            failure=failure,
            catalog_vocabulary=catalog_vocabulary,
            tools=tools,
            tool_choice={"type": "none"} if tools else None,
            output=output,
            validate=validate,
            summarize_thinking=summarize_thinking,
        )
        return Reply(message, parsed)

    def offer_tools(
        self,
        *,
        instructions: str,
        model_input: list[MessageParam],
        tools: list[ToolParam],
        failure: str,
        catalog_vocabulary: str | None = None,
        summarize_thinking: bool = False,
    ) -> Message:
        """One request in which the model may call one of `tools`, once, and that asks for no
        output schema: its answer is a tool call or text that no schema constrains."""
        message, _ = self._send(
            instructions=instructions,
            model_input=model_input,
            failure=failure,
            catalog_vocabulary=catalog_vocabulary,
            tools=tools,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            output=None,
            validate=lambda message: None,
            summarize_thinking=summarize_thinking,
        )
        return message

    def _send[ParsedT: BaseModel](
        self,
        *,
        instructions: str,
        model_input: str | list[MessageParam],
        failure: str,
        catalog_vocabulary: str | None,
        tools: list[ToolParam] | None,
        tool_choice: ToolChoiceParam | None,
        output: type[ParsedT] | None,
        validate: Callable[[Message], ParsedT | None],
        summarize_thinking: bool,
    ) -> tuple[Message, ParsedT | None]:
        messages: list[MessageParam] = (
            [{"role": "user", "content": model_input}]
            if isinstance(model_input, str)
            else model_input
        )
        options: dict[str, Any] = {}
        if tools:
            options["tools"] = tools
        if tool_choice is not None:
            options["tool_choice"] = tool_choice
        if summarize_thinking:
            options["thinking"] = {"type": "adaptive", "display": "summarized"}
        output_config: OutputConfigParam = {"effort": self.effort}
        if output is not None:
            output_config["format"] = {"type": "json_schema", "schema": strict_schema(output)}
        try:
            with self._recorder.request(
                instructions=instructions,
                model_input=model_input,
                text_format=output,
                catalog_vocabulary=catalog_vocabulary,
            ) as pending:
                message = self._client.messages.create(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=[
                        {
                            "type": "text",
                            "text": instructions,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=messages,
                    output_config=output_config,
                    **options,
                )
                pending.response = message
                parsed = validate(message)
                pending.parsed = parsed
        except (AnthropicError, ValidationError) as error:
            raise IntentInterpretationError(
                failure, failure_kind=classify_model_failure(error).kind
            ) from error
        return message, parsed

    def close(self) -> None:
        self._client.close()


def content_params(message: Message) -> list[dict[str, Any]]:
    """A response's blocks as request input, thinking included, unchanged, for the next turn."""
    return [json.loads(block.to_json(exclude_none=True)) for block in message.content]
