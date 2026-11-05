"""Conditional purchase intent (docs/design/p0-conditional-intent.md §3.1).

«A si el total es ≤ X; si no, B si tiene stock y el total es ≤ Y», «si A se agota, compra B» or
«nunca compres B», typed: up to three ordered branches, each identifying one variant, and up to
five exclusions. A branch may carry its own total limit and the intent a global one; the
effective limit of a branch is the smallest of those and the turn's own `intent.hard` total, so
nothing widens what the buyer asked for. Amounts are integer minor units, as everywhere else.

The router fills it; the backend resolves and evaluates it (application/conditional.py). D1
measures `intent.py`, `selection.py`, this package's `__init__.py` and `acp/feed.py`
(governance/provenance_artifacts.py), so the types live in this new module and `__init__.py`
does not import it: D1's algorithm identity stays the same for every run.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from commerce_agent.domain.intent import (
    HardConstraints,
    IntentModel,
    NormalizedText,
    OptionConstraint,
)

MAX_BRANCHES = 3
MAX_EXCLUSIONS = 5

# The Merchant's offer identifier (acp/offer_snapshot.py): a branch's variant is read as an offer.
VariantId = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    ),
]
MinorAmount = Annotated[int, Field(strict=True, ge=0)]


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class BranchMatch(IntentModel):
    """What a branch or an exclusion is, never its price: the criteria of `HardConstraints` that
    identify a variant, or the variant itself. At least one criterion."""

    variant_id: VariantId | None = None
    categories: tuple[NormalizedText, ...] = ()
    conditions: tuple[NormalizedText, ...] = ()
    required_options: tuple[OptionConstraint, ...] = ()
    required_terms: tuple[NormalizedText, ...] = ()

    @field_validator("categories", "conditions", "required_terms", mode="before")
    @classmethod
    def normalize_text_tuple(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(_normalize_text(item) if isinstance(item, str) else item for item in value)
        return value

    @field_validator("required_options", mode="before")
    @classmethod
    def options_tuple(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def names_something(self) -> Self:
        if self.variant_id is None and not (
            self.categories or self.conditions or self.required_options or self.required_terms
        ):
            raise ValueError("A branch match needs at least one criterion")
        return self


class PurchaseBranch(IntentModel):
    match: BranchMatch
    # Total with shipping, COP minor units; None leaves only the global limit, if any.
    max_total_amount_minor: MinorAmount | None = None
    # «Si A se agota, B»: this branch counts only when every branch before it was not for sale
    # (sold out or inactive), never when one was merely over its limit (spec §3.3, step 1).
    only_if_previous_out_of_stock: bool = False


class ConditionalIntent(IntentModel):
    branches: Annotated[tuple[PurchaseBranch, ...], Field(max_length=MAX_BRANCHES)] = ()
    max_total_amount_minor: MinorAmount | None = None
    # The lab sells in COP only; the router asks when the buyer's currency is unclear (D5), and
    # resolution asks when `intent.hard` names another one (§3.2, step 4).
    currency: Literal["COP"] | None = None
    excluded: Annotated[tuple[BranchMatch, ...], Field(max_length=MAX_EXCLUSIONS)] = ()

    @field_validator("branches", "excluded", mode="before")
    @classmethod
    def as_tuple(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def is_conditional(self) -> Self:
        if not self.branches and not self.excluded:
            raise ValueError("A conditional intent needs a branch or an exclusion")
        if self.branches and self.branches[0].only_if_previous_out_of_stock:
            raise ValueError("The first branch has no previous branch to fall back from")
        return self

    def effective_limit(self, index: int, hard: HardConstraints) -> int | None:
        """The smallest of the branch limit, the global one and the turn's own total limit
        (`intent.hard`, the search the buyer sees), ignoring the absent ones (D14). Nothing
        widens it."""
        limits = [
            limit
            for limit in (
                self.branches[index].max_total_amount_minor,
                self.max_total_amount_minor,
                hard.max_total_amount_minor,
            )
            if limit is not None
        ]
        return min(limits) if limits else None
