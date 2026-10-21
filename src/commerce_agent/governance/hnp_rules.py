"""Reference rules POL-07..POL-10 of Human Not Present (policy-v1 §6): pure, no store or clock.

Every rule of a phase is evaluated, in catalog order and without short-circuit, so a deny lists
every failing rule (N10 lists POL-09 and POL-10 when both fail). The D2/D3 boundary checks the
`*_prerequisite` guard first, as D2-HP does: incoherent facts, terms that are no longer current
and an unresolved profile or proof are operational denials that never reach a rule.

Each rule reads only its own facts, so a change the acceptance matrix varies alone fails one rule
alone: the grant's state and validity are POL-07 in D2 and POL-10 in D3; the amount against
`total_max_minor` is POL-08; uses, the reservation and its epoch are POL-09.
"""

from collections.abc import Sequence

from .hnp_models import (
    DispatchPolicyInput,
    GrantFacts,
    HnpAuthorizationPolicyInput,
    UsageFacts,
)
from .models import SCHEMA, Action, GuardReason, PolicyId, PrivateBinding, RuleResult

# The (D2, HNP) row is POL-03..06 and then these; the (D3, HNP) row is exactly POL-09 and POL-10.
DELEGATION_RULES: tuple[PolicyId, ...] = ("POL-07", "POL-08", "POL-09")
DISPATCH_RULES: tuple[PolicyId, ...] = ("POL-09", "POL-10")
# The states that hold the grant's single use (the `one_use_per_grant` index, minus `consumed`,
# which `uses_consumed` already counts).
_HOLDING = ("reserved", "committed", "uncertain")


def _in_force(grant: GrantFacts, now: str) -> bool:
    """Active and inside `[valid_from, expires_at)`: at `expires_at` it is already over.

    `UtcTime` is fixed-width millisecond UTC, so its strings order as the instants they name.
    """
    return grant.state == "active" and grant.valid_from <= now < grant.expires_at


def _same_binding(left: PrivateBinding, right: PrivateBinding) -> bool:
    # `binding_id` names where each HMAC was computed (proposal or checkout), not what it binds.
    return (left.scheme, left.key_id, left.value) == (right.scheme, right.key_id, right.value)


def delegation_active(data: HnpAuthorizationPolicyInput) -> bool:
    """POL-07: a grant in force, of this owner, bound to the verified Agent Key, for this action.

    The Agent Key is only verified by a `valid` open proof; an `unresolved` one never gets here.
    """
    grant, proof = data.hnp.grant, data.hnp.open_proof
    return (
        _in_force(grant, data.authority_now)
        and grant.owner_ref == data.owner_ref
        and proof.status == "valid"
        and proof.verified_agent_key_ref == grant.agent_key_ref
        and data.action in grant.permitted_actions
    )


def within_delegated_scope(data: HnpAuthorizationPolicyInput) -> bool:
    """POL-08: merchant, variant, currency, quantity, total, destination and instrument."""
    scope, terms = data.hnp.grant.scope, data.terms
    return (
        terms.merchant_ref in scope.merchant_refs
        and terms.variant_id in scope.variant_ids
        and terms.currency == scope.currency
        and terms.quantity <= scope.quantity_max
        and terms.total_minor <= scope.total_max_minor
        and _same_binding(terms.destination_binding, scope.destination_binding)
        and data.hnp.requested_instrument_ref == scope.instrument_ref
    )


def use_available(data: HnpAuthorizationPolicyInput) -> bool:
    """POL-09 in D2: no use consumed or held by another operation, and a reservation for this
    run, owner, operation, grant epoch and authoritative total; a retry reuses its own reservation
    only while it is still `reserved` and identical."""
    hnp, terms = data.hnp, data.terms
    usage, intent, grant = hnp.usage, hnp.reservation_intent, hnp.grant
    own = usage.same_operation_reservation
    return (
        usage.uses_consumed == 0
        and usage.uses_reserved_other == 0
        and intent.grant_ref == grant.grant_ref
        and intent.expected_epoch == grant.authority_epoch
        and (intent.run_id, intent.owner_ref, intent.operation_id)
        == (data.run_id, data.owner_ref, data.operation_id)
        and (intent.amount_minor, intent.currency) == (terms.total_minor, terms.currency)
        and (own is None or (own.state == "reserved" and own.intent() == intent))
    )


def reservation_held(data: DispatchPolicyInput) -> bool:
    """POL-09 in D3: this operation's own reservation, still `reserved`, under the current epoch,
    for the same run, owner, amount and currency, with no other use of the grant."""
    reservation, grant, usage, terms = data.reservation, data.grant, data.usage, data.terms
    return (
        reservation.state == "reserved"
        and usage.uses_consumed == 0
        and usage.uses_reserved_other == 0
        and reservation.grant_ref == grant.grant_ref
        and reservation.expected_epoch == grant.authority_epoch
        and (reservation.run_id, reservation.owner_ref, reservation.operation_id)
        == (data.run_id, data.owner_ref, data.operation_id)
        and (reservation.amount_minor, reservation.currency) == (terms.total_minor, terms.currency)
    )


def dispatch_authority_current(
    data: DispatchPolicyInput, allowed_actions: Sequence[Action]
) -> bool:
    """POL-10: the authority D2 relied on still holds when committing the dispatch.

    `allowed_actions` are the D3 actions of the verified policy artifact. The D2 allow must bind
    this intent, terms, grant, epoch, reservation and operation under the same policy, and the
    closed proof must bind the same Agent Key, authorization, operation, grant, terms and
    instrument.
    """
    principal, grant, d2, proof, terms = (
        data.principal,
        data.grant,
        data.d2,
        data.closed_proof,
        data.terms,
    )
    return (
        principal.authentication_basis == "service_and_grant"
        and principal.authentication_verified
        and principal.owns_run
        and data.action in principal.permitted_actions
        and data.action in grant.permitted_actions
        and data.action in allowed_actions
        and grant.owner_ref == data.owner_ref
        and _in_force(grant, data.authority_now)
        and grant.authority_epoch == d2.authority_epoch
        and d2.outcome == "allow"
        and (d2.run_id, d2.owner_ref) == (data.run_id, data.owner_ref)
        and (d2.purchase_mode, d2.governance_mode) == (data.purchase_mode, data.governance_mode)
        and (d2.policy_binding, d2.context_version) == (data.expected_policy, data.context_version)
        and d2.intent_ref == data.intent.intent_ref
        and d2.selected_variant_id == terms.variant_id
        and (d2.snapshot_ref, d2.material_digest) == (terms.snapshot_ref, terms.material_digest)
        and d2.grant_ref == grant.grant_ref
        and d2.operation_id == data.operation_id
        and d2.reservation_id == d2.application_transition_ref == data.reservation.reservation_id
        and proof.status == "valid"
        and proof.verified_agent_key_ref == grant.agent_key_ref
        and proof.authorization_id == d2.authorization_id
        and proof.operation_id == data.operation_id
        and proof.grant_ref == grant.grant_ref
        and (proof.snapshot_ref, proof.material_digest) == (d2.snapshot_ref, d2.material_digest)
        and proof.requested_instrument_ref == d2.requested_instrument_ref
    )


def _results(policies: tuple[PolicyId, ...], passes: tuple[bool, ...]) -> tuple[RuleResult, ...]:
    return tuple(
        RuleResult(schema_version=SCHEMA, policy_id=policy, result="pass" if passed else "fail")
        for policy, passed in zip(policies, passes, strict=True)
    )


def delegation_results(data: HnpAuthorizationPolicyInput) -> tuple[RuleResult, ...]:
    """POL-07, POL-08 and POL-09 of D2-HNP, every one evaluated, in catalog order."""
    return _results(
        DELEGATION_RULES,
        (delegation_active(data), within_delegated_scope(data), use_available(data)),
    )


def dispatch_results(
    data: DispatchPolicyInput, allowed_actions: Sequence[Action]
) -> tuple[RuleResult, ...]:
    """The whole D3-HNP row, POL-09 then POL-10, every one evaluated."""
    return _results(
        DISPATCH_RULES,
        (reservation_held(data), dispatch_authority_current(data, allowed_actions)),
    )


# Guards before the engine: a policy allow never skips them (policy-v1 §4).


def usage_coherent(
    usage: UsageFacts, grant: GrantFacts, *, operation_id: str, state_version: int
) -> bool:
    """The counters agree with the reservations resolved for this grant (policy-v1 §4).

    Single use: at most one use among consumed, other operations' and this operation's holding
    reservation; a zero count never hides a non-zero amount; usage and own reservation belong to
    this grant and operation, at the grant version the header expects (§4.3). Anything else is a
    projection error, not free capacity.
    """
    own = usage.same_operation_reservation
    own_holding = int(own is not None and own.state in _HOLDING)
    return (
        usage.grant_ref == grant.grant_ref
        and usage.state_version == state_version
        and usage.uses_consumed + usage.uses_reserved_other + own_holding <= grant.scope.uses_max
        and (usage.uses_consumed > 0 or usage.amount_consumed_minor == 0)
        and (usage.uses_reserved_other > 0 or usage.amount_reserved_other_minor == 0)
        and (own is None or (own.grant_ref, own.operation_id) == (grant.grant_ref, operation_id))
    )


def _terms_current(data: HnpAuthorizationPolicyInput | DispatchPolicyInput) -> bool:
    return (
        data.freshness.status == "current"
        and data.commercial_now < data.terms.commercial_expires_at
    )


def delegation_prerequisite(data: HnpAuthorizationPolicyInput) -> GuardReason | None:
    """D2-HNP guards, in order: coherent facts, current terms, resolved profile and open proof."""
    hnp = data.hnp
    if not usage_coherent(
        hnp.usage,
        hnp.grant,
        operation_id=data.operation_id,
        state_version=data.expected_local_state_version,
    ):
        return "GOV_FACTS_UNAVAILABLE"
    if not _terms_current(data):
        return "GOV_FRESHNESS_UNRESOLVED"
    if data.authorization_profile.status != "validated" or hnp.open_proof.status == "unresolved":
        return "GOV_AUTHORIZATION_PROFILE_UNRESOLVED"
    return None


def dispatch_prerequisite(data: DispatchPolicyInput) -> GuardReason | None:
    """D3-HNP guards: the usage shows this reservation, current terms, a resolved closed proof."""
    if data.usage.same_operation_reservation != data.reservation or not usage_coherent(
        data.usage,
        data.grant,
        operation_id=data.operation_id,
        state_version=data.expected_local_state_version,
    ):
        return "GOV_FACTS_UNAVAILABLE"
    if not _terms_current(data):
        return "GOV_FRESHNESS_UNRESOLVED"
    if data.closed_proof.status == "unresolved":
        return "GOV_AUTHORIZATION_PROFILE_UNRESOLVED"
    return None
