from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from commerce_agent.acp import ACPClient, load_product_feed
from commerce_agent.acp.models import Address, Buyer, FulfillmentDetails
from commerce_agent.application import CheckoutDetails, PurchaseService
from commerce_agent.approval import ApprovalService
from commerce_agent.completion import CompletionService
from commerce_agent.domain import PurchaseIntent
from commerce_agent.payment import SandboxPaymentProvider
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.webhooks import create_webhook_app
from commerce_agent.workflow import build_selection_workflow
from commerce_agent.workflow.selection import SelectionOutput

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_JOINT_E2E") != "1",
    reason="requires the pinned merchant service and PostgreSQL",
)


class _Interpreter:
    def interpret(self, request: str) -> PurchaseIntent:
        assert request
        return PurchaseIntent()


class _ForwardingHandler(BaseHTTPRequestHandler):
    client: TestClient

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        response = self.client.post(
            self.path,
            content=body,
            headers={name: value for name, value in self.headers.items()},
        )
        payload = response.content
        self.send_response(response.status_code)
        self.send_header("Content-Type", response.headers.get("content-type", "application/json"))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.fail(f"joint E2E environment is missing {name}")
    return value


def _merchant_command(*arguments: str) -> object:
    merchant = Path(_required("MERCHANT_REPO"))
    result = subprocess.run(
        ["uv", "run", "python", "-m", "commerce_lab.db", *arguments],
        cwd=merchant,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _details() -> CheckoutDetails:
    return CheckoutDetails(
        buyer=Buyer(email="buyer.e2e@example.test", first_name="Buyer", last_name="E2E"),
        fulfillment=FulfillmentDetails(
            name="Buyer E2E",
            email="buyer.e2e@example.test",
            phone_number="+573000000000",
            address=Address(
                name="Buyer E2E",
                line_one="Calle 100 # 10-20",
                city="Bogota",
                state="DC",
                country="CO",
                postal_code="110111",
            ),
        ),
    )


def test_complete_purchase_and_receive_order_webhooks() -> None:
    merchant_url = _required("MERCHANT_BASE_URL")
    store = SqliteAgentStore(Path(_required("AGENT_DATABASE_PATH")))
    store.initialize()
    approvals = ApprovalService(
        store,
        SecretStr(_required("AGENT_APPROVAL_SECRET")),
        clock=lambda: datetime(2026, 9, 10, 14, 5, tzinfo=UTC),
    )
    workflow = build_selection_workflow(
        feed=load_product_feed(Path(_required("PRODUCT_FEED_PATH"))),
        interpreter=_Interpreter(),
    )

    def select(request: str) -> SelectionOutput:
        return cast(SelectionOutput, workflow.invoke({"request": request}))

    app = create_webhook_app(
        store=store,
        secret=_required("MERCHANT_WEBHOOK_SECRET"),
    )
    with TestClient(app) as webhook_client:
        _ForwardingHandler.client = webhook_client
        server = ThreadingHTTPServer(
            ("127.0.0.1", int(_required("AGENT_WEBHOOK_PORT"))),
            _ForwardingHandler,
        )
        receiver = threading.Thread(target=server.serve_forever, daemon=True)
        receiver.start()
        try:
            with ACPClient(
                merchant_url,
                SecretStr(_required("MERCHANT_BEARER_TOKEN")),
                timeout_seconds=10,
            ) as gateway:
                discovery = gateway.discover()
                assert discovery.protocol.version == "2026-04-17"
                purchase = PurchaseService(
                    selection=select,
                    checkout=gateway,
                    store=store,
                    approvals=approvals,
                )
                run_id = f"buyer-{_required('MERCHANT_RUN_ID')}"
                prepared = purchase.start(
                    run_id=run_id,
                    request="Buy one available product",
                    details=_details(),
                )
                assert prepared.stage == "awaiting_confirmation"
                assert prepared.checkout.status == "ready_for_payment"

                terms = purchase.approval_terms(run_id)
                purchase.approve(
                    run_id=run_id,
                    actor_id="human-e2e",
                    expected_fingerprint=terms.fingerprint,
                )
                payment_provider = SandboxPaymentProvider(
                    SecretStr(_required("AGENT_PAYMENT_SECRET"))
                )
                completion = CompletionService(
                    gateway,
                    approvals,
                    payment_provider,
                    store,
                )
                completed = completion.complete(run_id=run_id)
                replayed = completion.complete(run_id=run_id)
                assert completed == replayed
                assert completed.status == "completed"
                assert completed.order.status == "confirmed"
                assert store.purchase(run_id)["stage"] == "completed"  # type: ignore[index]

                complete_key = store.idempotency_key(run_id, "checkout.complete")
                merchant_replay = gateway.complete_checkout(
                    completed.id,
                    payment_provider.payment_data(
                        run_id=run_id,
                        checkout_id=completed.id,
                        idempotency_key=complete_key,
                    ),
                    idempotency_key=complete_key,
                )
                assert merchant_replay == completed

                permalink = httpx.get(completed.order.permalink_url, timeout=5)
                assert permalink.status_code == 200
                for private_value in (
                    "buyer.e2e@example.test",
                    "+573000000000",
                    "Calle 100",
                    "spt_test_",
                ):
                    assert private_value not in permalink.text

            dispatched = _merchant_command("webhook-dispatch", "--limit", "10")
            assert isinstance(dispatched, list)
            assert dispatched and dispatched[0]["outcome"] == "delivered"
            projected = store.order(completed.order.id)
            assert projected is not None
            assert projected["data"]["status"] == "confirmed"

            updated = _merchant_command(
                "order-update",
                _required("MERCHANT_RUN_ID"),
                _required("MERCHANT_ACTOR_ID"),
                completed.order.id,
            )
            assert isinstance(updated, dict)
            assert updated["data"]["status"] == "processing"
            update_dispatch = _merchant_command("webhook-dispatch", "--limit", "10")
            assert isinstance(update_dispatch, list)
            assert update_dispatch and update_dispatch[0]["outcome"] == "delivered"
            projected = store.order(completed.order.id)
            assert projected is not None
            assert projected["data"]["status"] == "processing"
        finally:
            server.shutdown()
            server.server_close()
            receiver.join(timeout=5)
