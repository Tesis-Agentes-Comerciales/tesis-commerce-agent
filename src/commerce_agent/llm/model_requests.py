"""Record each request a model client sends, without changing it (run-traceability.md §4.4).

The callers of a model client name the run and the stage in a context variable, as
`llm/reasoning.py` does for summaries: `ConversationService` and the HNP executor name the run,
`TimedConversationRouter` and `ModelHnpSelector` the stage. The client wraps each SDK call in
`ModelRequestRecorder.request`, which writes one row once the call returns or raises.

The SDK retries a request once (`max_retries=1`) inside that call. The client gets the SDK's own
`DefaultHttpxClient` (its limits and redirects; the explicit timeout still applies per request)
with a response hook that stops the retry of a spent quota (llm/model_failures.py), with or
without a ledger. With a ledger, a request hook also counts the attempts by reading the SDK's
`x-stainless-retry-count` header. That hook only reads, and it never raises: httpx would turn an
exception there into a failed attempt that the SDK retries. A failed call's row keeps the HTTP
status and the provider's error code next to the SDK's error class (p0-model-failures.md §3.3).
Recording is fail-open throughout: an error is logged and the row dropped, never the request.
"""

import hashlib
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter_ns
from typing import Any, Protocol

import httpx
from openai import DefaultHttpxClient
from pydantic import BaseModel

from commerce_agent.llm.model_failures import classify_model_failure, refuse_quota_retry
from commerce_agent.persistence.model_requests_sqlite import ModelRequestRow

_log = logging.getLogger(__name__)

RETRY_HEADER = "x-stainless-retry-count"


class ModelRequestSink(Protocol):
    def record(self, row: ModelRequestRow) -> None: ...


@dataclass
class _Scope:
    run_id: str | None
    stage: str | None
    requests: int = 0


_scope: ContextVar[_Scope | None] = ContextVar("model_request_scope", default=None)


@contextmanager
def model_request_scope(*, run_id: str | None = None, stage: str | None = None) -> Iterator[None]:
    """Name the run or the stage of the model requests made in this context.

    What is not given is inherited from the enclosing scope; the request count starts again.
    """
    outer = _scope.get() or _Scope(run_id=None, stage=None)
    token = _scope.set(_Scope(run_id=run_id or outer.run_id, stage=stage or outer.stage))
    try:
        yield
    finally:
        _scope.reset(token)


@dataclass
class PendingRequest:
    """One SDK call being recorded. The client hands it the response the SDK returned."""

    instructions: str = ""
    model_input: object = None
    text_format: type[BaseModel] | None = None
    catalog_vocabulary: str | None = None
    response: object = None
    run_id: str | None = None
    stage: str | None = None
    request_index: int = 1
    # One entry per HTTP attempt, kept per call: the router is shared between threads.
    retry_counts: list[int] = field(default_factory=list)
    started_at: str = ""
    started_ns: int = 0


_pending: ContextVar[PendingRequest | None] = ContextVar("model_request_pending", default=None)


def _retry_count(request: httpx.Request) -> int:
    return int(request.headers.get(RETRY_HEADER, "0"))


def _observe_attempt(request: httpx.Request) -> None:
    try:
        pending = _pending.get()
        if pending is not None:
            pending.retry_counts.append(_retry_count(request))
    except Exception as error:
        _log.warning("Model request attempt not observed: %s", type(error).__name__)


def _sha256(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _input_json(model_input: object) -> str:
    """The request's `input`: the conversation as a JSON array, or the JSON payload text."""
    if isinstance(model_input, str):
        try:
            json.loads(model_input)
        except ValueError:
            return json.dumps(model_input, ensure_ascii=False)
        return model_input
    return json.dumps(model_input, ensure_ascii=False)


def _output_json(response: object) -> str:
    """The structured output and the tool calls of a response."""
    parsed = getattr(response, "output_parsed", None)
    calls = [
        {"name": item.name, "arguments": item.arguments}
        for item in getattr(response, "output", None) or ()
        if getattr(item, "type", None) == "function_call"
    ]
    structured = parsed.model_dump(mode="json") if isinstance(parsed, BaseModel) else None
    return json.dumps({"structured": structured, "function_calls": calls}, ensure_ascii=False)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _count(source: object, name: str) -> int | None:
    value = getattr(source, name, None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


class ModelRequestRecorder:
    """Records the requests of one model client; without a sink it does nothing at all."""

    def __init__(
        self, sink: ModelRequestSink | None, *, model: str, reasoning_effort: str | None
    ) -> None:
        self._sink, self._model, self._reasoning_effort = sink, model, reasoning_effort

    def http_client(self) -> httpx.Client | None:
        """The HTTP client to give the SDK. None, when it cannot be built, lets the SDK build its
        own: the call still goes out, without the quota hook or the attempt count."""
        hooks: dict[str, list[Callable[..., Any]]] = {"response": [refuse_quota_retry]}
        if self._sink is not None:
            hooks["request"] = [_observe_attempt]
        try:
            return DefaultHttpxClient(event_hooks=hooks)
        except Exception as error:
            _log.warning("Model HTTP client not built: %s", type(error).__name__)
            return None

    @contextmanager
    def request(
        self,
        *,
        instructions: str,
        model_input: object,
        text_format: type[BaseModel] | None = None,
        catalog_vocabulary: str | None = None,
    ) -> Iterator[PendingRequest]:
        """Wrap one SDK call; the caller sets `response` on what this yields."""
        if self._sink is None:
            yield PendingRequest()
            return
        pending = self._open(instructions, model_input, text_format, catalog_vocabulary)
        token = _pending.set(pending)
        try:
            yield pending
        except Exception as error:
            self._close(pending, error)
            raise
        else:
            self._close(pending, None)
        finally:
            _pending.reset(token)

    def _open(
        self,
        instructions: str,
        model_input: object,
        text_format: type[BaseModel] | None,
        catalog_vocabulary: str | None,
    ) -> PendingRequest:
        pending = PendingRequest(
            instructions=instructions,
            model_input=model_input,
            text_format=text_format,
            catalog_vocabulary=catalog_vocabulary,
        )
        try:
            scope = _scope.get() or _Scope(run_id=None, stage=None)
            scope.requests += 1
            pending.run_id, pending.stage = scope.run_id, scope.stage
            pending.request_index = scope.requests
        except Exception as error:
            _log.warning("Model request scope not read: %s", type(error).__name__)
        pending.started_at = datetime.now(UTC).isoformat()
        pending.started_ns = perf_counter_ns()
        return pending

    def _close(self, pending: PendingRequest, error: Exception | None) -> None:
        """Write the row after the call; the input is serialized only now, off the request path."""
        latency_ms = (perf_counter_ns() - pending.started_ns) / 1e6
        try:
            response = pending.response if error is None else None
            failure = None if error is None else classify_model_failure(error)
            usage = getattr(response, "usage", None)
            input_details = getattr(usage, "input_tokens_details", None)
            output_details = getattr(usage, "output_tokens_details", None)
            row = ModelRequestRow(
                run_id=pending.run_id,
                stage=pending.stage,
                request_index=pending.request_index,
                started_at=pending.started_at,
                latency_ms=latency_ms,
                outcome="ok" if error is None else "error",
                error_kind=None if error is None else type(error).__name__,
                model=self._model,
                response_model=_text(getattr(response, "model", None)),
                reasoning_effort=self._reasoning_effort,
                text_format=None if pending.text_format is None else pending.text_format.__name__,
                input_tokens=_count(usage, "input_tokens"),
                cached_input_tokens=_count(input_details, "cached_tokens"),
                cache_write_tokens=_count(input_details, "cache_write_tokens"),
                output_tokens=_count(usage, "output_tokens"),
                reasoning_tokens=_count(output_details, "reasoning_tokens"),
                retries=max(pending.retry_counts) if pending.retry_counts else None,
                request_sha256=_sha256(
                    {"instructions": pending.instructions, "input": pending.model_input}
                ),
                instructions_sha256=_sha256(pending.instructions),
                catalog_vocabulary=pending.catalog_vocabulary,
                input_json=_input_json(pending.model_input),
                output_json=None if response is None else _output_json(response),
                http_status=None if failure is None else failure.http_status,
                provider_error_code=None if failure is None else failure.provider_error_code,
            )
            if self._sink is not None:  # always, once a request is open
                self._sink.record(row)
        except Exception as failure:
            _log.warning(
                "Model request not recorded: %s (%s)", pending.stage, type(failure).__name__
            )
