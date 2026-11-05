"""Resolve and evaluate a conditional intent (docs/design/p0-conditional-intent.md §3.2-§3.7).

1. `resolve_branches` ties each branch to exactly one variant of the turn's feed, or asks
   (`ConditionalClarification`): no I/O, nothing to undo. A branch whose variant fails the
   turn's `intent.hard` is resolved as `intent_mismatch`; its offer is never read.
2. `evaluate_readings` is the pure evaluator: given the offer readings, it decides in order. The
   first eligible branch wins, unless an `intent_mismatch` branch comes before it: then the
   decision is `clarify` (HP; HNP escalates), since A is never skipped without a verified
   commercial fact. A read failure or a feed/offer disagreement makes it `undetermined`, and
   nothing after it counts. A branch that is sold out, inactive or over a limit (judged on the
   offer: the unit one on `items_total_minor`, the total one on `total_minor`, equality
   eligible) is false. Without branches the decision is `exclusions_only`: the normal selection
   runs on `without_variants(feed, excluded)`.
3. `evaluate_branches` is the only reader: it asks the evaluator which offer it needs next, so
   B is never read when A decides and nothing is read after an unknown branch.
4. `record_decision` and `verify_checkout` write `conditional_decisions`; the second compares the
   prepared checkout with the decision's offer, strictly (D4).

The model interprets the buyer's words into the intent; it never chooses the branch. Matching is
by whole tokens, so «xperia 5 ii» is not the Xperia 5 III (spec §10), and it is stricter than
`rank_candidates`, so a resolved variant always survives the re-rank of `PurchaseService.start`
with `effective_intent`. That effective intent is the turn's `intent.hard`, hardened with the
branch: it never drops a filter nor widens a limit.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.feed import ProductCandidate, ProductFeed
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.acp.offer_snapshot import DeliveryContext, MerchantOffer, offer_snapshot_sha256
from commerce_agent.application.offer_freshness import LAB_DESTINATION, OfferReader
from commerce_agent.domain.conditional import MAX_BRANCHES, BranchMatch, ConditionalIntent
from commerce_agent.domain.intent import HardConstraints, OptionConstraint, PurchaseIntent
from commerce_agent.domain.selection import rejection_reason
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.persistence.conditional_sqlite import ConditionalEntry, ConditionalRecord

CONDITIONAL_SCHEMA = "conditional-decision/1"
MAX_CLARIFY_OPTIONS = 3

BranchStatus = Literal[
    "eligible",
    "over_limit",
    "out_of_stock",
    "inactive",
    "intent_mismatch",
    "unknown",
    "not_triggered",
    "not_evaluated",
]
DecisionOutcome = Literal["branch", "none", "undetermined", "clarify", "exclusions_only"]
OracleLabel = Literal["A", "B", "C", "none", "undetermined"]
ClarifyCause = Literal[
    "branch_not_found",
    "branch_ambiguous",
    "duplicate_branch",
    "branch_excluded",
    "currency",
    "intent_mismatch",
]
PurchaseMode = Literal["HP", "HNP"]
GovernanceMode = Literal["off", "shadow", "enforce"]

# What allows «solo si la anterior se agotó»: every branch before it was not for sale.
NOT_FOR_SALE: tuple[BranchStatus, ...] = ("out_of_stock", "inactive")
_LABELS: tuple[OracleLabel, ...] = ("A", "B", "C")
# A branch with one of these statuses ends the evaluation.
_DECIDING: tuple[BranchStatus, ...] = ("eligible", "unknown")


def _outcome(statuses: Sequence[BranchStatus]) -> tuple[DecisionOutcome, int | None]:
    """§3.3: the deciding branch, if any, and whether an `intent_mismatch` came before it."""
    deciding = next((i for i, status in enumerate(statuses) if status in _DECIDING), None)
    if deciding is None:
        return "none", None
    if statuses[deciding] == "unknown":
        return "undetermined", None
    if "intent_mismatch" in statuses[:deciding]:
        return "clarify", None
    return "branch", deciding


BranchIndex = Annotated[int, Field(ge=0, lt=MAX_BRANCHES)]
Minor = Annotated[int, Field(ge=0)]
Text = Annotated[str, Field(min_length=1)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


# Matching a branch to the feed (§3.2).


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", text.casefold()))


def _contains_run(tokens: tuple[str, ...], run: tuple[str, ...]) -> bool:
    width = len(run)
    return any(tokens[start : start + width] == run for start in range(len(tokens) - width + 1))


def _identity_tokens(candidate: ProductCandidate) -> tuple[str, ...]:
    """The fields `rank_candidates` searches its terms in (domain/selection.py)."""
    return _tokens(
        " ".join(
            (
                candidate.product_title,
                candidate.variant_title,
                *candidate.categories,
                *candidate.conditions,
                *(value for option in candidate.options for value in option),
            )
        )
    )


def _any_availability(candidate: ProductCandidate) -> ProductCandidate:
    """Availability is the offer's to decide; the feed only says what the variant is."""
    return candidate.model_copy(update={"available": True})


def branch_matches(candidate: ProductCandidate, match: BranchMatch) -> bool:
    """Every criterion holds, terms as whole tokens in order; stricter than the substring
    filter of `rank_candidates`, which it also passes."""
    if match.variant_id is not None and candidate.variant_id != match.variant_id:
        return False
    identity = PurchaseIntent(
        hard=HardConstraints(
            categories=match.categories,
            conditions=match.conditions,
            required_options=match.required_options,
            required_terms=match.required_terms,
        )
    )
    if rejection_reason(_any_availability(candidate), identity) is not None:
        return False
    tokens = _identity_tokens(candidate)
    return all(_contains_run(tokens, _tokens(term)) for term in match.required_terms)


def without_variants(feed: ProductFeed, variant_ids: Iterable[str]) -> ProductFeed:
    """The turn's feed without the excluded variants (§3.6), for the shortlist, the recovery,
    `start(feed=…)` and D1; a product left without variants leaves too."""
    excluded = frozenset(variant_ids)
    if not excluded:
        return feed
    products = []
    for product in feed.products:
        kept = [variant for variant in product.variants if variant.id not in excluded]
        if kept:
            products.append(product.model_copy(update={"variants": kept}))
    return feed.model_copy(update={"products": tuple(products)})


class ResolvedBranch(_Model):
    index: BranchIndex
    variant_id: Text
    # The feed's variant title, for the buyer's message; never sent to a model.
    title: Text
    # min(branch, global, intent.hard) total limit; None when none of them exists.
    limit_minor: Minor | None
    # `intent.hard.max_unit_amount_minor`, judged on the offer's `items_total_minor` (quantity 1).
    unit_limit_minor: Minor | None
    # The feed's unit price is over `unit_limit_minor` (§3.2, step 5). The offer decides: over
    # it too is `over_limit`; within it is a disagreement, since `start` re-ranks the feed.
    feed_budget: bool
    only_if_previous_out_of_stock: bool
    # What the turn's feed says: `start` re-ranks it, so a sellable offer of a variant the feed
    # calls unavailable is a disagreement, not a purchase.
    feed_available: bool
    # `intent_mismatch`: the `rejection_reason` code of the variant against the branch's
    # effective intent (§3.2, step 5). Such a branch is never eligible and its offer never read.
    rejection_code: str | None = None


class ResolvedConditional(_Model):
    intent: ConditionalIntent
    # The turn's `intent.hard`: every branch is evaluated inside it (D14).
    hard: HardConstraints
    branches: tuple[ResolvedBranch, ...]
    excluded_variant_ids: tuple[str, ...]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if len(self.branches) != len(self.intent.branches):
            raise ValueError("One resolved branch per intent branch")
        if list(self.excluded_variant_ids) != sorted(set(self.excluded_variant_ids)):
            raise ValueError("Excluded variants are unique and sorted")
        variants = [branch.variant_id for branch in self.branches]
        if len(set(variants)) != len(variants):
            raise ValueError("Two branches resolve to the same variant")
        if set(variants) & set(self.excluded_variant_ids):
            raise ValueError("A branch is excluded")
        for position, (branch, typed) in enumerate(
            zip(self.branches, self.intent.branches, strict=True)
        ):
            if (
                branch.index != position
                or (branch.feed_budget and branch.unit_limit_minor is None)
                or branch.only_if_previous_out_of_stock != typed.only_if_previous_out_of_stock
                or branch.limit_minor != self.intent.effective_limit(position, self.hard)
                or branch.unit_limit_minor != self.hard.max_unit_amount_minor
            ):
                raise ValueError("A resolved branch differs from its intent")
        return self


class ConditionalClarification(_Model):
    """Why the agent asks instead of evaluating; no checkout is prepared (spec §3.2, D5)."""

    cause: ClarifyCause
    branch_index: BranchIndex | None = None
    # Up to three matching variants, with their feed titles in the same order.
    variant_ids: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()
    excluded_variant_ids: tuple[str, ...] = ()


def _union[T](first: tuple[T, ...], second: tuple[T, ...]) -> tuple[T, ...]:
    return tuple(dict.fromkeys((*first, *second)))


def _hardened(hard: HardConstraints, match: BranchMatch, limit: int | None) -> PurchaseIntent:
    options: tuple[OptionConstraint, ...] = _union(hard.required_options, match.required_options)
    return PurchaseIntent(
        hard=HardConstraints(
            currency="COP",
            max_unit_amount_minor=hard.max_unit_amount_minor,
            max_total_amount_minor=limit,
            categories=_union(hard.categories, match.categories),
            conditions=_union(hard.conditions, match.conditions),
            required_options=options,
            required_terms=_union(hard.required_terms, match.required_terms),
        )
    )


def effective_intent(resolved: ResolvedConditional, index: int) -> PurchaseIntent:
    """What `PurchaseService.start` receives for the chosen branch (§3.1, §3.4, D14): the turn's
    `intent.hard` with every filter kept, plus the branch identity, COP and the effective total
    limit; no preferences. Its candidates are a subset of those of `intent.hard`."""
    return _hardened(
        resolved.hard, resolved.intent.branches[index].match, resolved.branches[index].limit_minor
    )


def _mismatch(candidate: ProductCandidate, hard: HardConstraints, match: BranchMatch) -> str | None:
    """Step 5 of §3.2 against the effective intent without `max_unit_amount_minor`: its
    "budget" comes first in `rejection_reason` and would hide any other failing filter, and the
    offer decides it (`feed_budget`)."""
    identity = _hardened(hard.model_copy(update={"max_unit_amount_minor": None}), match, None)
    return rejection_reason(_any_availability(candidate), identity)


def excluded_variant_ids(conditional: ConditionalIntent, feed: ProductFeed) -> tuple[str, ...]:
    """Every variant of `feed` an exclusion matches, sorted (§3.2, step 3); one matching
    nothing excludes nothing. Also for a delegation turn, which evaluates no branch (§3.9)."""
    return tuple(
        sorted(
            {
                candidate.variant_id
                for match in conditional.excluded
                for candidate in feed.candidates
                if branch_matches(candidate, match)
            }
        )
    )


def resolve_branches(
    conditional: ConditionalIntent, feed: ProductFeed, *, hard: HardConstraints
) -> ResolvedConditional | ConditionalClarification:
    """Each branch to exactly one variant of `feed`, or the first reason to ask, in order (§3.2).

    `hard` is the turn's `intent.hard`. Asks when `intent.hard` names a currency other than COP
    (`conditional.currency` can only be COP), a branch matches no variant or several, it is
    excluded, or two branches are one variant. A branch whose variant fails its effective intent
    is resolved as `intent_mismatch`, with the `rejection_reason` code (step 5).
    """
    candidates = feed.candidates
    excluded = excluded_variant_ids(conditional, feed)

    def ask(
        cause: ClarifyCause, index: int | None = None, found: Sequence[ProductCandidate] = ()
    ) -> ConditionalClarification:
        shown = found[:MAX_CLARIFY_OPTIONS]
        return ConditionalClarification(
            cause=cause,
            branch_index=index,
            variant_ids=tuple(candidate.variant_id for candidate in shown),
            titles=tuple(candidate.variant_title for candidate in shown),
            excluded_variant_ids=excluded,
        )

    if hard.currency not in (None, "COP"):
        return ask("currency")

    resolved: list[ResolvedBranch] = []
    for index, branch in enumerate(conditional.branches):
        found = [candidate for candidate in candidates if branch_matches(candidate, branch.match)]
        if not found:
            return ask("branch_not_found", index)
        if len(found) > 1:
            return ask("branch_ambiguous", index, found)
        candidate = found[0]
        if candidate.variant_id in excluded:
            return ask("branch_excluded", index, found)
        if any(earlier.variant_id == candidate.variant_id for earlier in resolved):
            return ask("duplicate_branch", index, found)
        resolved.append(
            ResolvedBranch(
                index=index,
                variant_id=candidate.variant_id,
                title=candidate.variant_title,
                limit_minor=conditional.effective_limit(index, hard),
                unit_limit_minor=hard.max_unit_amount_minor,
                feed_budget=hard.max_unit_amount_minor is not None
                and candidate.observed_unit_amount_minor > hard.max_unit_amount_minor,
                only_if_previous_out_of_stock=branch.only_if_previous_out_of_stock,
                feed_available=candidate.available,
                # Another currency is a mismatch too: the effective intent is in COP.
                rejection_code=_mismatch(candidate, hard, branch.match),
            )
        )
    return ResolvedConditional(
        intent=conditional, hard=hard, branches=tuple(resolved), excluded_variant_ids=excluded
    )


# Evaluating the branches with the offers (§3.3).


class OfferReadFailure(_Model):
    """A read that did not return the offer asked for: never a fact about the branch (D3)."""

    code: Text
    status_code: int | None = None


type OfferReading = MerchantOffer | OfferReadFailure


class OfferFacts(_Model):
    revision: int
    product_status: Literal["active", "inactive"]
    availability: Literal["in_stock", "out_of_stock"]
    available_quantity: Minor
    items_total_minor: Minor
    shipping_total_minor: Minor
    total_minor: Minor
    expires_at: str
    offer_snapshot_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @classmethod
    def of(cls, offer: MerchantOffer) -> "OfferFacts":
        return cls(
            revision=offer.revision,
            product_status=offer.product_status,
            availability=offer.availability,
            available_quantity=offer.available_quantity,
            items_total_minor=offer.pricing.items_total_minor,
            shipping_total_minor=offer.pricing.shipping_total_minor,
            total_minor=offer.pricing.total_minor,
            expires_at=offer.expires_at,
            offer_snapshot_sha256=offer_snapshot_sha256(offer),
        )


class BranchEvaluation(_Model):
    index: BranchIndex
    variant_id: Text
    status: BranchStatus
    limit_minor: Minor | None
    unit_limit_minor: Minor | None
    feed_budget: bool
    # What the Merchant said, when an offer of this variant was read.
    offer: OfferFacts | None = None
    # `unknown` only: the read failure or `feed_offer_mismatch`, and its HTTP status if any.
    error_code: str | None = None
    status_code: int | None = None
    # `intent_mismatch` only: the `rejection_reason` code against the effective intent.
    rejection_code: str | None = None


class ConditionalDecision(_Model):
    outcome: DecisionOutcome
    chosen_index: BranchIndex | None
    # Empty only for `exclusions_only`: no branch was evaluated.
    branches: Annotated[tuple[BranchEvaluation, ...], Field(max_length=MAX_BRANCHES)]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        """The shape of an ordered evaluation: false (or `intent_mismatch`) branches, then at
        most one deciding branch (eligible or unknown), then only `not_evaluated` ones."""
        if (self.outcome == "exclusions_only") != (not self.branches):
            raise ValueError("Only an exclusion-only decision evaluates no branch")
        if not self.branches:
            if self.chosen_index is not None:
                raise ValueError("An exclusion-only decision chooses no branch")
            return self
        statuses: list[BranchStatus] = [branch.status for branch in self.branches]
        if [branch.index for branch in self.branches] != list(range(len(self.branches))):
            raise ValueError("Branches are evaluated in order")
        deciding = [i for i, status in enumerate(statuses) if status in _DECIDING]
        if len(deciding) > 1 or any(
            (status == "not_evaluated") != bool(deciding and i > deciding[0])
            for i, status in enumerate(statuses)
        ):
            raise ValueError("Nothing is evaluated after the deciding branch")
        expected = _outcome(statuses)
        if (self.outcome, self.chosen_index) != expected:
            raise ValueError("The outcome does not follow from the branches")
        return self

    @property
    def label(self) -> OracleLabel | None:
        """The label of Maldo's oracle (P3 §5): the chosen branch's letter, `none` or
        `undetermined`; None for `clarify` and `exclusions_only`, which are not labels (§3.3).
        P3 has two branches, so `C` only exists with a third one."""
        if self.chosen_index is not None:
            return _LABELS[self.chosen_index]
        if self.outcome in ("none", "undetermined"):
            return "none" if self.outcome == "none" else "undetermined"
        return None

    @property
    def chosen(self) -> BranchEvaluation | None:
        return None if self.chosen_index is None else self.branches[self.chosen_index]


# Without branches (UX07, or a delegation turn): nothing is evaluated nor read (§3.3, §3.9).
EXCLUSIONS_ONLY = ConditionalDecision(outcome="exclusions_only", chosen_index=None, branches=())


class MissingOfferReading(ValueError):
    """The evaluation needs the offer of this branch before it can decide."""

    def __init__(self, index: int) -> None:
        super().__init__(f"The evaluation needs the offer of branch {index}")
        self.index = index


def _plain(branch: ResolvedBranch, status: BranchStatus, **fields: object) -> BranchEvaluation:
    return BranchEvaluation(
        index=branch.index,
        variant_id=branch.variant_id,
        status=status,
        limit_minor=branch.limit_minor,
        unit_limit_minor=branch.unit_limit_minor,
        feed_budget=branch.feed_budget,
        **fields,  # type: ignore[arg-type]
    )


def _judge(branch: ResolvedBranch, reading: OfferReading) -> BranchEvaluation:
    """Steps 3 and 4 of §3.3, in their order: verified facts make a branch false, anything else
    unknown. Prices are the offer's; the feed only decides whether `start` could buy it."""
    if isinstance(reading, OfferReadFailure):
        return _plain(branch, "unknown", error_code=reading.code, status_code=reading.status_code)
    if reading.id != branch.variant_id:
        return _plain(branch, "unknown", error_code="offer_mismatch")
    facts = OfferFacts.of(reading)
    if reading.product_status != "active":
        return _plain(branch, "inactive", offer=facts)
    if reading.availability != "in_stock" or reading.available_quantity < 1:
        return _plain(branch, "out_of_stock", offer=facts)
    if not branch.feed_available:
        return _plain(branch, "unknown", offer=facts, error_code="feed_offer_mismatch")
    unit_limit = branch.unit_limit_minor
    if unit_limit is not None and reading.pricing.items_total_minor > unit_limit:
        return _plain(branch, "over_limit", offer=facts)
    if branch.feed_budget:
        # Within the unit limit by the offer, over it by the feed: `start` would refuse it.
        return _plain(branch, "unknown", offer=facts, error_code="feed_offer_mismatch")
    if branch.limit_minor is not None and reading.pricing.total_minor > branch.limit_minor:
        return _plain(branch, "over_limit", offer=facts)
    return _plain(branch, "eligible", offer=facts)


def _walk(
    resolved: ResolvedConditional, readings: Mapping[int, OfferReading]
) -> tuple[list[BranchEvaluation], int | None]:
    """The evaluations so far and, if the decision still needs one, the next branch to read."""
    done: list[BranchEvaluation] = []
    for branch in resolved.branches:
        # Step 1: «solo si se agota» holds only if every earlier branch was not for sale.
        if branch.only_if_previous_out_of_stock and any(
            earlier.status not in NOT_FOR_SALE for earlier in done
        ):
            done.append(_plain(branch, "not_triggered"))
            continue
        # Step 2: a branch against `intent.hard` is never read. It is not skipped either: a later
        # eligible branch makes the decision `clarify`, not that branch (`_outcome`).
        if branch.rejection_code is not None:
            done.append(_plain(branch, "intent_mismatch", rejection_code=branch.rejection_code))
            continue
        if branch.index not in readings:
            return done, branch.index
        evaluation = _judge(branch, readings[branch.index])
        done.append(evaluation)
        if evaluation.status in ("eligible", "unknown"):
            break
    done.extend(_plain(branch, "not_evaluated") for branch in resolved.branches[len(done) :])
    return done, None


def next_offer_to_read(
    resolved: ResolvedConditional, readings: Mapping[int, OfferReading]
) -> int | None:
    """The branch whose offer the decision needs next, or None once it can decide."""
    return _walk(resolved, readings)[1] if resolved.branches else None


def evaluate_readings(
    resolved: ResolvedConditional, readings: Mapping[int, OfferReading]
) -> ConditionalDecision:
    """Pure: the decision from the readings by branch index. Readings it does not need are
    ignored; a missing one it needs raises `MissingOfferReading`."""
    if not resolved.branches:
        return EXCLUSIONS_ONLY
    done, pending = _walk(resolved, readings)
    if pending is not None:
        raise MissingOfferReading(pending)
    outcome, chosen = _outcome([branch.status for branch in done])
    return ConditionalDecision(outcome=outcome, chosen_index=chosen, branches=tuple(done))


def read_offer(
    offers: OfferReader, variant_id: str, destination: DeliveryContext = LAB_DESTINATION
) -> OfferReading:
    """One read, never retried; any failure is a reading, so it can never become a fact."""
    try:
        offer = offers.get_offer(variant_id, destination)
    except ACPClientError as error:
        return OfferReadFailure(code=error.code, status_code=error.status_code)
    except Exception:
        return OfferReadFailure(code="offer_read_failed")
    # ACPClient refuses both already; kept for any other reader.
    if offer.id != variant_id or offer.delivery_context != destination:
        return OfferReadFailure(code="offer_mismatch")
    return offer


def evaluate_branches(
    resolved: ResolvedConditional,
    offers: OfferReader,
    destination: DeliveryContext = LAB_DESTINATION,
) -> ConditionalDecision:
    """§3.3: reads, in branch order, only the offers the evaluator asks for, then decides.
    `offers` is the `ACPClient` in production; without branches nothing is read."""
    readings: dict[int, OfferReading] = {}
    while (index := next_offer_to_read(resolved, readings)) is not None:
        readings[index] = read_offer(offers, resolved.branches[index].variant_id, destination)
    return evaluate_readings(resolved, readings)


# The record (§3.7) and the checkout check (§3.4).


class ConditionalRecorder(Protocol):
    """`SqliteAgentStore` in production."""

    def append_conditional(self, run_id: str, entry: ConditionalEntry) -> ConditionalRecord: ...


def _digest(domain: bytes, value: object) -> str:
    return sha256_digest(domain + canonical_bytes(value))


def conditional_intent_sha256(intent: ConditionalIntent) -> str:
    return _digest(b"conditional-intent/1\0", intent.model_dump(mode="json"))


def hard_sha256(hard: HardConstraints) -> str:
    return _digest(b"conditional-hard/1\0", hard.model_dump(mode="json"))


def decision_record(
    *,
    purchase_mode: PurchaseMode,
    governance_mode: GovernanceMode,
    intent: ConditionalIntent,
    hard: HardConstraints,
    resolution: ResolvedConditional | ConditionalClarification,
    decision: ConditionalDecision | None,
    explanation: str,
) -> ConditionalEntry:
    """The `decision` row. Outcome `clarify` for a clarification (no decision), else the
    decision's: `EXCLUSIONS_ONLY` for an intent without branches (UX07: the normal selection runs
    on the filtered feed) and for a delegation turn that brings exclusions, which evaluates no
    branch (§3.9). Text is hashed, never stored."""
    branches: list[dict[str, object]] = []
    clarify: dict[str, object] | None = None
    label: str | None = None
    index: int | None = None
    variant_id: str | None = None
    if isinstance(resolution, ConditionalClarification):
        if decision is not None:
            raise ValueError("A clarification has no decision")
        outcome: str = "clarify"
        clarify = {
            "cause": resolution.cause,
            "branch_index": resolution.branch_index,
            "variant_ids": list(resolution.variant_ids),
        }
    elif decision is None:
        raise ValueError("A resolved intent needs its decision")
    elif decision.outcome == "exclusions_only":
        if not intent.excluded:
            raise ValueError("An exclusion-only row needs an exclusion")
        outcome = "exclusions_only"
    else:
        if [b.variant_id for b in decision.branches] != [b.variant_id for b in resolution.branches]:
            raise ValueError("The decision is not of these branches")
        outcome, label, index = decision.outcome, decision.label, decision.chosen_index
        variant_id = None if decision.chosen is None else decision.chosen.variant_id
        branches = [branch.model_dump(mode="json") for branch in decision.branches]
        if outcome == "clarify":
            # The first branch the buyer's order would have skipped without a commercial fact.
            mismatch = next(b for b in decision.branches if b.status == "intent_mismatch")
            clarify = {
                "cause": "intent_mismatch",
                "branch_index": mismatch.index,
                "variant_ids": [mismatch.variant_id],
            }
    body = {
        "schema": CONDITIONAL_SCHEMA,
        "kind": "decision",
        "purchase_mode": purchase_mode,
        "governance_mode": governance_mode,
        "intent_sha256": conditional_intent_sha256(intent),
        "hard_sha256": hard_sha256(hard),
        "outcome": outcome,
        "label": label,
        "chosen_index": index,
        "branches": branches,
        "excluded_variant_ids": list(resolution.excluded_variant_ids),
        "clarify": clarify,
        "explanation_sha256": sha256_digest(b"conditional-explanation/1\0" + explanation.encode()),
    }
    return ConditionalEntry(
        kind="decision",
        outcome=outcome,  # type: ignore[arg-type]
        body=body,
        branch_index=index,
        variant_id=variant_id,
    )


def checkout_check_record(
    decision: ConditionalDecision,
    *,
    checkout_id: str,
    checkout_total_minor: int,
    checkout_currency: str = "cop",
) -> ConditionalEntry:
    """The `checkout_check` row: the prepared checkout against the offer that chose it. Equal
    or not, strictly (D4): a lower total is a change too, and so is another currency."""
    chosen = decision.chosen
    if chosen is None or chosen.offer is None:
        raise ValueError("A checkout check needs a chosen branch and its offer")
    offer_total = chosen.offer.total_minor
    matches = checkout_currency.casefold() == "cop" and checkout_total_minor == offer_total
    outcome = "checkout_matches" if matches else "offer_changed"
    body = {
        "schema": CONDITIONAL_SCHEMA,
        "kind": "checkout_check",
        "outcome": outcome,
        "branch_index": chosen.index,
        "variant_id": chosen.variant_id,
        "checkout_id": checkout_id,
        "offer_total_minor": offer_total,
        "checkout_total_minor": checkout_total_minor,
        "checkout_currency": checkout_currency.casefold(),
    }
    return ConditionalEntry(
        kind="checkout_check",
        outcome=outcome,
        body=body,
        branch_index=chosen.index,
        variant_id=chosen.variant_id,
    )


def record_decision(
    recorder: ConditionalRecorder,
    run_id: str,
    *,
    purchase_mode: PurchaseMode,
    governance_mode: GovernanceMode,
    intent: ConditionalIntent,
    hard: HardConstraints,
    resolution: ResolvedConditional | ConditionalClarification,
    decision: ConditionalDecision | None,
    explanation: str,
) -> ConditionalRecord:
    """Writes the run's `decision` row, the one the oracle reads."""
    return recorder.append_conditional(
        run_id,
        decision_record(
            purchase_mode=purchase_mode,
            governance_mode=governance_mode,
            intent=intent,
            hard=hard,
            resolution=resolution,
            decision=decision,
            explanation=explanation,
        ),
    )


@dataclass(frozen=True, slots=True)
class CheckoutCheck:
    matches: bool
    offer_total_minor: int
    checkout_total_minor: int
    record: ConditionalRecord


def verify_checkout(
    recorder: ConditionalRecorder,
    run_id: str,
    decision: ConditionalDecision,
    checkout: CheckoutSession,
) -> CheckoutCheck:
    """§3.4: after `start`, the checkout must carry the variant and the total (and COP) the
    decision read. The check is recorded either way; on `matches=False` the caller cancels the
    checkout, explains `offer_changed` and tries no other branch."""
    chosen = decision.chosen
    if chosen is None or chosen.offer is None:
        raise ValueError("A checkout check needs a chosen branch and its offer")
    lines = [line.get("item", {}).get("id") for line in checkout.line_items]
    if lines != [chosen.variant_id]:
        raise ValueError("The checkout is not of the chosen variant")
    entry = checkout_check_record(
        decision,
        checkout_id=checkout.id,
        checkout_total_minor=checkout.total_minor,
        checkout_currency=checkout.currency,
    )
    record = recorder.append_conditional(run_id, entry)
    return CheckoutCheck(
        matches=record.outcome == "checkout_matches",
        offer_total_minor=chosen.offer.total_minor,
        checkout_total_minor=checkout.total_minor,
        record=record,
    )


# The view of the decision in `select_product` (§3.10), for the UI.

ViewOutcome = Literal["branch", "none", "undetermined", "offer_changed"]


class ConditionalBranchView(_Model):
    index: BranchIndex
    variant_id: Text
    title: Text
    status: BranchStatus
    # The Merchant total with shipping, when its offer was read, and the effective total limit.
    total_minor: Minor | None
    limit_minor: Minor | None
    # The price without shipping and `intent.hard`'s unit limit: an `over_limit` within the
    # total limit is over this one (§3.3, step 4.4).
    items_total_minor: Minor | None
    unit_limit_minor: Minor | None

    @model_validator(mode="after")
    def one_offer(self) -> Self:
        if (self.total_minor is None) != (self.items_total_minor is None) or (
            self.total_minor is not None
            and self.items_total_minor is not None
            and self.items_total_minor > self.total_minor
        ):
            raise ValueError("Both totals come from the same offer, shipping included in one")
        return self


class ConditionalView(_Model):
    """`SelectProductResult.conditional` (ui/src/lib/agent-contract.ts), same rules both sides."""

    outcome: ViewOutcome
    chosen_index: BranchIndex | None
    currency: Literal["COP"]
    branches: Annotated[
        tuple[ConditionalBranchView, ...], Field(min_length=1, max_length=MAX_BRANCHES)
    ]
    # `offer_changed` only: the prepared checkout's total, which differs from the offer's.
    checkout_total_minor: Minor | None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if [branch.index for branch in self.branches] != list(range(len(self.branches))):
            raise ValueError("Branches are listed in order")
        chooses = self.outcome in ("branch", "offer_changed")
        if chooses != (self.chosen_index is not None):
            raise ValueError("Only a chosen branch has a chosen index")
        if self.chosen_index is not None and (
            self.chosen_index >= len(self.branches)
            or self.branches[self.chosen_index].status != "eligible"
        ):
            raise ValueError("The chosen branch is eligible")
        if (self.outcome == "offer_changed") != (self.checkout_total_minor is not None):
            raise ValueError("Only an offer change carries the checkout total")
        return self


def conditional_view(
    resolved: ResolvedConditional,
    decision: ConditionalDecision,
    *,
    checkout_total_minor: int | None = None,
) -> ConditionalView:
    """The view after the decision and, with `checkout_total_minor`, after its checkout.
    `exclusions_only` has none: the exclusion shows as `rejections.excluded` (§3.10); nor has
    `clarify`, which is a question."""
    if decision.outcome in ("exclusions_only", "clarify"):
        raise ValueError(f"A {decision.outcome} decision has no conditional view")
    changed = (
        checkout_total_minor is not None
        and decision.chosen is not None
        and decision.chosen.offer is not None
        and checkout_total_minor != decision.chosen.offer.total_minor
    )
    return ConditionalView(
        outcome="offer_changed" if changed else decision.outcome,
        chosen_index=decision.chosen_index,
        currency="COP",
        branches=tuple(
            ConditionalBranchView(
                index=evaluation.index,
                variant_id=evaluation.variant_id,
                title=branch.title,
                status=evaluation.status,
                total_minor=None if evaluation.offer is None else evaluation.offer.total_minor,
                limit_minor=evaluation.limit_minor,
                items_total_minor=(
                    None if evaluation.offer is None else evaluation.offer.items_total_minor
                ),
                unit_limit_minor=evaluation.unit_limit_minor,
            )
            for branch, evaluation in zip(resolved.branches, decision.branches, strict=True)
        ),
        checkout_total_minor=checkout_total_minor if changed else None,
    )
