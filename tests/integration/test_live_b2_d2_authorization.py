"""D2-HP of b2-hp-v2 authorizes live B2 approvals before AP2 signs (hp-p3-d2-authorization).

Contract: the Agent is assembled as in production, `build_runtime(Settings)`, with the variables
the demo launcher exports for its `b2` mode (`demo_stack.mode_environment`, policy set
`b2-hp-v2`) in the process environment. Nothing injects a policy, a gate or a model.

Scenarios on the B0 lane (scripted router, never a real LLM):
- S01: approving runs freshness, then D2 (POL-03..06 allow), then the AP2 signature in the same
  transaction as the approval; the order is confirmed and the AP2 evidence holds the consent and
  its effect, in that order. The trace shows the D2 step between the approval and the signed
  authorization. Once the Merchant's `order_create` arrives, `verify-evidence` verifies the nine
  links, D2 included, and the chain is complete.
- S04: the Merchant changes the offer after the terms were shown. Freshness refuses both the reread
  and a client that posts anyway, before D2: no D2 row and no D2 refusal for the chat. A second
  purchase of the same buyer is authorized by D2 and reaches its order.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from commerce_agent.application.conversation import ConversationTurnRequest
from commerce_agent.approval import ApprovalError
from commerce_agent.config import Settings
from commerce_agent.llm import ConversationMessage
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.runtime import build_runtime
from commerce_agent.trace.assembler import PurchaseTraces
from commerce_agent.trace.models import AuthorizationDecisionStep
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


def settings_for(b0, episode, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    condition = mode_environment(MODES["b2"], b0.origin, D1RunKeys.generate())
    assert condition["AGENT_D1_POLICY_SET_ID"] == "b2-hp-v2"
    for name, value in condition.items():
        monkeypatch.setenv(name, value)
    return Settings(
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


def turn(name: str) -> ConversationTurnRequest:
    return ConversationTurnRequest(
        turn_id=f"turn-{name}",
        messages=[ConversationMessage(role="user", content=PHONE_REQUEST)],
        details=buyer_details(name),
    )


def d2_rows(path: Path, run_id: str) -> list[tuple[str, str, str]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT record_kind, outcome, evidence_id FROM governance_evidence "
            "WHERE run_id=? AND phase='D2' ORDER BY sequence",
            (run_id,),
        ).fetchall()


def approval_evidence(path: Path, run_id: str) -> str | None:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT authorization_evidence_id FROM approvals WHERE run_id=?", (run_id,)
        ).fetchone()
    return row[0] if row else None


def ap2_kinds(path: Path, run_id: str) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT record_kind FROM ap2_evidence WHERE run_id=? ORDER BY sequence", (run_id,)
            )
        ]


def test_s01_d2_allows_the_approval_and_the_order_is_confirmed(b0, tmp_path, monkeypatch):
    episode = b0.episode("d2-s01")
    settings = settings_for(b0, episode, tmp_path, monkeypatch)
    runtime = build_runtime(
        settings, conversation_router=StaticConversationRouter(PHONE_REQUEST, PHONE_INTENT)
    )
    try:
        assert runtime.d2_gate is not None
        response = runtime.conversation.respond(turn("d2-s01"), owner_id=episode.actor_id)
        assert response.purchase is not None and response.approval_terms is not None
        run_id, terms = response.purchase.run_id, response.approval_terms
        approved = runtime.purchases.approve(
            run_id=run_id,
            actor_id=episode.actor_id,
            expected_fingerprint=terms.fingerprint,
            checkout_jwt=terms.checkout_jwt,
        )
        assert approved.checkout_mandate is not None
        completed = runtime.conversation.complete_purchase(run_id, episode.actor_id)
        assert completed.status == "completed" and completed.order.status == "confirmed"
    finally:
        runtime.close()

    path = settings.agent_database_path
    rows = d2_rows(path, run_id)
    assert [(kind, outcome) for kind, outcome, _ in rows] == [("decision_record", "allow")]
    assert approved.authorization_evidence_id == rows[0][2] == approval_evidence(path, run_id)
    # Consent only once the approval exists (after the D2 commit), then its effect.
    assert ap2_kinds(path, run_id) == ["consent", "effect"]
    steps = PurchaseTraces(settings).trace(run_id).steps
    kinds = [step.kind for step in steps]
    assert kinds.index("approval") < kinds.index("authorization_decision")
    assert kinds.index("authorization_decision") < kinds.index("authorization_signed")
    step = next(item for item in steps if isinstance(item, AuthorizationDecisionStep))
    assert step.state == "done" and step.decision.outcome == "allow"

    # The Merchant's dispatcher posts the order's `order_create` (as demo-stack and the runner).
    with b0.receiver.delivering_to(SqliteAgentStore(path)):
        b0.rpc("dispatch_webhooks", limit=50)
    [verified] = verify_database(path, run_id=run_id, psp_database=b0.root / "payments.sqlite")
    statuses = {name: link["status"] for name, link in verified["links"].items()}
    assert statuses == dict.fromkeys(LINKS, "ok"), verified["links"]
    assert verified["complete"] is True and verified["order_id"] == completed.order.id
    d2 = verified["links"]["d2_decision"]["refs"]
    assert (d2["evidence_id"], d2["outcome"], d2["replay"]) == (rows[0][2], "allow", "reproduced")
    assert verified["links"]["receipts"]["refs"]["psp"] == "verified"
    b0.stack.record("b2:d2-s01", d2=rows[0][1], chain_complete=verified["complete"])


def test_s04_freshness_refuses_before_d2_and_the_next_purchase_is_authorized(
    b0, tmp_path, monkeypatch
):
    episode = b0.episode("d2-s04")
    settings = settings_for(b0, episode, tmp_path, monkeypatch)
    runtime = build_runtime(
        settings, conversation_router=StaticConversationRouter(PHONE_REQUEST, PHONE_INTENT)
    )
    try:
        response = runtime.conversation.respond(turn("d2-s04"), owner_id=episode.actor_id)
        assert response.purchase is not None and response.approval_terms is not None
        run_id, terms = response.purchase.run_id, response.approval_terms
        assert response.purchase.selected_candidate is not None
        variant = response.purchase.selected_candidate.variant_id
        b0.rpc("edit_offer", run_id=episode.run_id, offer_id=variant, change="shipping")
        # The UI rereads the terms first (GET /approval): `present` freshness refuses them.
        with pytest.raises(ApprovalError) as reread:
            runtime.purchases.approval_terms(run_id)
        assert reread.value.code == "checkout_terms_changed"
        # A client that posts anyway: `approve` freshness refuses before D2 runs.
        with pytest.raises(ApprovalError) as stale:
            runtime.purchases.approve(
                run_id=run_id,
                actor_id=episode.actor_id,
                expected_fingerprint=terms.fingerprint,
                checkout_jwt=terms.checkout_jwt,
            )
        assert stale.value.code == "checkout_terms_changed"
        assert d2_rows(settings.agent_database_path, run_id) == []
        assert runtime.d2_gate is not None and runtime.d2_gate.refusal(run_id) is None

        # The lane's proxy refuses the cancel, so the second purchase starts directly.
        second = f"d2-s04-second-{episode.run_id}"
        prepared = runtime.purchases.start(
            run_id=second,
            request=PHONE_REQUEST,
            intent=PHONE_INTENT,
            selected_variant_id=variant,
            details=buyer_details("d2-s04-second"),
            owner_id=episode.actor_id,
        )
        assert prepared.stage == "awaiting_confirmation"
        renewed = runtime.purchases.approval_terms(second)
        # What GET /purchases/{run_id}/approval stores before the buyer approves.
        runtime.store.save_approval_terms(second, renewed.model_dump(mode="json"))
        runtime.purchases.approve(
            run_id=second,
            actor_id=episode.actor_id,
            expected_fingerprint=renewed.fingerprint,
            checkout_jwt=renewed.checkout_jwt,
        )
        completed = runtime.conversation.complete_purchase(second, episode.actor_id)
        assert completed.status == "completed"
    finally:
        runtime.close()

    rows = d2_rows(settings.agent_database_path, second)
    assert [(kind, outcome) for kind, outcome, _ in rows] == [("decision_record", "allow")]
    b0.stack.record("b2:d2-s04", first_d2_rows=0, second=rows[0][1])
