from typing import Protocol

import jwt

from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.models import (
    CheckoutSession,
    CheckoutSessionWithOrder,
    SandboxPaymentData,
)
from commerce_agent.application.purchase import D1Continuation
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

    def complete_checkout_ap2(
        self,
        checkout_id: str,
        payment_data: SandboxPaymentData,
        checkout_mandate: str,
        *,
        idempotency_key: str,
    ) -> tuple[CheckoutSessionWithOrder, str]: ...


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
        d1_continuation: D1Continuation | None = None,
    ) -> None:
        self._gateway = gateway
        self._approvals = approvals
        self._payments = payments
        self._store = store
        self._d1_continuation = d1_continuation

    def complete(self, *, run_id: str) -> CheckoutSessionWithOrder:
        if self._store.has_d1_operation(run_id) and not self._continues(run_id):
            raise CompletionError(
                "D1 continuation requires independent authorization", code="d1_continuation_blocked"
            )
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
        replay_completed = purchase["stage"] == "uncertain" and checkout.status == "completed"
        if replay_completed and not self._approvals.requires_ap2:
            reconciled = CheckoutSessionWithOrder.model_validate(checkout.model_dump(mode="json"))
            if not self._store.claim_completion(run_id):
                raise CompletionError(
                    "Another reconciliation attempt already owns this purchase",
                    code="completion_in_progress",
                )
            try:
                self._store.record_completion(run_id, reconciled.model_dump(mode="json"))
            except Exception:
                self._store.release_completion(run_id, stage="uncertain")
                raise
            return reconciled
        approval = (
            self._approvals.approved_record(run_id=run_id, checkout_id=checkout_id)
            if replay_completed
            else self._approvals.authorize(run_id=run_id, checkout=checkout)
        )
        approved_checkout = checkout
        if replay_completed:
            if approval.checkout_jwt is None:
                raise CompletionError("AP2 checkout is missing", code="ap2_checkout_missing")
            try:
                claims = jwt.decode(
                    approval.checkout_jwt,
                    options={"verify_signature": False, "verify_exp": False},
                    algorithms=["ES256"],
                )
                approved_checkout = CheckoutSession.model_validate(claims["checkout"])
            except (jwt.PyJWTError, KeyError, TypeError, ValueError) as error:
                raise CompletionError(
                    "AP2 checkout is invalid", code="ap2_checkout_invalid"
                ) from error
            if approved_checkout.id != checkout_id:
                raise CompletionError("AP2 checkout does not match", code="checkout_mismatch")

        idempotency_key = self._store.idempotency_key(run_id, "checkout.complete")
        try:
            payment_kwargs = {}
            if self._approvals.requires_ap2:
                payment_kwargs = {
                    "checkout_jwt": approval.checkout_jwt,
                    "payment_mandate": approval.payment_mandate,
                }
            payment_data = self._payments.payment_data(
                checkout=approved_checkout,
                idempotency_key=self._store.idempotency_key(run_id, "payment.delegate"),
                **payment_kwargs,
            )
        except DelegationError as error:
            raise CompletionError("Payment delegation failed", code=error.code) from error
        if not self._store.claim_completion(run_id):
            raise CompletionError(
                "Another completion attempt already owns this purchase",
                code="completion_in_progress",
            )

        try:
            checkout_receipt = None
            payment_receipt = None
            if self._approvals.requires_ap2:
                if approval.checkout_mandate is None or approval.payment_mandate is None:
                    raise CompletionError("AP2 mandates are missing", code="ap2_mandate_missing")
                completed, checkout_receipt = self._gateway.complete_checkout_ap2(
                    checkout_id,
                    payment_data,
                    approval.checkout_mandate,
                    idempotency_key=idempotency_key,
                )
                payment_receipt = self._payments.payment_receipt(
                    token=payment_data.instrument.credential.token.get_secret_value(),
                    mandate=approval.payment_mandate,
                )
            else:
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
            self._store.record_completion(
                run_id,
                completed.model_dump(mode="json"),
                checkout_receipt_jwt=checkout_receipt,
                payment_receipt_jwt=payment_receipt,
            )
        except ACPClientError as error:
            if (error.code, error.status_code) in {
                ("payment_declined", 422),
                ("out_of_stock", 409),
            }:
                stage = "failed"
            elif (
                error.code != "idempotency_in_flight"
                and error.status_code is not None
                and 400 <= error.status_code < 500
            ):
                stage = "approved"
            else:
                stage = "uncertain"
            self._store.release_completion(run_id, stage=stage)
            raise
        except Exception:
            self._store.release_completion(run_id, stage="uncertain")
            raise
        return completed

    def _continues(self, run_id: str) -> bool:
        return self._d1_continuation is not None and self._d1_continuation.admits(run_id)
