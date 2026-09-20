from typing import Protocol

from commerce_agent.acp.models import CheckoutSession, CheckoutUpdateRequest


class CheckoutGateway(Protocol):
    def create_checkout(self, variant_id: str, *, idempotency_key: str) -> CheckoutSession: ...

    def get_checkout(self, checkout_id: str) -> CheckoutSession: ...

    def update_checkout(
        self,
        checkout_id: str,
        update: CheckoutUpdateRequest,
        *,
        idempotency_key: str,
    ) -> CheckoutSession: ...

    def cancel_checkout(
        self,
        checkout_id: str,
        *,
        idempotency_key: str,
        reason_code: str | None = None,
    ) -> CheckoutSession: ...
