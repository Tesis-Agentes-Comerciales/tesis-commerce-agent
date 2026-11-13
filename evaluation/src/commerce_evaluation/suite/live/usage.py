"""The agent's usage in a trial, from its `model_requests` ledger (docs/design/eval-suite.md §7).

Each row is one request: its tokens and the `model` its response reported. The ledger's
`input_tokens` counts every input token, the cache's included
(src/commerce_agent/llm/model_requests.py); a `TokenUsage` bills the uncached ones apart from the
cache reads (`cached_input_tokens`) and writes (`cache_write_tokens`).

A request is priced and charged on its own: a rate card that changes above a prompt size (Haiku
above 100K prompt tokens) applies per request, never to a trial's sum.
"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from functools import reduce
from operator import add
from pathlib import Path

from commerce_evaluation.llm.budget import RunBudget
from commerce_evaluation.llm.cost import CLAUDE_PRICES, NO_USAGE, TokenUsage
from commerce_evaluation.llm.transport import Effort, FundsExhausted

# The ledger's columns for each TokenUsage field.
USAGE_COLUMNS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cached_input_tokens",
    "cache_write_tokens": "cache_write_tokens",
    "reasoning_tokens": "reasoning_tokens",
}


@dataclass(frozen=True)
class Request:
    """One row of the ledger: its tokens and the model that answered, if one did."""

    usage: TokenUsage
    model: str | None


@dataclass(frozen=True)
class AgentUsage:
    requests: tuple[Request, ...] = ()
    # Requests the provider answered 5xx on every attempt.
    provider_5xx: int = 0

    @property
    def total(self) -> TokenUsage:
        return reduce(add, (request.usage for request in self.requests), NO_USAGE)

    @property
    def models(self) -> tuple[str, ...]:
        """The models that answered, in order, each once."""
        return tuple(dict.fromkeys(item.model for item in self.requests if item.model))


def agent_usage(database: Path) -> AgentUsage:
    if not database.exists():
        return AgentUsage()
    columns = ", ".join(USAGE_COLUMNS.values())
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'model_requests'").fetchone():
            return AgentUsage()
        rows = db.execute(
            f"SELECT {columns}, response_model, outcome, http_status FROM model_requests "
            "ORDER BY sequence"
        ).fetchall()
    width = len(USAGE_COLUMNS)
    requests = []
    for row in rows:
        counts = {name: row[index] or 0 for index, name in enumerate(USAGE_COLUMNS)}
        cached = counts["cache_read_tokens"] + counts["cache_write_tokens"]
        counts["input_tokens"] = max(counts["input_tokens"] - cached, 0)
        requests.append(Request(TokenUsage(**counts), row[width] or None))
    failed = sum(row[width + 1] == "error" and (row[width + 2] or 0) >= 500 for row in rows)
    return AgentUsage(tuple(requests), failed)


def cost(model: str, usage: AgentUsage) -> Decimal:
    """Each request at its own rate; zero for a model without a known rate card."""
    if model not in CLAUDE_PRICES:
        return Decimal(0)
    return sum((CLAUDE_PRICES[model].cost(item.usage) for item in usage.requests), Decimal(0))


def user_cost(model: str | None, used: TokenUsage | None) -> Decimal:
    """The simulated user's usage as its transport reported it, summed: neither simulator model
    has a long-prompt rate card, so the sum prices as its calls do."""
    if used is None or model is None or model not in CLAUDE_PRICES:
        return Decimal(0)
    return CLAUDE_PRICES[model].cost(used)


def charge(budget: RunBudget, model: str, effort: Effort | None, usage: AgentUsage) -> None:
    """Charges every request that spent tokens, one by one, to the run's budget (role `agent`).
    Past the cap every request is still charged, then `FundsExhausted` goes up once."""
    over = False
    for item in usage.requests:
        if item.usage == NO_USAGE:
            continue
        try:
            budget.charge(
                role="agent",
                model=model,
                usage=item.usage,
                model_returned=item.model,
                effort=effort,
            )
        except FundsExhausted:
            over = True
    if over:
        raise FundsExhausted("BudgetExhausted", attempts=0)
