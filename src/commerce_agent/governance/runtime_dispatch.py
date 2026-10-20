"""Production dispatch ports of the governed Human Present flow (AGENT_HP_FLOW=governed).

The gate admits each call only after its own durable D1 claim, so these ports add no
authority: they perform exactly one Merchant call through the ACP client. The client
never retries (httpx makes one attempt and follows no redirect), and every failure,
including a timeout, propagates so the gate records the operation as `uncertain`.
"""

from collections.abc import Callable

from commerce_agent.acp.models import CheckoutSession
from commerce_agent.application.purchase import CheckoutGateway

from .post_create import Effect


class SingleAttemptCheckoutDispatch:
    """`POST /checkout_sessions` once, with the idempotency key sealed in the D1 envelope."""

    def create_once(
        self,
        gateway: CheckoutGateway,
        *,
        variant_id: str,
        currency: str,
        idempotency_key: str,
    ) -> CheckoutSession:
        return gateway.create_checkout(
            variant_id, currency=currency, idempotency_key=idempotency_key
        )


class SingleAttemptPostCreateDispatch:
    """Each journaled post-create effect (verify, details, read, budget cancel) runs once."""

    def perform_once(self, effect: Effect, call: Callable[[], CheckoutSession]) -> CheckoutSession:
        return call()
