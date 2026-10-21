"""AP2 Human Present purchases in process, from the turn to signed receipts. No model.

The merchant double (test_stale_terms_pending's) also completes over AP2 and signs a checkout
receipt; the payment provider double delegates a token and signs a payment receipt. Both sign the
way the real services do: `reference` is the `sd_hash` of the mandate each one received, so the
agent's own receipt checks run unchanged. Keys are generated per double.
"""

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
from ap2.sdk.sdjwt import common
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm

from commerce_agent.api import create_app
from commerce_agent.buyer_auth import LocalBuyerAuthenticator
from commerce_agent.config import Settings
from tests.governance.test_hp_continuation import router
from tests.governance.test_stale_terms_pending import MERCHANT_KEY_ID, Ap2Merchant, ap2, presented

BUYER = "buyer-1"
PAYMENT_ISSUER = "tesis_payment_provider"
PAYMENT_ID = "pay_ap2_1"
PAYMENT_TOKEN = "vt_" + "c" * 64


def _jwk(key: ec.EllipticCurvePrivateKey, kid: str) -> dict[str, Any]:
    public = json.loads(ECAlgorithm.to_jwk(key.public_key()))
    return {**public, "kid": kid, "use": "sig", "alg": "ES256"}


def sd_hash(mandate: str) -> str:
    return common.compute_sd_hash(common.parse_token(mandate))


class CompletingMerchant(Ap2Merchant):
    """Completes `POST /checkout_sessions/{id}/ap2/complete` with an order and a receipt."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        session_id, _, action = request.url.path.removeprefix("/checkout_sessions/").partition("/")
        if (request.method, action) != ("POST", "ap2/complete"):
            return super().__call__(request)
        self.requests.append((request.method, request.url.path))
        mandate = json.loads(request.content)["checkout_mandate"]
        order_id = f"ord_{session_id}"
        completed = {**deepcopy(self.sessions[session_id]), "status": "completed"}
        completed["order"] = {
            "type": "order",
            "id": order_id,
            "checkout_session_id": session_id,
            "permalink_url": f"{self.base_url}/orders/{order_id}",
            "status": "confirmed",
            "line_items": [],
            "totals": completed["totals"],
        }
        self.sessions[session_id] = completed
        claims = {
            "iss": self.base_url,
            "iat": int(time.time()),
            "status": "Success",
            "reference": sd_hash(mandate),
            "order_id": order_id,
        }
        # The merchant signs receipts with its AP2 key, the one its JWKS publishes.
        receipt = jwt.encode(claims, self._key, algorithm="ES256", headers={"kid": MERCHANT_KEY_ID})
        return httpx.Response(200, json={"checkout": completed, "checkout_receipt_jwt": receipt})


class Ap2PaymentProvider:
    """Delegates one token per checkout and signs the payment receipt for its mandate."""

    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.delegations: list[dict[str, Any]] = []
        self._mandates: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/ap2/jwks.json":
            return httpx.Response(200, json={"keys": [_jwk(self._key, "psp-ap2-1")]})
        body = json.loads(request.content)
        if path == "/agentic_commerce/delegate_payment":
            mandate = body["ap2_payment_mandate"]
            self._mandates[PAYMENT_TOKEN] = mandate
            self.delegations.append(
                {
                    "checkout_id": body["allowance"]["checkout_session_id"],
                    "max_amount": body["allowance"]["max_amount"],
                    "payment_mandate_hash": sd_hash(mandate),
                    "payment_id": PAYMENT_ID,
                }
            )
            return httpx.Response(201, json={"id": PAYMENT_TOKEN})
        if path == "/agentic_commerce/payment_receipt":
            claims = {
                "iss": PAYMENT_ISSUER,
                "iat": int(time.time()),
                "status": "Success",
                "reference": sd_hash(self._mandates[body["token"]]),
                "payment_id": PAYMENT_ID,
            }
            receipt = jwt.encode(claims, self._key, algorithm="ES256", headers={"kid": "psp-ap2-1"})
            result = {"id": PAYMENT_ID, "payment_receipt_jwt": receipt}
            return httpx.Response(200, json={"status": "approved", "result": result})
        raise AssertionError(f"Unexpected payment request {request.method} {path}")


@dataclass
class Ap2Shop:
    client: TestClient
    settings: Settings
    merchant: CompletingMerchant
    payments: Ap2PaymentProvider

    def buy(self, turn_id: str = "turn-ap2") -> str:
        """Turn, approval with the signed checkout and completion: the purchase's run_id."""
        run_id, terms = presented(self.client, turn_id)
        approval = self.client.post(
            f"/purchases/{run_id}/approval",
            json={
                "expected_fingerprint": terms["fingerprint"],
                "checkout_jwt": terms["checkout_jwt"],
            },
        )
        assert approval.status_code == 200, approval.text
        completed = self.client.post(f"/purchases/{run_id}/complete")
        assert completed.status_code == 200, completed.text
        return run_id


@contextmanager
def ap2_shop(database: Path, *, mode: str = "enforce", flow: str = "governed") -> Iterator[Ap2Shop]:
    settings = ap2(database, mode=mode, flow=flow)
    merchant = CompletingMerchant(settings.merchant_base_url)
    payments = Ap2PaymentProvider()
    app = create_app(
        settings=settings,
        transport=httpx.MockTransport(merchant),
        payment_transport=httpx.MockTransport(payments),
        conversation_router=router(),
        buyer_authenticator=LocalBuyerAuthenticator(BUYER),
    )
    with TestClient(app) as client:
        yield Ap2Shop(client, settings, merchant, payments)
