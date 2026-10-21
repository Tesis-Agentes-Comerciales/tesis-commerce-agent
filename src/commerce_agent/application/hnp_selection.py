"""How the HNP executor picks one variant of the grant's shortlist (spec §3.3, step 2).

The shortlist is `rank_candidates(feed, intent)` restricted to the grant's variants, in rank order.
`AGENT_HNP_SELECTOR=deterministic` takes its first; `llm` asks the model to choose among exactly
those ids and records the call's time and tokens in `stage_timings`. A selector only sees catalog
observations, the intent and the buyer's preference text, and answers a variant id: never a
mandate, key or amount. Whatever comes back is checked against the shortlist again (K12): by the
executor's guard in B0-HNP and by D1-HNP's POL-01 in B2-HNP, so no selector widens the grant.
"""

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Protocol

from commerce_agent.domain import PurchaseIntent, RankedCandidate
from commerce_agent.persistence.stage_timings_sqlite import ModelUsage

if TYPE_CHECKING:
    from commerce_agent.llm.delegated_choice import DelegatedChoice

MODEL_STAGE = "model.hnp_select"


class HnpSelector(Protocol):
    def choose(
        self,
        shortlist: tuple[RankedCandidate, ...],
        intent: PurchaseIntent,
        preference_text: str | None,
    ) -> str: ...


class DelegatedChooser(Protocol):
    def choose(
        self,
        intent: PurchaseIntent,
        candidates: tuple[RankedCandidate, ...],
        preference_text: str | None,
    ) -> "DelegatedChoice": ...


class ModelTimer(Protocol):
    def measure(
        self, stage: str, usage: ModelUsage | None = None
    ) -> AbstractContextManager[None]: ...


class DeterministicHnpSelector:
    """`deterministic`: the best ranked variant the grant names."""

    def choose(
        self,
        shortlist: tuple[RankedCandidate, ...],
        intent: PurchaseIntent,
        preference_text: str | None,
    ) -> str:
        return shortlist[0].candidate.variant_id


class ModelHnpSelector:
    """`llm`: the bounded model choice, timed with its tokens."""

    def __init__(self, chooser: DelegatedChooser, timer: ModelTimer) -> None:
        self._chooser, self._timer = chooser, timer

    def choose(
        self,
        shortlist: tuple[RankedCandidate, ...],
        intent: PurchaseIntent,
        preference_text: str | None,
    ) -> str:
        usage = ModelUsage()
        with self._timer.measure(MODEL_STAGE, usage):
            choice = self._chooser.choose(intent, shortlist, preference_text)
            usage.input_tokens, usage.output_tokens = choice.input_tokens, choice.output_tokens
        return choice.variant_id
