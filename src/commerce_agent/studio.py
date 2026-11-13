import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.graph.state import CompiledStateGraph

from commerce_agent.acp import load_product_feed
from commerce_agent.acp.published_feed import PublishedFeedClient
from commerce_agent.application.conversation import (
    ConversationService,
    GraphContext,
    GraphInput,
    GraphState,
)
from commerce_agent.config import get_settings
from commerce_agent.llm import ClaudeConversationRouter
from commerce_agent.persistence import SqliteAgentStore


@asynccontextmanager
async def make_graph() -> AsyncIterator[
    CompiledStateGraph[GraphState, GraphContext, GraphInput, GraphState]
]:
    graph, router, published_feed = await asyncio.to_thread(_build_graph)
    try:
        yield graph
    finally:
        if router is not None:
            await asyncio.to_thread(router.close)
        if published_feed is not None:
            await asyncio.to_thread(published_feed.close)


def _build_graph() -> tuple[
    CompiledStateGraph[GraphState, GraphContext, GraphInput, GraphState],
    ClaudeConversationRouter | None,
    PublishedFeedClient | None,
]:
    settings = get_settings()
    studio_database = settings.agent_database_path.with_name(
        f"{settings.agent_database_path.stem}.studio.db"
    )
    store = SqliteAgentStore(studio_database)
    store.initialize()
    published_feed = None
    if settings.agent_feed_source == "merchant":
        published_feed = PublishedFeedClient(
            settings.merchant_base_url, timeout_seconds=settings.merchant_timeout_seconds
        )
        feed = published_feed.get()
    else:
        if settings.agent_feed_path is None:
            raise RuntimeError("AGENT_FEED_PATH is required for a local feed")
        feed = load_product_feed(settings.agent_feed_path)
    router = (
        ClaudeConversationRouter(
            api_key=settings.anthropic_api_key,
            model=settings.agent_model,
            feed=feed,
            effort=settings.agent_model_effort,
            timeout_seconds=settings.agent_model_timeout_seconds,
            reasoning_summary=settings.agent_model_reasoning_summary,
        )
        if settings.anthropic_api_key is not None
        else None
    )
    conversation = ConversationService(
        feed=feed,
        store=store,
        purchases=None,
        completion=None,
        discover=None,
        router=router,
        checkpointer=None,
        allow_checkout=False,
    )
    return conversation.graph, router, published_feed
