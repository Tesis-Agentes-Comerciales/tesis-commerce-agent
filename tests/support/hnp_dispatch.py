"""The PSP (HNP-P1) and the merchant (HNP-M1) behind the HNP dispatch, as HTTP doubles.

The dispatcher talks to them through the product's clients (`DelegatedPaymentClient`, `ACPClient`)
over `httpx.MockTransport`. Both replay a request under the same `Idempotency-Key`; the PSP answers
409 `idempotency_conflict` to the same key with another body, before verifying anything, as the
real one does. Each request takes the next scripted answer, `ok` once the script runs out.
`HnpMerchant` is the HP tests' AP2 merchant (`Ap2Merchant`) plus `/ap2/complete`, for the runtime
end to end.
"""

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
from pydantic import SecretStr

from commerce_agent.acp import ACPClient
from commerce_agent.application.hnp_dispatch import HnpDispatcher
from commerce_agent.delegated_payment import DelegatedPaymentClient
from tests.governance.test_stale_terms_pending import MERCHANT_KEY_ID, Ap2Merchant
from tests.support.ap2_hnp_fixtures import MERCHANT_ISSUER
from tests.support.ap2_receipts import PSP_ISSUER, Signer, chain_hash, psp_payment_id
from tests.support.hnp_executor import Bench
from tests.test_completion_client import completed_checkout

AP2_ERRORS = ("invalid_credential", "invalid_mandate", "unresolved_constraint")
ORDER_ID = "ord_1"
HNP_ORDER_ID = "ord_hnp_1"


def _next(script: list[str]) -> str:
    return script.pop(0) if script else "ok"


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("no answer", request=request)


class Psp:
    """`delegate_payment` and `payment_receipt`; `delegations` keeps (key, body) per request."""

    def __init__(self, *answers: str) -> None:
        self.signer = Signer(PSP_ISSUER)
        self.answers, self.receipt_answers = list(answers), list[str]()
        self.delegations: list[tuple[str, dict[str, Any]]] = []
        self.receipts: list[str] = []
        self.issued: list[str] = []  # the success receipts served
        self._tokens: dict[str, tuple[dict[str, Any], str]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/ap2/jwks.json":
            return httpx.Response(200, json=self.signer.jwks())
        body = json.loads(request.read())
        if path == "/agentic_commerce/payment_receipt":
            return self._receipt(request, body["token"])
        assert path == "/agentic_commerce/delegate_payment", path
        key = request.headers["Idempotency-Key"]
        self.delegations.append((key, body))
        answer = _next(self.answers)
        if key in self._tokens:  # idempotency answers before any verification
            kept, token = self._tokens[key]
            if kept != body:
                return httpx.Response(409, json={"code": "idempotency_conflict"})
            return httpx.Response(201, json={"id": token})
        chain = body["ap2_payment_mandate"]
        if answer == "ok":
            token = "vt_" + hashlib.sha256(key.encode()).hexdigest()
            self._tokens[key] = (body, token)
            return httpx.Response(201, json={"id": token})
        if answer == "timeout":
            return _timeout(request)
        if answer == "unavailable":
            return httpx.Response(503, json={"code": "mandates_not_supported"})
        if answer in ("idempotency_conflict", "merchant_mismatch"):
            return httpx.Response(
                409 if answer == "idempotency_conflict" else 422, json={"code": answer}
            )
        code, _, altered = answer.partition(":")
        receipt = self.signer.error(chain, code, payment_id=psp_payment_id(key))
        if altered == "wrong_reference":
            receipt = self.signer.error(
                body["ap2_checkout_mandate"], code, payment_id=psp_payment_id(key)
            )
        return httpx.Response(422, json={"code": code, "payment_receipt_jwt": receipt})

    def _receipt(self, request: httpx.Request, token: str) -> httpx.Response:
        self.receipts.append(token)
        answer = _next(self.receipt_answers)
        if answer == "timeout":
            return _timeout(request)
        [(key, body)] = [(k, b) for k, (b, t) in self._tokens.items() if t == token]
        payment_id = "pay_" + hashlib.sha256(key.encode()).hexdigest()[:24]
        chain = body["ap2_payment_mandate"]
        if answer == "wrong_reference":
            chain = body["ap2_checkout_mandate"]
        receipt = self.signer.success(
            chain,
            payment_id=payment_id,
            psp_confirmation_id="psp_1",
            network_confirmation_id="net_1",
        )
        self.issued.append(receipt)
        result = {"id": payment_id, "payment_receipt_jwt": receipt}
        return httpx.Response(200, json={"status": "approved", "result": result})

    def keys(self) -> set[str]:
        return {key for key, _ in self.delegations}


def acp_session(checkout_id: str, status: str) -> dict[str, Any]:
    session = completed_checkout()
    session.pop("order")
    return {**session, "id": checkout_id, "status": status}


class MerchantApi:
    """`/ap2/complete` (replayed by key), `cancel` and the JWKS of the receipts."""

    def __init__(self, *answers: str) -> None:
        self.signer = Signer.merchant(MERCHANT_ISSUER)
        self.answers = list(answers)
        self.completions: list[tuple[str, dict[str, Any]]] = []
        self.cancels: list[str] = []
        self.issued: list[str] = []  # the success receipts served
        self._replies: dict[str, httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/ap2/jwks.json":
            return httpx.Response(200, json=self.signer.jwks())
        checkout_id, _, action = path.removeprefix("/checkout_sessions/").partition("/")
        if action == "cancel":
            self.cancels.append(checkout_id)
            return httpx.Response(200, json=acp_session(checkout_id, "canceled"))
        assert action == "ap2/complete", path
        key, body = request.headers["Idempotency-Key"], json.loads(request.read())
        self.completions.append((key, body))
        answer = _next(self.answers)
        if key in self._replies and answer != "timeout":
            return self._replies[key]
        chain = body["checkout_mandate"]
        if answer == "timeout":
            return _timeout(request)
        if answer == "unavailable":
            return httpx.Response(503, json={"code": "mandates_not_supported"})
        if answer == "no_receipt":
            return httpx.Response(422, json={"code": "checkout_not_ready"})
        if answer in AP2_ERRORS:
            receipt = self.signer.error(chain, answer)
            return httpx.Response(422, json={"code": answer, "checkout_receipt_jwt": receipt})
        order = {
            **completed_checkout()["order"],
            "id": ORDER_ID,
            "checkout_session_id": checkout_id,
        }
        receipt = self.signer.success(chain, order_id=ORDER_ID)
        if answer == "wrong_reference":
            receipt = self.signer.success(chain, order_id=ORDER_ID, reference="sha256-of-another")
        self.issued.append(receipt)
        completed = {**acp_session(checkout_id, "completed"), "order": order}
        reply = httpx.Response(200, json={"checkout": completed, "checkout_receipt_jwt": receipt})
        self._replies[key] = reply
        return reply

    def keys(self) -> set[str]:
        return {key for key, _ in self.completions}


class HnpMerchant(Ap2Merchant):
    """The AP2 merchant double, completing a Human Not Present chain with a signed receipt."""

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self.completions: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.endswith("/ap2/complete"):
            return super().__call__(request)
        session_id = path.removeprefix("/checkout_sessions/").partition("/")[0]
        chain = json.loads(request.read())["checkout_mandate"]
        self.completions.append((request.headers["Idempotency-Key"], chain))
        order = {
            "type": "order",
            "id": HNP_ORDER_ID,
            "checkout_session_id": session_id,
            "permalink_url": f"{self.base_url}/orders/{HNP_ORDER_ID}",
            "status": "confirmed",
        }
        completed = {**self.sessions[session_id], "status": "completed", "order": order}
        claims = {
            "iss": self.base_url,
            "iat": int(time.time()),
            "status": "Success",
            "reference": chain_hash(chain),
            "order_id": HNP_ORDER_ID,
        }
        receipt = jwt.encode(claims, self._key, algorithm="ES256", headers={"kid": MERCHANT_KEY_ID})
        return httpx.Response(200, json={"checkout": completed, "checkout_receipt_jwt": receipt})


def dispatching(psp: Psp, merchant: MerchantApi) -> Callable[[Bench], HnpDispatcher]:
    """The `dispatcher` of a `Bench`: the product's clients over the two doubles."""

    def build(bench: Bench) -> HnpDispatcher:
        return HnpDispatcher(
            mode=bench.mode,
            ledger=bench.ledger,
            store=bench.store,
            payments=DelegatedPaymentClient(
                "http://127.0.0.1:4130",
                SecretStr("psp-bearer"),
                transport=httpx.MockTransport(psp),
            ),
            merchant=ACPClient(
                MERCHANT_ISSUER,
                SecretStr("merchant-bearer"),
                transport=httpx.MockTransport(merchant),
            ),
        )

    return build
