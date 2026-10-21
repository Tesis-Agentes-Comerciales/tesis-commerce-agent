import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
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
from commerce_agent.application.ap2_evidence import Ap2EvidenceRecorder
from commerce_agent.application.offer_freshness import FreshnessStage, OfferFreshnessGuard
from commerce_agent.approval import ApprovalError, ApprovalService, ApprovalTerms
from commerce_agent.domain import PurchaseIntent, SelectionResult, rank_candidates
from commerce_agent.governance.canonical import canonical_bytes
from commerce_agent.governance.hnp_policy import HnpGrant
from commerce_agent.governance.post_create import PostCreateFlow
from commerce_agent.persistence import ApprovalRecord, SqliteAgentStore


class D1SelectionObserver(Protocol):
    def failure(self) -> None: ...

    def capture(
        self,
        feed: ProductFeed,
        intent: PurchaseIntent,
        ranking: Callable[[ProductFeed, PurchaseIntent], SelectionResult],
    ) -> object | None: ...

    def observe(
        self,
        capture: object,
        *,
        run_id: str,
        owner_id: str,
        selection: SelectionResult,
        selected_variant_id: str,
    ) -> None: ...


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


class D1EnforceGate(Protocol):
    """Backend injection only; never a shadow observer or a model-facing authority."""

    def capture(
        self,
        feed: ProductFeed,
        intent: PurchaseIntent,
        ranking: Callable[[ProductFeed, PurchaseIntent], SelectionResult],
    ) -> object: ...

    def create_checkout(
        self,
        capture: object,
        *,
        run_id: str,
        owner_id: str,
        selection: SelectionResult,
        candidate: ProductCandidate,
        gateway: CheckoutGateway,
    ) -> CheckoutSession: ...

    def post_create_flow(self, run_id: str, owner_id: str) -> PostCreateFlow: ...


class D1HnpGate(Protocol):
    """D1-HNP: the D1 gate bound to b2-hnp-v1, deciding over the grant's shortlist (§3.4)."""

    def capture(
        self,
        feed: ProductFeed,
        intent: PurchaseIntent,
        ranking: Callable[[ProductFeed, PurchaseIntent], SelectionResult],
    ) -> object: ...

    def create_checkout(
        self,
        capture: object,
        *,
        run_id: str,
        owner_id: str,
        selection: SelectionResult,
        candidate: ProductCandidate,
        gateway: CheckoutGateway,
        grant: HnpGrant,
    ) -> CheckoutSession: ...

    def post_create_flow(self, run_id: str, owner_id: str) -> PostCreateFlow: ...


class D2Attempt(Protocol):
    """One approval: `authorize` before any mandate exists, `commit` in place of the save."""

    def authorize(self, current: ApprovalTerms) -> str: ...

    def commit(self, approval: ApprovalRecord) -> None: ...


class D2AuthorizationGate(Protocol):
    """Backend injection only: POL-03..06 when the human approves (D2-HP)."""

    def attempt(
        self,
        *,
        run_id: str,
        actor_id: str,
        expected_fingerprint: str,
        checkout: CheckoutSession,
    ) -> AbstractContextManager[D2Attempt]: ...


class D1Continuation(Protocol):
    """Whether a purchase with D1 history may continue past checkout preparation."""

    def admits(self, run_id: str) -> bool: ...


class PurchaseServiceError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def invalid_candidate() -> PurchaseServiceError:
    return PurchaseServiceError(
        "Selected candidate does not satisfy the request", code="invalid_candidate"
    )


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
        offer_freshness: OfferFreshnessGuard | None = None,
        d1_observer: D1SelectionObserver | None = None,
        d1_gate: D1EnforceGate | None = None,
        d1_continuation: D1Continuation | None = None,
        d2_gate: D2AuthorizationGate | None = None,
        ap2_evidence: Ap2EvidenceRecorder | None = None,
        hnp_d1_gate: D1HnpGate | None = None,
    ) -> None:
        self._feed = feed
        self._checkout = checkout
        self._store = store
        self._approvals = approvals
        self._offer_freshness = offer_freshness
        self._d1_observer = d1_observer
        if d1_gate is not None and d1_observer is not None:
            raise ValueError("D1 enforce and shadow must be configured separately")
        self._d1_gate = d1_gate
        if d1_continuation is not None and d1_gate is None:
            raise ValueError("Governed continuation requires the D1 enforce gate")
        self._d1_continuation = d1_continuation
        if d2_gate is not None and d1_continuation is None:
            raise ValueError("D2 authorization requires the governed continuation")
        self._d2_gate = d2_gate
        self._ap2_evidence = ap2_evidence
        if hnp_d1_gate is not None and d1_gate is None:
            raise ValueError("D1-HNP runs only beside the D1 enforce gate")
        self._hnp_d1_gate = hnp_d1_gate

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
        grant: HnpGrant | None = None,
    ) -> PurchasePreparation:
        """Prepare a checkout. With a `grant` the purchase is Human Not Present: D1 runs with
        its own gate and the purchase stops at `checkout_prepared` for the executor (§4.6)."""
        if grant is not None and (
            self._d1_observer is not None
            or (self._d1_gate is not None and self._hnp_d1_gate is None)
        ):
            # B0-HNP runs no D1 and B2-HNP runs D1-HNP; shadow is not defined for HNP (§2.2).
            raise PurchaseServiceError("D1-HNP is not configured", code="d1_unavailable")
        self._store.create_purchase(
            run_id, request, owner_id, grant_id=None if grant is None else grant.grant_id
        )
        gate = self._d1_gate if grant is None else self._hnp_d1_gate
        if gate is None and self._store.has_d1_operation(run_id):
            raise PurchaseServiceError(
                "D1 operation requires explicit recovery", code="d1_recovery_required"
            )
        if gate is not None:
            current = self._store.purchase(run_id)
            if current is None or current["stage"] != PurchaseStage.RECEIVED:
                raise PurchaseServiceError(
                    "D1 operation requires explicit recovery", code="d1_recovery_required"
                )
        if graph_thread_id is not None:
            self._store.save_graph_thread(run_id, graph_thread_id)
        effective_feed = feed or self._feed
        captured = None
        if gate is not None:
            try:
                captured = gate.capture(effective_feed, intent, rank_candidates)
            except Exception:
                raise PurchaseServiceError(
                    "D1 capture unavailable", code="d1_capture_failed"
                ) from None
        if self._d1_observer is not None:
            try:
                captured = self._d1_observer.capture(effective_feed, intent, rank_candidates)
            except Exception:
                self._shadow_unavailable()
        selection = rank_candidates(effective_feed, intent)
        if selection.selected is None:
            self._store.update_purchase(run_id, stage=PurchaseStage.FAILED, failure="no_candidate")
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
            error = invalid_candidate()
            self._store.update_purchase(run_id, stage=PurchaseStage.FAILED, failure=error.code)
            raise error
        enforced_checkout = None
        if gate is not None:
            enforced_checkout = self._governed_checkout(
                captured,
                run_id=run_id,
                owner_id=owner_id,
                selection=selection,
                candidate=candidate,
                grant=grant,
            )
            if not isinstance(enforced_checkout, CheckoutSession):
                raise PurchaseServiceError(
                    "D1 gate returned an invalid checkout", code="d1_invalid_result"
                )
        else:
            self._store.update_purchase(
                run_id,
                stage=PurchaseStage.SELECTED,
                selected_variant_id=candidate.variant_id,
            )
        if self._d1_observer is not None and captured is not None:
            try:
                self._d1_observer.observe(
                    captured,
                    run_id=run_id,
                    owner_id=owner_id,
                    selection=selection,
                    selected_variant_id=candidate.variant_id,
                )
            except Exception:
                self._shadow_unavailable()
        idempotency_key = (
            self._store.idempotency_key(run_id, "checkout.create") if gate is None else None
        )
        # Preserve legacy abandonment only for off/shadow episodes without D1 history.
        flow = gate.post_create_flow(run_id, owner_id) if gate is not None else None
        with self._abandon_on_error(run_id, flow):
            if enforced_checkout is not None:
                checkout = enforced_checkout
            else:
                if idempotency_key is None:
                    raise PurchaseServiceError(
                        "Checkout lacks authorization", code="d1_invalid_result"
                    )
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
            self._enforce_authoritative_budget(run_id, checkout, intent, flow)
            ready = self._apply_details(run_id, checkout, details, flow)
            self._validate_created_checkout(ready, candidate)
            self._enforce_authoritative_budget(run_id, ready, intent, flow)
            # An HNP purchase stays `checkout_prepared`: nobody is there to confirm it.
            if flow is None and grant is None:
                self._store.update_purchase(run_id, stage=PurchaseStage.AWAITING_CONFIRMATION)
            price_changed = _checkout_unit_amount(ready, candidate.variant_id) != (
                candidate.observed_unit_amount_minor
            )
            if flow is not None and grant is None:
                flow.finish()
        return PurchasePreparation(
            run_id=run_id,
            stage=PurchaseStage.AWAITING_CONFIRMATION
            if grant is None
            else PurchaseStage.CHECKOUT_PREPARED,
            selected_candidate=candidate,
            checkout=ready,
            price_changed=price_changed,
        )

    def _governed_checkout(
        self,
        captured: object,
        *,
        run_id: str,
        owner_id: str,
        selection: SelectionResult,
        candidate: ProductCandidate,
        grant: HnpGrant | None,
    ) -> CheckoutSession:
        """The checkout D1 authorized: the HP gate, or D1-HNP over the grant's shortlist."""
        if grant is None:
            assert self._d1_gate is not None
            return self._d1_gate.create_checkout(
                captured,
                run_id=run_id,
                owner_id=owner_id,
                selection=selection,
                candidate=candidate,
                gateway=self._checkout,
            )
        assert self._hnp_d1_gate is not None
        return self._hnp_d1_gate.create_checkout(
            captured,
            run_id=run_id,
            owner_id=owner_id,
            selection=selection,
            candidate=candidate,
            gateway=self._checkout,
            grant=grant,
        )

    def _check_offer(self, run_id: str, checkout: CheckoutSession, stage: FreshnessStage) -> None:
        if self._offer_freshness is not None:
            self._offer_freshness.check(run_id=run_id, checkout=checkout, stage=stage)

    def _shadow_unavailable(self) -> None:
        try:
            if self._d1_observer is not None:
                self._d1_observer.failure()
        except Exception:
            pass
        logging.getLogger(__name__).warning("D1 shadow observation unavailable")

    def approval_terms(self, run_id: str) -> ApprovalTerms:
        checkout = self._current_checkout(run_id)
        # Only terms the agent would present read the live offer and leave freshness evidence.
        self._approvals.require_approvable(checkout)
        self._check_offer(run_id, checkout, "present")
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
        self._block_d1_continuation(run_id)
        purchase = self._require_purchase(run_id)
        if purchase.get("stage") != PurchaseStage.AWAITING_CONFIRMATION:
            raise PurchaseServiceError("Purchase is not awaiting approval", code="invalid_stage")
        checkout = self._checkout_of(purchase)
        if self._approvals.requires_ap2:
            if checkout_jwt is None:
                raise ApprovalError("Signed checkout is required", code="ap2_checkout_required")
            self._checkout.verify_signed_checkout(checkout_jwt, checkout)
        if self._d2_gate is None:
            record = self._approvals.approve(
                run_id=run_id,
                actor_id=actor_id,
                expected_fingerprint=expected_fingerprint,
                checkout=checkout,
                checkout_jwt=checkout_jwt,
                before_signing=lambda: self._check_offer(run_id, checkout, "approve"),
            )
            self._store.update_purchase(run_id, stage=PurchaseStage.APPROVED)
        else:
            with self._d2_gate.attempt(
                run_id=run_id,
                actor_id=actor_id,
                expected_fingerprint=expected_fingerprint,
                checkout=checkout,
            ) as attempt:

                def authorize() -> str:
                    # Freshness first: D2 reads the `approve` check this request just recorded.
                    self._check_offer(run_id, checkout, "approve")
                    return attempt.authorize(self._approvals.present(checkout))

                # D2 writes the approval, the `approved` stage and its evidence in one transaction.
                record = self._approvals.approve(
                    run_id=run_id,
                    actor_id=actor_id,
                    expected_fingerprint=expected_fingerprint,
                    checkout=checkout,
                    checkout_jwt=checkout_jwt,
                    before_signing=authorize,
                    save=attempt.commit,
                )
        # AP2 consent: only once the approval exists (after the D2 commit when D2 runs).
        if self._ap2_evidence is not None:
            self._ap2_evidence.consent(record)
        return record

    def cancel(self, *, run_id: str) -> None:
        self._block_d1_continuation(run_id)
        purchase = self._require_purchase(run_id)
        if purchase.get("stage") == PurchaseStage.CANCELED:
            return
        if purchase.get("stage") != PurchaseStage.AWAITING_CONFIRMATION:
            raise PurchaseServiceError("Purchase cannot be canceled", code="invalid_stage")
        checkout = self._checkout_of(purchase)
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
    def _abandon_on_error(self, run_id: str, flow: PostCreateFlow | None = None) -> Iterator[None]:
        try:
            yield
        except BaseException as error:
            if flow is not None:
                if isinstance(error, PurchaseServiceError) and error.code == "budget_exceeded":
                    # Raised only after `cancel_budget` ended the journal `canceled`: nothing to
                    # mark uncertain, and the journal refuses any phase after it.
                    raise
                flow.fail()
                if isinstance(error, Exception):
                    raise PurchaseServiceError(
                        "Post-create operation requires local inspection",
                        code="d1_post_create_uncertain",
                    ) from None
                raise
            purchase = self._store.purchase(run_id)
            unfinished = {PurchaseStage.SELECTED, PurchaseStage.CHECKOUT_PREPARED}
            if purchase is not None and purchase.get("stage") in unfinished:
                # Merchant and service errors carry a code; any other failure stays generic.
                failure = getattr(error, "code", None)
                self._store.update_purchase(run_id, stage=PurchaseStage.FAILED, failure=failure)
            raise

    def _apply_details(
        self,
        run_id: str,
        checkout: CheckoutSession,
        details: CheckoutDetails,
        flow: PostCreateFlow | None = None,
    ) -> CheckoutSession:
        option = _selected_fulfillment(checkout)
        update = CheckoutUpdateRequest(
            buyer=details.buyer,
            fulfillment_details=details.fulfillment,
            selected_fulfillment_options=[option],
        )
        key = self._store.idempotency_key(run_id, "checkout.update.details")
        if flow is not None:

            def update_once() -> CheckoutSession:
                updated = self._checkout.update_checkout(checkout.id, update, idempotency_key=key)
                if updated.id != checkout.id:
                    raise PurchaseServiceError(
                        "Merchant changed checkout identity", code="checkout_mismatch"
                    )
                return updated

            flow.effect("update_details", canonical_bytes(update), key, update_once)

            def get_once() -> CheckoutSession:
                current = self._checkout.get_checkout(checkout.id)
                if current.id != checkout.id or current.status != "ready_for_payment":
                    raise PurchaseServiceError("Checkout is not ready", code="checkout_not_ready")
                return current

            return flow.effect("get_current", canonical_bytes(["GET", checkout.id]), None, get_once)
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
        self,
        run_id: str,
        checkout: CheckoutSession,
        intent: PurchaseIntent,
        flow: PostCreateFlow | None = None,
    ) -> None:
        maximum = intent.hard.max_total_amount_minor
        if maximum is None or checkout.total_minor <= maximum:
            return
        key = self._store.idempotency_key(run_id, "checkout.cancel.budget")
        if flow is not None:

            def cancel_once() -> CheckoutSession:
                canceled = self._checkout.cancel_checkout(
                    checkout.id,
                    idempotency_key=key,
                    reason_code="price_sensitivity",
                )
                if canceled.id != checkout.id or canceled.status != "canceled":
                    raise PurchaseServiceError(
                        "Cancellation is unverified", code="checkout_mismatch"
                    )
                return canceled

            flow.effect(
                "cancel_budget",
                canonical_bytes(["cancel", checkout.id, "price_sensitivity"]),
                key,
                cancel_once,
            )
            flow.finish(canceled=True)
        else:
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
        self._block_d1_continuation(run_id)
        return self._checkout_of(self._require_purchase(run_id))

    def _checkout_of(self, purchase: dict[str, str | None]) -> CheckoutSession:
        """Only after `_block_d1_continuation`, which each step runs once: it verifies journals."""
        checkout_id = purchase.get("checkout_id")
        if not isinstance(checkout_id, str):
            raise PurchaseServiceError("Purchase has no checkout", code="checkout_missing")
        return self._checkout.get_checkout(checkout_id)

    def _block_d1_continuation(self, run_id: str) -> None:
        if self._d1_gate is None and not self._store.has_d1_operation(run_id):
            return
        # Enforce refuses every later step unless the governed flow admits this purchase.
        if self._d1_continuation is None or not self._d1_continuation.admits(run_id):
            raise PurchaseServiceError(
                "D1 continuation requires independent authorization", code="d1_continuation_blocked"
            )

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
