"""POL-11 of b2-hp-v2 stops a live B2 purchase turn before the Merchant sees a checkout.

Contract: the Agent is assembled as in production, `build_runtime(Settings)`, with the variables
the demo launcher exports for its `b2` mode (`demo_stack.mode_environment`, policy set
`b2-hp-v2`) in the process environment. Nothing injects a policy, a gate or a model.

Scenario: the phone purchase turn of the B0 lane (scripted router, never a real LLM) with a total
budget below every phone's catalog price. The shortlist only filters unit budgets, so the router
still picks a phone; D1 denies it with BUDGET_NOT_SATISFIABLE, the trace shows the catalog price
against the total budget and the Merchant receives no checkout creation POST.
"""

import pytest

from commerce_agent.application import PurchaseServiceError
from commerce_agent.application.conversation import ConversationTurnRequest
from commerce_agent.config import Settings
from commerce_agent.llm import ConversationMessage
from commerce_agent.runtime import build_runtime
from commerce_agent.trace.assembler import PurchaseTraces
from commerce_agent.trace.models import Money, PolicyDecisionStep
from commerce_evaluation.agent_condition import D1RunKeys
from commerce_evaluation.demo_stack import MODES, mode_environment
from tests.integration.live_purchase import (
    APPROVED_AT,
    PHONE_INTENT,
    PHONE_REQUEST,
    StaticConversationRouter,
    buyer_details,
)

# 100.000 COP in total: below the catalog price of every phone of the lane.
TOTAL_BUDGET_MINOR = 100_000_00


def test_b2_hp_v2_denies_a_total_budget_below_the_catalog_price(b0, tmp_path, monkeypatch):
    episode = b0.episode("pol11-budget")
    condition = mode_environment(MODES["b2"], b0.origin, D1RunKeys.generate())
    assert condition["AGENT_D1_POLICY_SET_ID"] == "b2-hp-v2"
    for name, value in condition.items():
        monkeypatch.setenv(name, value)
    settings = Settings(
        merchant_base_url=b0.origin,
        merchant_bearer_token=episode.bearer,
        merchant_webhook_secret=b0.webhook_secret,
        agent_approval_secret=b0.approval_secret,
        payment_provider_url=b0.payment.origin,
        payment_agent_bearer_token=b0.payment_token,
        agent_database_path=tmp_path / "agent.sqlite",
        agent_feed_source="file",
        agent_feed_path=episode.feed,
        agent_sandbox_clock_at=APPROVED_AT,
        agent_ap2_required=True,
        agent_ap2_private_key_pem=b0.agent_key,
        # build_runtime never authenticates buyers; jwt only satisfies the AP2 settings rule.
        agent_auth_mode="jwt",
        agent_auth_issuer="http://127.0.0.1:9",
        openai_api_key=None,
    )
    intent = PHONE_INTENT.model_copy(
        update={
            "hard": PHONE_INTENT.hard.model_copy(
                update={"max_total_amount_minor": TOTAL_BUDGET_MINOR}
            )
        }
    )
    turn = ConversationTurnRequest(
        turn_id="turn-pol11-budget",
        messages=[ConversationMessage(role="user", content=PHONE_REQUEST)],
        details=buyer_details("pol11-budget"),
    )
    before = b0.rpc("effects")
    runtime = build_runtime(
        settings, conversation_router=StaticConversationRouter(PHONE_REQUEST, intent)
    )
    try:
        with pytest.raises(PurchaseServiceError) as denied:
            runtime.conversation.respond(turn, owner_id=episode.actor_id)
        assert denied.value.code == "d1_denied"
    finally:
        runtime.close()

    traces = PurchaseTraces(settings)
    history = traces.history(episode.actor_id, limit=5, before=None)
    assert history is not None and len(history.purchases) == 1
    decision = next(
        item.decision
        for item in traces.trace(history.purchases[0].run_id).steps
        if isinstance(item, PolicyDecisionStep)
    )
    assert (decision.policy.set_id, decision.policy.revision) == ("b2-hp-v2", 1)
    assert (decision.mode, decision.outcome, decision.effect) == ("enforce", "deny", "blocked")
    assert [(rule.id, rule.result) for rule in decision.rules] == [
        ("POL-01", "pass"),
        ("POL-02", "pass"),
        ("POL-11", "fail"),
    ]
    assert decision.reason_codes == ("BUDGET_NOT_SATISFIABLE",)
    pol11 = decision.rules[2]
    assert pol11.expected == Money(minor=TOTAL_BUDGET_MINOR, currency="COP")
    assert isinstance(pol11.observed, Money) and pol11.observed.minor > TOTAL_BUDGET_MINOR
    creations = b0.proxy.count("POST", "/checkout_sessions")
    assert creations == 0
    assert b0.rpc("effects") == before
    b0.stack.record("b2:pol11", creation_posts=creations, pol11=pol11.result)
