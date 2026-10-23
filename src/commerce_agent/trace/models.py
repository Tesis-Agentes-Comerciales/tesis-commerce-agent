"""`PurchaseTrace` contract (docs/design/trace-panel.md), mirror of ui/src/lib/trace-contract.ts.

The rules match `parsePurchaseTrace`: non-empty texts, non-negative integers, times in UTC ending
in `Z`, and no field beyond the contract. Nothing here may carry tokens, mandates, canonical
inputs, encrypted sources or the owner's identity.
"""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

UTC_TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"


def _real_date(value: str) -> str:
    datetime.fromisoformat(value)
    return value


Text = Annotated[str, Field(min_length=1)]
Count = Annotated[int, Field(ge=0)]
Currency = Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")]
UtcTime = Annotated[str, Field(pattern=UTC_TIME_PATTERN), AfterValidator(_real_date)]

# `operator`: the HNP kill switch (docs/design/hnp-governed.md §10).
TraceActor = Literal["user", "agent", "policy", "merchant", "payment", "operator"]
PurchaseMode = Literal["HP", "HNP"]
PurchaseStatus = Literal[
    "in_progress",
    "awaiting_approval",
    "completed",
    "blocked",
    "stale_terms",
    "canceled",
    "failed",
    "uncertain",
]
# Only a history row: its trace could not be built without inventing data (evidence that does
# not verify, or a stored value outside the contract). The trace itself answers 503.
SummaryStatus = PurchaseStatus | Literal["trace_unavailable"]
StepState = Literal["done", "running", "waiting", "blocked", "error", "skipped"]
PolicyMode = Literal["off", "shadow", "enforce"]
Receipt = Literal["checkout", "payment"]


class TraceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Money(TraceModel):
    minor: Count
    currency: Currency


class PolicyRef(TraceModel):
    set_id: Text
    revision: Count


class PolicyRule(TraceModel):
    id: Text
    result: Literal["pass", "fail", "not_evaluated"]
    # A text (POL-01 variants, POL-02 origins) or an amount (POL-11), which the UI formats.
    observed: Text | Money | None
    expected: Text | Money | None


class PolicyDecision(TraceModel):
    mode: Literal["shadow", "enforce"]
    outcome: Literal["allow", "deny"]
    effect: Literal["observed", "blocked", "continued"]
    policy: PolicyRef
    rules: tuple[PolicyRule, ...]
    reason_codes: tuple[Text, ...]
    latency_ms: Count | None
    decision_id: Text
    evidence_id: Text


class StepBase(TraceModel):
    id: Text
    at: UtcTime | None
    duration_ms: Count | None
    actor: TraceActor
    state: StepState


class RequestStep(StepBase):
    kind: Literal["request"]


class SelectionStep(StepBase):
    kind: Literal["selection"]
    product: Text
    total_minor: Count
    currency: Currency


class PolicyDecisionStep(StepBase):
    kind: Literal["policy_decision"]
    decision: PolicyDecision


class AuthorizationDecisionStep(StepBase):
    """D2: the policy checked the approval (POL-03..06) before anything was signed.

    `state` is `blocked` when it stopped the purchase: no approval was recorded.
    """

    kind: Literal["authorization_decision"]
    decision: PolicyDecision


class CheckoutCreatedStep(StepBase):
    kind: Literal["checkout_created"]
    checkout_id: Text


class ApprovalStep(StepBase):
    kind: Literal["approval"]
    fingerprint: Text
    expires_at: UtcTime
    approved_at: UtcTime | None


class AuthorizationSignedStep(StepBase):
    kind: Literal["authorization_signed"]
    mandates: tuple[Receipt, ...]


class PaymentAuthorizedStep(StepBase):
    kind: Literal["payment_authorized"]
    max_minor: Count
    currency: Currency


class OrderConfirmedStep(StepBase):
    kind: Literal["order_confirmed"]
    order_id: Text
    receipts: tuple[Receipt, ...]


class OrderEventStep(StepBase):
    kind: Literal["order_event"]
    event_type: Text


class CanceledStep(StepBase):
    kind: Literal["canceled"]
    # `expiry`: an HNP delegation that expired before the purchase (`grant_expired`).
    by: Literal["user", "budget", "expiry"]


class FailedStep(StepBase):
    kind: Literal["failed"]
    code: Text


# Application controls of the governed Human Present flow (docs/design/b2-governed-pilot.md):
# they come from hp_evidence, never from the D1 policy decision.


class AmountChange(TraceModel):
    field: Literal["subtotal_minor", "shipping_minor", "total_minor", "delivery_days", "stock"]
    presented: Count | None
    current: Count | None


class CurrencyChange(TraceModel):
    field: Literal["currency"]
    presented: Currency | None
    current: Currency | None


class ExpiryChange(TraceModel):
    field: Literal["expires_at"]
    presented: UtcTime | None
    current: UtcTime | None


FreshnessChange = Annotated[
    AmountChange | CurrencyChange | ExpiryChange, Field(discriminator="field")
]


class FreshnessCheckStep(StepBase):
    """Live-offer freshness; `state` is `blocked` when the check stopped the purchase."""

    kind: Literal["freshness_check"]
    # `close`: right before an HNP purchase reserves its single use (hnp-governed.md §3.3).
    stage: Literal["present", "approve", "close"]
    mode: Literal["shadow", "enforce"]
    outcome: Literal["fresh", "changed", "unavailable"]
    changes: tuple[FreshnessChange, ...]
    currency: Currency | None
    offer_revision: Count | None
    read_error: Text | None
    evidence_digest: Text


RecoveryCause = Literal[
    "budget",
    "unavailable",
    "currency",
    "category",
    "condition",
    "option",
    "term",
    "not_shortlisted",
    "not_in_catalog",
    "no_match",
]


class GovernedRecoveryStep(StepBase):
    """The agent explained why a selection could not proceed instead of a technical error."""

    kind: Literal["governed_recovery"]
    recovery: Literal[
        "no_candidate", "selection_outside_shortlist", "budget_exceeded_after_checkout"
    ]
    cause: RecoveryCause
    variant_id: Text | None
    budget_minor: Count | None
    observed_minor: Count | None
    currency: Currency | None
    evidence_digest: Text


# Human Not Present (docs/design/hnp-governed.md §10): the delegation ledger, never the model.

EscalationReason = Literal[
    "budget_not_satisfiable",
    "no_candidate_in_scope",
    "choice_not_confirmed",
    "offer_changed",
    "terms_out_of_scope",
    "authorization_profile_unresolved",
    "verifier_unresolved_constraint",
    "verifier_mandates_not_supported",
]


class DelegationConsentedStep(StepBase):
    """The buyer consented to the scope before leaving; `total_max` includes shipping."""

    kind: Literal["delegation_consented"]
    grant_id: Text
    total_max: Money
    expires_at: UtcTime


class TaskActivatedStep(StepBase):
    """The executor claimed the task (one step per claim: a re-claim resumes it)."""

    kind: Literal["task_activated"]
    trigger: Literal["service"]


# Mirror of `application/hnp_intent_review.py` (hnp-intent-review.md).
ReviewDecision = Literal["confirmed", "not_confirmed"]
ReviewReason = Literal[
    "matches_request",
    "contradicts_preference",
    "outside_request",
    "unclear",
    "review_unavailable",
]


class ChoiceReviewedStep(StepBase):
    """The intent reviewer checked the selector's choice against what the buyer asked, before D1.

    `state` is `blocked` when it did not confirm it: the task escalated, nothing was created.
    """

    kind: Literal["choice_reviewed"]
    decision: ReviewDecision
    reason: ReviewReason


class ReservationStep(StepBase):
    kind: Literal["reservation"]
    outcome: Literal["reserved", "released", "consumed", "uncertain"]
    amount: Money


class MandatesClosedStep(StepBase):
    kind: Literal["mandates_closed"]


class DispatchDecisionStep(StepBase):
    """D3-HNP: the policy checked the authority right before committing the dispatch."""

    kind: Literal["dispatch_decision"]
    decision: PolicyDecision


class RevokedStep(StepBase):
    kind: Literal["revoked"]
    by: Literal["owner", "operator"]
    effect: Literal["no_effect", "released", "in_flight"]


class EscalatedStep(StepBase):
    """The decision went back to the buyer: nothing was signed or charged (§8.2)."""

    kind: Literal["escalated"]
    reason: EscalationReason


TraceStep = Annotated[
    RequestStep
    | SelectionStep
    | PolicyDecisionStep
    | CheckoutCreatedStep
    | ApprovalStep
    | AuthorizationDecisionStep
    | AuthorizationSignedStep
    | PaymentAuthorizedStep
    | OrderConfirmedStep
    | OrderEventStep
    | CanceledStep
    | FailedStep
    | FreshnessCheckStep
    | GovernedRecoveryStep
    | DelegationConsentedStep
    | TaskActivatedStep
    | ChoiceReviewedStep
    | ReservationStep
    | MandatesClosedStep
    | DispatchDecisionStep
    | RevokedStep
    | EscalatedStep,
    Field(discriminator="kind"),
]


def _human_present(mode: PurchaseMode | None) -> bool:
    """Absent means HP: only an HNP purchase writes its mode."""
    return mode != "HNP"


class PurchaseTrace(TraceModel):
    run_id: Text
    status: PurchaseStatus
    policy_mode: PolicyMode
    request: Text
    merchant: Text | None
    merchant_name: Text | None
    total: Money | None
    started_at: UtcTime
    updated_at: UtcTime
    steps: tuple[TraceStep, ...]
    # Optional and omitted in HP, so HP traces keep their bytes (hnp-governed.md §5.4).
    purchase_mode: PurchaseMode | None = Field(default=None, exclude_if=_human_present)


class PolicySummary(TraceModel):
    outcome: Literal["allow", "deny"]
    failed_rules: tuple[Text, ...]


class PurchaseSummary(TraceModel):
    """One row of `GET /me/purchases`."""

    run_id: Text
    # Null only in a `trace_unavailable` row whose own purchase row is outside the contract.
    started_at: UtcTime | None
    request: Text | None
    merchant: Text | None
    merchant_name: Text | None
    total: Money | None
    status: SummaryStatus
    policy: PolicySummary | None
    approved_at: UtcTime | None
    order_id: Text | None
    duration_ms: Count | None
    purchase_mode: PurchaseMode | None = Field(default=None, exclude_if=_human_present)

    @model_validator(mode="after")
    def _only_an_unavailable_row_omits_its_start(self) -> Self:
        if self.status != "trace_unavailable" and None in (self.started_at, self.request):
            raise ValueError("Only a trace_unavailable row may omit started_at or request")
        return self


class PurchaseHistory(TraceModel):
    """A page of the buyer's purchases, newest first; `next_cursor` goes in `before`."""

    purchases: tuple[PurchaseSummary, ...]
    next_cursor: Text | None
