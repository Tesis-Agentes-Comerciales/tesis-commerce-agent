from enum import StrEnum
from typing import TypedDict


class PurchaseStage(StrEnum):
    RECEIVED = "received"
    DISCOVERING = "discovering"
    SELECTING = "selecting"
    SELECTED = "selected"
    CHECKOUT_PREPARED = "checkout_prepared"
    COLLECTING_DETAILS = "collecting_details"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    APPROVED = "approved"
    COMPLETING = "completing"
    UNCERTAIN = "uncertain"
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"


class PurchaseState(TypedDict):
    run_id: str
    request: str
    stage: PurchaseStage
    selected_variant_id: str | None
    checkout_id: str | None
    approved_checkout_fingerprint: str | None
    order_id: str | None
    errors: list[str]
