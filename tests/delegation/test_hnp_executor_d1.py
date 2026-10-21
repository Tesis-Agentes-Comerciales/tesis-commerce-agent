"""The executor over the real D1-HNP gate (docs/design/hnp-governed.md §3.3 steps 3-4, §4.6, §8.1).

The merchant runs behind httpx.MockTransport, SQLite is real and D1-HNP is the gate HNP-05 wires:
`SqliteD1EnforceGate(purchase_mode="HNP", merchant_refs=operator_merchant_refs(...))`. D2/D3-HNP,
the freshness check and the Agent Key are the scripted doubles of tests/support/hnp_executor.py.
"""

import secrets
from pathlib import Path

from pydantic import SecretStr

from commerce_agent.acp.feed import load_product_feed
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.application import PurchaseService
from commerce_agent.application.hnp_execution import sqlite_d1_denials
from commerce_agent.approval import ApprovalService
from commerce_agent.governance.bootstrap import configured_d1
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.hnp_policy import operator_merchant_refs
from commerce_agent.governance.provenance import D1EnforceConfiguration
from commerce_agent.runtime import AgentRuntime
from tests.governance.test_d1_budget_runtime import decision_records
from tests.governance.test_d1_hnp_gate import hnp_service, settings_for
from tests.governance.test_hp_continuation import StoreMerchant
from tests.governance.test_runtime_dispatch import CREATE, runtime
from tests.governance.test_shadow_integration import FIXTURE
from tests.support.hnp_executor import CHECKOUT_JWT, Bench


class SignedClient:
    """The runtime's ACP client, with the merchant's AP2 checkout signature scripted."""

    def __init__(self, built: AgentRuntime) -> None:
        self._client = built.client

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    def signed_checkout(self, checkout: CheckoutSession) -> str:
        return CHECKOUT_JWT

    def verify_signed_checkout(self, token: str, checkout: CheckoutSession) -> None:
        assert token == CHECKOUT_JWT


def bench(tmp_path: Path, built: AgentRuntime, purchases, **options) -> Bench:
    return Bench(
        tmp_path,
        mode="enforce",
        purchases=lambda _: purchases,
        d1_denials=sqlite_d1_denials(tmp_path / "agent.db"),
        checkout=SignedClient(built),  # type: ignore[arg-type]
        **options,
    )


def test_a_d1_hnp_allow_reaches_the_dispatch_commitment(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    merchant = StoreMerchant()
    built = runtime(settings, merchant)
    try:
        run = bench(tmp_path, built, hnp_service(settings, built))
        step = run.run()
    finally:
        built.close()

    assert step.status == "applied"
    assert merchant.creations() == 1
    assert run.purchase() == ("completing", None)
    (record,) = decision_records(settings.agent_database_path, run.run_id())
    assert (record.decision.purchase_mode, record.decision.outcome) == ("HNP", "allow")
    assert record.policy_input().eligible_variant_ids == ("SON-01",)
    assert run.closer.signatures == 1 and run.effects().commits == 1


def test_a_pol11_deny_escalates_before_any_checkout(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    merchant = StoreMerchant()
    built = runtime(settings, merchant)
    try:
        run = bench(tmp_path, built, hnp_service(settings, built), budget=71_999)
        step = run.run()
    finally:
        built.close()

    assert step.status == "closed"
    assert run.ended() == ("escalated", "budget_not_satisfiable")
    assert run.execution().grant.revoked_reason == "escalated"
    assert CREATE not in merchant.requests
    assert run.purchase() == ("canceled", None)
    (record,) = decision_records(settings.agent_database_path, run.run_id())
    assert record.decision.reason_codes == ("BUDGET_NOT_SATISFIABLE",)
    assert run.closer.signatures == 0 and run.effects().commits == 0


def test_an_allow_left_without_its_dispatch_waits_for_d1_recovery(tmp_path: Path) -> None:
    """HNP-05b: a `selected` purchase has no HNP transition; the task ends without touching it."""
    settings = settings_for(tmp_path)
    merchant = StoreMerchant()
    built = runtime(settings, merchant)
    configured = configured_d1(settings, None)
    assert isinstance(configured.configuration, D1EnforceConfiguration)
    undispatched = SqliteD1EnforceGate(
        settings.agent_database_path,
        configured.configuration,
        configured.keys,
        configured.clocks,
        purchase_mode="HNP",
        merchant_refs=operator_merchant_refs(settings, configured),
    )
    purchases = PurchaseService(
        feed=load_product_feed(FIXTURE),
        checkout=built.client,
        store=built.store,
        approvals=ApprovalService(built.store, SecretStr(secrets.token_urlsafe(40))),
        d1_gate=built.d1_gate,
        hnp_d1_gate=undispatched,
    )
    try:
        run = bench(tmp_path, built, purchases)
        step = run.run()
    finally:
        built.close()

    assert step.status == "closed"
    assert run.ended() == ("failed", "governance_unavailable")
    assert run.purchase() == ("selected", None)
    assert CREATE not in merchant.requests
    (record,) = decision_records(settings.agent_database_path, run.run_id())
    assert (record.decision.purchase_mode, record.decision.outcome) == ("HNP", "allow")
