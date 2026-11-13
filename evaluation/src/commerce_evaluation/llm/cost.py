"""Token usage and its price. Prices come from configuration, never from code."""

from dataclasses import dataclass
from decimal import Decimal
from math import ceil

from pydantic import BaseModel, ConfigDict, Field

MILLION = Decimal(1_000_000)
# Pre-run estimates only: no tokenizer is installed offline. JSON with hex digests packs fewer
# characters per token than prose, so 3 errs towards a higher estimate.
ESTIMATED_CHARS_PER_TOKEN = 3


class TokenUsage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)


@dataclass(frozen=True)
class TokenPrices:
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal

    def cost(self, usage: TokenUsage) -> Decimal:
        # Cached input is billed at the full input price: an upper bound, never an undercount.
        return (
            usage.input_tokens * self.input_usd_per_mtok
            + usage.output_tokens * self.output_usd_per_mtok
        ) / MILLION


def estimated_tokens(text: str) -> int:
    return ceil(len(text) / ESTIMATED_CHARS_PER_TOKEN)
