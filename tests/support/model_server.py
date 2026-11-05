"""A scripted OpenAI Responses API behind `httpx.MockTransport`: the real SDK, no network.

Each answer is a function of the request body, served in order. `install` gives the scripted
transport to the two HTTP clients the SDK can use: the `DefaultHttpxClient` the recorder hands it
(llm/model_requests.py), with or without a ledger, and the one the SDK builds by itself if that
fails. The model clients build theirs exactly as in production, with the same event hooks; only
the transport changes.
"""

import json
import threading
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from openai import DefaultHttpxClient, _base_client
from pydantic import BaseModel

from commerce_agent.llm import model_requests

_SDK_CLIENT = _base_client.SyncHttpxClientWrapper

Answer = Callable[[dict[str, Any]], httpx.Response]
SentRequest = tuple[str, str, list[tuple[str, str]], bytes]

# The median HNP-02 call of gpt-5-mini (run-traceability.md §4.4).
USAGE: dict[str, Any] = {
    "input_tokens": 1825,
    "input_tokens_details": {"cached_tokens": 1024, "cache_write_tokens": 0},
    "output_tokens": 786,
    "output_tokens_details": {"reasoning_tokens": 640},
    "total_tokens": 2611,
}


def _response(output: list[dict[str, Any]], usage: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 0,
        "model": "test-model-2026-08-07",
        "output": output,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "status": "completed",
        "usage": usage,
    }


def _message(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_test",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _serve(completed: dict[str, Any], body: dict[str, Any]) -> httpx.Response:
    """The response as JSON, or as server-sent events when the request asked for a stream."""
    if not body.get("stream"):
        return httpx.Response(200, json=completed)
    created = {**completed, "output": [], "status": "in_progress", "usage": None}
    events = (
        {"type": "response.created", "sequence_number": 0, "response": created},
        {"type": "response.completed", "sequence_number": 1, "response": completed},
    )
    sse = "".join(f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)


def structured(
    answer: BaseModel | Callable[[dict[str, Any]], dict[str, Any]],
    *,
    usage: dict[str, Any] | None = USAGE,
) -> Answer:
    """A structured output: a model, or a function of the request body that builds it."""

    def respond(body: dict[str, Any]) -> httpx.Response:
        payload = answer(body) if callable(answer) else answer.model_dump(mode="json")
        text = json.dumps(payload, ensure_ascii=False)
        return _serve(_response([_message(text)], usage), body)

    return respond


def function_call(name: str, arguments: dict[str, Any]) -> Answer:
    def respond(body: dict[str, Any]) -> httpx.Response:
        call = {
            "type": "function_call",
            "id": "fc_test",
            "call_id": "call_test",
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
            "status": "completed",
        }
        return _serve(_response([call], USAGE), body)

    return respond


def failure(status_code: int, *, code: str | None = None, error_type: str = "test") -> Answer:
    """An error response with OpenAI's error body; `code` is the provider's error code.

    A spent quota is `failure(429, code="insufficient_quota")`; OpenAI also sends it as the type.
    """

    def respond(body: dict[str, Any]) -> httpx.Response:
        error: dict[str, Any] = {"message": "scripted failure", "type": error_type}
        if code is not None:
            error |= {"param": None, "code": code}
        # retry-after-ms keeps the SDK's single retry fast.
        return httpx.Response(status_code, headers={"retry-after-ms": "1"}, json={"error": error})

    return respond


def disconnect() -> Answer:
    """The connection drops before any response: the SDK sees `httpx.ConnectError`."""

    def respond(body: dict[str, Any]) -> httpx.Response:
        raise httpx.ConnectError("scripted disconnect")

    return respond


class Held:
    """An answer the test releases (p0-ux08-ux10.md §4.1): `entered` is set when its request
    arrives, and the response waits for `release`. Meanwhile the test stops the chat, revokes or
    turns the kill switch on, so the moment the answer lands is the test's, not the scheduler's."""

    def __init__(self, answer: Answer, timeout: float) -> None:
        self._answer, self._timeout = answer, timeout
        self.entered = threading.Event()
        self._released = threading.Event()

    def __call__(self, body: dict[str, Any]) -> httpx.Response:
        self.entered.set()
        if not self._released.wait(self._timeout):
            raise AssertionError("The held answer was never released")
        return self._answer(body)

    def release(self) -> None:
        self._released.set()


def held(answer: Answer, *, timeout: float = 10) -> Held:
    return Held(answer, timeout)


def shortlisted_choice(body: dict[str, Any]) -> dict[str, Any]:
    """The first variant the request's JSON schema admits, with its one-sentence reason."""
    schema = body["text"]["format"]["schema"]["properties"]["selected_variant_id"]
    variant_id = schema["enum"][0] if "enum" in schema else schema["anyOf"][0]["enum"][0]
    return {
        "action": "purchase",
        "assistant_message": f"Elijo {variant_id} porque es el más barato.",
        "selected_variant_id": variant_id,
    }


class ModelServer:
    def __init__(self, *answers: Answer) -> None:
        self._answers = list(answers)
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._answers:
            raise AssertionError("The model server has no answer left")
        return self._answers.pop(0)(json.loads(request.content))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transport = httpx.MockTransport(self.handle)

        def sdk_client(**options: Any) -> httpx.Client:
            return _SDK_CLIENT(**options, transport=transport)

        def default_client(**options: Any) -> httpx.Client:
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
