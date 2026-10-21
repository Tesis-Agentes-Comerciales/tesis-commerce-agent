"""Steps 11 and 12 of an HNP task: the dispatch of its committed outbox row, then UoW-3.

docs/design/hnp-governed.md §3.3, §4.4 (outbox), §4.5 and §8.1. The dispatch never signs nor
closes: it sends the closed chains UoW-C kept under the keys the outbox fixes
(`<operation_id>:payment` at the PSP, `<operation_id>:complete` at the merchant), so a re-claim
resumes it with the same requests whatever the grant or the kill switch say by then. Before any
send it rebuilds the PSP request and compares its digest with the one UoW-2 committed; a request
that no longer matches is never sent.

Each send is written ahead: the outbox names it (`payment_sent`, `complete_sent`) before it leaves,
and moves on only with what the verifier answered:

- a PSP token: `payment_accepted`, then `complete_sent`;
- the order with the merchant's verified receipt, and the PSP's verified payment receipt:
  `completed`, then UoW-3 (`purchased`, the purchase `completed` with both receipts, §4.6);
- a valid PSP error receipt: `rejected(psp)`, the reservation released and the task escalated or
  failed (§8.1); a valid merchant error receipt: `uncertain(partial_verifier_failure)`;
- no classifiable answer (timeout, transport error, 5xx): `uncertain(dispatch_timeout)`;
- an answer without a valid receipt (a 4xx without one, 409 `idempotency_conflict` included; a
  receipt that does not verify or names another chain or order), an order of another checkout, or
  a request that no longer matches its digest: `uncertain(receipt_mismatch)`.

From `payment_accepted` the only way to `uncertain` is through `complete_sent`, and from
`completed` there is none: a run that cannot fetch the receipts again leaves the task running, and
the next run replays both idempotent requests to get them.
"""

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol

import jwt

from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.models import CheckoutSession, CheckoutSessionWithOrder, SandboxPaymentData
from commerce_agent.application.hnp_execution import (
    CheckoutCanceler,
    ClosedRequest,
    Mode,
    cancel_escalated_checkout,
    closed_request,
    payment_request_digest,
    purchase_terminal,
)
from commerce_agent.delegated_payment import DelegationError
from commerce_agent.delegation.models import (
    DispatchStep,
    Execution,
    Receipt,
    Step,
    UncertainReason,
)
from commerce_agent.delegation.ports import TransactionHook
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger

_UNANSWERED = frozenset({"timeout", "transport_error"})  # ACPClient codes without a response
PAYMENT_OPERATION = "payment.delegate"  # HP's operation for the PSP delegation (completion.py)
_log = logging.getLogger(__name__)


class PaymentProvider(Protocol):
    """`DelegatedPaymentClient`: the PSP's token, its payment receipt and its error receipts."""

    def hnp_payment_data(
        self,
        *,
        checkout: CheckoutSession,
        idempotency_key: str,
        checkout_jwt: str,
        payment_mandate: str,
        checkout_mandate: str,
    ) -> SandboxPaymentData: ...

    def payment_receipt(self, *, token: str, mandate: str) -> str: ...

    def verify_error_receipt(self, receipt: str, *, mandate: str) -> dict[str, Any]: ...


class MerchantCompletion(CheckoutCanceler, Protocol):
    """`ACPClient`: the AP2 completion, its error receipts and an escalation's cancel."""

    def complete_checkout_ap2(
        self,
        checkout_id: str,
        payment_data: SandboxPaymentData,
        checkout_mandate: str,
        *,
        idempotency_key: str,
    ) -> tuple[CheckoutSessionWithOrder, str]: ...

    def verify_checkout_error_receipt(self, receipt: str, *, mandate: str) -> dict[str, Any]: ...


class _Ended(Exception):
    def __init__(self, step: Step) -> None:
        super().__init__(step.status)
        self.step = step


@dataclass(frozen=True)
class _Send:
    """One dispatch run: its lease, what the outbox fixed and the purchase's terminal hook."""

    task_id: str
    lease_id: str
    run_id: str
    operation_id: str
    checkout_id: str
    order_id: str | None
    hooks: tuple[TransactionHook, ...]


class HnpDispatcher:
    def __init__(
        self,
        *,
        mode: Mode,
        ledger: SqliteDelegationLedger,
        store: SqliteAgentStore,
        payments: PaymentProvider,
        merchant: MerchantCompletion,
    ) -> None:
        self._mode: Mode = mode
        self._ledger, self._store = ledger, store
        self._payments, self._merchant = payments, merchant

    def dispatch(self, execution: Execution) -> Step:
        """Resume a claimed task's outbox row from its durable step. The result is the step that
        ended this run: `closed` with `purchased`, `escalated`, `failed` or `uncertain`;
        `lease_lost`; or `blocked`, which leaves the task running for the next run."""
        try:
            return self._dispatch(execution)
        except _Ended as ended:
            return ended.step

    def _dispatch(self, ex: Execution) -> Step:
        task, outbox = ex.task, ex.outbox
        version = None if task.run_id is None else self._store.purchase_state_version(task.run_id)
        if outbox is None or task.lease_id is None or task.run_id is None or version is None:
            raise LookupError("A dispatch without its outbox, lease, run or purchase")
        send = _Send(
            task.task_id,
            task.lease_id,
            task.run_id,
            outbox.operation_id,
            outbox.checkout_id,
            outbox.order_id,
            (purchase_terminal(task.task_id, task.run_id, version),),
        )
        state = outbox.state
        if state == "committed":
            state = self._advance(send, "payment_sent")
        elif state == "payment_accepted":
            state = self._advance(send, "complete_sent")
        if state not in ("payment_sent", "complete_sent", "completed"):
            return Step("blocked", ex)  # `rejected` and `uncertain` closed the task with them
        request = closed_request(self._ledger, task.grant_id, outbox.reservation_id)
        if request is None or outbox.payment_request_digest != payment_request_digest(
            operation_id=outbox.operation_id,
            checkout=request.checkout,
            checkout_jwt=request.checkout_jwt,
            closed=request.closed,
        ):
            _log.error("HNP dispatch of task %s no longer matches its commitment", task.task_id)
            self._doubt(send, "receipt_mismatch")
        payment = self._pay(send, request, rejectable=state == "payment_sent")
        if state == "payment_sent":
            self._advance(send, "payment_accepted")
            self._advance(send, "complete_sent")
        completed, checkout_receipt = self._complete(send, request, payment)
        payment_receipt = self._payment_receipt(send, request, payment)
        if state != "completed":
            self._advance(send, "completed", order_id=completed.order.id)
        elif completed.order.id != send.order_id:
            _log.error("HNP dispatch of task %s replayed another order", task.task_id)
            return Step("blocked", ex)
        return self._record(send, version, completed, checkout_receipt, payment_receipt)

    # The PSP

    def _pay(self, send: _Send, request: ClosedRequest, *, rejectable: bool) -> SandboxPaymentData:
        """The token for the committed request; the PSP replays it under the same key. Right
        before the first send the purchase notes it as `payment.delegate`, with that key, as HP
        notes its delegation: the trace's `payment_authorized` reads that time (§4.6)."""
        key = f"{send.operation_id}:payment"
        self._store.record_operation_key(send.run_id, PAYMENT_OPERATION, key)
        try:
            return self._payments.hnp_payment_data(
                checkout=request.checkout,
                idempotency_key=key,
                checkout_jwt=request.checkout_jwt,
                payment_mandate=request.closed.payment.token,
                checkout_mandate=request.closed.checkout.token,
            )
        except DelegationError as error:
            if rejectable and error.receipt is not None and _client_error(error.status_code):
                self._rejected_by_psp(send, request, error.receipt)
            self._doubt(send, _psp_doubt(error))

    def _rejected_by_psp(self, send: _Send, request: ClosedRequest, receipt: str) -> NoReturn:
        """Nothing reached the merchant: the reservation is released and the task escalates
        (B2-HNP) or fails with the receipt's code; an escalation cancels its checkout (§8.2)."""
        try:
            claims = self._payments.verify_error_receipt(
                receipt, mandate=request.closed.payment.token
            )
        except DelegationError as error:
            self._doubt(send, _psp_doubt(error))
        rejected = Receipt(
            kind="payment",
            jwt=receipt,
            issuer=claims["iss"],
            status=claims["status"],
            reference=claims["reference"],
            error=claims["error"],
            payment_id=claims["payment_id"],
        )
        step = self._ledger.reject_dispatch(
            send.task_id,
            send.lease_id,
            claims["error"],
            rejected,
            escalate=self._mode == "enforce",
            hooks=send.hooks,
        )
        if step.execution is not None and step.execution.task.state == "escalated":
            cancel_escalated_checkout(self._store, self._merchant, send.run_id, send.checkout_id)
        raise _Ended(step)

    def _payment_receipt(
        self, send: _Send, request: ClosedRequest, payment: SandboxPaymentData
    ) -> str:
        try:
            return self._payments.payment_receipt(
                token=payment.instrument.credential.token.get_secret_value(),
                mandate=request.closed.payment.token,
            )
        except DelegationError as error:
            unanswered = error.status_code is None or error.status_code >= 500
            self._doubt(send, "dispatch_timeout" if unanswered else "receipt_mismatch")

    # The merchant

    def _complete(
        self, send: _Send, request: ClosedRequest, payment: SandboxPaymentData
    ) -> tuple[CheckoutSessionWithOrder, str]:
        """The order and its receipt, which `complete_checkout_ap2` verified against the chain,
        for the checkout the outbox names. An order of another checkout (`checkout.id` or
        `order.checkout_session_id`) is doubt before `completed`, as HP's `completion_mismatch`:
        UoW-3 would refuse it, and from `completed` there is no way to `uncertain`."""
        try:
            completed, receipt = self._merchant.complete_checkout_ap2(
                send.checkout_id,
                payment,
                request.closed.checkout.token,
                idempotency_key=f"{send.operation_id}:complete",
            )
        except ACPClientError as error:
            if _unanswered(error):
                self._doubt(send, "dispatch_timeout")
            if error.receipt is not None and _client_error(error.status_code):
                self._rejected_by_merchant(send, request, error.receipt)
            self._doubt(send, "receipt_mismatch")
        ordered = (completed.id, completed.order.checkout_session_id)
        if ordered != (send.checkout_id, send.checkout_id):
            _log.error("HNP dispatch of task %s got the order of another checkout", send.task_id)
            self._doubt(send, "receipt_mismatch")
        return completed, receipt

    def _rejected_by_merchant(self, send: _Send, request: ClosedRequest, receipt: str) -> NoReturn:
        """After the PSP's token: a partial failure between verifiers, left `uncertain` (§8.1)."""
        try:
            claims = self._merchant.verify_checkout_error_receipt(
                receipt, mandate=request.closed.checkout.token
            )
        except ACPClientError as error:
            self._doubt(send, "dispatch_timeout" if _unanswered(error) else "receipt_mismatch")
        rejected = Receipt(
            kind="checkout",
            jwt=receipt,
            issuer=claims["iss"],
            status=claims["status"],
            reference=claims["reference"],
            error=claims["error"],
            order_id=claims.get("order_id"),
        )
        step = self._ledger.reject_dispatch(
            send.task_id, send.lease_id, claims["error"], rejected, hooks=send.hooks
        )
        raise _Ended(step)

    # The ledger

    def _advance(self, send: _Send, to: DispatchStep, *, order_id: str | None = None) -> str:
        step = self._ledger.advance_dispatch(send.task_id, send.lease_id, to, order_id=order_id)
        if step.status not in ("applied", "reused"):
            raise _Ended(step)
        return to

    def _doubt(self, send: _Send, reason: UncertainReason) -> NoReturn:
        """`uncertain` from a send; from `completed` the ledger refuses (`blocked`), so the task
        stays running for the next run."""
        _log.warning("HNP dispatch of task %s in doubt: %s", send.task_id, reason)
        raise _Ended(
            self._ledger.mark_uncertain(send.task_id, send.lease_id, reason, hooks=send.hooks)
        )

    def _record(
        self,
        send: _Send,
        version: int,
        completed: CheckoutSessionWithOrder,
        checkout_receipt: str,
        payment_receipt: str,
    ) -> Step:
        """UoW-3 with both verified receipts; the purchase completes in the same transaction."""
        paid, ordered = _claims(payment_receipt), _claims(checkout_receipt)
        payment = Receipt(
            kind="payment",
            jwt=payment_receipt,
            issuer=paid["iss"],
            status=paid["status"],
            reference=paid["reference"],
            payment_id=paid["payment_id"],
        )
        checkout = Receipt(
            kind="checkout",
            jwt=checkout_receipt,
            issuer=ordered["iss"],
            status=ordered["status"],
            reference=ordered["reference"],
            order_id=ordered["order_id"],
        )
        snapshot = completed.model_dump(mode="json")

        def complete(connection: sqlite3.Connection) -> None:
            SqliteAgentStore.record_hnp_completion(
                connection,
                send.run_id,
                snapshot,
                expected_state_version=version,
                checkout_receipt_jwt=checkout_receipt,
                payment_receipt_jwt=payment_receipt,
            )

        return self._ledger.record_completion(
            send.task_id, send.lease_id, payment, checkout, hooks=(complete,)
        )


def _claims(receipt: str) -> dict[str, Any]:
    """The claims of a receipt the client already verified (signature, issuer, reference)."""
    return jwt.decode(receipt, options={"verify_signature": False})


def _client_error(status_code: int | None) -> bool:
    return status_code is not None and 400 <= status_code < 500


def _unanswered(error: ACPClientError) -> bool:
    return error.code in _UNANSWERED or (error.status_code or 0) >= 500


def _psp_doubt(error: DelegationError) -> UncertainReason:
    """No answer (transport or 5xx) is a timeout; any other answer lacked a valid receipt."""
    return "dispatch_timeout" if error.code == "provider_unavailable" else "receipt_mismatch"
