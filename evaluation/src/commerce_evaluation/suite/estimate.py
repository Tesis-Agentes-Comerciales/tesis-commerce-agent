"""What a run should cost before it starts, for `eval-suite run --dry-run` (eval-suite.md §7).

Per trial, from the tokens measured in the October pilots (evaluation/results) and the prices of
`llm/cost.py`. The agent's calls are measured; the simulated user's are not yet, so its typical
call is an assumption and its worst call is what the budget reserves for it. The judge runs
apart, after the run, and is not counted here.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal

from commerce_evaluation.llm.cost import CLAUDE_PRICES, TokenPrices, TokenUsage
from commerce_evaluation.suite.run import Planned
from commerce_evaluation.suite.users import MAX_OUTPUT_TOKENS as SIMULATOR_MAX_OUTPUT

# §2: a presente conversation ends after at most 6 turns of the user.
MAX_TURNS = 6
# Worst case: thinking can multiply a call's measured output.
WORST_OUTPUT_FACTOR = 4


@dataclass(frozen=True)
class Tokens:
    input: int
    cached: int
    output: int

    def usage(self) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input - self.cached,
            output_tokens=self.output,
            cache_read_tokens=self.cached,
            cache_write_tokens=0,
            reasoning_tokens=0,
        )

    def worst(self) -> "Tokens":
        """Nothing read from the cache, and thinking at its worst."""
        return replace(self, cached=0, output=self.output * WORST_OUTPUT_FACTOR)


# One call of each agent role, measured: the router (cached prompt), the delegated selector and
# the delegated intent review. The presente selection is priced as the delegated one.
ROUTE = Tokens(input=1_746, cached=1_664, output=90)
SELECT = Tokens(input=635, cached=0, output=284)
REVIEW = Tokens(input=819, cached=0, output=186)
# The simulated user: its prompt and a few turns, and a short answer after low-effort thinking.
SIMULATOR = Tokens(input=1_500, cached=0, output=300)
SIMULATOR_WORST = Tokens(input=4_000, cached=0, output=SIMULATOR_MAX_OUTPUT)

# Calls per trial and mode: (tokens, how many).
Calls = tuple[tuple[Tokens, int], ...]
TYPICAL_AGENT: dict[str, Calls] = {
    "presente": ((ROUTE, 3), (SELECT, 1)),
    "delegado": ((SELECT, 1), (REVIEW, 1)),
}
TYPICAL_SIMULATOR: dict[str, Calls] = {"presente": ((SIMULATOR, 2),), "delegado": ()}
WORST_AGENT: dict[str, Calls] = {
    "presente": ((ROUTE.worst(), MAX_TURNS), (SELECT.worst(), MAX_TURNS)),
    "delegado": ((SELECT.worst(), 2), (REVIEW.worst(), 2)),
}
# The first message is the script's, so the simulator speaks at most MAX_TURNS - 1 times.
WORST_SIMULATOR: dict[str, Calls] = {
    "presente": ((SIMULATOR_WORST, MAX_TURNS - 1),),
    "delegado": (),
}


@dataclass(frozen=True)
class Estimate:
    typical_usd: Decimal
    worst_usd: Decimal


def estimate(
    trials: Iterable[Planned],
    agent: str | None,
    simulator: str | None,
    prices: Mapping[str, TokenPrices] = CLAUDE_PRICES,
) -> Estimate:
    """The run's agent and simulated user. A scripted role (None) costs nothing."""
    typical, worst = Decimal(0), Decimal(0)
    for planned in trials:
        mode = planned.case.mode
        typical += _cost(agent, TYPICAL_AGENT[mode], prices)
        typical += _cost(simulator, TYPICAL_SIMULATOR[mode], prices)
        worst += _cost(agent, WORST_AGENT[mode], prices)
        worst += _cost(simulator, WORST_SIMULATOR[mode], prices)
    return Estimate(typical, worst)


def _cost(model: str | None, calls: Calls, prices: Mapping[str, TokenPrices]) -> Decimal:
    if model is None:
        return Decimal(0)
    price = prices[model]
    return sum((price.cost(tokens.usage()) * count for tokens, count in calls), Decimal(0))
