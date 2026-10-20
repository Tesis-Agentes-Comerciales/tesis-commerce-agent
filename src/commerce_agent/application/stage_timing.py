"""Time the model, D1 and merchant calls of a turn without changing what they do.

Each wrapper delegates every call unchanged, results and errors included, and records how long
it took under a fixed stage name (persistence/stage_timings_sqlite.py).
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol

from commerce_agent.acp.feed import ProductCandidate, ProductFeed
from commerce_agent.acp.models import CheckoutSession, CheckoutUpdateRequest
from commerce_agent.application.purchase import CheckoutGateway, D1EnforceGate
from commerce_agent.domain import PurchaseIntent, RankedCandidate, SelectionResult
from commerce_agent.governance.post_create import PostCreateFlow
from commerce_agent.llm import (
    CandidateDecision,
    CatalogRecovery,
    CommerceDecision,
    ConversationMessage,
    ConversationRouter,
)


class StageTimer(Protocol):
    def measure(self, stage: str) -> AbstractContextManager[None]: ...


class TimedConversationRouter:
    def __init__(self, router: ConversationRouter, timer: StageTimer) -> None:
        self._router, self._timer = router, timer

    def route(self, messages: tuple[ConversationMessage, ...]) -> CommerceDecision:
        with self._timer.measure("model.route"):
            return self._router.route(messages)

    def choose_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        candidates: tuple[RankedCandidate, ...],
    ) -> CandidateDecision:
        with self._timer.measure("model.choose_candidate"):
            return self._router.choose_candidate(messages, intent, candidates)

    def recover_no_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        rejection_counts: dict[str, int],
        alternatives: tuple[ProductCandidate, ...],
    ) -> CatalogRecovery:
        with self._timer.measure("model.recover_no_candidate"):
            return self._router.recover_no_candidate(
                messages, intent, rejection_counts, alternatives
            )

    def close(self) -> None:
        # A router built per feed owns an HTTP client; the conversation closes it after the turn.
        close = getattr(self._router, "close", None)
        if callable(close):
            close()


class TimedCheckoutGateway:
    def __init__(self, gateway: CheckoutGateway, timer: StageTimer) -> None:
        self._gateway, self._timer = gateway, timer

    def create_checkout(
        self, variant_id: str, *, currency: str, idempotency_key: str
    ) -> CheckoutSession:
        with self._timer.measure("merchant.create_checkout"):
            return self._gateway.create_checkout(
                variant_id, currency=currency, idempotency_key=idempotency_key
            )

    def get_checkout(self, checkout_id: str) -> CheckoutSession:
        with self._timer.measure("merchant.get_checkout"):
            return self._gateway.get_checkout(checkout_id)

    def update_checkout(
        self,
        checkout_id: str,
        update: CheckoutUpdateRequest,
        *,
        idempotency_key: str,
    ) -> CheckoutSession:
        with self._timer.measure("merchant.update_checkout"):
            return self._gateway.update_checkout(
                checkout_id, update, idempotency_key=idempotency_key
            )

    def cancel_checkout(
        self,
        checkout_id: str,
        *,
        idempotency_key: str,
        reason_code: str | None = None,
    ) -> CheckoutSession:
        with self._timer.measure("merchant.cancel_checkout"):
            return self._gateway.cancel_checkout(
                checkout_id, idempotency_key=idempotency_key, reason_code=reason_code
            )

    # Local signatures: no merchant call to time.
    def signed_checkout(self, checkout: CheckoutSession) -> str:
        return self._gateway.signed_checkout(checkout)

    def verify_signed_checkout(self, token: str, checkout: CheckoutSession) -> None:
        self._gateway.verify_signed_checkout(token, checkout)


class TimedD1Gate:
    """D1 as a whole: in the governed flow `create_checkout` includes the merchant POST."""

    def __init__(self, gate: D1EnforceGate, timer: StageTimer) -> None:
        self._gate, self._timer = gate, timer

    def capture(
        self,
        feed: ProductFeed,
        intent: PurchaseIntent,
        ranking: Callable[[ProductFeed, PurchaseIntent], SelectionResult],
    ) -> object:
        with self._timer.measure("d1.capture"):
            return self._gate.capture(feed, intent, ranking)

    def create_checkout(
        self,
        capture: object,
        *,
        run_id: str,
        owner_id: str,
        selection: SelectionResult,
        candidate: ProductCandidate,
        gateway: CheckoutGateway,
    ) -> CheckoutSession:
        with self._timer.measure("d1.create_checkout"):
            return self._gate.create_checkout(
                capture,
                run_id=run_id,
                owner_id=owner_id,
                selection=selection,
                candidate=candidate,
                gateway=gateway,
            )

    def post_create_flow(self, run_id: str, owner_id: str) -> PostCreateFlow:
        return self._gate.post_create_flow(run_id, owner_id)
