"""Production dispatch ports of the governed Human Present flow (AGENT_HP_FLOW=governed).

The gate admits each call only after its own durable D1 claim, so these ports add no
authority: they perform exactly one Merchant call through the ACP client. The client
never retries (httpx makes one attempt and follows no redirect). The Merchant's
definitive refusals of the creation become CheckoutCreationRefused; every other failure,
including a timeout, propagates so the gate records the operation as `uncertain`.
"""

from collections.abc import Callable

from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.application.purchase import CheckoutGateway

from .enforce import CheckoutCreationRefused, RefusalCode
from .post_create import Effect

# Refusals of POST /checkout_sessions that the Merchant answers only after finding no checkout
# for the key and before creating one (tesis-commerce-merchant: acp.create_in_transaction and
# checkout.prepare_in_transaction), by exact HTTP status and `detail.code`. D1 sends one POST
# per key, so these prove that nothing exists. Nothing else does: 409 idempotency_in_flight or
# creation_unrecoverable, 422 idempotency_conflict, a 404 without `invalid_item`, a 5xx or a
# timeout stay uncertain (docs/design/hp-p5a-definitive-failures.md).
DEFINITIVE_CREATION_REFUSALS: dict[tuple[int, str], RefusalCode] = {
    (409, "out_of_stock"): "out_of_stock",
    (409, "OFFER_EXPIRED"): "offer_expired",
    (409, "offer_expired"): "offer_expired",
    (404, "invalid_item"): "invalid_item",
}


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
        try:
            return gateway.create_checkout(
                variant_id, currency=currency, idempotency_key=idempotency_key
            )
        except ACPClientError as error:
            refusal = (
                DEFINITIVE_CREATION_REFUSALS.get((error.status_code, error.code))
                if error.status_code is not None
                else None
            )
            if refusal is None:
                raise
            raise CheckoutCreationRefused(refusal) from None


class SingleAttemptPostCreateDispatch:
    """Each journaled post-create effect (verify, details, read, budget cancel) runs once."""

    def perform_once(self, effect: Effect, call: Callable[[], CheckoutSession]) -> CheckoutSession:
        return call()
