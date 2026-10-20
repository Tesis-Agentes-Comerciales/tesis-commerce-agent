"""Token pricing and pre-run estimates. Prices come from configuration, never from code."""

from dataclasses import dataclass
from decimal import Decimal
from math import ceil

from .trace import TokenUsage

MILLION = Decimal(1_000_000)
# Pre-run estimates only: no tokenizer is installed offline. JSON with hex digests packs fewer
# characters per token than prose, so 3 errs towards a higher estimate.
ESTIMATED_CHARS_PER_TOKEN = 3


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


@dataclass(frozen=True)
class CostEstimate:
    """Bounds for a planned run. The budget gate uses ``max_usd``."""

    calls: int
    input_tokens: int
    min_output_tokens: int
    max_output_tokens: int
    min_usd: Decimal
    max_usd: Decimal

    def describe(self) -> str:
        return (
            f"{self.calls} validator calls; ~{self.input_tokens} input tokens; "
            f"{self.min_output_tokens}-{self.max_output_tokens} output tokens; "
            f"estimated USD {self.min_usd:.4f}-{self.max_usd:.4f} "
            "(upper bound assumes every call uses max_output_tokens)"
        )


def estimate(
    inputs: list[str],
    *,
    instructions: str,
    sample_output: str,
    max_output_tokens: int,
    prices: TokenPrices,
) -> CostEstimate:
    input_tokens = sum(estimated_tokens(instructions + text) for text in inputs)
    low = len(inputs) * min(estimated_tokens(sample_output), max_output_tokens)
    high = len(inputs) * max_output_tokens

    def usd(output_tokens: int) -> Decimal:
        return prices.cost(
            TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=0,
                reasoning_tokens=0,
            )
        )

    return CostEstimate(
        calls=len(inputs),
        input_tokens=input_tokens,
        min_output_tokens=low,
        max_output_tokens=high,
        min_usd=usd(low),
        max_usd=usd(high),
    )
