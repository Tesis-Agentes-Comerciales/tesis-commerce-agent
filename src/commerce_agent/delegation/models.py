"""Rows and requests of the delegation ledger (spec §4.3, §4.4 and §9.1).

Protected values (mandate tokens, checkout details) stay out of `repr`, events and evidence.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ..governance.canonical import canonical_bytes, sha256_digest

if TYPE_CHECKING:
    from ..llm.model_failures import ModelFailureKind

EXECUTOR_REF = "service:hnp-executor"
OPERATOR_REF = "operator"
# Profile constants (spec §6), not settings: each executor step renews a lease this long, and
# UoW-1 reserves only with this much validity left, so the open mandates outlive the close.
LEASE_SECONDS = 120
RESERVE_MARGIN_SECONDS = 300

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
    # The intent reviewer's verdict on the selector's choice (hnp-intent-review.md).
    "choice_reviewed",
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
    "choice_not_confirmed",
    "selection_unavailable",
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
ReservationState = Literal["reserved", "committed", "consumed", "released", "uncertain"]
ReleaseBasis = Literal["no_outbox", "psp_error_receipt"]
UncertainReason = Literal["dispatch_timeout", "partial_verifier_failure", "receipt_mismatch"]
OutboxState = Literal[
    "committed",
    "payment_sent",
    "payment_accepted",
    "complete_sent",
    "completed",
    "rejected",
    "uncertain",
]
Ap2Error = Literal[
    "invalid_credential", "invalid_mandate", "unresolved_constraint", "mandates_not_supported"
]
KillSwitchStage = Literal["claim", "uow1", "uow2"]
DispatchStep = Literal["payment_sent", "payment_accepted", "complete_sent", "completed"]
RevokeEffect = Literal["no_effect", "released", "in_flight"]
# applied: this call wrote its step. reused: the step was already durable (a retry).
# closed: the call ended the task. lease_lost: another lease owns the task; nothing written.
# blocked: a comparison failed; nothing written, `outcome` classifies it (spec §4.5).
StepStatus = Literal[
    "applied", "reused", "closed", "lease_lost", "blocked", "not_runnable", "not_found"
]


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
class LedgerCounts:
    """What `/health` reports (spec §5.4): grants still usable now, tasks running or in doubt."""

    active_grants: int
    running_tasks: int
    uncertain_tasks: int


@dataclass(frozen=True, slots=True)
class ReservationRecord:
    reservation_id: str
    grant_id: str
    owner_id: str
    run_id: str
    checkout_id: str
    operation_id: str
    amount_minor: int
    currency: str
    expected_epoch: int
    state: ReservationState
    state_version: int
    release_basis: ReleaseBasis | None
    uncertain_reason: UncertainReason | None
    d2_evidence_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    outbox_id: str
    reservation_id: str
    operation_id: str
    grant_id: str
    run_id: str
    checkout_id: str
    amount_minor: int
    currency: str
    checkout_expires_at: str
    payment_request_digest: str
    authority_epoch: int
    control_epoch: int
    d3_evidence_id: str | None
    state: OutboxState
    rejected_by: Literal["psp", "merchant"] | None
    ap2_error: Ap2Error | None
    order_id: str | None
    error_receipt_jwt: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Authority:
    """What an executor step read and expects unchanged when it commits (spec §4.5)."""

    authority_epoch: int
    state_version: int
    control_epoch: int


@dataclass(frozen=True, slots=True)
class Execution:
    """The durable state of one task: what the executor resumes from after any crash."""

    task: TaskRecord
    grant: GrantRecord
    control: KillSwitchState
    reservation: ReservationRecord | None
    outbox: OutboxRecord | None

    @property
    def authority(self) -> Authority:
        return Authority(
            self.grant.authority_epoch, self.grant.state_version, self.control.control_epoch
        )


@dataclass(frozen=True, slots=True)
class Step:
    status: StepStatus
    execution: Execution | None = None
    outcome: OutcomeCode | None = None


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    task_id: str
    lease_id: str
    expected: Authority
    reservation_id: str
    operation_id: str
    checkout_id: str
    amount_minor: int
    d2_evidence_id: str | None  # None in B0-HNP


@dataclass(frozen=True, slots=True)
class ClosedMandates:
    task_id: str
    lease_id: str
    reservation_id: str
    checkout_hash: str
    closed_checkout: SignedMandate
    closed_payment: SignedMandate


@dataclass(frozen=True, slots=True)
class DispatchCommit:
    task_id: str
    lease_id: str
    expected: Authority
    reservation_id: str
    reservation_version: int
    outbox_id: str
    checkout_expires_at: str
    payment_request_digest: str
    d3_evidence_id: str | None  # None in B0-HNP


# The status of an AP2 receipt as the profile admits it: the SDK's `status` (`Success`, `Error`),
# which the JWTs carry, or the spec's `result` (`success`, `error`) (docs/ap2-profile.md, Recibos).
# The ledger keeps the lowercase form.
RECEIPT_STATUSES: Mapping[str, Literal["success", "error"]] = {
    "Success": "success",
    "Error": "error",
    "success": "success",
    "error": "error",
}


def receipt_status(status: str) -> Literal["success", "error"]:
    """The ledger's form of an AP2 receipt status; anything outside the profile raises."""
    try:
        return RECEIPT_STATUSES[status]
    except (KeyError, TypeError):
        raise ValueError("Not an AP2 receipt status") from None


@dataclass(frozen=True, slots=True)
class Receipt:
    """A verified AP2 receipt; the event keeps its digest and public fields, not the JWT.

    `status` takes either form of the profile and is kept lowercase (`receipt_status`), the one
    place where the two meet."""

    kind: Literal["payment", "checkout"]
    jwt: str = field(repr=False)
    issuer: str
    status: str
    reference: str
    error: str | None = None
    payment_id: str | None = None
    order_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", receipt_status(self.status))


@dataclass(frozen=True, slots=True)
class TaskFinish:
    """A terminal without an outbox row: escalation, cancellation or failure (spec §8.1)."""

    task_id: str
    lease_id: str
    state: Literal["escalated", "failed", "canceled"]
    outcome_code: OutcomeCode
    stage: KillSwitchStage | None = None  # where the kill switch was met
    # Why the model failed a selector that made no choice (p0-model-failures.md §3.5).
    failure_kind: "ModelFailureKind | None" = None


@dataclass(frozen=True, slots=True)
class Revocation:
    """revoked: this or an earlier owner revocation, with its original effect; already_closed:
    revoked by another cause, nothing written; not_revocable: consumed or expired."""

    status: Literal["revoked", "already_closed", "not_revocable"]
    grant: GrantRecord
    effect: RevokeEffect | None


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
