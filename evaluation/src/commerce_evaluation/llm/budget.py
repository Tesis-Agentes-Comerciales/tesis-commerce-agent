"""The run's spending: one ``--max-usd`` cap and one usage ledger for every LLM call (§7).

A ``RunBudget`` is shared by the simulated user and the judge, so the cap holds across both.
A call is admitted only if the spend so far plus its worst case (estimated input and
``max_output_tokens`` output, at the requested model's price) stays within the cap (fail
closed). The provider's reported usage then replaces the reservation; a call that failed or
timed out keeps its worst case reserved, because it may have been billed.

Every call, admitted or refused, appends one JSON line to ``llm-usage.jsonl`` in the run
directory: role, models asked for and answering, tokens, cost, latency and error class.
Prompts, answers and provider error bodies never enter it.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .cost import CLAUDE_PRICES, TokenPrices, TokenUsage, estimated_tokens
from .transport import (
    Effort,
    FailureKind,
    FundsExhausted,
    LlmRequest,
    LlmResult,
    StructuredLLM,
    TransportError,
    TransportId,
)

LEDGER_FILE = "llm-usage.jsonl"

Role = Literal["simulator", "judge"]
Mode = Literal["sync", "batch"]


class UsageRow(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    at: datetime
    role: Role
    transport: TransportId
    mode: Mode
    model_requested: str
    model_returned: str | None
    effort: Effort | None
    batch_id: str | None
    custom_id: str | None
    usage: TokenUsage | None
    # What the call counts against the cap: its reported cost, or its reserved worst case when
    # the provider reported no usage. Zero for a call the cap refused.
    usd: float
    latency_ms: int | None
    failure: FailureKind | None
    error: str | None


@dataclass(frozen=True)
class Call:
    """One request as the budget and the ledger see it."""

    role: Role
    transport: TransportId
    mode: Mode
    request: LlmRequest
    custom_id: str | None = None


class RunBudget:
    def __init__(
        self,
        *,
        max_usd: Decimal,
        run_dir: Path,
        prices: Mapping[str, TokenPrices] = CLAUDE_PRICES,
    ):
        self.max_usd = max_usd
        self.ledger_path = run_dir / LEDGER_FILE
        self.spent_usd = Decimal(0)
        self._prices = prices
        self._lock = Lock()
        run_dir.mkdir(parents=True, exist_ok=True)

    def reserve(self, calls: Sequence[Call]) -> list[Decimal]:
        """Reserves every call's worst case, or none of them and raises ``FundsExhausted``."""
        worst = [self._price(call).cost(_worst_usage(call.request)) for call in calls]
        total = sum(worst, Decimal(0))
        with self._lock:
            admitted = self.spent_usd + total <= self.max_usd
            if admitted:
                self.spent_usd += total
        if not admitted:
            refusal = FundsExhausted("BudgetExhausted")
            for call in calls:
                self._write(call, refusal, usd=Decimal(0), latency_seconds=None, batch_id=None)
            raise refusal
        return worst

    def settle(
        self,
        call: Call,
        reserved: Decimal,
        outcome: LlmResult[Any] | BaseException,
        *,
        latency_seconds: float,
        batch_id: str | None = None,
    ) -> None:
        usd = reserved
        if isinstance(outcome, LlmResult) and outcome.usage is not None:
            usd = self._price(call).cost(outcome.usage)
            with self._lock:
                self.spent_usd += usd - reserved
        self._write(call, outcome, usd=usd, latency_seconds=latency_seconds, batch_id=batch_id)

    def _price(self, call: Call) -> TokenPrices:
        prices = self._prices.get(call.request.model)
        if prices is None:
            raise ValueError(f"No price for model {call.request.model!r}: it cannot be capped")
        return prices.batch() if call.mode == "batch" else prices

    def _write(
        self,
        call: Call,
        outcome: LlmResult[Any] | BaseException,
        *,
        usd: Decimal,
        latency_seconds: float | None,
        batch_id: str | None,
    ) -> None:
        result = outcome if isinstance(outcome, LlmResult) else None
        row = UsageRow(
            at=datetime.now(UTC),
            role=call.role,
            transport=call.transport,
            mode=call.mode,
            model_requested=call.request.model,
            model_returned=None if result is None else result.response_model,
            effort=call.request.effort,
            batch_id=batch_id,
            custom_id=call.custom_id,
            usage=None if result is None else result.usage,
            usd=float(usd),
            latency_ms=None if latency_seconds is None else round(latency_seconds * 1000),
            failure=None if result is None else result.failure,
            error=None if result is not None else _error_class(outcome),
        )
        line = row.model_dump_json() + "\n"
        with self._lock, self.ledger_path.open("a", encoding="utf-8") as ledger:
            ledger.write(line)


class BudgetedTransport:
    """A ``StructuredLLM`` whose every call is capped and recorded by the run's budget."""

    def __init__(self, inner: StructuredLLM, *, budget: RunBudget, role: Role):
        self.transport_id: TransportId = inner.transport_id
        self._inner = inner
        self._budget = budget
        self._role: Role = role

    def parse[T: BaseModel](self, request: LlmRequest, schema: type[T]) -> LlmResult[T]:
        call = Call(self._role, self.transport_id, "sync", request)
        [reserved] = self._budget.reserve([call])
        started = monotonic()
        try:
            result = self._inner.parse(request, schema)
        except Exception as error:
            self._budget.settle(call, reserved, error, latency_seconds=monotonic() - started)
            raise
        self._budget.settle(call, reserved, result, latency_seconds=monotonic() - started)
        return result


def _worst_usage(request: LlmRequest) -> TokenUsage:
    return TokenUsage(
        input_tokens=estimated_tokens(request.instructions + request.input_text),
        output_tokens=request.max_output_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
    )


def _error_class(error: object) -> str:
    # A TransportError's message is already a class name; other errors are named by their class.
    return str(error) if isinstance(error, TransportError) else type(error).__name__
