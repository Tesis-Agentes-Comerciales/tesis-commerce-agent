"""Mandatory experimental controls, independent of scripted validator code.

No POL-03--10 evaluation, signatures, grant reservation or commercial I/O occurs.
"""

from .models import CheckId, ProposalEnvelope, TrustedReviewContext


def mandatory_failures(
    proposal: ProposalEnvelope, context: TrustedReviewContext
) -> tuple[CheckId, ...]:
    d, t, g = proposal.draft, context.terms, context.delegation
    failed: list[CheckId] = []
    identities = {(x.product_id, x.variant_id) for x in context.candidates}
    if (
        d.product_id,
        d.variant_id,
    ) not in identities or d.variant_id not in context.eligible_variant_ids:
        failed.append("selection")
    if d.merchant_ref != context.authorized_merchant_ref or t.merchant_ref != d.merchant_ref:
        failed.append("merchant")
    hard = context.intent.hard
    if (
        d.claimed_total_minor is None
        or d.claimed_unit_minor is None
        or d.claimed_total_minor != t.total_minor
        or d.claimed_unit_minor != t.unit_minor
        or (hard.max_total_amount_minor is not None and t.total_minor > hard.max_total_amount_minor)
        or (hard.max_unit_amount_minor is not None and t.unit_minor > hard.max_unit_amount_minor)
        or t.total_minor > g.total_max_minor
    ):
        failed.append("budget")
    if d.offer_ref != t.offer_ref or not t.available:
        failed.append("offer")
    if not g.human_consent:
        failed.append("consent")
    if not g.valid_from <= context.authority_now < g.expires_at or g.state == "expired":
        failed.append("validity")
    if g.state != "active":
        failed.append("revocation")
    if g.uses_consumed != 0 or g.uses_reserved_other != 0:
        failed.append("usage")
    if set(d.acknowledged_constraints) != set(context.required_constraint_ids):
        failed.append("constraints")
    if (
        d.variant_id != t.variant_id
        or d.product_id != t.product_id
        or t.variant_id not in g.variant_ids
        or t.merchant_ref != g.merchant_ref
        or d.currency != t.currency
        or t.currency != g.currency
        or t.currency != hard.currency
        or d.quantity != 1
        or t.quantity != 1
        or g.quantity_max != 1
        or t.destination_ref != g.destination_ref
    ):
        failed.append("scope")
    if proposal.episode_ref != context.episode_ref or proposal.intent_ref != context.intent_ref:
        failed.append("binding")
    return tuple(failed)
