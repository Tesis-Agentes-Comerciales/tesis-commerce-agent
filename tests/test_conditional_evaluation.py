"""Resolution and evaluation of conditional branches (p0-conditional-intent.md §3.2, §3.3, §3.6).

The five UX03 cells of Maldo's P3 protocol (docs/design/p3-ux03-protocol.md §5, PR #158) are
evaluator cases here, with the limits T-1/T taken from the offer (D9). The evaluator is pure: it
receives the readings; `evaluate_branches` is the only part that reads, and it reads lazily, so
B is never read when A decides, and nothing after an unknown or `intent_mismatch` branch is read.
"""

import functools
import itertools
from collections.abc import Mapping, Sequence

import pytest
from pydantic import ValidationError

from commerce_agent.acp.offer_snapshot import DeliveryContext, MerchantOffer
from commerce_agent.application.conditional import (
    EXCLUSIONS_ONLY,
    ConditionalClarification,
    ConditionalDecision,
    MissingOfferReading,
    OfferReadFailure,
    OfferReading,
    ResolvedConditional,
    effective_intent,
    evaluate_branches,
    evaluate_readings,
    next_offer_to_read,
    read_offer,
    resolve_branches,
    without_variants,
)
from commerce_agent.application.offer_freshness import LAB_DESTINATION
from commerce_agent.domain import HardConstraints, PurchaseIntent, SoftPreferences
from commerce_agent.domain.conditional import BranchMatch, ConditionalIntent, PurchaseBranch
from commerce_agent.domain.intent import OptionConstraint
from commerce_agent.domain.selection import rank_candidates, rejection_reason
from tests.support.conditional import (
    A_III,
    A_REFURB,
    PRICES,
    SEARCH,
    T_A,
    T_B,
    A,
    B,
    C,
    FakeOfferReader,
    acp_error,
    match_a,
    match_b,
    offer,
    phone_feed,
    ux03,
)


def resolved(
    intent: ConditionalIntent, search: HardConstraints = SEARCH, **feed: object
) -> ResolvedConditional:
    result = resolve_branches(intent, phone_feed(**feed), hard=search)  # type: ignore[arg-type]
    assert isinstance(result, ResolvedConditional), result
    return result


def statuses(decision: ConditionalDecision) -> list[str]:
    return [branch.status for branch in decision.branches]


def run(
    intent: ConditionalIntent,
    answers: Mapping[str, MerchantOffer | Exception],
    search: HardConstraints = SEARCH,
    **feed: object,
) -> tuple[ConditionalDecision, list[str]]:
    reader = FakeOfferReader(dict(answers))
    decision = evaluate_branches(resolved(intent, search, **feed), reader)
    assert all(context == LAB_DESTINATION for _, context in reader.reads)
    return decision, reader.read_ids


# The five UX03 cells (P3 §5) and their oracle labels (spec §3.3, §4.2).


def test_ux03_a_takes_a_at_its_exact_total_and_never_reads_b() -> None:
    decision, reads = run(ux03(T_A, T_B - 1), {A: offer(A), B: offer(B)})
    assert decision.label == "A"
    assert decision.outcome == "branch" and decision.chosen_index == 0
    assert statuses(decision) == ["eligible", "not_evaluated"]
    assert reads == [A]
    chosen = decision.chosen
    assert chosen is not None and chosen.variant_id == A
    assert chosen.offer is not None and chosen.offer.total_minor == T_A == chosen.limit_minor


def test_ux03_b_falls_back_when_a_is_over_its_own_limit() -> None:
    decision, reads = run(ux03(T_A - 1, T_B), {A: offer(A), B: offer(B)})
    assert decision.label == "B"
    assert statuses(decision) == ["over_limit", "eligible"]
    first, second = decision.branches
    assert first.offer is not None and first.offer.total_minor == T_A
    assert first.limit_minor == T_A - 1
    assert second.offer is not None and second.offer.total_minor == T_B == second.limit_minor
    assert reads == [A, B]


def test_ux03_c_priority_wins_over_a_cheaper_b() -> None:
    assert T_B < T_A
    decision, reads = run(ux03(T_A, T_B), {A: offer(A), B: offer(B)})
    assert decision.label == "A"
    assert reads == [A]


def test_ux03_d_is_none_and_never_reads_c() -> None:
    decision, reads = run(ux03(T_A - 1, T_B - 1), {A: offer(A), B: offer(B)})
    assert decision.label == "none"
    assert decision.outcome == "none" and decision.chosen is None
    assert statuses(decision) == ["over_limit", "over_limit"]
    assert reads == [A, B]
    assert C not in reads


@pytest.mark.parametrize(
    ("failure", "code", "status_code"),
    [
        (acp_error("timeout"), "timeout", None),
        (acp_error("transport_error"), "transport_error", None),
        (acp_error("http_error", 401), "http_error", 401),
        (acp_error("http_error", 429), "http_error", 429),
        (acp_error("http_error", 503), "http_error", 503),
        (acp_error("invalid_offer"), "invalid_offer", None),  # OFFER_NOT_FOUND, invalid JSON
        (acp_error("offer_expired"), "offer_expired", None),
        (acp_error("offer_mismatch"), "offer_mismatch", None),
        (RuntimeError("anything else"), "offer_read_failed", None),
    ],
)
def test_ux03_unknown_a_is_undetermined_and_never_falls_back_to_b(
    failure: Exception, code: str, status_code: int | None
) -> None:
    """D3: no read failure ever becomes `out_of_stock` or `over_limit`."""
    decision, reads = run(ux03(T_A, T_B), {A: failure, B: offer(B)})
    assert decision.label == "undetermined"
    assert decision.outcome == "undetermined" and decision.chosen is None
    assert statuses(decision) == ["unknown", "not_evaluated"]
    unknown = decision.branches[0]
    assert (unknown.error_code, unknown.status_code, unknown.offer) == (code, status_code, None)
    assert reads == [A]


# UX06 and UX07 (spec §4.2).


def fallback(global_limit: int | None) -> ConditionalIntent:
    """«Si A se agota, compra B», with one global limit."""
    return ConditionalIntent(
        branches=(
            PurchaseBranch(match=match_a()),
            PurchaseBranch(match=match_b(), only_if_previous_out_of_stock=True),
        ),
        max_total_amount_minor=global_limit,
        currency="COP",
    )


def test_ux06_hp_a_sold_out_and_b_over_the_budget_is_none() -> None:
    """UX06-HP."""
    decision, reads = run(fallback(T_B - 1), {A: offer(A, stock=0), B: offer(B)})
    assert decision.label == "none"
    assert statuses(decision) == ["out_of_stock", "over_limit"]
    assert reads == [A, B]


def test_ux06_hp_ctl_a_sold_out_and_b_at_the_budget_is_b() -> None:
    """UX06-HP-ctl."""
    decision, _ = run(fallback(T_B), {A: offer(A, stock=0), B: offer(B)})
    assert decision.label == "B"


def test_the_fallback_is_not_triggered_by_a_over_the_budget() -> None:
    """«Solo si A se agota»: A over the limit is not A sold out, so B is never read."""
    decision, reads = run(fallback(T_B), {A: offer(A), B: offer(B)})
    assert decision.label == "none"
    assert statuses(decision) == ["over_limit", "not_triggered"]
    assert reads == [A]


def test_the_fallback_counts_an_inactive_product_as_not_for_sale() -> None:
    decision, _ = run(fallback(T_B), {A: offer(A, status="inactive"), B: offer(B)})
    assert statuses(decision) == ["inactive", "eligible"]


def test_ux07_never_b_removes_b_from_the_feed_even_if_it_is_cheaper() -> None:
    """«Prefiero A, nunca compres B»: B leaves the turn's feed, so neither the shortlist nor D1
    can name it (D11); the preference for A stays soft."""
    feed = phone_feed()
    intent = ConditionalIntent(excluded=(match_b(),))
    result = resolve_branches(intent, feed, hard=SEARCH)
    assert isinstance(result, ResolvedConditional)
    assert result.branches == () and result.excluded_variant_ids == (B,)
    filtered = without_variants(feed, result.excluded_variant_ids)
    assert B not in {candidate.variant_id for candidate in filtered.candidates}
    assert {candidate.variant_id for candidate in filtered.candidates} == (
        {candidate.variant_id for candidate in feed.candidates} - {B}
    )
    smartphones = PurchaseIntent(
        hard=HardConstraints(currency="COP", categories=("smartphones",), conditions=("new",)),
        preferences=SoftPreferences(terms=("xperia 5 ii",)),
    )
    # Without the ban, B is a candidate the selection model could choose.
    assert B in [item.candidate.variant_id for item in rank_candidates(feed, smartphones).ranked]
    ranked = [item.candidate.variant_id for item in rank_candidates(filtered, smartphones).ranked]
    assert B not in ranked and ranked[0] == A


def test_ux07_a_branch_that_is_banned_asks_instead_of_buying() -> None:
    intent = ConditionalIntent(
        branches=(PurchaseBranch(match=match_a()), PurchaseBranch(match=match_b())),
        excluded=(BranchMatch(variant_id=B),),
        currency="COP",
    )
    result = resolve_branches(intent, phone_feed(), hard=SEARCH)
    assert isinstance(result, ConditionalClarification)
    assert (result.cause, result.branch_index, result.variant_ids) == ("branch_excluded", 1, (B,))
    assert result.excluded_variant_ids == (B,)


def test_an_excluded_branch_cannot_be_built_by_hand() -> None:
    """Defense in depth: the evaluator only ever sees branches that are not banned."""
    good = resolved(ux03(T_A, T_B))
    with pytest.raises(ValidationError):
        ResolvedConditional(
            intent=good.intent, hard=SEARCH, branches=good.branches, excluded_variant_ids=(B,)
        )


def test_without_variants_drops_products_left_empty_and_keeps_the_rest() -> None:
    feed = phone_feed()
    filtered = without_variants(feed, (C,))
    assert [p.title for p in filtered.products] == [
        p.title for p in feed.products if p.title != "Motorola Moto G54"
    ]
    assert without_variants(feed, ()) == feed
    assert feed.candidate(C) is not None  # the original is untouched


# Boundaries (spec §3.3, step 3).


@pytest.mark.parametrize(
    ("total", "status"), [(T_A - 1, "eligible"), (T_A, "eligible"), (T_A + 1, "over_limit")]
)
def test_the_total_equal_to_the_limit_is_eligible_and_one_more_is_not(
    total: int, status: str
) -> None:
    single = ConditionalIntent(
        branches=(PurchaseBranch(match=match_a(), max_total_amount_minor=T_A),), currency="COP"
    )
    decision, _ = run(single, {A: offer(A, price=total - 2_500_000)})
    assert statuses(decision) == [status]


def test_a_smaller_global_limit_overrides_the_branch_limit() -> None:
    intent = ux03(T_A, T_B).model_copy(update={"max_total_amount_minor": T_A - 1})
    decision, reads = run(intent, {A: offer(A), B: offer(B)})
    assert decision.label == "B"
    assert [branch.limit_minor for branch in decision.branches] == [T_A - 1, T_B]
    assert reads == [A, B]


def test_shipping_is_part_of_the_total() -> None:
    """The feed price of A fits; the Merchant total with shipping does not."""
    single = ConditionalIntent(
        branches=(PurchaseBranch(match=match_a(), max_total_amount_minor=T_A - 1),),
        currency="COP",
    )
    decision, _ = run(single, {A: offer(A)})
    assert statuses(decision) == ["over_limit"]


def test_an_inactive_branch_is_false_and_the_next_one_is_evaluated() -> None:
    decision, reads = run(ux03(T_A, T_B), {A: offer(A, status="inactive"), B: offer(B)})
    assert decision.label == "B"
    assert statuses(decision) == ["inactive", "eligible"]
    assert reads == [A, B]


def test_a_sold_out_branch_is_false_even_if_the_feed_still_lists_it() -> None:
    decision, _ = run(ux03(T_A, T_B), {A: offer(A, stock=0), B: offer(B)})
    assert statuses(decision) == ["out_of_stock", "eligible"]


def test_feed_offer_mismatch_is_undetermined_and_stops() -> None:
    """The offer sells A but the turn's feed says it is unavailable: `start` would reject it
    on its re-rank, so the branch is unknown and B is never read (D3)."""
    decision, reads = run(ux03(T_A, T_B), {A: offer(A), B: offer(B)}, unavailable=(A,))
    assert decision.label == "undetermined"
    first = decision.branches[0]
    assert (first.status, first.error_code) == ("unknown", "feed_offer_mismatch")
    assert first.offer is not None and first.offer.total_minor == T_A
    assert reads == [A]


def test_an_offer_of_another_variant_is_undetermined() -> None:
    readings: dict[int, OfferReading] = {0: offer(A, offer_id=A_III)}
    decision = evaluate_readings(resolved(ux03(T_A, T_B)), readings)
    first = decision.branches[0]
    assert (decision.label, first.error_code, first.offer) == (
        "undetermined",
        "offer_mismatch",
        None,
    )


def test_the_offer_facts_are_kept_for_the_record() -> None:
    decision, _ = run(ux03(T_A - 1, T_B), {A: offer(A, revision=11), B: offer(B)})
    facts = decision.branches[0].offer
    assert facts is not None
    assert facts.model_dump() == {
        "revision": 11,
        "product_status": "active",
        "availability": "in_stock",
        "available_quantity": 3,
        "items_total_minor": T_A - 2_500_000,
        "shipping_total_minor": 2_500_000,
        "total_minor": T_A,
        "expires_at": "2026-11-05T13:00:00Z",
        "offer_snapshot_sha256": facts.offer_snapshot_sha256,
    }
    assert len(facts.offer_snapshot_sha256) == 64


def test_three_branches_and_a_branch_without_limit() -> None:
    intent = ConditionalIntent(
        branches=(
            PurchaseBranch(match=match_a(), max_total_amount_minor=T_A - 1),
            PurchaseBranch(match=match_b(), max_total_amount_minor=T_B - 1),
            PurchaseBranch(match=BranchMatch(variant_id=C)),
        ),
        currency="COP",
    )
    decision, reads = run(intent, {A: offer(A), B: offer(B), C: offer(C)})
    assert (decision.label, decision.chosen_index) == ("C", 2)
    assert decision.branches[2].limit_minor is None
    assert reads == [A, B, C]


def fallback_c(limit_a: int | None) -> ConditionalIntent:
    """«A (≤ limit); si no, B; y si se agotan, C»."""
    return ConditionalIntent(
        branches=(
            PurchaseBranch(match=match_a(), max_total_amount_minor=limit_a),
            PurchaseBranch(match=match_b()),
            PurchaseBranch(match=BranchMatch(variant_id=C), only_if_previous_out_of_stock=True),
        ),
        currency="COP",
    )


def test_a_fallback_needs_every_earlier_branch_not_for_sale() -> None:
    """§3.3, step 1: A over its limit is not A sold out, so B sold out does not trigger C."""
    decision, reads = run(fallback_c(T_A - 1), {A: offer(A), B: offer(B, stock=0), C: offer(C)})
    assert statuses(decision) == ["over_limit", "out_of_stock", "not_triggered"]
    assert (decision.label, reads) == ("none", [A, B])
    decision, reads = run(
        fallback_c(None), {A: offer(A, status="inactive"), B: offer(B, stock=0), C: offer(C)}
    )
    assert statuses(decision) == ["inactive", "out_of_stock", "eligible"]
    assert (decision.label, reads) == ("C", [A, B, C])


def test_the_pure_evaluator_needs_the_reading_it_asks_for() -> None:
    branches = resolved(ux03(T_A - 1, T_B))
    assert next_offer_to_read(branches, {}) == 0
    assert next_offer_to_read(branches, {0: offer(A)}) == 1
    assert next_offer_to_read(branches, {0: offer(A), 1: offer(B)}) is None
    with pytest.raises(MissingOfferReading) as missing:
        evaluate_readings(branches, {0: offer(A)})
    assert missing.value.index == 1
    # Readings it does not need are ignored, so a reader that read too much changes nothing.
    assert evaluate_readings(resolved(ux03(T_A, T_B)), {0: offer(A), 1: offer(B)}).label == "A"


def test_an_exclusion_only_intent_evaluates_and_reads_nothing() -> None:
    """UX07: `exclusions_only` is not a label; the normal selection runs on the filtered feed."""
    nothing = resolve_branches(ConditionalIntent(excluded=(match_b(),)), phone_feed(), hard=SEARCH)
    assert isinstance(nothing, ResolvedConditional)
    reader = FakeOfferReader({})
    decision = evaluate_branches(nothing, reader)
    assert decision == EXCLUSIONS_ONLY == evaluate_readings(nothing, {})
    assert (decision.outcome, decision.label, decision.branches, reader.reads) == (
        "exclusions_only",
        None,
        (),
        [],
    )
    assert next_offer_to_read(nothing, {}) is None


@pytest.mark.parametrize(
    "fields",
    [
        {"outcome": "exclusions_only", "chosen_index": 0, "branches": ()},
        {"outcome": "none", "chosen_index": None, "branches": ()},
    ],
)
def test_only_an_exclusion_only_decision_has_no_branches(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ConditionalDecision.model_validate(fields)


def test_read_offer_turns_every_failure_into_a_reading() -> None:
    reader = FakeOfferReader({A: acp_error("http_error", 503), B: offer(B), C: offer(B)})
    assert read_offer(reader, A) == OfferReadFailure(code="http_error", status_code=503)
    assert read_offer(reader, B) == offer(B)
    assert read_offer(reader, C) == OfferReadFailure(code="offer_mismatch", status_code=None)
    elsewhere = DeliveryContext.model_construct(country="CO", city="Medellin", postal_code="050001")
    assert read_offer(reader, B, elsewhere) == OfferReadFailure(code="offer_mismatch")


# Resolution against the feed and ambiguity (spec §3.2, D5).


def clarify(
    intent: ConditionalIntent, search: HardConstraints = SEARCH, **feed: object
) -> ConditionalClarification:
    result = resolve_branches(intent, phone_feed(**feed), hard=search)  # type: ignore[arg-type]
    assert isinstance(result, ConditionalClarification), result
    return result


def one(match: BranchMatch, limit: int | None = None) -> ConditionalIntent:
    return ConditionalIntent(
        branches=(PurchaseBranch(match=match, max_total_amount_minor=limit),), currency="COP"
    )


def test_xperia_5_ii_is_not_a_substring_match_for_the_iii() -> None:
    """The risk of §10: substring matching takes «xperia 5 ii» for the III too. Resolution
    matches whole tokens, so A is exactly one variant; D1's substring filter still keeps it."""
    terms = PurchaseIntent(hard=HardConstraints(required_terms=("xperia 5 ii",)))
    substring = {item.candidate.variant_id for item in rank_candidates(phone_feed(), terms).ranked}
    assert {A, A_III} <= substring
    branches = resolved(one(match_a()))
    assert [branch.variant_id for branch in branches.branches] == [A]
    assert branches.branches[0].title == "Sony Xperia 5 II 256 GB negro"
    iii = resolved(one(BranchMatch(required_terms=("xperia 5 iii",))))
    assert [branch.variant_id for branch in iii.branches] == [A_III]


@pytest.mark.parametrize(
    ("match", "candidates"),
    [
        (BranchMatch(required_terms=("xperia 5",)), {A, A_III, A_REFURB}),
        (BranchMatch(required_terms=("xperia 5 ii",)), {A, A_REFURB}),
        (BranchMatch(categories=("smartphones",), conditions=("new",)), {A, A_III, B, C}),
    ],
)
def test_a_branch_with_several_variants_asks_which_one(
    match: BranchMatch, candidates: set[str]
) -> None:
    result = clarify(one(match))
    assert (result.cause, result.branch_index) == ("branch_ambiguous", 0)
    assert set(result.variant_ids) <= candidates
    assert len(result.variant_ids) == min(3, len(candidates))
    assert len(result.titles) == len(result.variant_ids)


@pytest.mark.parametrize(
    "match",
    [
        BranchMatch(required_terms=("pixel 9",)),
        BranchMatch(required_terms=("xperia", "128 gb")),
        BranchMatch(variant_id="SON-X5II-512-BLK"),
        BranchMatch(variant_id=A, conditions=("refurbished",)),  # id and criteria disagree
        BranchMatch(required_terms=("oneplus 8",)),  # a partial token: «8» is not «8t»
    ],
)
def test_a_branch_without_a_variant_asks(match: BranchMatch) -> None:
    result = clarify(one(match))
    assert (result.cause, result.branch_index, result.variant_ids) == ("branch_not_found", 0, ())


def test_a_branch_resolves_by_variant_id_even_if_the_feed_says_unavailable() -> None:
    """Availability is the offer's to decide; the feed only says what the variant is."""
    branches = resolved(one(BranchMatch(variant_id=A)), unavailable=(A,))
    [branch] = branches.branches
    assert (branch.variant_id, branch.feed_available) == (A, False)


def test_two_branches_on_the_same_variant_ask() -> None:
    intent = ConditionalIntent(
        branches=(PurchaseBranch(match=match_a()), PurchaseBranch(match=BranchMatch(variant_id=A))),
        currency="COP",
    )
    result = clarify(intent)
    assert (result.cause, result.branch_index, result.variant_ids) == ("duplicate_branch", 1, (A,))


def test_a_turn_in_another_currency_asks() -> None:
    """§3.2, step 4. `conditional.currency` can only be COP (test_conditional_intent.py)."""
    result = clarify(ux03(T_A, T_B), HardConstraints(currency="USD"))
    assert (result.cause, result.branch_index) == ("currency", None)


@pytest.mark.parametrize("search", [HardConstraints(), HardConstraints(currency="COP")])
def test_without_a_currency_the_effective_intent_is_in_cop(search: HardConstraints) -> None:
    """§3.1: «la moneda es COP». The router asks when the buyer's currency is unclear (D5)."""
    intent = ux03(T_A, T_B).model_copy(update={"currency": None})
    branches = resolved(intent, search)
    assert branches.branches[0].variant_id == A
    assert effective_intent(branches, 0).hard.currency == "COP"


def test_a_branch_priced_in_another_currency_is_an_intent_mismatch() -> None:
    """The effective intent is in COP, so the variant fails it: never read, never bought."""
    decision, reads = run(ux03(T_A, T_B), {A: offer(A), B: offer(B)}, currency={A: "USD"})
    first = decision.branches[0]
    assert (first.status, first.rejection_code) == ("intent_mismatch", "currency")
    assert (decision.outcome, decision.label, reads) == ("clarify", None, [B])


def test_without_limits_the_currency_may_be_omitted() -> None:
    intent = ConditionalIntent(
        branches=(
            PurchaseBranch(match=match_a()),
            PurchaseBranch(match=match_b(), only_if_previous_out_of_stock=True),
        )
    )
    assert isinstance(
        resolve_branches(intent, phone_feed(), hard=HardConstraints()), ResolvedConditional
    )


def test_the_first_problem_in_branch_order_is_the_one_asked() -> None:
    intent = ConditionalIntent(
        branches=(
            PurchaseBranch(match=BranchMatch(required_terms=("pixel 9",))),
            PurchaseBranch(match=BranchMatch(required_terms=("xperia 5",))),
        ),
        currency="COP",
    )
    assert (clarify(intent).cause, clarify(intent).branch_index) == ("branch_not_found", 0)


def test_an_exclusion_that_matches_nothing_changes_nothing() -> None:
    intent = ux03(T_A, T_B).model_copy(
        update={"excluded": (BranchMatch(required_terms=("pixel 9",)),)}
    )
    assert resolved(intent).excluded_variant_ids == ()


def test_an_exclusion_marks_every_variant_it_matches() -> None:
    intent = ux03(T_A, T_B).model_copy(
        update={
            "excluded": (BranchMatch(required_terms=("xperia 5 iii",)), BranchMatch(variant_id=C))
        }
    )
    assert resolved(intent).excluded_variant_ids == tuple(sorted((A_III, C)))


def test_the_effective_intent_keeps_the_branch_through_the_purchase_re_rank() -> None:
    """§3.4: `start` re-ranks the turn's feed with the effective intent; the resolved variant
    must survive it, and D1's eligible set is that ranking."""
    branches = resolved(ux03(T_A - 1, T_B))
    for branch in branches.branches:
        intent = effective_intent(branches, branch.index)
        assert intent.hard.currency == "COP"
        assert intent.hard.max_total_amount_minor == branch.limit_minor
        assert intent.preferences == SoftPreferences()
        ranked = rank_candidates(phone_feed(), intent).ranked
        assert branch.variant_id in [item.candidate.variant_id for item in ranked]


# The turn's `intent.hard`: the effective intent hardens it and never widens it (B2).


def test_ux06_hp_hard_takes_the_turns_total_limit_when_no_branch_has_one() -> None:
    """UX06-HP-hard (D14): «si A se agota, compra B» with the budget only in `intent.hard`. The
    limit is that one, never None, so B over it is not bought."""
    search = HardConstraints(currency="COP", max_total_amount_minor=T_B - 1)
    decision, reads = run(fallback(None), {A: offer(A, stock=0), B: offer(B)}, search)
    assert [branch.limit_minor for branch in decision.branches] == [T_B - 1, T_B - 1]
    assert (decision.label, statuses(decision), reads) == (
        "none",
        ["out_of_stock", "over_limit"],
        [A, B],
    )
    control = HardConstraints(currency="COP", max_total_amount_minor=T_B)
    assert run(fallback(None), {A: offer(A, stock=0), B: offer(B)}, control)[0].label == "B"


@pytest.mark.parametrize(
    ("search", "code", "index", "reads"),
    [
        # A is over its limit; B, reached next, is not a Sony.
        (HardConstraints(currency="COP", required_terms=("sony",)), "term", 1, [A]),
        (
            HardConstraints(
                currency="COP", required_options=(OptionConstraint(name="Color", value="negro"),)
            ),
            "option",
            1,
            [A],
        ),
        # Neither is: nothing is read.
        (HardConstraints(currency="COP", categories=("headphones",)), "category", 0, []),
        (HardConstraints(currency="COP", conditions=("refurbished",)), "condition", 0, []),
    ],
)
def test_a_branch_against_the_turns_filters_is_never_read_nor_bought(
    search: HardConstraints, code: str, index: int, reads: list[str]
) -> None:
    """§3.3, step 2: `intent_mismatch`, with the `rejection_reason` code and no offer. With no
    eligible branch after it, the decision is `none`."""
    decision, read = run(ux03(T_A - 1, T_B), {A: offer(A), B: offer(B)}, search)
    mismatch = decision.branches[index]
    assert (mismatch.status, mismatch.rejection_code, mismatch.offer) == (
        "intent_mismatch",
        code,
        None,
    )
    assert (decision.outcome, decision.label, decision.chosen, read) == (
        "none",
        "none",
        None,
        reads,
    )


def test_a_mismatched_a_is_not_skipped_to_buy_an_eligible_b() -> None:
    """A contradicts `intent.hard` and B is eligible: B is not bought; the agent asks (HP) or
    escalates (HNP), as for an excluded branch, since A fell without a commercial fact."""
    search = HardConstraints(currency="COP", required_terms=("oneplus",))
    assert resolved(ux03(T_A, T_B), search).branches[1].rejection_code is None
    decision, reads = run(ux03(T_A, T_B), {A: offer(A), B: offer(B)}, search)
    assert (decision.outcome, decision.label, decision.chosen) == ("clarify", None, None)
    assert (statuses(decision), reads) == (["intent_mismatch", "eligible"], [B])
    assert decision.branches[0].rejection_code == "term"


@pytest.mark.parametrize(
    ("answer_b", "outcome", "status_b"),
    [
        (offer(B, stock=0), "none", "out_of_stock"),
        (acp_error("timeout"), "undetermined", "unknown"),
    ],
)
def test_after_a_mismatch_a_false_or_unknown_branch_decides_as_usual(
    answer_b: MerchantOffer | Exception, outcome: str, status_b: str
) -> None:
    search = HardConstraints(currency="COP", required_terms=("oneplus",))
    decision, reads = run(ux03(T_A, T_B), {A: offer(A), B: answer_b}, search)
    assert (decision.outcome, statuses(decision), reads) == (
        outcome,
        ["intent_mismatch", status_b],
        [B],
    )


def test_a_mismatch_after_the_chosen_branch_changes_nothing() -> None:
    search = HardConstraints(currency="COP", required_terms=("sony",))
    decision, reads = run(ux03(T_A, T_B), {A: offer(A), B: offer(B)}, search)
    assert (decision.label, statuses(decision), reads) == ("A", ["eligible", "not_evaluated"], [A])


def test_a_mismatch_does_not_trigger_a_fallback() -> None:
    """A mismatched A is not A sold out: «solo si A se agota» does not hold."""
    search = HardConstraints(currency="COP", required_terms=("oneplus",))
    decision, reads = run(fallback(None), {A: offer(A, stock=0), B: offer(B)}, search)
    assert (decision.outcome, statuses(decision), reads) == (
        "none",
        ["intent_mismatch", "not_triggered"],
        [],
    )


def test_property_no_branch_after_a_mismatch_is_ever_chosen() -> None:
    """Every order of A, B and C with a turn that only C fails, and every reading."""
    search = HardConstraints(currency="COP", required_terms=("256 gb",))
    seen: set[str] = set()
    for order in itertools.permutations(VARIANTS):
        intent = ConditionalIntent(
            branches=tuple(PurchaseBranch(match=BranchMatch(variant_id=v)) for v in order),
            currency="COP",
        )
        branches = resolved(intent, search)
        assert [b.rejection_code is not None for b in branches.branches] == [v == C for v in order]
        for kinds in itertools.product(KINDS, repeat=2):
            kind = iter(kinds)
            readings = {i: reading(next(kind), v) for i, v in enumerate(order) if v != C}
            decision = evaluate_readings(branches, readings)
            seen.add(decision.outcome)
            found = statuses(decision)
            first = next((i for i, st in enumerate(found) if st in ("eligible", "unknown")), None)
            before = first is not None and "intent_mismatch" in found[:first]
            if decision.chosen is not None:
                assert "intent_mismatch" not in found[: decision.chosen.index]
            if first is not None and found[first] == "eligible":
                assert (decision.outcome == "clarify") == before
    assert seen == {"branch", "clarify", "none", "undetermined"}


# The unit limit of `intent.hard` is judged on the offer, never on the feed's price.


def unit(limit: int) -> HardConstraints:
    return HardConstraints(currency="COP", max_unit_amount_minor=limit)


def test_a_feed_price_over_the_unit_limit_is_a_feed_budget_mark_not_a_mismatch() -> None:
    """§3.2, step 5: `max_unit` is left out of `rejection_reason`, whose "budget" would hide any
    other failing filter, and the branch is marked `feed_budget` for the offer to decide."""
    branches = resolved(ux03(T_A, T_B), unit(PRICES[A] - 1))
    assert [b.rejection_code for b in branches.branches] == [None, None]
    assert [b.feed_budget for b in branches.branches] == [True, False]
    assert [b.unit_limit_minor for b in branches.branches] == [PRICES[A] - 1] * 2
    hidden = HardConstraints(currency="COP", max_unit_amount_minor=1, conditions=("refurbished",))
    assert resolved(ux03(T_A, T_B), hidden).branches[0].rejection_code == "condition"


def test_an_offer_over_the_unit_limit_is_over_limit() -> None:
    """Feed and offer agree that A costs more than the unit limit: A is false, B is next."""
    decision, reads = run(ux03(T_A, T_B), {A: offer(A), B: offer(B)}, unit(PRICES[A] - 1))
    assert (decision.label, statuses(decision), reads) == ("B", ["over_limit", "eligible"], [A, B])
    assert decision.branches[0].unit_limit_minor == PRICES[A] - 1


def test_an_offer_over_the_unit_limit_is_over_limit_even_if_the_feed_fits() -> None:
    decision, _ = run(
        ux03(T_A + 10, T_B), {A: offer(A, price=PRICES[A] + 1), B: offer(B)}, unit(PRICES[A])
    )
    assert statuses(decision) == ["over_limit", "eligible"]


def test_an_offer_within_the_unit_limit_that_the_feed_prices_over_it_is_undetermined() -> None:
    """`start` re-ranks the feed and would refuse A: a disagreement, so B is never read (D3)."""
    decision, reads = run(
        ux03(T_A, T_B), {A: offer(A, price=PRICES[A] - 1), B: offer(B)}, unit(PRICES[A] - 1)
    )
    first = decision.branches[0]
    assert (first.status, first.error_code) == ("unknown", "feed_offer_mismatch")
    assert (decision.label, reads) == ("undetermined", [A])


def test_feed_budget_is_judged_before_the_total_limit() -> None:
    """§3.3, steps 4.5 and 4.6: within the unit limit by the offer but marked `feed_budget`,
    the branch is unknown even if its total is over the limit too."""
    decision, reads = run(
        ux03(T_A - 2, T_B), {A: offer(A, price=PRICES[A] - 1), B: offer(B)}, unit(PRICES[A] - 1)
    )
    first = decision.branches[0]
    assert (first.status, first.error_code, first.feed_budget) == (
        "unknown",
        "feed_offer_mismatch",
        True,
    )
    assert reads == [A]


@pytest.mark.parametrize("items", [PRICES[A] - 1, PRICES[A]])
def test_the_unit_limit_accepts_equality(items: int) -> None:
    decision, _ = run(ux03(T_A, T_B), {A: offer(A, price=items), B: offer(B)}, unit(PRICES[A]))
    assert decision.label == "A"


def test_the_effective_intent_keeps_every_filter_of_the_turn() -> None:
    search = HardConstraints(
        currency="COP",
        max_unit_amount_minor=PRICES[A],
        max_total_amount_minor=T_A + 5,
        categories=("smartphones",),
        conditions=("new",),
        required_options=(OptionConstraint(name="Almacenamiento", value="256 GB"),),
        required_terms=("256 gb",),
    )
    branches = resolved(ux03(T_A, T_B + 10), search)
    first = effective_intent(branches, 0).hard
    assert first.max_unit_amount_minor == PRICES[A]
    assert first.max_total_amount_minor == T_A
    assert effective_intent(branches, 1).hard.max_total_amount_minor == T_B + 10
    assert set(search.categories) <= set(first.categories)
    assert set(search.conditions) <= set(first.conditions)
    assert set(search.required_options) <= set(first.required_options)
    assert set(search.required_terms) <= set(first.required_terms)
    assert set(match_a().required_terms) <= set(first.required_terms)


SEARCHES = (
    HardConstraints(),
    HardConstraints(currency="COP"),
    HardConstraints(currency="COP", max_total_amount_minor=T_B),
    HardConstraints(currency="COP", max_unit_amount_minor=PRICES[B]),
    HardConstraints(currency="COP", conditions=("new",), categories=("smartphones",)),
    HardConstraints(currency="COP", required_terms=("sony",)),
    HardConstraints(
        currency="COP", required_options=(OptionConstraint(name="Color", value="negro"),)
    ),
)
MATCHES = (
    match_a(),
    match_b(),
    BranchMatch(variant_id=C),
    BranchMatch(variant_id=A_REFURB),
    BranchMatch(required_terms=("xperia 5 iii",), conditions=("new",)),
)


@pytest.mark.parametrize("search", SEARCHES)
@pytest.mark.parametrize("match", MATCHES)
@pytest.mark.parametrize("limits", [(None, None), (T_A, None), (None, T_B - 1), (T_A, T_B + 1)])
def test_property_the_effective_intent_is_a_subset_of_the_turns_intent(
    search: HardConstraints, match: BranchMatch, limits: tuple[int | None, int | None]
) -> None:
    branch_limit, global_limit = limits
    intent = ConditionalIntent(
        branches=(PurchaseBranch(match=match, max_total_amount_minor=branch_limit),),
        max_total_amount_minor=global_limit,
        currency="COP",
    )
    feed = phone_feed()
    result = resolve_branches(intent, feed, hard=search)
    assert isinstance(result, ResolvedConditional)
    [branch] = result.branches
    effective = effective_intent(result, 0)
    original = PurchaseIntent(hard=search)
    for candidate in feed.candidates:
        if rejection_reason(candidate, effective) is None:
            assert rejection_reason(candidate, original) is None
    narrower = {item.candidate.variant_id for item in rank_candidates(feed, effective).ranked}
    wider = {item.candidate.variant_id for item in rank_candidates(feed, original).ranked}
    assert narrower <= wider
    assert effective.hard.max_unit_amount_minor == search.max_unit_amount_minor
    limits_set = (branch_limit, global_limit, search.max_total_amount_minor)
    present = [limit for limit in limits_set if limit is not None]
    assert effective.hard.max_total_amount_minor == (min(present) if present else None)
    # A branch that survives the turn's filters survives `start`; one that does not is never
    # bought: `intent_mismatch`, or over the unit limit by the feed (judged on the offer).
    assert (branch.rejection_code is None and not branch.feed_budget) == (
        branch.variant_id in narrower
    )


# Properties: exhaustive over small domains, without a property-testing dependency.

L = 100_000_000
KINDS = ("error", "inactive", "sold_out", "below", "equal", "above")
VARIANTS = (A, B, C)


@functools.cache
def reading(kind: str, variant_id: str) -> OfferReading:
    match kind:
        case "error":
            return OfferReadFailure(code="timeout")
        case "inactive":
            return offer(variant_id, status="inactive")
        case "sold_out":
            return offer(variant_id, stock=0)
        case _:
            total = {"below": L - 1, "equal": L, "above": L + 1}[kind]
            return offer(variant_id, price=total - 2_500_000)


def cases() -> Sequence[tuple[ResolvedConditional, dict[int, OfferReading]]]:
    """Every combination of 1 to 3 branches: fallback flags, per-branch and global limits, one
    reading per branch, and a feed that says B is unavailable or not."""
    built = []
    feeds = {hidden: phone_feed(unavailable=hidden) for hidden in ((), (B,))}
    for size in (1, 2, 3):
        for flags, limits, hidden, global_limit in itertools.product(
            itertools.product((False, True), repeat=size - 1),
            itertools.product((None, L), repeat=size),
            feeds,
            (None, L - 1),
        ):
            intent = ConditionalIntent(
                branches=tuple(
                    PurchaseBranch(
                        match=BranchMatch(variant_id=VARIANTS[i]),
                        max_total_amount_minor=limits[i],
                        only_if_previous_out_of_stock=(False, *flags)[i],
                    )
                    for i in range(size)
                ),
                max_total_amount_minor=global_limit,
                currency="COP",
                excluded=(BranchMatch(variant_id=A_III),),
            )
            result = resolve_branches(intent, feeds[hidden], hard=SEARCH)
            assert isinstance(result, ResolvedConditional)
            for kinds in itertools.product(KINDS, repeat=size):
                built.append(
                    (result, {i: reading(kind, VARIANTS[i]) for i, kind in enumerate(kinds)})
                )
    return built


CASES = cases()
DECISIONS = [evaluate_readings(branches, readings) for branches, readings in CASES]


def test_the_property_space_is_not_trivial() -> None:
    assert {decision.label for decision in DECISIONS} == {"A", "B", "C", "none", "undetermined"}
    assert {b.status for decision in DECISIONS for b in decision.branches} == {
        "eligible",
        "over_limit",
        "out_of_stock",
        "inactive",
        "unknown",
        "not_triggered",
        "not_evaluated",
    }
    assert len(CASES) > 10_000


def test_property_never_chooses_an_excluded_branch_or_one_over_its_limit() -> None:
    for (branches, _), decision in zip(CASES, DECISIONS, strict=True):
        chosen = decision.chosen
        if chosen is None:
            continue
        assert chosen.variant_id not in branches.excluded_variant_ids
        assert chosen.offer is not None
        assert chosen.limit_minor is None or chosen.offer.total_minor <= chosen.limit_minor
        assert chosen.offer.availability == "in_stock" and chosen.offer.product_status == "active"
        assert branches.branches[chosen.index].feed_available


def test_property_the_first_eligible_branch_wins_and_nothing_after_it_is_evaluated() -> None:
    for decision in DECISIONS:
        found = [b.index for b in decision.branches if b.status in ("eligible", "unknown")]
        assert len(found) <= 1
        if found:
            assert all(b.status == "not_evaluated" for b in decision.branches[found[0] + 1 :])
        if decision.outcome == "none":
            assert "not_evaluated" not in statuses(decision)


def test_property_undetermined_stops_the_evaluation() -> None:
    for decision in DECISIONS:
        unknown = [b.index for b in decision.branches if b.status == "unknown"]
        assert (decision.outcome == "undetermined") == bool(unknown)
        if unknown:
            assert decision.chosen is None
            assert all(b.status == "not_evaluated" for b in decision.branches[unknown[0] + 1 :])


def test_property_a_fallback_runs_only_after_every_earlier_branch_was_not_for_sale() -> None:
    for (branches, _), decision in zip(CASES, DECISIONS, strict=True):
        for branch, evaluation in zip(branches.branches, decision.branches, strict=True):
            if not branch.only_if_previous_out_of_stock or evaluation.status == "not_evaluated":
                continue
            earlier = decision.branches[: branch.index]
            triggered = all(b.status in ("out_of_stock", "inactive") for b in earlier)
            assert (evaluation.status != "not_triggered") == triggered


def test_property_lazy_reading_reads_exactly_what_the_pure_evaluator_needs() -> None:
    for (branches, readings), decision in list(zip(CASES, DECISIONS, strict=True))[::7]:
        answers: dict[str, MerchantOffer | Exception] = {}
        for index, value in readings.items():
            variant_id = branches.branches[index].variant_id
            answers[variant_id] = (
                acp_error(value.code) if isinstance(value, OfferReadFailure) else value
            )
        reader = FakeOfferReader(answers)
        assert evaluate_branches(branches, reader) == decision
        unread = ("not_triggered", "not_evaluated")
        assert reader.read_ids == [
            b.variant_id for b in decision.branches if b.status not in unread
        ]
