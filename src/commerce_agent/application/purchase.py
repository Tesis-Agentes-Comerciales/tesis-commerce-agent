from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from commerce_agent.acp.feed import ProductCandidate, ProductFeed
from commerce_agent.acp.models import (
    Buyer,
    CheckoutSession,
    CheckoutUpdateRequest,
    FulfillmentDetails,
    SelectedFulfillmentOption,
)
from commerce_agent.approval import ApprovalError, ApprovalService, ApprovalTerms
from commerce_agent.domain import PurchaseIntent, rank_candidates
from commerce_agent.persistence import ApprovalRecord, SqliteAgentStore


class PurchaseStage(StrEnum):
    RECEIVED = "received"
    SELECTED = "selected"
    CHECKOUT_PREPARED = "checkout_prepared"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    APPROVED = "approved"
    COMPLETING = "completing"
    UNCERTAIN = "uncertain"
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"


class CheckoutGateway(Protocol):
    def create_checkout(
        self, variant_id: str, *, currency: str, idempotency_key: str
    ) -> CheckoutSession: ...

    def get_checkout(self, checkout_id: str) -> CheckoutSession: ...

    def signed_checkout(self, checkout: CheckoutSession) -> str: ...

    def verify_signed_checkout(self, token: str, checkout: CheckoutSession) -> None: ...

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


class PurchaseServiceError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class CheckoutDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    buyer: Buyer
    fulfillment: FulfillmentDetails


class PurchasePreparation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str
    stage: PurchaseStage
    selected_candidate: ProductCandidate
    checkout: CheckoutSession
    price_changed: bool


class PurchaseService:
    def __init__(
        self,
        *,
        feed: ProductFeed,
        checkout: CheckoutGateway,
        store: SqliteAgentStore,
        approvals: ApprovalService,
    ) -> None:
        self._feed = feed
        self._checkout = checkout
        self._store = store
        self._approvals = approvals

    def start(
        self,
        *,
        run_id: str,
        request: str,
        intent: PurchaseIntent,
        selected_variant_id: str,
        details: CheckoutDetails,
        graph_thread_id: str | None = None,
        owner_id: str = "local-human",
        feed: ProductFeed | None = None,
    ) -> PurchasePreparation:
        self._store.create_purchase(run_id, request, owner_id)
        if graph_thread_id is not None:
            self._store.save_graph_thread(run_id, graph_thread_id)
        selection = rank_candidates(feed or self._feed, intent)
        if selection.selected is None:
            self._store.update_purchase(run_id, stage=PurchaseStage.FAILED)
            raise PurchaseServiceError("No candidate satisfies the request", code="no_candidate")
        candidate = next(
            (
                item.candidate
                for item in selection.ranked
                if item.candidate.variant_id == selected_variant_id
            ),
            None,
        )
        if candidate is None:
            self._store.update_purchase(run_id, stage=PurchaseStage.FAILED)
            raise PurchaseServiceError(
                "Selected candidate does not satisfy the request", code="invalid_candidate"
            )
        self._store.update_purchase(
            run_id,
            stage=PurchaseStage.SELECTED,
            selected_variant_id=candidate.variant_id,
        )
        idempotency_key = self._store.idempotency_key(run_id, "checkout.create")
        # Un fallo a medio camino deja la compra en `selected` o `checkout_prepared`: se marca
        # `failed` para que no quede bloqueando la siguiente compra sin poder cancelarse.
        with self._abandon_on_error(run_id):
            checkout = self._checkout.create_checkout(
                candidate.variant_id,
                currency=candidate.currency,
                idempotency_key=idempotency_key,
            )
            self._validate_created_checkout(checkout, candidate)
            self._store.update_purchase(
                run_id,
                stage=PurchaseStage.CHECKOUT_PREPARED,
                checkout_id=checkout.id,
            )
            self._enforce_authoritative_budget(run_id, checkout, intent)
            ready = self._apply_details(run_id, checkout, details)
            self._validate_created_checkout(ready, candidate)
            self._enforce_authoritative_budget(run_id, ready, intent)
            self._store.update_purchase(run_id, stage=PurchaseStage.AWAITING_CONFIRMATION)
            price_changed = _checkout_unit_amount(ready, candidate.variant_id) != (
                candidate.observed_unit_amount_minor
            )
        return PurchasePreparation(
            run_id=run_id,
            stage=PurchaseStage.AWAITING_CONFIRMATION,
            selected_candidate=candidate,
            checkout=ready,
            price_changed=price_changed,
        )

    def approval_terms(self, run_id: str) -> ApprovalTerms:
        checkout = self._current_checkout(run_id)
        signed = self._checkout.signed_checkout(checkout) if self._approvals.requires_ap2 else None
        return self._approvals.present(checkout, checkout_jwt=signed)

    def approve(
        self,
        *,
        run_id: str,
        actor_id: str,
        expected_fingerprint: str,
        checkout_jwt: str | None = None,
    ) -> ApprovalRecord:
        purchase = self._require_purchase(run_id)
        if purchase.get("stage") != PurchaseStage.AWAITING_CONFIRMATION:
            raise PurchaseServiceError("Purchase is not awaiting approval", code="invalid_stage")
        checkout = self._current_checkout(run_id)
        if self._approvals.requires_ap2:
            if checkout_jwt is None:
                raise ApprovalError("Signed checkout is required", code="ap2_checkout_required")
            self._checkout.verify_signed_checkout(checkout_jwt, checkout)
        record = self._approvals.approve(
            run_id=run_id,
            actor_id=actor_id,
            expected_fingerprint=expected_fingerprint,
            checkout=checkout,
            checkout_jwt=checkout_jwt,
        )
        self._store.update_purchase(run_id, stage=PurchaseStage.APPROVED)
        return record

    def cancel(self, *, run_id: str) -> None:
        purchase = self._require_purchase(run_id)
        if purchase.get("stage") == PurchaseStage.CANCELED:
            return
        if purchase.get("stage") != PurchaseStage.AWAITING_CONFIRMATION:
            raise PurchaseServiceError("Purchase cannot be canceled", code="invalid_stage")
        checkout = self._current_checkout(run_id)
        key = self._store.idempotency_key(run_id, "checkout.cancel.user")
        canceled = self._checkout.cancel_checkout(
            checkout.id,
            idempotency_key=key,
            reason_code="other",
        )
        if canceled.id != checkout.id or canceled.status != "canceled":
            raise PurchaseServiceError(
                "Merchant did not cancel the expected checkout", code="checkout_mismatch"
            )
        self._store.update_purchase(run_id, stage=PurchaseStage.CANCELED)

    @contextmanager
    def _abandon_on_error(self, run_id: str) -> Iterator[None]:
        try:
            yield
        except BaseException:
            purchase = self._store.purchase(run_id)
            unfinished = {PurchaseStage.SELECTED, PurchaseStage.CHECKOUT_PREPARED}
            if purchase is not None and purchase.get("stage") in unfinished:
                self._store.update_purchase(run_id, stage=PurchaseStage.FAILED)
            raise

    def _apply_details(
        self, run_id: str, checkout: CheckoutSession, details: CheckoutDetails
    ) -> CheckoutSession:
        option = _selected_fulfillment(checkout)
        update = CheckoutUpdateRequest(
            buyer=details.buyer,
            fulfillment_details=details.fulfillment,
            selected_fulfillment_options=[option],
        )
        key = self._store.idempotency_key(run_id, "checkout.update.details")
        updated = self._checkout.update_checkout(checkout.id, update, idempotency_key=key)
        if updated.id != checkout.id:
            raise PurchaseServiceError(
                "Merchant changed checkout identity", code="checkout_mismatch"
            )
        current = self._checkout.get_checkout(checkout.id)
        if current.id != checkout.id:
            raise PurchaseServiceError(
                "Merchant changed checkout identity", code="checkout_mismatch"
            )
        if current.status != "ready_for_payment":
            raise PurchaseServiceError(
                "Checkout did not become ready for payment", code="checkout_not_ready"
            )
        return current

    def _enforce_authoritative_budget(
        self, run_id: str, checkout: CheckoutSession, intent: PurchaseIntent
    ) -> None:
        maximum = intent.hard.max_total_amount_minor
        if maximum is None or checkout.total_minor <= maximum:
            return
        key = self._store.idempotency_key(run_id, "checkout.cancel.budget")
        self._checkout.cancel_checkout(
            checkout.id,
            idempotency_key=key,
            reason_code="price_sensitivity",
        )
        self._store.update_purchase(run_id, stage=PurchaseStage.CANCELED)
        raise PurchaseServiceError(
            "Authoritative checkout total exceeds the budget",
            code="budget_exceeded",
        )

    @staticmethod
    def _validate_created_checkout(checkout: CheckoutSession, candidate: ProductCandidate) -> None:
        if checkout.currency.upper() != candidate.currency.upper():
            raise PurchaseServiceError(
                "Merchant checkout currency differs from the selected variant",
                code="checkout_mismatch",
            )
        if len(checkout.line_items) != 1:
            raise PurchaseServiceError(
                "Merchant checkout contains unexpected items", code="checkout_mismatch"
            )
        matching = [
            line
            for line in checkout.line_items
            if isinstance(line.get("item"), dict) and line["item"].get("id") == candidate.variant_id
        ]
        if len(matching) != 1 or matching[0].get("quantity") != 1:
            raise PurchaseServiceError(
                "Merchant checkout does not match the selected variant",
                code="checkout_mismatch",
            )

    def _current_checkout(self, run_id: str) -> CheckoutSession:
        purchase = self._require_purchase(run_id)
        checkout_id = purchase.get("checkout_id")
        if not isinstance(checkout_id, str):
            raise PurchaseServiceError("Purchase has no checkout", code="checkout_missing")
        return self._checkout.get_checkout(checkout_id)

    def _require_purchase(self, run_id: str) -> dict[str, str | None]:
        purchase = self._store.purchase(run_id)
        if purchase is None:
            raise PurchaseServiceError("Purchase run does not exist", code="purchase_not_found")
        return purchase


def _checkout_unit_amount(checkout: CheckoutSession, variant_id: str) -> int:
    for line in checkout.line_items:
        item = line.get("item")
        if isinstance(item, dict) and item.get("id") == variant_id:
            amount = item.get("unit_amount")
            if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
                return amount
    raise PurchaseServiceError("Checkout unit price is invalid", code="checkout_mismatch")


def _selected_fulfillment(checkout: CheckoutSession) -> SelectedFulfillmentOption:
    options = getattr(checkout, "fulfillment_options", None)
    if not isinstance(options, list):
        raise PurchaseServiceError(
            "Checkout has no fulfillment options", code="fulfillment_missing"
        )
    shipping = [
        option
        for option in options
        if isinstance(option, dict)
        and option.get("type") == "shipping"
        and isinstance(option.get("id"), str)
    ]
    item_ids: list[str] = []
    for line in checkout.line_items:
        item_id = line.get("id")
        if not isinstance(item_id, str):
            raise PurchaseServiceError(
                "Checkout fulfillment cannot be selected deterministically",
                code="fulfillment_ambiguous",
            )
        item_ids.append(item_id)
    if len(shipping) != 1 or not item_ids:
        raise PurchaseServiceError(
            "Checkout fulfillment cannot be selected deterministically",
            code="fulfillment_ambiguous",
        )
    return SelectedFulfillmentOption(
        type="shipping",
        option_id=shipping[0]["id"],
        item_ids=item_ids,
    )
