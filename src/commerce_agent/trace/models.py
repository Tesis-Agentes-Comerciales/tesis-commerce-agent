"""`PurchaseTrace` contract (docs/design/trace-panel.md), mirror of ui/src/lib/trace-contract.ts.

The rules match `parsePurchaseTrace`: non-empty texts, non-negative integers, times in UTC ending
in `Z`, and no field beyond the contract. Nothing here may carry tokens, mandates, canonical
inputs, encrypted sources or the owner's identity.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

UTC_TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"


def _real_date(value: str) -> str:
    datetime.fromisoformat(value)
    return value


Text = Annotated[str, Field(min_length=1)]
Count = Annotated[int, Field(ge=0)]
Currency = Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")]
UtcTime = Annotated[str, Field(pattern=UTC_TIME_PATTERN), AfterValidator(_real_date)]

TraceActor = Literal["user", "agent", "policy", "merchant", "payment"]
PurchaseStatus = Literal[
    "in_progress", "awaiting_approval", "completed", "blocked", "canceled", "failed", "uncertain"
]
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
    observed: Text | None
    expected: Text | None


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
    by: Literal["user", "budget"]


class FailedStep(StepBase):
    kind: Literal["failed"]
    code: Text


TraceStep = Annotated[
    RequestStep
    | SelectionStep
    | PolicyDecisionStep
    | CheckoutCreatedStep
    | ApprovalStep
    | AuthorizationSignedStep
    | PaymentAuthorizedStep
    | OrderConfirmedStep
    | OrderEventStep
    | CanceledStep
    | FailedStep,
    Field(discriminator="kind"),
]


class PurchaseTrace(TraceModel):
    run_id: Text
    status: PurchaseStatus
    policy_mode: PolicyMode
    request: Text
    merchant: Text | None
    total: Money | None
    started_at: UtcTime
    updated_at: UtcTime
    steps: tuple[TraceStep, ...]


class PolicySummary(TraceModel):
    outcome: Literal["allow", "deny"]
    failed_rules: tuple[Text, ...]


class PurchaseSummary(TraceModel):
    """One row of `GET /me/purchases`."""

    run_id: Text
    started_at: UtcTime
    request: Text
    merchant: Text | None
    total: Money | None
    status: PurchaseStatus
    policy: PolicySummary | None
    approved_at: UtcTime | None
    order_id: Text | None
    duration_ms: Count | None


class PurchaseHistory(TraceModel):
    """A page of the buyer's purchases, newest first; `next_cursor` goes in `before`."""

    purchases: tuple[PurchaseSummary, ...]
    next_cursor: Text | None
