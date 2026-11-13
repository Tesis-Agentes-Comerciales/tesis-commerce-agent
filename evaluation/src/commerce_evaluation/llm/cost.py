"""Token usage and its price at Anthropic's published rates (docs/design/eval-suite.md §7)."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from math import ceil
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

MILLION = Decimal(1_000_000)
# Pre-run estimates only: no tokenizer is installed offline. JSON with hex digests packs fewer
# characters per token than prose, so 3 errs towards a higher estimate.
ESTIMATED_CHARS_PER_TOKEN = 3
# A model with a long-prompt rate card bills the whole call at it above this many prompt tokens.
LONG_PROMPT_TOKENS = 100_000
# Message Batches bill every token, cache included, at half price.
BATCH_DISCOUNT = Decimal("0.5")
# 5-minute cache writes cost 1.25x the input price; no transport here asks for the 1-hour TTL.
CACHE_WRITE_MULTIPLIER = Decimal("1.25")


class TokenUsage(BaseModel):
    """One response's usage. Each count is billed at its own rate.

    ``input_tokens`` excludes cache reads and writes; ``output_tokens`` includes thinking, and
    ``reasoning_tokens`` is the thinking part of it, reported for observability only.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass(frozen=True)
class TokenPrices:
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cache_read_usd_per_mtok: Decimal
    cache_write_usd_per_mtok: Decimal
    long_prompt: "TokenPrices | None" = None

    def cost(self, usage: TokenUsage) -> Decimal:
        if self.long_prompt is not None and usage.prompt_tokens > LONG_PROMPT_TOKENS:
            return self.long_prompt.cost(usage)
        return (
            usage.input_tokens * self.input_usd_per_mtok
            + usage.output_tokens * self.output_usd_per_mtok
            + usage.cache_read_tokens * self.cache_read_usd_per_mtok
            + usage.cache_write_tokens * self.cache_write_usd_per_mtok
        ) / MILLION

    def batch(self) -> "TokenPrices":
        return TokenPrices(
            self.input_usd_per_mtok * BATCH_DISCOUNT,
            self.output_usd_per_mtok * BATCH_DISCOUNT,
            self.cache_read_usd_per_mtok * BATCH_DISCOUNT,
            self.cache_write_usd_per_mtok * BATCH_DISCOUNT,
            None if self.long_prompt is None else self.long_prompt.batch(),
        )


def _rates(
    input_usd: str, output_usd: str, cache_read_usd: str, long_prompt: TokenPrices | None = None
) -> TokenPrices:
    base = Decimal(input_usd)
    return TokenPrices(
        base,
        Decimal(output_usd),
        Decimal(cache_read_usd),
        base * CACHE_WRITE_MULTIPLIER,
        long_prompt,
    )


# USD per million tokens, Claude API first-party rates as bundled with the claude-api skill on
# 2026-11-03 (shared/models.md, shared/prompt-caching.md, shared/model-migration.md). Re-check
# them against the pricing page before a paid run.
CLAUDE_PRICES: Mapping[str, TokenPrices] = MappingProxyType(
    {
        # Haiku has two rate cards: up to 100K prompt tokens, and above.
        "claude-haiku-5-5": _rates("0.10", "0.50", "0.01", _rates("0.50", "2.50", "0.05")),
        "claude-sonnet-5-5": _rates("2", "10", "0.10"),
        "claude-opus-5-5": _rates("4", "20", "0.20"),
    }
)


def estimated_tokens(text: str) -> int:
    return ceil(len(text) / ESTIMATED_CHARS_PER_TOKEN)
