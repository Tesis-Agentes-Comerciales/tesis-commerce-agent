"""Rows and requests of the delegation ledger (spec §4.3, §4.4 and §9.1).

Protected values (mandate tokens, checkout details) stay out of `repr`, events and evidence.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ..governance.canonical import canonical_bytes, sha256_digest

EXECUTOR_REF = "service:hnp-executor"
OPERATOR_REF = "operator"

GrantState = Literal["active", "consumed", "revoked", "expired"]
RevokedReason = Literal["owner", "operator", "escalated", "failed"]
TaskState = Literal[
    "scheduled", "running", "purchased", "escalated", "failed", "canceled", "uncertain"
]
MandateKind = Literal["open_checkout", "open_payment", "closed_checkout", "closed_payment"]
ActorKind = Literal["user", "service", "operator"]
EventType = Literal[
    "consented",
    "mandates_issued",
    "task_claimed",
    "reservation_reserved",
    "mandates_closed",
    "dispatch_committed",
    "receipt_recorded",
    "reservation_released",
    "reservation_uncertain",
    "consumed",
    "revoked",
    "expired",
    "kill_switch_blocked",
    "task_finished",
]
# Task outcomes of spec §8.1: escalation, cancellation, failure and uncertainty codes.
OutcomeCode = Literal[
    "budget_not_satisfiable",
    "no_candidate_in_scope",
    "offer_changed",
    "terms_out_of_scope",
    "authorization_profile_unresolved",
    "verifier_unresolved_constraint",
    "verifier_mandates_not_supported",
    "revoked_by_owner",
    "kill_switch",
    "grant_expired",
    "selection_not_eligible",
    "merchant_not_allowed",
    "mandate_proof_invalid",
    "policy_denied",
    "governance_unavailable",
    "state_conflict",
    "verifier_invalid_mandate",
    "verifier_invalid_credential",
    "checkout_failed",
    "budget_guard",
    "close_expired_before_dispatch",
    "dispatch_timeout",
    "partial_verifier_failure",
    "receipt_mismatch",
]
ConsentStatus = Literal["created", "existing", "not_found", "scope_mismatch", "proposal_expired"]


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    proposal_id: str
    owner_id: str
    scope_json: str  # canonical ConsentScope (HNP-03); stored as received
    scope_digest: str
    checkout_details_json: str = field(repr=False)
    presented_at: str
    expires_at: str  # of the proposal (AGENT_HNP_PROPOSAL_TTL_SECONDS)


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    proposal_id: str
    owner_id: str
    scope_json: str
    scope_digest: str
    checkout_details_json: str = field(repr=False)
    presented_at: str
    expires_at: str
    grant_id: str | None


@dataclass(frozen=True, slots=True)
class SignedMandate:
    """A mandate as signed by HNP-04: the token is protected, the rest is public."""

    token: str = field(repr=False)
    link_hash: str
    public_jwk_json: str


@dataclass(frozen=True, slots=True)
class ConsentRequest:
    owner_id: str
    proposal_id: str
    scope_digest: str
    intent_json: str
    agent_key_kid: str
    issuer_kid: str
    open_checkout: SignedMandate
    open_payment: SignedMandate


@dataclass(frozen=True, slots=True)
class GrantRecord:
    grant_id: str
    proposal_id: str
    owner_id: str
    grant_version: int
    scope_json: str
    scope_digest: str
    intent_json: str
    checkout_details_json: str = field(repr=False)
    state: GrantState
    revoked_reason: RevokedReason | None
    authority_epoch: int
    state_version: int
    valid_from: str
    expires_at: str
    consented_at: str
    updated_at: str
    closed_at: str | None
    agent_key_kid: str
    issuer_kid: str


@dataclass(frozen=True, slots=True)
class MandateRecord:
    mandate_id: str
    grant_id: str
    kind: MandateKind
    reservation_id: str | None
    token: str = field(repr=False)
    token_digest: str
    link_hash: str
    public_jwk_json: str
    created_at: str


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    grant_id: str
    owner_id: str
    run_id: str | None
    state: TaskState
    outcome_code: OutcomeCode | None
    claim_count: int
    lease_id: str | None
    lease_expires_at: str | None
    created_at: str
    updated_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class ConsentResult:
    status: ConsentStatus
    grant: GrantRecord | None = None
    task: TaskRecord | None = None


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    enabled: bool
    control_epoch: int
    changed_at: str


@dataclass(frozen=True, slots=True)
class DelegationEvent:
    grant_id: str
    sequence: int
    event_type: EventType
    actor_kind: ActorKind
    actor_ref: str
    on_behalf_of: str
    task_id: str | None
    run_id: str | None
    body: Mapping[str, Any]
    recorded_at: str
    previous_digest: str | None
    digest: str

    def link(self) -> dict[str, Any]:
        """The row without its digest: what the digest covers."""
        return {
            "grant_id": self.grant_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "actor_kind": self.actor_kind,
            "actor_ref": self.actor_ref,
            "on_behalf_of": self.on_behalf_of,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "body": dict(self.body),
            "recorded_at": self.recorded_at,
            "previous_digest": self.previous_digest,
        }


def event_digest(link: Mapping[str, Any]) -> str:
    return sha256_digest(b"delegation-event/v1\0" + canonical_bytes(dict(link)))


def token_digest(token: str) -> str:
    return sha256_digest(token.encode("ascii"))


def chain_is_valid(grant_id: str, events: Sequence[DelegationEvent]) -> bool:
    """Consecutive from 1, each linked to the previous digest, each digest recomputed."""
    previous = None
    for expected, event in enumerate(events, start=1):
        if (
            event.grant_id != grant_id
            or event.sequence != expected
            or event.previous_digest != previous
            or event.digest != event_digest(event.link())
        ):
            return False
        previous = event.digest
    return bool(events)
