from typing import Protocol

from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.models import (
    CheckoutSession,
    CheckoutSessionWithOrder,
    SandboxPaymentData,
)
from commerce_agent.approval import ApprovalService
from commerce_agent.delegated_payment import DelegatedPaymentClient, DelegationError
from commerce_agent.persistence import SqliteAgentStore


class TrustedCompletionGateway(Protocol):
    def get_checkout(self, checkout_id: str) -> CheckoutSession: ...

    def complete_checkout(
        self,
        checkout_id: str,
        payment_data: SandboxPaymentData,
        *,
        idempotency_key: str,
    ) -> CheckoutSessionWithOrder: ...


class CompletionError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class CompletionService:
    def __init__(
        self,
        gateway: TrustedCompletionGateway,
        approvals: ApprovalService,
        payments: DelegatedPaymentClient,
        store: SqliteAgentStore,
    ) -> None:
        self._gateway = gateway
        self._approvals = approvals
        self._payments = payments
        self._store = store

    def complete(self, *, run_id: str) -> CheckoutSessionWithOrder:
        persisted = self._store.completion_result(run_id)
        if persisted is not None:
            return CheckoutSessionWithOrder.model_validate(persisted)

        purchase = self._store.purchase(run_id)
        if purchase is None:
            raise CompletionError("Purchase run does not exist", code="purchase_not_found")
        checkout_id = purchase["checkout_id"]
        if not isinstance(checkout_id, str):
            raise CompletionError("Purchase has no checkout", code="checkout_missing")
        if purchase["stage"] not in {"approved", "uncertain"}:
            code = (
                "completion_in_progress" if purchase["stage"] == "completing" else "invalid_stage"
            )
            raise CompletionError("Purchase is not approved for completion", code=code)

        checkout = self._gateway.get_checkout(checkout_id)
        if checkout.id != checkout_id:
            raise CompletionError("Merchant returned another checkout", code="checkout_mismatch")
        if purchase["stage"] == "uncertain" and checkout.status == "completed":
            reconciled = CheckoutSessionWithOrder.model_validate(checkout.model_dump(mode="json"))
            if not self._store.claim_completion(run_id):
                raise CompletionError(
                    "Another reconciliation attempt already owns this purchase",
                    code="completion_in_progress",
                )
            try:
                self._store.record_completion(run_id, reconciled.model_dump(mode="json"))
            except Exception:
                self._store.release_completion(run_id, uncertain=True)
                raise
            return reconciled
        self._approvals.authorize(run_id=run_id, checkout=checkout)

        idempotency_key = self._store.idempotency_key(run_id, "checkout.complete")
        try:
            payment_data = self._payments.payment_data(
                checkout=checkout,
                idempotency_key=self._store.idempotency_key(run_id, "payment.delegate"),
            )
        except DelegationError as error:
            raise CompletionError("Payment delegation failed", code=error.code) from error
        if not self._store.claim_completion(run_id):
            raise CompletionError(
                "Another completion attempt already owns this purchase",
                code="completion_in_progress",
            )

        try:
            completed = self._gateway.complete_checkout(
                checkout_id,
                payment_data,
                idempotency_key=idempotency_key,
            )
            if completed.id != checkout_id or completed.order.checkout_session_id != checkout_id:
                raise CompletionError(
                    "Completed checkout and order do not match the purchase",
                    code="completion_mismatch",
                )
            self._store.record_completion(run_id, completed.model_dump(mode="json"))
        except ACPClientError as error:
            definite = error.status_code is not None and 400 <= error.status_code < 500
            self._store.release_completion(run_id, uncertain=not definite)
            raise
        except Exception:
            self._store.release_completion(run_id, uncertain=True)
            raise
        return completed
