"""POL-02 configured from Settings governs a live B2 purchase turn, on the B0 lane.

Contract: the Agent is assembled as in production, `build_runtime(Settings)`, with the
variables the demo launcher exports for its mode (`demo_stack.mode_environment` over
`demo_stack.MODES`, the pilot's B2 condition) in the process environment. Nothing injects a
policy, a gate or a model: the merchant allowlist reaches the D1 gate only through
AGENT_D1_ALLOWED_MERCHANT_ORIGINS.

Scenario: the same conversational purchase turn (scripted router, never a real LLM) for a
seeded B0 episode under two allowlists.

- `pol02-deny`: the allowlist names another merchant (the demo's `pol02-deny` mode). The turn
  is refused with `d1_denied`, its trace shows POL-02 `fail` with
  ACTION_OR_MERCHANT_NOT_ALLOWED and the Merchant receives no checkout creation POST.
- `b2`: the allowlist names the lane's Merchant (the demo's `b2` mode). The same turn reaches
  `awaiting_confirmation` after exactly one creation POST, with POL-02 `pass`.

In both, `verify-evidence` verifies what the turn left: the deny's intent and decision, or the
allowed purchase up to its freshness check. Nothing after it applies yet.
"""

import pytest

from commerce_agent.application import PurchaseServiceError
from commerce_agent.application.conversation import ConversationTurnRequest
from commerce_agent.config import Settings
from commerce_agent.llm import ConversationMessage
from commerce_agent.runtime import build_runtime
from commerce_agent.trace.assembler import PurchaseTraces
from commerce_agent.trace.models import PolicyDecisionStep
from commerce_evaluation.agent_condition import D1RunKeys
from commerce_evaluation.demo_stack import MODES, mode_environment
from commerce_evaluation.stack.purchase_agent import (
    APPROVED_AT,
    PHONE_INTENT,
    PHONE_REQUEST,
    StaticConversationRouter,
    buyer_details,
)
from commerce_evaluation.verify_evidence import LINKS, verify_database


@pytest.mark.parametrize("mode", ["pol02-deny", "b2"])
def test_b2_pol02_allowlist_from_settings_governs_the_purchase_turn(
    b0, tmp_path, monkeypatch, mode
):
    episode = b0.episode(f"pol02-{mode}")
    # The demo's own modes: b2 allows the lane's Merchant, pol02-deny names another one.
    condition = mode_environment(MODES[mode], b0.origin, D1RunKeys.generate())
    allowed = condition["AGENT_D1_ALLOWED_MERCHANT_ORIGINS"]
    assert (allowed == b0.origin) is (mode == "b2")
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
        anthropic_api_key=None,
    )
    # The condition comes from the environment, as in the pilot and the demo.
    assert (settings.agent_governance_mode, settings.agent_hp_flow) == ("enforce", "governed")
    assert settings.agent_d1_allowed_merchant_origins == allowed
    turn = ConversationTurnRequest(
        turn_id=f"turn-pol02-{mode}",
        messages=[ConversationMessage(role="user", content=PHONE_REQUEST)],
        details=buyer_details(f"pol02-{mode}"),
    )
    before = b0.rpc("effects")
    runtime = build_runtime(
        settings, conversation_router=StaticConversationRouter(PHONE_REQUEST, PHONE_INTENT)
    )
    try:
        if mode == "b2":
            response = runtime.conversation.respond(turn, owner_id=episode.actor_id)
            assert response.action == "purchase" and response.purchase is not None
            assert response.purchase.stage == "awaiting_confirmation"
            assert response.approval_terms is not None
        else:
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
    pol02 = next(rule for rule in decision.rules if rule.id == "POL-02")
    assert (pol02.observed, pol02.expected) == (b0.origin, allowed)
    creations = b0.proxy.count("POST", "/checkout_sessions")
    after = b0.rpc("effects")
    if mode == "b2":
        assert (decision.outcome, pol02.result) == ("allow", "pass")
        assert creations == 1
        assert after["checkout_sessions"] == before["checkout_sessions"] + 1
    else:
        assert (decision.mode, decision.outcome, decision.effect) == ("enforce", "deny", "blocked")
        assert pol02.result == "fail"
        assert decision.reason_codes == ("ACTION_OR_MERCHANT_NOT_ALLOWED",)
        assert creations == 0
        assert after == before
    [verified] = verify_database(settings.agent_database_path)
    reached = ("intent", "d1_decision", "checkout_created", "freshness")
    if mode == "b2":
        assert verified["stage"] == "awaiting_confirmation"
        reached_ok = {name: "ok" for name in reached}
        # b2-hp-v2 requires D2 at approval, and the purchase was not approved.
        assert verified["links"]["d2_decision"]["refs"] == {
            "policy_set_id": "b2-hp-v2",
            "d2_required": False,
        }
    else:
        assert verified["stage"] == "received"
        reached_ok = {"intent": "ok", "d1_decision": "ok"}
        assert verified["links"]["d1_decision"]["refs"]["outcome"] == "deny"
    statuses = {name: link["status"] for name, link in verified["links"].items()}
    assert statuses == dict.fromkeys(LINKS, "not_applicable") | reached_ok
    assert verified["links"]["d1_decision"]["refs"]["replay"] == "reproduced"
    assert verified["complete"] is True
    b0.stack.record(f"b2:{mode}", creation_posts=creations, pol02=pol02.result)
