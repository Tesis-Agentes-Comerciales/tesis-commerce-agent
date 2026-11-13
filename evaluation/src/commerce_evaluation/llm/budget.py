"""The run's spending: one ``--max-usd`` cap and one usage ledger for every LLM call (§7).

A ``RunBudget`` is shared by the simulated user, the judge and the agent under test. The first
two call through ``BudgetedTransport`` and ``BudgetedBatchTransport``: a call is admitted only if
the spend so far plus its worst case (estimated instructions, input and schema, and
``max_output_tokens`` output, at the requested model's price) stays within the cap. The
provider's reported usage then replaces the reservation; a call that failed, timed out or was
interrupted keeps its worst case reserved, because it may have been billed. The agent calls the
API from the product, so the runner reports its usage afterwards with ``charge``.

Every call, admitted or refused, appends one JSON line to ``llm-usage.jsonl`` in the run
directory: role, models asked for and answering, tokens, cost, latency, attempts and error
class. Prompts, answers and provider error bodies never enter it. Each submitted batch is also
written to ``batches.jsonl`` before it is awaited, so an uncollected batch can be recovered.
"""

import json
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
BATCHES_FILE = "batches.jsonl"

Role = Literal["agent", "simulator", "judge"]
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
    # Requests sent; None when the cap refused the call or the runner charged it afterwards.
    attempts: int | None
    failure: FailureKind | None
    error: str | None


@dataclass(frozen=True)
class Call:
    """One request as the budget and the ledger see it."""

    role: Role
    transport: TransportId
    mode: Mode
    request: LlmRequest
    schema: type[BaseModel]
    custom_id: str | None = None


class RunBudget:
    def __init__(
        self,
        *,
        max_usd: Decimal,
        run_dir: Path,
        prices: Mapping[str, TokenPrices] = CLAUDE_PRICES,
    ):
        if not (max_usd.is_finite() and max_usd > 0):
            raise ValueError("max_usd must be a positive amount")
        self.max_usd = max_usd
        self.ledger_path = run_dir / LEDGER_FILE
        self.batches_path = run_dir / BATCHES_FILE
        self._prices = prices
        self._lock = Lock()
        run_dir.mkdir(parents=True, exist_ok=True)
        # A resumed run keeps what it already spent.
        self.spent_usd = sum(
            (Decimal(str(row.usd)) for row in read_ledger(self.ledger_path)), Decimal(0)
        )

    def reserve(self, calls: Sequence[Call]) -> list[Decimal]:
        """Reserves every call's worst case, or none of them and raises ``FundsExhausted``."""
        worst = [self._price(call.request.model, call.mode).cost(_worst(call)) for call in calls]
        total = sum(worst, Decimal(0))
        with self._lock:
            admitted = self.spent_usd + total <= self.max_usd
            if admitted:
                self.spent_usd += total
        if not admitted:
            refusal = FundsExhausted("BudgetExhausted", attempts=0)
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
            usd = self._price(call.request.model, call.mode).cost(outcome.usage)
            with self._lock:
                self.spent_usd += usd - reserved
        self._write(call, outcome, usd=usd, latency_seconds=latency_seconds, batch_id=batch_id)

    def charge(
        self,
        *,
        role: Role,
        model: str,
        usage: TokenUsage,
        model_returned: str | None = None,
        effort: Effort | None = None,
    ) -> Decimal:
        """Adds usage spent outside these transports (the agent's) and returns its cost.

        It was already spent, so it is always recorded; once the spend passes the cap it raises
        ``FundsExhausted``, and every later reservation is refused.
        """
        usd = self._price(model, "sync").cost(usage)
        with self._lock:
            self.spent_usd += usd
            over = self.spent_usd > self.max_usd
        row = UsageRow(
            at=datetime.now(UTC),
            role=role,
            transport="anthropic",
            mode="sync",
            model_requested=model,
            model_returned=model_returned,
            effort=effort,
            batch_id=None,
            custom_id=None,
            usage=usage,
            usd=float(usd),
            latency_ms=None,
            attempts=None,
            failure=None,
            error="BudgetExhausted" if over else None,
        )
        self._append(self.ledger_path, row.model_dump_json())
        if over:
            raise FundsExhausted("BudgetExhausted", attempts=0)
        return usd

    def record_batch(self, batch_id: str, calls: Sequence[Call]) -> None:
        """Writes a submitted batch down before anyone waits for it: results last 29 days."""
        line = {
            "at": datetime.now(UTC).isoformat(),
            "batch_id": batch_id,
            "role": calls[0].role if calls else None,
            "custom_ids": [call.custom_id for call in calls],
        }
        self._append(self.batches_path, json.dumps(line))

    def _price(self, model: str, mode: Mode) -> TokenPrices:
        prices = self._prices.get(model)
        if prices is None:
            raise ValueError(f"No price for model {model!r}: it cannot be capped")
        return prices.batch() if mode == "batch" else prices

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
        if result is not None:
            attempts: int | None = result.attempts
        else:
            attempts = outcome.attempts if isinstance(outcome, TransportError) else 1
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
            attempts=attempts or None,
            failure=None if result is None else result.failure,
            error=None if result is not None else _error_class(outcome),
        )
        self._append(self.ledger_path, row.model_dump_json())

    def _append(self, path: Path, line: str) -> None:
        with self._lock, path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")


class BudgetedTransport:
    """A ``StructuredLLM`` whose every call is capped and recorded by the run's budget."""

    def __init__(self, inner: StructuredLLM, *, budget: RunBudget, role: Role):
        self.transport_id: TransportId = inner.transport_id
        self._inner = inner
        self._budget = budget
        self._role: Role = role

    def parse[T: BaseModel](self, request: LlmRequest, schema: type[T]) -> LlmResult[T]:
        call = Call(self._role, self.transport_id, "sync", request, schema)
        [reserved] = self._budget.reserve([call])
        started = monotonic()
        try:
            result = self._inner.parse(request, schema)
        except BaseException as error:
            # Interrupts too: the request may have been sent and billed.
            self._budget.settle(call, reserved, error, latency_seconds=monotonic() - started)
            raise
        self._budget.settle(call, reserved, result, latency_seconds=monotonic() - started)
        return result


def read_ledger(path: Path) -> list[UsageRow]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [UsageRow.model_validate_json(line, strict=False) for line in lines if line]


def _worst(call: Call) -> TokenUsage:
    request = call.request
    schema = json.dumps(call.schema.model_json_schema())
    return TokenUsage(
        input_tokens=estimated_tokens(request.instructions + request.input_text + schema),
        output_tokens=request.max_output_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
    )


def _error_class(error: object) -> str:
    # A TransportError's message is already a class name; other errors are named by their class.
    return str(error) if isinstance(error, TransportError) else type(error).__name__
