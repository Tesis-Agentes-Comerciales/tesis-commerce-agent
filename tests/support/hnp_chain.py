"""Agent databases of real HNP runs, for `verify-evidence` (docs/design/hnp-governed.md §9.2).

The delegation service, the executor, D1/D2/D3-HNP, the `close` freshness guard, the open
mandate issuer and the Agent Key are the product's, over one real SQLite file, wired as
tests/governance/test_hnp_gate.py wires them. Only the merchant (httpx.MockTransport), its AP2
checkout signature (a key generated for the test) and the authority clock are doubles. The
executor stops at UoW-2: the dispatch is HNP-09b's.
"""

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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
from commerce_agent.delegation.models import Step
from commerce_agent.delegation.service import (
    DelegationConsentRequest,
    DelegationProposalRequest,
    DelegationService,
    HnpTrust,
)
from commerce_agent.governance.bootstrap import build_hnp_policy_gate
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


@dataclass
class HnpChain:
    path: Path
    settings: Settings
    clock: AuthorityClock
    ledger: SqliteDelegationLedger
    delegations: DelegationService
    store: SqliteAgentStore
    executor: HnpExecutionService
    grant_id: str
    task_id: str

    def run(self) -> Step:
        return self.executor.run(self.task_id)

    def revoke(self) -> None:
        self.delegations.revoke(OWNER, self.grant_id)

    def kill_switch(self) -> None:
        self.delegations.set_kill_switch(True)


@contextmanager
def hnp_chain(
    tmp_path: Path,
    *,
    mode: Mode = "enforce",
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
        proposal = delegations.propose(
            OWNER,
            DelegationProposalRequest(
                variant_ids=("SON-01",),
                total_max_minor=total_max_minor,
                currency="COP",
                valid_for_seconds=valid_for_seconds,
                details=details(),
            ),
        )
        view, _ = delegations.consent(
            OWNER,
            DelegationConsentRequest(
                proposal_id=proposal.proposal_id, scope_digest=proposal.scope_digest
            ),
        )
        enforce = mode == "enforce"
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
            gate=(
                build_hnp_policy_gate(settings, None, trust=trust.mandates)
                if enforce
                else NullHnpPolicyGate()
            ),
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
