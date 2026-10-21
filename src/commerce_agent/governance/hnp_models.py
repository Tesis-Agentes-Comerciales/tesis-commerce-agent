"""Human Not Present facts: the consented scope and the D2-HNP and D3-HNP inputs.

`DelegationScope`, `ConsentScope`, its canonical form and `scope_digest` are the shared names of
docs/design/hnp-governed.md §4.2: the delegation API proposes and consents with them and the
evidence verifier recomputes the digest from the stored bytes. The grant, usage, reservation and
mandate-proof facts are the types of docs/b2-governance/policy-v1.md §4, assembled by the
backend and never by the model.

This module imports only `models` and `canonical`: the D2/D3 boundary (`evaluator.py`) may import
it without a cycle, and it never imports the trace or delegation view contracts.
"""

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from .canonical import canonical_bytes, parse_model, sha256_digest
from .models import (
    Actions,
    AuthorizationProfile,
    ConstraintMatch,
    ContractModel,
    Digest,
    FreshnessFacts,
    GovernanceMode,
    Id,
    IntentFacts,
    MaterialTerms,
    Minor,
    ObjectRef,
    One,
    PolicyBinding,
    PolicyHeader,
    PrincipalFacts,
    PrivateBinding,
    PurchaseMode,
    ResolvedD1,
    UtcTime,
    Version,
    _constraint_set,
)

# Profile constants (§6): part of the profile and of the evidence, never settings.
MAX_SCOPE_VARIANTS = 5
MAX_PREFERENCE_TEXT = 280
SCOPE_DIGEST_DOMAIN = b"commerce-delegation-scope/v1\0"

PositiveMinor = Annotated[int, Field(ge=1)]
Text = Annotated[str, Field(min_length=1)]
PreferenceText = Annotated[str, Field(min_length=1, max_length=MAX_PREFERENCE_TEXT)]
ProofStatus = Literal["valid", "invalid", "unresolved"]
ReservationState = Literal["reserved", "committed", "consumed", "released", "uncertain"]


def _ordered_set(values: tuple[str, ...]) -> tuple[str, ...]:
    """A declared set: unique and already sorted, so one scope has exactly one byte form."""
    if len(set(values)) != len(values) or list(values) != sorted(values):
        raise ValueError("A scope set is unique and sorted")
    return values


OrderedIds = Annotated[tuple[Id, ...], Field(min_length=1), AfterValidator(_ordered_set)]


class DelegationScope(ContractModel):
    """What one grant authorizes (policy-v1 §4): one purchase, one instrument, one destination."""

    merchant_refs: OrderedIds
    variant_ids: Annotated[OrderedIds, Field(max_length=MAX_SCOPE_VARIANTS)]
    currency: Literal["COP"]
    quantity_max: One
    total_max_minor: PositiveMinor
    destination_binding: PrivateBinding
    instrument_ref: Literal["tesis_sandbox"]
    uses_max: One


class _DisplayModel(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, revalidate_instances="always"
    )


class DisplayVariant(_DisplayModel):
    variant_id: Id
    title: Text


class DisplayMoney(_DisplayModel):
    minor: Minor
    currency: Literal["COP"]


class ScopeDisplay(_DisplayModel):
    """The consent card's `ScopeView` (§5.3), typed here without the trace contract.

    Same JSON as the `ScopeView` of the delegation views, only narrower (COP and millisecond
    times), so any valid display is a valid `ScopeView`. It is part of the consented bytes.
    """

    merchant_name: Text
    variants: Annotated[
        tuple[DisplayVariant, ...], Field(min_length=1, max_length=MAX_SCOPE_VARIANTS)
    ]
    total_max: DisplayMoney
    expires_at: UtcTime
    destination_label: Text
    instrument_label: Text
    uses: One
    preference_text: PreferenceText | None


class ConsentScope(_DisplayModel):
    """What the buyer consents to (§4.2); its canonical bytes are what `scope_digest` binds."""

    schema_version: Literal["commerce-delegation/1.0"]
    proposal_id: Annotated[str, Field(max_length=128, pattern=r"^dpr_[A-Za-z0-9._:-]+$")]
    owner_ref: Annotated[str, Field(pattern=r"^owner:[0-9a-f]{64}$")]
    scope: DelegationScope
    display: ScopeDisplay
    presented_at: UtcTime
    expires_at: UtcTime
    preference_text: PreferenceText | None

    @model_validator(mode="after")
    def card_shows_the_scope(self) -> Self:
        """The card shows exactly what is consented: variants, total, expiry, uses, text."""
        display, scope = self.display, self.scope
        if not self.presented_at < self.expires_at:
            raise ValueError("A consent expires after it is presented")
        if (
            tuple(variant.variant_id for variant in display.variants) != scope.variant_ids
            or (display.total_max.minor, display.total_max.currency)
            != (scope.total_max_minor, scope.currency)
            or display.expires_at != self.expires_at
            or display.uses != scope.uses_max
            or display.preference_text != self.preference_text
        ):
            raise ValueError("The display differs from the consented scope")
        return self


def consent_scope_bytes(consent: ConsentScope) -> bytes:
    """`commerce-canonical-json/1` bytes of the consent: what is stored and digested."""
    return canonical_bytes(consent)


def scope_digest(consent: ConsentScope) -> str:
    return sha256_digest(SCOPE_DIGEST_DOMAIN + consent_scope_bytes(consent))


def parse_consent_scope(payload: bytes) -> ConsentScope:
    """Strict parse of stored consent bytes; only their canonical form is accepted."""
    consent = parse_model(ConsentScope, payload)
    if consent_scope_bytes(consent) != payload:
        raise ValueError("The consent scope is not canonical JSON")
    return consent


# Grant, usage and reservation facts (policy-v1 §4), resolved from the delegation store.


class GrantFacts(ContractModel):
    grant_ref: ObjectRef
    owner_ref: Id
    agent_key_ref: Id
    state: Literal["active", "revoked", "expired", "consumed"]
    authority_epoch: Version
    valid_from: UtcTime
    expires_at: UtcTime
    permitted_actions: Actions
    scope: DelegationScope


class ReservationIntent(ContractModel):
    reservation_id: Id
    operation_id: Id
    run_id: Id
    owner_ref: Id
    grant_ref: ObjectRef
    expected_epoch: Version
    amount_minor: Minor
    currency: Literal["COP"]


class ReservationFacts(ReservationIntent):
    state: ReservationState
    state_version: Version

    def intent(self) -> ReservationIntent:
        """The reservation as it was requested, without its lifecycle."""
        return ReservationIntent.model_validate(self.model_dump(exclude={"state", "state_version"}))


class UsageFacts(ContractModel):
    """Uses of the grant; `uses_reserved_other` excludes the reservation of this operation."""

    grant_ref: ObjectRef
    state_version: Version
    uses_consumed: Minor
    uses_reserved_other: Minor
    amount_consumed_minor: Minor
    amount_reserved_other_minor: Minor
    same_operation_reservation: ReservationFacts | None


# Mandate proofs: what the AP2 verifiers established, never the mandates themselves.


class _ProofFacts(ContractModel):
    status: ProofStatus
    profile_ref: ObjectRef | None

    @model_validator(mode="after")
    def referenced(self) -> Self:
        if self.status == "valid" and self.profile_ref is None:
            raise ValueError("A valid proof names the profile it was verified against")
        return self


class OpenProofFacts(_ProofFacts):
    open_checkout_ref: ObjectRef
    open_payment_ref: ObjectRef
    consent_ref: ObjectRef
    verified_agent_key_ref: Id


class ClosedProofFacts(_ProofFacts):
    checkout_chain_ref: ObjectRef
    payment_chain_ref: ObjectRef
    original_checkout_jwt_digest: Digest
    verified_agent_key_ref: Id
    authorization_id: Id
    operation_id: Id
    grant_ref: ObjectRef
    snapshot_ref: ObjectRef
    material_digest: Digest
    requested_instrument_ref: Id


# D2-HNP: the `hnp` branch of AuthorizationPolicyInput (policy-v1 §4).


class HnpAuthorizationFacts(ContractModel):
    """Grant, usage and open proof in place of the HP click; no `hp` and no human approval."""

    grant: GrantFacts
    usage: UsageFacts
    reservation_intent: ReservationIntent
    requested_instrument_ref: Id
    open_proof: OpenProofFacts


class HnpAuthorizationPolicyInput(PolicyHeader):
    """HNP branch of the D2 input, selected by `purchase_mode`."""

    purchase_mode: Literal["HNP"]
    phase: Literal["D2"]
    action: Literal["complete_purchase"]
    principal: PrincipalFacts
    intent: IntentFacts
    selected_variant_id: Id
    terms: MaterialTerms
    freshness: FreshnessFacts
    # Same set rule as the HP branch, so both canonicalize their matches alike.
    constraint_matches: Annotated[tuple[ConstraintMatch, ...], AfterValidator(_constraint_set)]
    d1: ResolvedD1
    authorization_id: Id
    operation_id: Id
    authorization_profile: AuthorizationProfile
    hnp: HnpAuthorizationFacts


# D3-HNP (policy-v1 §4, DispatchPolicyInput).


class ResolvedD2(ContractModel):
    """The applied D2-HNP decision of the run, resolved from the ledger, never from an ID alone."""

    decision_id: Id
    evidence_id: Id
    evidence_digest: Digest
    outcome: Literal["allow", "deny"]
    run_id: Id
    owner_ref: Id
    purchase_mode: PurchaseMode
    governance_mode: GovernanceMode
    policy_binding: PolicyBinding
    context_version: Version
    input_digest: Digest
    authorization_id: Id
    intent_ref: ObjectRef
    selected_variant_id: Id
    snapshot_ref: ObjectRef
    material_digest: Digest
    grant_ref: ObjectRef
    authority_epoch: Version
    reservation_id: Id
    operation_id: Id
    requested_instrument_ref: Id
    # `reservation_id` in D2 (§4.3): proves the reservation was applied, not only observed.
    application_transition_ref: Id


class DispatchPolicyInput(PolicyHeader):
    purchase_mode: Literal["HNP"]
    phase: Literal["D3"]
    action: Literal["commit_dispatch"]
    principal: PrincipalFacts
    intent: IntentFacts
    terms: MaterialTerms
    freshness: FreshnessFacts
    grant: GrantFacts
    usage: UsageFacts
    reservation: ReservationFacts
    operation_id: Id
    d2: ResolvedD2
    closed_proof: ClosedProofFacts
