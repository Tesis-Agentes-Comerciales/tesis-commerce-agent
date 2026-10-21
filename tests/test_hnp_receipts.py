"""The PSP and merchant clients with Human Not Present chains (docs/ap2-profile.md, Recibos).

Both send the `open~~closed` chains, keep a rejection's status and its still unverified error
receipt, and verify a receipt only if its signature, schema, AP2 code and `reference` (the closed
segment of the chain sent) all match. The verifiers are HTTP doubles signing with test keys.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from commerce_agent.acp import ACPClient
from commerce_agent.acp.client import ACPClientError
from commerce_agent.acp.models import (
    CheckoutSession,
    PaymentCredential,
    SandboxPaymentData,
    SandboxPaymentInstrument,
)
from commerce_agent.ap2_hnp import closed_checkout_jwt, closed_issued_at
from commerce_agent.ap2_receipt import chain_reference
from commerce_agent.delegated_payment import DelegatedPaymentClient, DelegationError
from tests.support.ap2_hnp_fixtures import MERCHANT_ISSUER
from tests.support.ap2_receipts import (
    PSP_ISSUER,
    Chains,
    Signer,
    chain_hash,
    chains,
    psp_payment_id,
)
from tests.test_completion_client import completed_checkout

CLOSED_AT = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
TOKEN = "vt_" + "a" * 64
Handler = Callable[[httpx.Request], httpx.Response]


def ready() -> CheckoutSession:
    data = completed_checkout()
    data.pop("order")
    return CheckoutSession.model_validate({**data, "status": "ready_for_payment"})


@pytest.fixture(scope="module")
def chain() -> Chains:
    return chains(ready(), closed_at=CLOSED_AT)


def payments(handler: Handler) -> DelegatedPaymentClient:
    transport = httpx.MockTransport(handler)
    return DelegatedPaymentClient(
        "http://127.0.0.1:4130", SecretStr("agent-payment-bearer"), transport=transport
    )


def merchant(handler: Handler) -> ACPClient:
    return ACPClient(MERCHANT_ISSUER, SecretStr("bearer"), transport=httpx.MockTransport(handler))


def psp_answering(
    signer: Signer, path: str, response: httpx.Response
) -> tuple[Handler, list[dict[str, Any]]]:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/ap2/jwks.json":
            return httpx.Response(200, json=signer.jwks())
        assert request.url.path == path
        bodies.append(json.loads(request.read()))
        return response

    return handler, bodies


def delegate(client: DelegatedPaymentClient, chain: Chains) -> SandboxPaymentData:
    return client.hnp_payment_data(
        checkout=ready(),
        idempotency_key="op_1:payment",
        checkout_jwt=chain.checkout_jwt,
        payment_mandate=chain.closed.payment.token,
        checkout_mandate=chain.closed.checkout.token,
    )


# The PSP


def test_an_hnp_delegation_sends_both_chains_and_the_checkout_jwt(chain: Chains) -> None:
    created = httpx.Response(201, json={"id": TOKEN})
    handler, bodies = psp_answering(
        Signer(PSP_ISSUER), "/agentic_commerce/delegate_payment", created
    )

    result = delegate(payments(handler), chain)

    assert result.instrument.credential.token.get_secret_value() == TOKEN
    [body] = bodies
    assert body["ap2_checkout_jwt"] == chain.checkout_jwt
    assert body["ap2_payment_mandate"] == chain.closed.payment.token
    assert body["ap2_checkout_mandate"] == chain.closed.checkout.token


@pytest.mark.parametrize("missing", ["checkout_jwt", "payment_mandate", "checkout_mandate"])
def test_an_hnp_delegation_never_travels_without_all_three(chain: Chains, missing: str) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Nothing is sent")

    fields = {
        "checkout_jwt": chain.checkout_jwt,
        "payment_mandate": chain.closed.payment.token,
        "checkout_mandate": chain.closed.checkout.token,
    }
    with pytest.raises(DelegationError, match="invalid_ap2_mandates"):
        payments(unreachable).hnp_payment_data(
            checkout=ready(), idempotency_key="op_1:payment", **(fields | {missing: ""})
        )


def test_a_human_present_delegation_carries_no_checkout_chain(chain: Chains) -> None:
    created = httpx.Response(201, json={"id": TOKEN})
    handler, bodies = psp_answering(
        Signer(PSP_ISSUER), "/agentic_commerce/delegate_payment", created
    )

    payments(handler).payment_data(
        checkout=ready(),
        idempotency_key="run_1:payment",
        checkout_jwt=chain.checkout_jwt,
        payment_mandate=chain.closed.payment.token,
    )

    [body] = bodies
    assert "ap2_checkout_mandate" not in body
    assert body["ap2_payment_mandate"] == chain.closed.payment.token


@pytest.mark.parametrize(
    ("response", "code", "status", "with_receipt"),
    [
        (httpx.Response(422, json={"code": "invalid_mandate"}), "delegation_rejected", 422, True),
        (
            httpx.Response(409, json={"code": "idempotency_conflict"}),
            "delegation_rejected",
            409,
            False,
        ),
        (
            httpx.Response(422, json={"code": "merchant_mismatch"}),
            "delegation_rejected",
            422,
            False,
        ),
        (
            httpx.Response(503, json={"code": "mandates_not_supported"}),
            "provider_unavailable",
            503,
            False,
        ),
    ],
)
def test_a_rejection_keeps_its_status_and_its_unverified_receipt(
    chain: Chains, response: httpx.Response, code: str, status: int, with_receipt: bool
) -> None:
    receipt = Signer(PSP_ISSUER).error(chain.closed.payment.token, "invalid_mandate")
    if with_receipt:
        response = httpx.Response(
            response.status_code, json={**response.json(), "payment_receipt_jwt": receipt}
        )
    handler, _ = psp_answering(Signer(PSP_ISSUER), "/agentic_commerce/delegate_payment", response)

    with pytest.raises(DelegationError) as raised:
        delegate(payments(handler), chain)

    assert (raised.value.code, raised.value.status_code) == (code, status)
    assert raised.value.receipt == (receipt if with_receipt else None)


def test_no_answer_is_a_provider_without_status(chain: Chains) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(DelegationError) as raised:
        delegate(payments(timeout), chain)

    assert (raised.value.code, raised.value.status_code, raised.value.receipt) == (
        "provider_unavailable",
        None,
        None,
    )


def psp_error(chain: Chains, signer: Signer, code: str = "invalid_mandate", **claims: Any) -> str:
    payment_id = psp_payment_id("op_1:payment")
    return signer.error(chain.closed.payment.token, code, payment_id=payment_id, **claims)


def test_a_psp_error_receipt_verifies_against_the_payment_chain(chain: Chains) -> None:
    signer = Signer(PSP_ISSUER)
    handler, _ = psp_answering(signer, "/unused", httpx.Response(500))

    claims = payments(handler).verify_error_receipt(
        psp_error(chain, signer), mandate=chain.closed.payment.token
    )

    assert (claims["status"], claims["error"]) == ("Error", "invalid_mandate")
    assert claims["payment_id"] == psp_payment_id("op_1:payment")


@pytest.mark.parametrize(
    "receipt",
    [
        pytest.param(lambda c, s: psp_error(c, s, reference="another-chain"), id="reference"),
        pytest.param(lambda c, s: psp_error(c, s, status="Success"), id="success-status"),
        pytest.param(lambda c, s: psp_error(c, s, "card_declined"), id="not-an-ap2-code"),
        pytest.param(lambda c, s: psp_error(c, s, iss="another_psp"), id="issuer"),
        pytest.param(
            lambda c, s: s.error(c.closed.payment.token, "invalid_mandate"), id="no-payment-id"
        ),
        pytest.param(lambda c, s: psp_error(c, Signer(PSP_ISSUER)), id="another-key"),
    ],
)
def test_a_psp_error_receipt_that_does_not_match_is_invalid(
    chain: Chains, receipt: Callable[[Chains, Signer], str]
) -> None:
    signer = Signer(PSP_ISSUER)
    handler, _ = psp_answering(signer, "/unused", httpx.Response(500))

    with pytest.raises(DelegationError, match="invalid_error_receipt"):
        payments(handler).verify_error_receipt(
            receipt(chain, signer), mandate=chain.closed.payment.token
        )


def test_the_payment_receipt_references_the_closed_segment_of_the_chain(chain: Chains) -> None:
    signer = Signer(PSP_ISSUER)
    good = signer.success(chain.closed.payment.token, payment_id="pay_1")
    stale = signer.success(chain.opens.payment.token, payment_id="pay_1")
    for receipt, accepted in ((good, True), (stale, False)):
        approved = {"status": "approved", "result": {"id": "pay_1", "payment_receipt_jwt": receipt}}
        handler, bodies = psp_answering(
            signer, "/agentic_commerce/payment_receipt", httpx.Response(200, json=approved)
        )
        client = payments(handler)
        if accepted:
            assert client.payment_receipt(token=TOKEN, mandate=chain.closed.payment.token) == good
        else:
            with pytest.raises(DelegationError, match="invalid_payment_receipt"):
                client.payment_receipt(token=TOKEN, mandate=chain.closed.payment.token)
        assert bodies == [{"token": TOKEN}]


def test_a_failed_payment_receipt_keeps_its_code_and_tells_whether_the_psp_answered(
    chain: Chains,
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    answered, _ = psp_answering(
        Signer(PSP_ISSUER), "/agentic_commerce/payment_receipt", httpx.Response(502)
    )
    for handler, status in ((answered, 502), (unreachable, None)):
        with pytest.raises(DelegationError) as raised:
            payments(handler).payment_receipt(token=TOKEN, mandate=chain.closed.payment.token)

        # Human Present reads the code; the dispatch tells a timeout from a mismatch by the status.
        assert (raised.value.code, raised.value.status_code) == ("invalid_payment_receipt", status)


# The merchant

PAYMENT = SandboxPaymentData(
    instrument=SandboxPaymentInstrument(credential=PaymentCredential(token=SecretStr(TOKEN)))
)
COMPLETE = "/checkout_sessions/cs_test_001/ap2/complete"


def merchant_answering(signer: Signer, response: httpx.Response) -> tuple[Handler, list[Any]]:
    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/ap2/jwks.json":
            return httpx.Response(200, json=signer.jwks())
        assert request.url.path == COMPLETE
        assert request.headers["Idempotency-Key"] == "op_1:complete"
        bodies.append(json.loads(request.read()))
        return response

    return handler, bodies


def complete(client: ACPClient, chain: Chains) -> tuple[Any, str]:
    return client.complete_checkout_ap2(
        "cs_test_001", PAYMENT, chain.closed.checkout.token, idempotency_key="op_1:complete"
    )


def test_an_hnp_completion_sends_the_chain_and_verifies_its_receipt(chain: Chains) -> None:
    signer = Signer.merchant()
    receipt = signer.success(chain.closed.checkout.token, order_id="ord_test_001")
    body = {"checkout": completed_checkout(), "checkout_receipt_jwt": receipt}
    handler, bodies = merchant_answering(signer, httpx.Response(200, json=body))

    completed, returned = complete(merchant(handler), chain)

    assert (completed.order.id, returned) == ("ord_test_001", receipt)
    assert bodies[0]["checkout_mandate"] == chain.closed.checkout.token


@pytest.mark.parametrize(
    "mandate", ["opens", "order"], ids=["open-segment-reference", "another-order"]
)
def test_an_hnp_completion_receipt_that_does_not_match_fails(chain: Chains, mandate: str) -> None:
    signer = Signer.merchant()
    receipt = (
        signer.success(chain.opens.checkout.token, order_id="ord_test_001")
        if mandate == "opens"
        else signer.success(chain.closed.checkout.token, order_id="ord_other")
    )
    body = {"checkout": completed_checkout(), "checkout_receipt_jwt": receipt}
    handler, _ = merchant_answering(signer, httpx.Response(200, json=body))

    with pytest.raises(ACPClientError, match="receipt is invalid") as raised:
        complete(merchant(handler), chain)

    assert raised.value.status_code is None


def test_a_merchant_rejection_carries_its_receipt_for_the_caller_to_verify(
    chain: Chains,
) -> None:
    signer = Signer.merchant()
    receipt = signer.error(chain.closed.checkout.token, "unresolved_constraint")
    rejected = {"code": "unresolved_constraint", "checkout_receipt_jwt": receipt}
    handler, _ = merchant_answering(signer, httpx.Response(422, json=rejected))
    client = merchant(handler)

    with pytest.raises(ACPClientError) as raised:
        complete(client, chain)
    claims = client.verify_checkout_error_receipt(receipt, mandate=chain.closed.checkout.token)

    assert (raised.value.code, raised.value.status_code) == ("unresolved_constraint", 422)
    assert raised.value.receipt == receipt
    assert (claims["status"], claims["error"], claims["iss"]) == (
        "Error",
        "unresolved_constraint",
        MERCHANT_ISSUER,
    )


@pytest.mark.parametrize(
    "receipt",
    [
        pytest.param(
            lambda chain, signer: signer.error(chain.closed.payment.token, "invalid_mandate"),
            id="the-payment-chain",
        ),
        pytest.param(
            lambda chain, signer: signer.success(chain.closed.checkout.token, order_id="o"),
            id="success",
        ),
        pytest.param(
            lambda chain, signer: signer.error(chain.closed.checkout.token, "out_of_stock"),
            id="not-an-ap2-code",
        ),
        pytest.param(
            lambda chain, signer: Signer.merchant().error(
                chain.closed.checkout.token, "invalid_mandate"
            ),
            id="another-key",
        ),
    ],
)
def test_a_merchant_error_receipt_that_does_not_match_is_invalid(
    chain: Chains, receipt: Callable[[Chains, Signer], str]
) -> None:
    signer = Signer.merchant()
    handler, _ = merchant_answering(signer, httpx.Response(500))

    with pytest.raises(ACPClientError, match="error receipt is invalid"):
        merchant(handler).verify_checkout_error_receipt(
            receipt(chain, signer), mandate=chain.closed.checkout.token
        )


def test_a_merchant_timeout_has_no_status_and_no_receipt(chain: Chains) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ACPClientError) as raised:
        complete(merchant(timeout), chain)

    assert (raised.value.code, raised.value.status_code, raised.value.receipt) == (
        "timeout",
        None,
        None,
    )


# The closed chain itself


def test_a_receipt_references_the_link_hash_the_ledger_keeps(chain: Chains) -> None:
    # The doubles sign with `chain_hash` and the clients verify with `chain_reference`, one formula
    # twice; the closer's `link_hash`, which the ledger keeps and V1 compares, anchors both.
    for closed in (chain.closed.checkout, chain.closed.payment):
        assert chain_reference(closed.token) == chain_hash(closed.token) == closed.link_hash


def test_a_closed_chain_names_its_checkout_jwt_and_when_it_was_closed(chain: Chains) -> None:
    assert closed_checkout_jwt(chain.closed.checkout.token) == chain.checkout_jwt
    assert closed_issued_at(chain.closed.payment.token) == int(CLOSED_AT.timestamp())
    assert closed_checkout_jwt(chain.opens.checkout.token) == ""
    with pytest.raises(ValueError):
        closed_issued_at(chain.opens.payment.token)
