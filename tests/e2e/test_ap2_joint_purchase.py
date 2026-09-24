import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from commerce_agent.acp import ACPClient, load_product_feed
from commerce_agent.acp.models import Address, Buyer, FulfillmentDetails
from commerce_agent.ap2 import AgentMandateSigner
from commerce_agent.application import CheckoutDetails, PurchaseService
from commerce_agent.approval import ApprovalService
from commerce_agent.completion import CompletionError, CompletionService
from commerce_agent.delegated_payment import DelegatedPaymentClient
from commerce_agent.domain import PurchaseIntent
from commerce_agent.persistence import SqliteAgentStore

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_JOINT_AP2_E2E") != "1",
    reason="requires the AP2 merchant, payment provider, and PostgreSQL",
)


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.fail(f"AP2 E2E environment is missing {name}")
    return value


def test_human_present_ap2_crosses_all_three_services() -> None:
    path = Path(_required("AGENT_DATABASE_PATH"))
    store = SqliteAgentStore(path)
    store.initialize()
    signer = AgentMandateSigner(
        _required("AGENT_AP2_PRIVATE_KEY_PEM"),
        merchant_id="tesis_merchant",
        merchant_name="Tesis Commerce Merchant",
    )
    approvals = ApprovalService(
        store,
        SecretStr(_required("AGENT_APPROVAL_SECRET")),
        merchant_origin=_required("MERCHANT_BASE_URL"),
        clock=lambda: datetime(2026, 9, 10, 14, 5, tzinfo=UTC),
        mandate_signer=signer,
    )
    details = CheckoutDetails(
        buyer=Buyer(email="buyer.ap2@example.test", first_name="Buyer", last_name="AP2"),
        fulfillment=FulfillmentDetails(
            name="Buyer AP2",
            email="buyer.ap2@example.test",
            phone_number="+573000000000",
            address=Address(
                name="Buyer AP2",
                line_one="Calle 100 # 10-20",
                city="Bogota",
                state="DC",
                country="CO",
                postal_code="110111",
            ),
        ),
    )
    with ACPClient(
        _required("MERCHANT_BASE_URL"), SecretStr(_required("MERCHANT_BEARER_TOKEN"))
    ) as gateway:
        purchases = PurchaseService(
            feed=load_product_feed(Path(_required("PRODUCT_FEED_PATH"))),
            checkout=gateway,
            store=store,
            approvals=approvals,
        )
        run_id = f"ap2-{_required('MERCHANT_RUN_ID')}"
        prepared = purchases.start(
            run_id=run_id,
            request="Quiero comprar estos audífonos",
            intent=PurchaseIntent(),
            selected_variant_id="SON-01",
            details=details,
        )
        assert prepared.checkout.status == "ready_for_payment"
        terms = purchases.approval_terms(run_id)
        assert terms.checkout_jwt
        payments = DelegatedPaymentClient(
            _required("PAYMENT_PROVIDER_URL"),
            SecretStr(_required("PAYMENT_AGENT_BEARER_TOKEN")),
        )
        try:
            completion = CompletionService(gateway, approvals, payments, store)
            with pytest.raises(CompletionError, match="not approved"):
                completion.complete(run_id=run_id)
            approved = purchases.approve(
                run_id=run_id,
                actor_id="human-ap2-e2e",
                expected_fingerprint=terms.fingerprint,
                checkout_jwt=terms.checkout_jwt,
            )
            assert approved.checkout_mandate and approved.payment_mandate
            result = completion.complete(run_id=run_id)
            assert result.status == "completed"
            assert result.order.status == "confirmed"
            assert completion.complete(run_id=run_id) == result
        finally:
            payments.close()

        with sqlite3.connect(path) as connection:
            receipts = connection.execute(
                "SELECT checkout_receipt_jwt, payment_receipt_jwt "
                "FROM completion_results WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        assert receipts is not None
        assert all(isinstance(receipt, str) and receipt for receipt in receipts)
        assert store.purchase(run_id)["stage"] == "completed"  # type: ignore[index]
