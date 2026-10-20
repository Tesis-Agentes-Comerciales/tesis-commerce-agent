import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import httpx
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from commerce_agent.acp import ACPClient, load_product_feed
from commerce_agent.acp.published_feed import PublishedFeedClient
from commerce_agent.ap2 import AgentMandateSigner
from commerce_agent.application import PurchaseService
from commerce_agent.application.conversation import ConversationService
from commerce_agent.application.governed_recovery import build_governed_recovery
from commerce_agent.application.offer_freshness import build_offer_freshness
from commerce_agent.approval import ApprovalService
from commerce_agent.completion import CompletionService
from commerce_agent.config import Settings
from commerce_agent.delegated_payment import DelegatedPaymentClient
from commerce_agent.llm import ConversationRouter, OpenAIConversationRouter
from commerce_agent.persistence import SqliteAgentStore

if TYPE_CHECKING:
    from commerce_agent.governance.enforce import SqliteD1EnforceGate
    from commerce_agent.governance.shadow import ShadowD1Observer, UnavailableShadowObserver


@dataclass
class AgentRuntime:
    store: SqliteAgentStore
    client: ACPClient
    payments: DelegatedPaymentClient
    purchases: PurchaseService
    conversation: ConversationService
    owned_router: OpenAIConversationRouter | None
    published_feed: PublishedFeedClient | None
    checkpoint_connection: sqlite3.Connection
    mandate_signer: AgentMandateSigner | None = None
    d1_shadow: "ShadowD1Observer | UnavailableShadowObserver | None" = None
    d1_gate: "SqliteD1EnforceGate | None" = None

    def close(self) -> None:
        self.client.close()
        self.payments.close()
        if self.owned_router is not None:
            self.owned_router.close()
        if self.published_feed is not None:
            self.published_feed.close()
        self.checkpoint_connection.close()


def build_runtime(
    configuration: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
    payment_transport: httpx.BaseTransport | None = None,
    clock: Callable[[], datetime] | None = None,
    conversation_router: ConversationRouter | None = None,
) -> AgentRuntime:
    if configuration.merchant_bearer_token is None:
        raise RuntimeError("MERCHANT_BEARER_TOKEN is required")
    if configuration.merchant_webhook_secret is None:
        raise RuntimeError("MERCHANT_WEBHOOK_SECRET is required")
    if configuration.agent_approval_secret is None:
        raise RuntimeError("AGENT_APPROVAL_SECRET is required")
    if configuration.payment_agent_bearer_token is None:
        raise RuntimeError("PAYMENT_AGENT_BEARER_TOKEN is required")
    d1_gate = None
    if configuration.agent_governance_mode == "enforce":
        from commerce_agent.governance.bootstrap import build_enforce_gate

        # Before any store or client exists: invalid enforce settings stop startup.
        d1_gate = build_enforce_gate(configuration, clock)

    store = SqliteAgentStore(configuration.agent_database_path)
    store.initialize()
    checkpoint_path = configuration.agent_database_path.with_name(
        f"{configuration.agent_database_path.stem}.graph.db"
    )
    checkpoint_connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
    checkpointer = SqliteSaver(
        checkpoint_connection, serde=JsonPlusSerializer(allowed_msgpack_modules=[])
    )
    published_feed = None
    if configuration.agent_feed_source == "merchant":
        published_feed = PublishedFeedClient(
            configuration.merchant_base_url,
            timeout_seconds=configuration.merchant_timeout_seconds,
            transport=transport,
        )
        feed = published_feed.get()
    else:
        if configuration.agent_feed_path is None:
            raise RuntimeError("AGENT_FEED_PATH is required for a local feed")
        feed = load_product_feed(configuration.agent_feed_path)
    client = ACPClient(
        configuration.merchant_base_url,
        configuration.merchant_bearer_token,
        timeout_seconds=configuration.merchant_timeout_seconds,
        transport=transport,
    )
    approval_clock = clock
    if approval_clock is None and configuration.agent_sandbox_clock_at is not None:
        sandbox_now = configuration.agent_sandbox_clock_at

        def fixed_sandbox_clock() -> datetime:
            return sandbox_now

        approval_clock = fixed_sandbox_clock
    mandate_signer = None
    if configuration.agent_ap2_required:
        key = configuration.agent_ap2_private_key_pem
        if key is None:
            raise RuntimeError("AGENT_AP2_PRIVATE_KEY_PEM is required")
        mandate_signer = AgentMandateSigner(
            key.get_secret_value().replace("\\n", "\n"),
            merchant_id="tesis_merchant",
            merchant_name="Tesis Commerce Merchant",
        )
    approvals = ApprovalService(
        store,
        configuration.agent_approval_secret,
        merchant_origin=configuration.merchant_base_url,
        clock=approval_clock,
        mandate_signer=mandate_signer,
    )
    payments = DelegatedPaymentClient(
        configuration.payment_provider_url,
        configuration.payment_agent_bearer_token,
        transport=payment_transport,
    )
    d1_continuation = None
    if d1_gate is not None:
        from commerce_agent.governance.hp_continuation import governed_continuation

        d1_continuation = governed_continuation(configuration, d1_gate, store)
    completion = CompletionService(client, approvals, payments, store, d1_continuation)
    d1_shadow = None
    if configuration.agent_governance_mode == "shadow":
        from commerce_agent.governance.shadow import build_shadow_observer

        d1_shadow = build_shadow_observer(configuration, clock)
    purchases = PurchaseService(
        feed=feed,
        checkout=client,
        store=store,
        approvals=approvals,
        offer_freshness=build_offer_freshness(configuration, client, clock),
        d1_observer=d1_shadow,
        d1_gate=d1_gate,
        d1_continuation=d1_continuation,
    )
    owned_router = None
    router_factory = None
    if (
        configuration.openai_api_key is not None
        and configuration.openai_model is not None
        and conversation_router is None
    ):
        api_key = configuration.openai_api_key
        model = configuration.openai_model

        def make_router(current_feed):
            return OpenAIConversationRouter(
                api_key=api_key,
                model=model,
                feed=current_feed,
                timeout_seconds=configuration.openai_timeout_seconds,
                reasoning_summary=configuration.openai_reasoning_summary,
                reasoning_effort=configuration.openai_reasoning_effort,
            )

        if published_feed is None:
            owned_router = make_router(feed)
            conversation_router = owned_router
        else:
            router_factory = make_router
    conversation = ConversationService(
        feed=feed,
        store=store,
        purchases=purchases,
        completion=completion,
        discover=client.discover,
        router=conversation_router,
        checkpointer=checkpointer,
        feed_provider=published_feed.get if published_feed is not None else None,
        router_factory=router_factory,
        recovery=build_governed_recovery(configuration),
    )
    return AgentRuntime(
        store=store,
        client=client,
        payments=payments,
        purchases=purchases,
        conversation=conversation,
        owned_router=owned_router,
        published_feed=published_feed,
        checkpoint_connection=checkpoint_connection,
        mandate_signer=mandate_signer,
        d1_shadow=d1_shadow,
        d1_gate=d1_gate,
    )
