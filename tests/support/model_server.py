"""A scripted Anthropic Messages API behind `httpx2.MockTransport`: the real SDK, no network.

Each answer is a function of the request body, served in order. `install` gives the scripted
transport to the two HTTP clients the SDK can use: the `DefaultHttpxClient` the recorder hands it
(llm/model_requests.py), with or without a ledger, and the one the SDK builds by itself if that
fails. The model clients build theirs exactly as in production, with the same event hooks; only
the transport changes. No client streams, so every answer is one JSON message.
"""

import json
import threading
from collections.abc import Callable, Sequence
from typing import Any

import httpx2
import pytest
from anthropic import DefaultHttpxClient, _base_client
from pydantic import BaseModel

from commerce_agent.llm import model_requests

_SDK_CLIENT = _base_client.SyncHttpxClientWrapper

Answer = Callable[[dict[str, Any]], httpx2.Response]
SentRequest = tuple[str, str, list[tuple[str, str]], bytes]

MODEL = "claude-haiku-5-5"
# The median HNP-02 call (run-traceability.md §4.4), as the Messages API counts it: the 1825
# input tokens are 801 uncached and 1024 read from the cache; 640 of the 786 output were thinking.
USAGE: dict[str, Any] = {
    "input_tokens": 801,
    "cache_read_input_tokens": 1024,
    "cache_creation_input_tokens": 0,
    "output_tokens": 786,
    "output_tokens_details": {"thinking_tokens": 640},
}


def _message(
    content: list[dict[str, Any]], usage: dict[str, Any], stop_reason: str = "end_turn"
) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": usage,
        },
    )


def thinking_block(summary: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": summary, "signature": "sig_test"}


def structured(
    answer: BaseModel | Callable[[dict[str, Any]], dict[str, Any]],
    *,
    usage: dict[str, Any] = USAGE,
    thinking: Sequence[str] = (),
) -> Answer:
    """A structured output: a model, or a function of the request body that builds it.

    `thinking` are the summaries of the thinking blocks that come before the answer.
    """

    def respond(body: dict[str, Any]) -> httpx2.Response:
        payload = answer(body) if callable(answer) else answer.model_dump(mode="json")
        text = {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
        return _message([*map(thinking_block, thinking), text], usage)

    return respond


def text(raw: str, *, stop_reason: str = "end_turn") -> Answer:
    """A text block as it comes, valid JSON or not; `max_tokens` cuts it, `refusal` refuses."""

    def respond(body: dict[str, Any]) -> httpx2.Response:
        return _message([{"type": "text", "text": raw}], USAGE, stop_reason=stop_reason)

    return respond


def function_call(name: str, arguments: dict[str, Any]) -> Answer:
    """A call of the client tool `name`, the answer's only block."""

    def respond(body: dict[str, Any]) -> httpx2.Response:
        call = {"type": "tool_use", "id": "toolu_test", "name": name, "input": arguments}
        return _message([call], USAGE, stop_reason="tool_use")

    return respond


def failure(
    status_code: int,
    *,
    code: str | None = None,
    error_type: str = "api_error",
    message: str = "scripted failure",
) -> Answer:
    """An error response with Anthropic's error body; `code` is its `error.details.error_code`.

    The tier's spent spend cap is `failure(429, code="enforced_spend_limit_reached",
    error_type="rate_limit_error")`; a spent credit balance is `failure(402,
    error_type="billing_error")`.
    """

    def respond(body: dict[str, Any]) -> httpx2.Response:
        error: dict[str, Any] = {"type": error_type, "message": message}
        if code is not None:
            error["details"] = {"error_code": code}
        # retry-after-ms keeps the SDK's single retry fast.
        return httpx2.Response(
            status_code,
            headers={"retry-after-ms": "1", "request-id": "req_test"},
            json={"type": "error", "error": error, "request_id": "req_test"},
        )

    return respond


def disconnect() -> Answer:
    """The connection drops before any response: the SDK sees `httpx2.ConnectError`."""

    def respond(body: dict[str, Any]) -> httpx2.Response:
        raise httpx2.ConnectError("scripted disconnect")

    return respond


class Held:
    """An answer the test releases (p0-ux08-ux10.md §4.1): `entered` is set when its request
    arrives, and the response waits for `release`. Meanwhile the test stops the chat, revokes or
    turns the kill switch on, so the moment the answer lands is the test's, not the scheduler's."""

    def __init__(self, answer: Answer, timeout: float) -> None:
        self._answer, self._timeout = answer, timeout
        self.entered = threading.Event()
        self._released = threading.Event()

    def __call__(self, body: dict[str, Any]) -> httpx2.Response:
        self.entered.set()
        if not self._released.wait(self._timeout):
            raise AssertionError("The held answer was never released")
        return self._answer(body)

    def release(self) -> None:
        self._released.set()


def held(answer: Answer, *, timeout: float = 10) -> Held:
    return Held(answer, timeout)


def output_schema(body: dict[str, Any]) -> dict[str, Any]:
    """The JSON schema the request asks the answer to follow."""
    return body["output_config"]["format"]["schema"]


def shortlisted_choice(body: dict[str, Any]) -> dict[str, Any]:
    """The first variant the request's JSON schema admits, with its one-sentence reason."""
    schema = output_schema(body)["properties"]["selected_variant_id"]
    variant_id = schema["enum"][0] if "enum" in schema else schema["anyOf"][0]["enum"][0]
    return {
        "action": "purchase",
        "assistant_message": f"Elijo {variant_id} porque es el más barato.",
        "selected_variant_id": variant_id,
    }


class ModelServer:
    def __init__(self, *answers: Answer) -> None:
        self._answers = list(answers)
        self.requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self._answers:
            raise AssertionError("The model server has no answer left")
        return self._answers.pop(0)(json.loads(request.content))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transport = httpx2.MockTransport(self.handle)

        def sdk_client(**options: Any) -> httpx2.Client:
            return _SDK_CLIENT(**options, transport=transport)

        def default_client(**options: Any) -> httpx2.Client:
            return DefaultHttpxClient(**options, transport=transport)

        monkeypatch.setattr(_base_client, "SyncHttpxClientWrapper", sdk_client)
        monkeypatch.setattr(model_requests, "DefaultHttpxClient", default_client)

    def sent(self) -> list[SentRequest]:
        """What went on the wire: method, URL, every header and the body bytes."""
        return [
            (item.method, str(item.url), list(item.headers.multi_items()), item.content)
            for item in self.requests
        ]

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(item.content) for item in self.requests]
