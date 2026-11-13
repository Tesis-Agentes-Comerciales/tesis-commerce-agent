import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import httpx
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from commerce_agent.acp import ACPClient, load_product_feed
from commerce_agent.acp.feed import ProductFeed
from commerce_agent.acp.published_feed import PublishedFeedClient
from commerce_agent.ap2 import AgentMandateSigner
from commerce_agent.application import PurchaseService
from commerce_agent.application.ap2_evidence import build_ap2_evidence
from commerce_agent.application.conversation import ConversationService
from commerce_agent.application.governed_recovery import build_governed_recovery
from commerce_agent.application.offer_freshness import build_offer_freshness
from commerce_agent.application.stage_timing import (
    TimedCheckoutGateway,
    TimedConversationRouter,
    TimedD1Gate,
)
from commerce_agent.approval import ApprovalService
from commerce_agent.completion import CompletionService
from commerce_agent.config import Settings
from commerce_agent.delegated_payment import DelegatedPaymentClient
from commerce_agent.llm import ClaudeConversationRouter, ConversationRouter
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.model_requests_sqlite import SqliteModelRequests
from commerce_agent.persistence.stage_timings_sqlite import SqliteStageTimings

if TYPE_CHECKING:
    from commerce_agent.application.hnp_execution import HnpExecutionService
    from commerce_agent.delegation.service import DelegationService
    from commerce_agent.governance.authorization import SqliteD2AuthorizationGate
    from commerce_agent.governance.enforce import SqliteD1EnforceGate
    from commerce_agent.governance.shadow import ShadowD1Observer, UnavailableShadowObserver
    from commerce_agent.llm.delegated_choice import ClaudeDelegatedChooser
    from commerce_agent.llm.intent_review import ModelHnpIntentReviewer


@dataclass
class AgentRuntime:
    store: SqliteAgentStore
    client: ACPClient
    payments: DelegatedPaymentClient
    purchases: PurchaseService
    conversation: ConversationService
    owned_router: ClaudeConversationRouter | None
    published_feed: PublishedFeedClient | None
    checkpoint_connection: sqlite3.Connection
    mandate_signer: AgentMandateSigner | None = None
    d1_shadow: "ShadowD1Observer | UnavailableShadowObserver | None" = None
    d1_gate: "SqliteD1EnforceGate | None" = None
    d2_gate: "SqliteD2AuthorizationGate | None" = None
    # Human Not Present delegations; None unless AGENT_HNP_ENABLED (hnp-governed.md §6).
    delegations: "DelegationService | None" = None
    # Its executor (§3.3) and, with AGENT_HNP_SELECTOR=llm, the model client it owns.
    hnp_executor: "HnpExecutionService | None" = None
    owned_hnp_chooser: "ClaudeDelegatedChooser | None" = None
    # With AGENT_HNP_REVIEWER=llm, the intent reviewer's model client (hnp-intent-review.md §9).
    owned_hnp_reviewer: "ModelHnpIntentReviewer | None" = None
    # Display name of each merchant origin, from the seller its feed declares at startup.
    merchant_names: dict[str, str] = field(default_factory=dict)

    def close(self) -> None:
        self.client.close()
        self.payments.close()
        if self.owned_router is not None:
            self.owned_router.close()
        if self.owned_hnp_chooser is not None:
            self.owned_hnp_chooser.close()
        if self.owned_hnp_reviewer is not None:
            self.owned_hnp_reviewer.close()
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
    authority_clock: Callable[[], datetime] | None = None,
) -> AgentRuntime:
    if configuration.merchant_bearer_token is None:
        raise RuntimeError("MERCHANT_BEARER_TOKEN is required")
    if configuration.merchant_webhook_secret is None:
        raise RuntimeError("MERCHANT_WEBHOOK_SECRET is required")
    if configuration.agent_approval_secret is None:
        raise RuntimeError("AGENT_APPROVAL_SECRET is required")
    if configuration.payment_agent_bearer_token is None:
        raise RuntimeError("PAYMENT_AGENT_BEARER_TOKEN is required")
    if configuration.agent_hnp_reviewer == "llm" and not configuration.agent_hnp_enabled:
        raise RuntimeError("AGENT_HNP_REVIEWER=llm requires AGENT_HNP_ENABLED=true")
    hnp_trust = None
    if configuration.agent_hnp_enabled:
        from commerce_agent.delegation.bootstrap import load_hnp_trust

        # Before any store or client exists: an unsafe HNP condition stops startup.
        hnp_trust = load_hnp_trust(configuration)
    d1_gate = None
    if configuration.agent_governance_mode == "enforce":
        from commerce_agent.governance.bootstrap import build_enforce_gate

        # Before any store or client exists: invalid enforce settings stop startup.
        d1_gate = build_enforce_gate(configuration, clock)
    hnp_d1_gate = None
    if hnp_trust is not None and d1_gate is not None:
        from commerce_agent.governance.bootstrap import build_hnp_d1_gate

        hnp_d1_gate = build_hnp_d1_gate(configuration, clock)

    store = SqliteAgentStore(configuration.agent_database_path)
    store.initialize()
    timings = SqliteStageTimings(configuration.agent_database_path)
    timings.initialize()
    if hnp_trust is not None:
        # The HNP model selector reports its tokens; HP keeps the table's shape.
        timings.initialize_model_tokens()
    # Every request to the model, HP and HNP (docs/design/run-traceability.md §4.4).
    model_requests = SqliteModelRequests(configuration.agent_database_path)
    model_requests.initialize()
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
    d2_gate = None
    if d1_continuation is not None:
        from commerce_agent.governance.bootstrap import build_authorization_gate

        # After the store exists: the gate checks the ledger of this very database.
        d2_gate = build_authorization_gate(configuration, clock, ap2=mandate_signer is not None)
    ap2_evidence = build_ap2_evidence(configuration)
    completion = CompletionService(
        client, approvals, payments, store, d1_continuation, ap2_evidence=ap2_evidence
    )
    d1_shadow = None
    if configuration.agent_governance_mode == "shadow":
        from commerce_agent.governance.shadow import build_shadow_observer

        d1_shadow = build_shadow_observer(configuration, clock)
    checkout = TimedCheckoutGateway(client, timings)
    offer_freshness = build_offer_freshness(configuration, client, clock)
    purchases = PurchaseService(
        feed=feed,
        checkout=checkout,
        store=store,
        approvals=approvals,
        offer_freshness=offer_freshness,
        d1_observer=d1_shadow,
        d1_gate=TimedD1Gate(d1_gate, timings) if d1_gate is not None else None,
        d1_continuation=d1_continuation,
        d2_gate=d2_gate,
        ap2_evidence=ap2_evidence,
        hnp_d1_gate=hnp_d1_gate,
    )
    owned_router = None
    router_factory: Callable[[ProductFeed], ConversationRouter] | None = None
    if configuration.anthropic_api_key is not None and conversation_router is None:
        api_key = configuration.anthropic_api_key

        def make_router(current_feed: ProductFeed) -> ClaudeConversationRouter:
            return ClaudeConversationRouter(
                api_key=api_key,
                model=configuration.agent_model,
                feed=current_feed,
                effort=configuration.agent_model_effort,
                timeout_seconds=configuration.agent_model_timeout_seconds,
                reasoning_summary=configuration.agent_model_reasoning_summary,
                model_requests=model_requests,
            )

        if published_feed is None:
            owned_router = make_router(feed)
            conversation_router = owned_router
        else:

            def timed_router(current_feed: ProductFeed) -> ConversationRouter:
                return TimedConversationRouter(make_router(current_feed), timings)

            router_factory = timed_router
    conversation = ConversationService(
        feed=feed,
        store=store,
        purchases=purchases,
        completion=completion,
        discover=client.discover,
        router=(
            TimedConversationRouter(conversation_router, timings)
            if conversation_router is not None
            else None
        ),
        checkpointer=checkpointer,
        feed_provider=published_feed.get if published_feed is not None else None,
        router_factory=router_factory,
        recovery=build_governed_recovery(configuration),
        offer_reader=client,
        governance_mode=configuration.agent_governance_mode,
        checkout_reader=client,
    )
    delegations = hnp_executor = owned_hnp_chooser = owned_hnp_reviewer = None
    if hnp_trust is not None:
        from commerce_agent.delegation.bootstrap import (
            build_delegation_service,
            build_hnp_executor,
            build_hnp_reviewer,
            build_hnp_selector,
        )

        delegation_feed = published_feed.get if published_feed is not None else lambda: feed
        delegations = build_delegation_service(
            configuration, hnp_trust, store=store, feed=delegation_feed, clock=authority_clock
        )
        hnp_gate = None
        if d1_gate is not None:
            from commerce_agent.governance.bootstrap import build_hnp_policy_gate

            # After the ledger exists: D2-HNP and D3-HNP write their evidence inside its UoWs.
            hnp_gate = build_hnp_policy_gate(configuration, clock, trust=hnp_trust.mandates)
        selector, owned_hnp_chooser = build_hnp_selector(configuration, timings)
        owned_hnp_reviewer = build_hnp_reviewer(configuration, timings)
        hnp_executor = build_hnp_executor(
            configuration,
            hnp_trust,
            store=store,
            purchases=purchases,
            checkout=checkout,
            gate=hnp_gate,
            freshness=offer_freshness,
            selector=selector,
            feed=delegation_feed,
            clock=authority_clock,
            payments=payments,
            merchant=client,
            offers=client,
            reviewer=owned_hnp_reviewer,
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
        d2_gate=d2_gate,
        delegations=delegations,
        hnp_executor=hnp_executor,
        owned_hnp_chooser=owned_hnp_chooser,
        owned_hnp_reviewer=owned_hnp_reviewer,
        merchant_names=(
            {configuration.merchant_base_url: feed.seller_name} if feed.seller_name else {}
        ),
    )
