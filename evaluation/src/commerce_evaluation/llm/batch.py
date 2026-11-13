"""Structured calls in one Message Batch, for the judge (docs/design/eval-suite.md §3).

``parse_batch`` takes the requests keyed by ``custom_id`` and returns one entry per key: the
parsed result, or a ``TransportError`` when that item errored, expired, was canceled or never
came back. Those are infrastructure, never a verdict. Results are matched by ``custom_id``,
never by position. A batch is priced at the batch rates and reserved whole before it is sent.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import count
from time import monotonic
from typing import Protocol

from pydantic import BaseModel

from .budget import Call, Role, RunBudget
from .transport import FakeTransport, LlmRequest, LlmResult, TransportError, TransportId


@dataclass(frozen=True)
class BatchReply[T: BaseModel]:
    batch_id: str
    results: Mapping[str, LlmResult[T] | TransportError]


class StructuredBatchLLM(Protocol):
    transport_id: TransportId

    def parse_batch[T: BaseModel](
        self, requests: Mapping[str, LlmRequest], schema: type[T]
    ) -> BatchReply[T]: ...


class FakeBatchTransport:
    """Answers each item with a ``FakeTransport``; a scripted ``TransportError`` errors the item."""

    transport_id: TransportId = "fake"

    def __init__(self, inner: FakeTransport):
        self._inner = inner
        self._ids = count(1)

    def parse_batch[T: BaseModel](
        self, requests: Mapping[str, LlmRequest], schema: type[T]
    ) -> BatchReply[T]:
        results: dict[str, LlmResult[T] | TransportError] = {}
        for custom_id, request in requests.items():
            try:
                results[custom_id] = self._inner.parse(request, schema)
            except TransportError as error:
                results[custom_id] = error
        return BatchReply(f"fake_batch_{next(self._ids)}", results)


class BudgetedBatchTransport:
    """A ``StructuredBatchLLM`` whose batches are capped and recorded item by item."""

    def __init__(self, inner: StructuredBatchLLM, *, budget: RunBudget, role: Role):
        self.transport_id: TransportId = inner.transport_id
        self._inner = inner
        self._budget = budget
        self._role: Role = role

    def parse_batch[T: BaseModel](
        self, requests: Mapping[str, LlmRequest], schema: type[T]
    ) -> BatchReply[T]:
        calls = {
            custom_id: Call(self._role, self.transport_id, "batch", request, custom_id)
            for custom_id, request in requests.items()
        }
        reserved = dict(zip(calls, self._budget.reserve(list(calls.values())), strict=True))
        started = monotonic()
        try:
            reply = self._inner.parse_batch(requests, schema)
        except Exception as error:
            latency = monotonic() - started
            for custom_id, call in calls.items():
                self._budget.settle(call, reserved[custom_id], error, latency_seconds=latency)
            raise
        latency = monotonic() - started
        for custom_id, call in calls.items():
            outcome = reply.results.get(custom_id, TransportError("BatchResultMissing"))
            self._budget.settle(
                call, reserved[custom_id], outcome, latency_seconds=latency, batch_id=reply.batch_id
            )
        return reply
