"""How the HNP executor picks one variant of the grant's shortlist (spec §3.3, step 2).

The shortlist is `rank_candidates(feed, intent)` restricted to the grant's variants, in rank order.
`AGENT_HNP_SELECTOR=deterministic` takes its first. A selector only sees catalog observations, the
intent and the buyer's preference text, and answers a variant id: never a mandate, key or amount.
Whatever comes back, the executor checks it against the shortlist again (K12), so no selector
widens the grant.
"""

from typing import Protocol

from commerce_agent.domain import PurchaseIntent, RankedCandidate


class HnpSelector(Protocol):
    def choose(
        self,
        shortlist: tuple[RankedCandidate, ...],
        intent: PurchaseIntent,
        preference_text: str | None,
    ) -> str: ...


class DeterministicHnpSelector:
    """`deterministic`: the best ranked variant the grant names."""

    def choose(
        self,
        shortlist: tuple[RankedCandidate, ...],
        intent: PurchaseIntent,
        preference_text: str | None,
    ) -> str:
        return shortlist[0].candidate.variant_id
