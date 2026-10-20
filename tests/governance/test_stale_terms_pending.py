"""A purchase whose live offer changed stays visible and cancelable (b2-governed-pilot, S04).

AP2 re-signs the terms on every `GET /me/pending-purchase`, so the governed freshness check
runs again there. When the offer changed or cannot be read, the endpoint reports why the terms
are missing instead of failing; cancel never reads the offer, and the buyer then asks again for
a new checkout at the current price, which goes through D1 again. A mock Merchant, no model.
"""

import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm
from pydantic import SecretStr

from commerce_agent.api import create_app
from commerce_agent.buyer_auth import LocalBuyerAuthenticator
from commerce_agent.config import Settings
from tests.governance.test_enforce_runtime import decisions
from tests.governance.test_hp_continuation import DISCOVERY, PAYMENT_TOKEN, ask, router
from tests.governance.test_runtime_dispatch import CREATE, LIVE_OFFER, OFFER_READ, acp_checkout
from tests.governance.test_shadow_integration import configuration

MERCHANT_KEY_ID = "merchant-ap2-1"


def private_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


class Ap2Merchant:
    """ACP double with AP2 checkout JWTs and one session per creation.

    Each session is priced at the live offer when it is created and keeps that snapshot, as the
    merchant does; the offer itself is mutable, so a test can reprice it or make it unreadable.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.offer: dict[str, Any] = deepcopy(LIVE_OFFER)
        self.sessions: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self._key = ec.generate_private_key(ec.SECP256R1())
        public = json.loads(ECAlgorithm.to_jwk(self._key.public_key()))
        self._jwk = {**public, "kid": MERCHANT_KEY_ID, "use": "sig", "alg": "ES256"}

    def reprice(self, items_total_minor: int) -> None:
        pricing = self.offer["pricing"]
        self.offer = {
            **self.offer,
            "revision": self.offer["revision"] + 1,
            "pricing": {
                **pricing,
                "items_total_minor": items_total_minor,
                "total_minor": items_total_minor + pricing["shipping_total_minor"],
            },
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        if path == "/.well-known/acp.json":
            return httpx.Response(200, json=DISCOVERY)
        if path == "/.well-known/ap2/jwks.json":
            return httpx.Response(200, json={"keys": [self._jwk]})
        if (method, path) == OFFER_READ:
            return httpx.Response(200, json={"ok": True, "data": self.offer})
        if (method, path) == CREATE:
            created = self._priced(f"cs_api_{len(self.sessions) + 1}")
            self.sessions[created["id"]] = created
            return httpx.Response(201, json=created)
        session_id, _, action = path.removeprefix("/checkout_sessions/").partition("/")
        session = self.sessions[session_id]
        if (method, action) == ("POST", ""):
            ready = acp_checkout("ready_for_payment", with_details=True)
            details = ("buyer", "fulfillment_details", "selected_fulfillment_options")
            session = {**session, "status": "ready_for_payment"}
            session.update({name: ready[name] for name in details})
        elif (method, action) == ("POST", "cancel"):
            session = {**session, "status": "canceled"}
        elif (method, action) == ("GET", "ap2/checkout-jwt"):
            return httpx.Response(200, json={"checkout_jwt": self._signed(session)})
        elif (method, action) != ("GET", ""):
            raise AssertionError(f"Unexpected request: {method} {path}")
        self.sessions[session_id] = session
        return httpx.Response(200, json=session)

    def _priced(self, session_id: str) -> dict[str, Any]:
        """test_runtime_dispatch's checkout at the live offer's price."""
        pricing = self.offer["pricing"]
        items, total = pricing["items_total_minor"], pricing["total_minor"]
        created = acp_checkout("not_ready_for_payment", with_details=False)
        created["id"] = session_id
        [line] = created["line_items"]
        line["item"]["unit_amount"] = items
        line["totals"] = [{"type": "total", "display_text": "Item", "amount": items}]
        amounts = {"subtotal": items, "fulfillment": pricing["shipping_total_minor"]}
        for row in created["totals"]:
            row["amount"] = amounts.get(row["type"], total)
        return created

    def _signed(self, session: dict[str, Any]) -> str:
        now = int(time.time())
        claims = {
            "iss": self.base_url,
            "aud": "ap2-shopping-agent",
            "iat": now,
            "exp": now + 600,
            "jti": f"jti-{len(self.requests)}",
            "checkout": session,
        }
        return jwt.encode(claims, self._key, algorithm="ES256", headers={"kid": MERCHANT_KEY_ID})

    def creations(self) -> int:
        return self.requests.count(CREATE)

    def effects(self) -> list[tuple[str, str]]:
        """Commercial writes after creation: buyer cancel and completion."""
        return [call for call in self.requests if call[1].endswith(("/cancel", "/complete"))]


def ap2(path: Path, *, mode: str = "enforce", flow: str = "governed") -> Settings:
    """AP2 Human Present: authenticated buyers and a mandate signer, so terms are re-signed."""
    return configuration(
        path,
        mode=mode,
        agent_hp_flow=flow,
        agent_auth_mode="jwt",
        agent_auth_issuer="http://127.0.0.1:3000",
        agent_ap2_required=True,
        agent_ap2_private_key_pem=SecretStr(private_pem()),
    )


def app_client(settings: Settings, merchant: Ap2Merchant) -> TestClient:
    return TestClient(
        create_app(
            settings=settings,
            transport=httpx.MockTransport(merchant),
            payment_transport=httpx.MockTransport(
                lambda request: httpx.Response(201, json=PAYMENT_TOKEN)
            ),
            conversation_router=router(),
            buyer_authenticator=LocalBuyerAuthenticator("buyer-1"),
        )
    )


def presented(client: TestClient, turn_id: str) -> tuple[str, dict[str, Any]]:
    turn = ask(client, turn_id)
    assert turn.status_code == 200, turn.text
    assert turn.json()["approval_terms"]["checkout_jwt"]
    return turn.json()["purchase"]["run_id"], turn.json()["approval_terms"]


def pending(client: TestClient) -> dict[str, Any] | None:
    response = client.get("/me/pending-purchase")
    assert response.status_code == 200, response.text
    return response.json()


def awaiting(run_id: str, checkout_id: str = "cs_api_1") -> dict[str, Any]:
    return {
        "run_id": run_id,
        "stage": "awaiting_confirmation",
        "checkout_id": checkout_id,
        "order_id": None,
    }


def test_s04_changed_terms_stay_visible_cancelable_and_a_new_checkout_follows(
    tmp_path: Path,
) -> None:
    settings = ap2(tmp_path / "agent.db")
    merchant = Ap2Merchant(settings.merchant_base_url)
    with app_client(settings, merchant) as client:
        run_id, terms = presented(client, "turn-s04")
        assert terms["total_minor"] == 74_000
        # The merchant reprices the offer after the terms were presented (S04 stimulus).
        merchant.reprice(80_000)
        approval = client.post(
            f"/purchases/{run_id}/approval",
            json={
                "expected_fingerprint": terms["fingerprint"],
                "checkout_jwt": terms["checkout_jwt"],
            },
        )
        assert approval.status_code == 409, approval.text
        assert approval.json()["code"] == "checkout_terms_changed"
        # The stuck purchase is still the buyer's pending one, without approvable terms.
        assert pending(client) == {
            "purchase": awaiting(run_id),
            "approval_terms": None,
            "terms_status": "changed",
        }
        canceled = client.post(f"/purchases/{run_id}/cancel")
        assert canceled.status_code == 200, canceled.text
        assert canceled.json()["stage"] == "canceled"
        assert pending(client) is None
        # Asking again yields a new checkout at the current price, through D1 again.
        new_run_id, new_terms = presented(client, "turn-s04-again")
        assert new_run_id != run_id
        assert (new_terms["checkout_id"], new_terms["total_minor"]) == ("cs_api_2", 82_000)
        current = pending(client)
        assert current is not None and current["terms_status"] is None
        assert current["purchase"] == awaiting(new_run_id, "cs_api_2")
        assert current["approval_terms"]["fingerprint"] == new_terms["fingerprint"]
    assert merchant.creations() == 2
    assert merchant.effects() == [("POST", "/checkout_sessions/cs_api_1/cancel")]
    assert merchant.sessions["cs_api_1"]["status"] == "canceled"
    assert [record.decision.outcome for record in decisions(settings.agent_database_path)] == [
        "allow",
        "allow",
    ]


def test_an_unreadable_offer_keeps_the_purchase_visible_and_cancelable(tmp_path: Path) -> None:
    settings = ap2(tmp_path / "agent.db")
    merchant = Ap2Merchant(settings.merchant_base_url)
    with app_client(settings, merchant) as client:
        run_id, terms = presented(client, "turn-unreadable")
        fresh = pending(client)
        assert fresh is not None and fresh["terms_status"] is None
        assert fresh["approval_terms"]["fingerprint"] == terms["fingerprint"]
        merchant.offer = {"unexpected": "body"}
        assert pending(client) == {
            "purchase": awaiting(run_id),
            "approval_terms": None,
            "terms_status": "unavailable",
        }
        canceled = client.post(f"/purchases/{run_id}/cancel")
        assert canceled.status_code == 200, canceled.text
        assert canceled.json()["stage"] == "canceled"
        assert pending(client) is None
    assert merchant.effects() == [("POST", "/checkout_sessions/cs_api_1/cancel")]


def test_off_mode_never_reads_the_offer_for_the_pending_purchase(tmp_path: Path) -> None:
    settings = ap2(tmp_path / "agent.db", mode="off")
    merchant = Ap2Merchant(settings.merchant_base_url)
    with app_client(settings, merchant) as client:
        run_id, terms = presented(client, "turn-off")
        merchant.reprice(80_000)
        restored = pending(client)
        assert restored is not None
        assert restored["purchase"] == awaiting(run_id)
        assert restored["terms_status"] is None
        assert restored["approval_terms"]["fingerprint"] == terms["fingerprint"]
    assert OFFER_READ not in merchant.requests


def test_blocked_flow_still_refuses_a_governed_pending_purchase(tmp_path: Path) -> None:
    settings = ap2(tmp_path / "agent.db")
    merchant = Ap2Merchant(settings.merchant_base_url)
    with app_client(settings, merchant) as client:
        presented(client, "turn-flow-change")
        merchant.reprice(80_000)
    blocked = settings.model_copy(update={"agent_hp_flow": "blocked"})
    with app_client(blocked, merchant) as client:
        # Only freshness becomes a terms status; the D1 refusal keeps today's response.
        refused = client.get("/me/pending-purchase")
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "d1_continuation_blocked"
    assert merchant.effects() == []


def test_blocked_flow_reports_its_reservation_without_a_terms_status(tmp_path: Path) -> None:
    settings = ap2(tmp_path / "agent.db", flow="blocked")
    merchant = Ap2Merchant(settings.merchant_base_url)
    with app_client(settings, merchant) as client:
        turn = ask(client, "turn-blocked")
        assert turn.status_code == 409 and turn.json()["code"] == "d1_dispatch_blocked"
        reserved = pending(client)
        assert reserved is not None
        assert reserved["purchase"]["stage"] == "selected"
        assert (reserved["approval_terms"], reserved["terms_status"]) == (None, None)
    assert merchant.requests == [("GET", "/.well-known/acp.json")]
