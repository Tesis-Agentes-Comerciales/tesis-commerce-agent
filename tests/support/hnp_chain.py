"""Agent databases of real HNP runs, for `verify-evidence` (docs/design/hnp-governed.md §9.2).

The delegation service, the executor, D1/D2/D3-HNP, the `close` freshness guard, the open
mandate issuer and the Agent Key are the product's, over one real SQLite file, wired as
tests/governance/test_hnp_gate.py wires them. Only the merchant (httpx.MockTransport), its AP2
checkout signature (a key generated for the test) and the authority clock are doubles.

The executor stops at UoW-2 (HNP-09b dispatches). `dispatch_with_order`, `reject_by_psp` and
`reject_by_merchant` drive the ledger's own dispatch steps as tests/support/delegation.py does, with AP2 receipts whose
claims bind the closed mandates; `verify-evidence` reads their claims, never their signature.
`record_results` writes the `completion_results` row that HNP-09b's UoW-3 is to write.
"""

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import jwt
from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.feed import load_product_feed
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.ap2_hnp import AgentKeyCloser, OpenMandateIssuer
from commerce_agent.application import PurchaseService
from commerce_agent.application.hnp_execution import (
    HnpExecutionService,
    NullHnpPolicyGate,
    sqlite_d1_denials,
)
from commerce_agent.application.hnp_selection import DeterministicHnpSelector
from commerce_agent.application.offer_freshness import build_offer_freshness
from commerce_agent.approval import ApprovalService
from commerce_agent.config import Settings
from commerce_agent.delegation.models import Receipt, Step
from commerce_agent.delegation.service import (
    DelegationConsentRequest,
    DelegationProposalRequest,
    DelegationService,
    HnpTrust,
)
from commerce_agent.governance.bootstrap import build_hnp_policy_gate
from commerce_agent.governance.hnp_ports import HnpGateDecision, HnpPolicyGate
from commerce_agent.governance.provenance import ProvenanceKeys
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger
from tests.governance.test_d1_hnp_gate import hnp_service, settings_for
from tests.governance.test_hp_continuation import StoreMerchant
from tests.governance.test_runtime_dispatch import runtime
from tests.governance.test_shadow_integration import FIXTURE, OWNER, configuration
from tests.support.ap2_hnp_fixtures import checkout_jwt, generate_keys
from tests.support.delegation import AuthorityClock, Ids
from tests.test_purchase_service import details

Mode = Literal["off", "enforce"]
ORDER_ID = "ord_api_1"
PSP = "tesis_payment_provider"


def receipt_jwt(**claims: Any) -> str:
    """An AP2 receipt; its issuer's key is a test one, because only its claims are read."""
    return jwt.encode({"iat": 1_792_000_000, **claims}, "x" * 32, algorithm="HS256")


class SignedClient:
    """The runtime's ACP client, with the merchant's AP2 Checkout JWT signed by a test key."""

    def __init__(self, client: Any, merchant_pem: str) -> None:
        self._client, self._pem = client, merchant_pem

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def signed_checkout(self, checkout: CheckoutSession) -> str:
        return checkout_jwt(self._pem, checkout.model_dump(mode="json"), jti=f"jti_{checkout.id}")

    def verify_signed_checkout(self, token: str, checkout: CheckoutSession) -> None:
        signed = jwt.decode(token, options={"verify_signature": False})["checkout"]
        assert signed == checkout.model_dump(mode="json")


class Before:
    """A D2/D3 gate that first runs what a test injects (a revocation, the kill switch)."""

    def __init__(self, gate: HnpPolicyGate) -> None:
        self._gate = gate
        self.hooks: dict[str, Callable[[], object]] = {}

    def decide_d2(self, **request: Any) -> HnpGateDecision:
        if "D2" in self.hooks:
            self.hooks["D2"]()
        return self._gate.decide_d2(**request)

    def decide_d3(self, **request: Any) -> HnpGateDecision:
        if "D3" in self.hooks:
            self.hooks["D3"]()
        return self._gate.decide_d3(**request)


@dataclass
class HnpChain:
    path: Path
    settings: Settings
    clock: AuthorityClock
    ledger: SqliteDelegationLedger
    delegations: DelegationService
    store: SqliteAgentStore
    executor: HnpExecutionService
    gate: Before | None
    grant_id: str
    task_id: str
    receipts: dict[str, str] = field(default_factory=dict)

    def run(self) -> Step:
        return self.executor.run(self.task_id)

    def revoke(self) -> None:
        self.delegations.revoke(OWNER, self.grant_id)

    def kill_switch(self) -> None:
        self.delegations.set_kill_switch(True)

    def lease(self) -> str:
        execution = self.ledger.execution(self.task_id)
        assert execution is not None and execution.task.lease_id is not None
        return execution.task.lease_id

    def closed_links(self) -> dict[str, str]:
        return {
            mandate.kind: mandate.link_hash
            for mandate in self.ledger.mandates(self.grant_id)
            if mandate.reservation_id is not None
        }

    def dispatch_with_order(self, status: str = "Success") -> None:
        """The outbox steps to `completed` and UoW-3, as the ledger records them. `status` is the
        form the receipts reach the ledger in (the SDK's `Success` or the spec's `success`); their
        JWTs keep the SDK's."""
        lease = self.lease()
        for step in ("payment_sent", "payment_accepted", "complete_sent"):
            assert self.ledger.advance_dispatch(self.task_id, lease, step).status == "applied"
        completed = self.ledger.advance_dispatch(
            self.task_id, lease, "completed", order_id=ORDER_ID
        )
        assert completed.status == "applied"
        links, merchant = self.closed_links(), self.settings.merchant_base_url
        self.receipts = {
            "payment": receipt_jwt(
                status="Success",
                iss=PSP,
                reference=links["closed_payment"],
                payment_id="pay_1",
                psp_confirmation_id="psp_1",
                network_confirmation_id="net_1",
            ),
            "checkout": receipt_jwt(
                status="Success",
                iss=merchant,
                reference=links["closed_checkout"],
                order_id=ORDER_ID,
            ),
        }
        payment = Receipt(
            "payment",
            self.receipts["payment"],
            PSP,
            status,
            links["closed_payment"],
            payment_id="pay_1",
        )
        checkout = Receipt(
            "checkout",
            self.receipts["checkout"],
            merchant,
            status,
            links["closed_checkout"],
            order_id=ORDER_ID,
        )
        step = self.ledger.record_completion(self.task_id, lease, payment, checkout)
        assert step.status == "closed"

    def record_results(self) -> None:
        """Both receipt JWTs in `completion_results`, as HNP-09b's UoW-3 is to keep them."""
        execution = self.ledger.execution(self.task_id)
        assert execution is not None and execution.outbox is not None
        outbox = execution.outbox
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "INSERT INTO completion_results (run_id, checkout_id, order_id, snapshot, "
                "completed_at, checkout_receipt_jwt, payment_receipt_jwt) "
                "VALUES (?, ?, ?, '{}', ?, ?, ?)",
                (
                    outbox.run_id,
                    outbox.checkout_id,
                    outbox.order_id,
                    self.clock().isoformat(),
                    self.receipts["checkout"],
                    self.receipts["payment"],
                ),
            )

    def deliver_order_create(self) -> None:
        """The merchant's `order_create` for the order, as the webhook endpoint stores it."""
        execution = self.ledger.execution(self.task_id)
        assert execution is not None and execution.outbox is not None
        order = {
            "type": "order",
            "id": ORDER_ID,
            "checkout_session_id": execution.outbox.checkout_id,
            "status": "confirmed",
        }
        event = {"type": "order_create", "data": order}
        body = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert self.store.record_webhook(
            request_id=f"whd_{ORDER_ID}",
            event_type="order_create",
            order_id=ORDER_ID,
            payload_sha256=hashlib.sha256(body.encode()).hexdigest(),
            payload=event,
        )

    def reject_by_psp(self) -> None:
        """The PSP answers the payment with a verified AP2 error receipt."""
        lease = self.lease()
        assert self.ledger.advance_dispatch(self.task_id, lease, "payment_sent").status == "applied"
        reference = self.closed_links()["closed_payment"]
        token = receipt_jwt(
            status="Error",
            iss=PSP,
            reference=reference,
            error="invalid_mandate",
            error_description="The payment mandate does not verify",
            payment_id="pay_1",
        )
        # The ledger keeps the status lowercase, the form HNP-09b normalizes every receipt to; the
        # JWT keeps the SDK's `Error`. The event keeps the JWT's `payment_id`, as HNP-09b does.
        receipt = Receipt(
            "payment", token, PSP, "error", reference, error="invalid_mandate", payment_id="pay_1"
        )
        step = self.ledger.reject_dispatch(self.task_id, lease, "invalid_mandate", receipt)
        assert step.status == "closed"

    def reject_by_merchant(self) -> None:
        """After the PSP's token, the merchant answers the completion with a verified AP2 error
        receipt, without an order (`order_id` only comes with a success)."""
        lease = self.lease()
        for step in ("payment_sent", "payment_accepted", "complete_sent"):
            assert self.ledger.advance_dispatch(self.task_id, lease, step).status == "applied"
        merchant, reference = (
            self.settings.merchant_base_url,
            self.closed_links()["closed_checkout"],
        )
        token = receipt_jwt(
            status="Error",
            iss=merchant,
            reference=reference,
            error="invalid_mandate",
            error_description="The checkout mandate does not verify",
        )
        receipt = Receipt("checkout", token, merchant, "error", reference, error="invalid_mandate")
        closed = self.ledger.reject_dispatch(self.task_id, lease, "invalid_mandate", receipt)
        assert closed.status == "closed"


@contextmanager
def hnp_chain(
    tmp_path: Path,
    *,
    mode: Mode = "enforce",
    street: str | None = None,
    total_max_minor: int = 80_000,
    valid_for_seconds: int = 3_600,
    **changes: Any,
) -> Iterator[HnpChain]:
    """One consented grant and its executor; `changes` override settings (an allowlist)."""
    path = tmp_path / "agent.db"
    settings = (
        settings_for(tmp_path, **changes)
        if mode == "enforce"
        else configuration(path, mode="off", **changes)
    )
    built = runtime(settings, StoreMerchant())
    try:
        clock = AuthorityClock(datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=5))
        ledger = SqliteDelegationLedger(path, clock=clock, id_factory=Ids())
        ledger.initialize()
        pems = generate_keys()
        closer = AgentKeyCloser(pems.agent_pem, merchant_issuer=settings.merchant_base_url)
        issuer = OpenMandateIssuer(
            pems.provider_pem,
            agent_public_jwk=closer.public_jwk,
            merchant_issuer=settings.merchant_base_url,
        )
        trust = HnpTrust(
            _keys(settings), issuer, closer.public_jwk, closer.kid, settings.merchant_base_url
        )
        delegations = DelegationService(
            ledger,
            trust,
            feed=lambda: load_product_feed(FIXTURE),
            purchase=built.store.purchase,
            max_validity_seconds=86_400,
            proposal_ttl_seconds=600,
            clock=clock,
            new_id=Ids(),
        )
        wanted = details()
        address = wanted.fulfillment.address
        address = address.model_copy(update={"line_one": street or address.line_one})
        wanted = wanted.model_copy(
            update={"fulfillment": wanted.fulfillment.model_copy(update={"address": address})}
        )
        proposal = delegations.propose(
            OWNER,
            DelegationProposalRequest(
                variant_ids=("SON-01",),
                total_max_minor=total_max_minor,
                currency="COP",
                valid_for_seconds=valid_for_seconds,
                details=wanted,
            ),
        )
        view, _ = delegations.consent(
            OWNER,
            DelegationConsentRequest(
                proposal_id=proposal.proposal_id, scope_digest=proposal.scope_digest
            ),
        )
        enforce = mode == "enforce"
        gate = (
            Before(build_hnp_policy_gate(settings, None, trust=trust.mandates)) if enforce else None
        )
        purchases = (
            hnp_service(settings, built)
            if enforce
            else PurchaseService(
                feed=load_product_feed(FIXTURE),
                checkout=built.client,
                store=built.store,
                approvals=ApprovalService(built.store, SecretStr(secrets.token_urlsafe(40))),
            )
        )
        executor = HnpExecutionService(
            mode=mode,
            ledger=ledger,
            store=built.store,
            purchases=purchases,
            checkout=SignedClient(built.client, pems.merchant_pem),  # type: ignore[arg-type]
            closer=closer,
            gate=gate or NullHnpPolicyGate(),
            selector=DeterministicHnpSelector(),
            feed=lambda: load_product_feed(FIXTURE),
            clock=clock,
            id_factory=Ids(),
            freshness=build_offer_freshness(settings, built.client, clock) if enforce else None,
            d1_denials=sqlite_d1_denials(path) if enforce else None,
        )
        yield HnpChain(
            path=path,
            settings=settings,
            clock=clock,
            ledger=ledger,
            delegations=delegations,
            store=built.store,
            executor=executor,
            gate=gate,
            grant_id=view.grant_id,
            task_id=view.task.task_id,
        )
    finally:
        built.close()


def _keys(settings: Settings) -> ProvenanceKeys:
    """The vault keys of the owner and destination pseudonyms, as delegation startup reads them."""

    def secret(value: SecretStr | None) -> SecretBytes:
        assert value is not None
        return SecretBytes(bytes.fromhex(value.get_secret_value()))

    return ProvenanceKeys(
        key_id=settings.agent_d1_key_id or "",
        pseudonym_key=secret(settings.agent_d1_hmac_key_hex),
        encryption_key=secret(settings.agent_d1_aes_key_hex),
    )
