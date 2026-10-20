"""Scripted roles. These adapters do not call, emulate or measure an LLM."""

from collections.abc import Callable

from commerce_agent.governance.canonical import parse_model

from .context import check_refs
from .models import (
    CHECKS,
    REASONS,
    RELATED,
    BuyerContext,
    BuyerDraft,
    CheckResult,
    ProposalView,
    ReviewerContext,
    ValidatorAssessment,
    wire_bytes,
)
from .ports import RoleOutputError


class ScriptedBuyerAgent:
    adapter_id = "scripted-buyer:1"

    def __init__(self, response: object | Callable[[BuyerContext], object]):
        self._response = response

    def propose(self, context: BuyerContext) -> BuyerDraft:
        result = self._response(context) if callable(self._response) else self._response
        try:
            return parse_model(BuyerDraft, wire_bytes(result))
        except Exception:
            raise RoleOutputError("BUYER_OUTPUT_INVALID") from None


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def reviewer_eligible_variants(c: ReviewerContext) -> frozenset[str] | None:
    """Hard-constraint eligibility recomputed from public candidate facts.

    None means a hard constraint (category, option or term) is not visible in the candidate
    projection, so the reviewer cannot claim the selection passes. Availability of the chosen
    variant is reviewed through the offer terms.
    """
    hard = c.intent.hard
    if hard.categories or hard.required_options or hard.required_terms:
        return None
    amounts_limited = (
        hard.max_unit_amount_minor is not None or hard.max_total_amount_minor is not None
    )
    if hard.currency is None and amounts_limited and len({x.currency for x in c.candidates}) > 1:
        return frozenset()
    required = {_normalized(value) for value in hard.conditions}
    return frozenset(
        x.variant_id
        for x in c.candidates
        if (hard.currency is None or x.currency == hard.currency)
        and (
            hard.max_unit_amount_minor is None
            or x.observed_unit_minor <= hard.max_unit_amount_minor
        )
        and required <= {_normalized(value) for value in x.conditions}
    )


def independent_assessment(proposal: ProposalView, c: ReviewerContext) -> ValidatorAssessment:
    """Compare original resolved facts directly; never call backend controls or oracle."""
    d, t, grant = proposal.draft, c.terms, c.delegation
    selected = next((item for item in c.candidates if item.variant_id == d.variant_id), None)
    eligible = reviewer_eligible_variants(c)
    limits = (c.intent.hard.max_unit_amount_minor, c.intent.hard.max_total_amount_minor)
    amounts = (t.unit_minor, t.total_minor)
    verdicts = (
        selected is not None
        and selected.product_id == d.product_id
        and (None if eligible is None else d.variant_id in eligible),
        d.merchant_ref == t.merchant_ref == c.authorized_merchant_ref,
        all(limit is None or amount <= limit for limit, amount in zip(limits, amounts, strict=True))
        and d.claimed_unit_minor == t.unit_minor
        and d.claimed_total_minor == t.total_minor
        and t.total_minor <= grant.total_max_minor,
        d.offer_ref == t.offer_ref and t.available,
        grant.human_consent,
        grant.valid_from <= c.authority_now < grant.expires_at and grant.state != "expired",
        grant.state == "active",
        not (grant.uses_consumed or grant.uses_reserved_other),
        frozenset(d.acknowledged_constraints) == frozenset(c.required_constraint_ids),
        (d.product_id, d.variant_id, d.quantity, d.currency)
        == (t.product_id, t.variant_id, 1, t.currency)
        and t.quantity == grant.quantity_max == 1
        and t.variant_id in grant.variant_ids
        and t.currency == grant.currency == c.intent.hard.currency
        and t.merchant_ref == grant.merchant_ref
        and t.destination_ref == grant.destination_ref,
        (proposal.episode_ref, proposal.intent_ref) == (c.episode_ref, c.intent_ref),
    )
    checks = tuple(
        CheckResult(
            check_id=check,
            result="unresolved" if passed is None else ("pass" if passed else "fail"),
            related_policy_ids=RELATED[check],
            fact_refs=check_refs(check, c),
        )
        for check, passed in zip(CHECKS, verdicts, strict=True)
    )
    results = {x.result for x in checks}
    return ValidatorAssessment(
        proposal_digest=proposal.proposal_digest,
        context_ref=c.context_ref,
        validator_adapter_id="scripted-validator:1",
        verdict="reject"
        if "fail" in results
        else ("abstain" if "unresolved" in results else "accept"),
        checks=checks,
        coverage=CHECKS,
        reasons=tuple(REASONS[x.check_id] for x in checks if x.result != "pass"),
    )


class ScriptedValidatorAgent:
    adapter_id = "scripted-validator:1"

    def __init__(
        self,
        response: Callable[[ProposalView, ReviewerContext], object] = independent_assessment,
    ):
        self._response = response

    def review(self, proposal: ProposalView, context: ReviewerContext) -> ValidatorAssessment:
        # Fresh objects; non-authoritative justification is intentionally absent.
        proposal = parse_model(ProposalView, wire_bytes(proposal))
        context = parse_model(ReviewerContext, wire_bytes(context))
        raw = self._response(proposal, context)
        try:
            return parse_model(ValidatorAssessment, wire_bytes(raw))
        except Exception:
            raise RoleOutputError("VALIDATOR_OUTPUT_INVALID") from None
