"""No request of the agent asks for an output schema and lets the model call a tool (llm/claude.py).

The API compiles both into one grammar, and Haiku 5.5 rejected the router's decision schema next
to `lookup_catalog` with `tool_choice` auto (400 "The compiled grammar is too large", 13-nov-2026).
Every path of the three model clients runs here over the scripted transport of
tests/support/model_server.py, which answers that same 400 to such a request: no request leaves
the process and no model is called.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import anthropic
import pytest
from pydantic import SecretStr

from commerce_agent.domain import HardConstraints, PurchaseIntent, rank_candidates
from commerce_agent.llm import ClaudeConversationRouter, CommerceDecision, ConversationMessage
from commerce_agent.llm.delegated_choice import ClaudeDelegatedChooser
from commerce_agent.llm.intent_review import ModelHnpIntentReviewer
from commerce_agent.llm.reasoning import reasoning_listener
from commerce_agent.persistence.stage_timings_sqlite import SqliteStageTimings
from tests.support.model_server import (
    GRAMMAR_TOO_LARGE,
    MODEL,
    Answer,
    ModelServer,
    function_call,
    lets_a_tool_be_called_with_a_schema,
    routed,
    shortlisted_choice,
    structured,
)
from tests.test_conversation_router import FEED

API_KEY = SecretStr("sk-ant-test-request-grammar-0123456789abcdef")
INTENT = PurchaseIntent(
    hard=HardConstraints(currency="COP", categories=("headphones",), max_total_amount_minor=150_000)
)
SHORTLIST = rank_candidates(FEED, INTENT).ranked
MESSAGES = (ConversationMessage(role="user", content="Quiero audífonos por máximo 150.000 COP"),)
ROUTED = CommerceDecision(action="purchase", assistant_message="Busco.", intent=INTENT)
RECOVERY = {"action": "clarify", "assistant_message": "No hay audífonos en ese presupuesto."}
TOOLS = [{"name": "lookup_catalog", "input_schema": {"type": "object"}}]
FORMAT = {"effort": "low", "format": {"type": "json_schema", "schema": {"type": "object"}}}


def router(summaries: bool = False) -> ClaudeConversationRouter:
    return ClaudeConversationRouter(api_key=API_KEY, feed=FEED, reasoning_summary=summaries)


def route(summaries: bool = False) -> Callable[[], object]:
    def run() -> object:
        with reasoning_listener((lambda delta: None) if summaries else None):
            return router(summaries).route(MESSAGES)

    return run


def reviewer(tmp_path: Path) -> ModelHnpIntentReviewer:
    timings = SqliteStageTimings(tmp_path / "agent.db")
    timings.initialize()
    timings.initialize_model_tokens()
    return ModelHnpIntentReviewer(api_key=API_KEY, timer=timings)


PATHS: dict[str, tuple[Callable[[Path], Callable[[], object]], tuple[Answer, ...]]] = {
    "route": (lambda tmp: route(), routed(ROUTED)),
    "route-with-summaries": (lambda tmp: route(summaries=True), routed(ROUTED)),
    "route-with-lookup": (
        lambda tmp: route(),
        (function_call("lookup_catalog", {"query": "audífonos"}), structured(ROUTED)),
    ),
    "route-with-lookup-and-summaries": (
        lambda tmp: route(summaries=True),
        (function_call("lookup_catalog", {"query": "audífonos"}), structured(ROUTED)),
    ),
    "choose-candidate": (
        lambda tmp: lambda: router().choose_candidate(MESSAGES, INTENT, SHORTLIST),
        (structured(shortlisted_choice),),
    ),
    "recover-no-candidate": (
        lambda tmp: (
            lambda: router().recover_no_candidate(
                MESSAGES, INTENT, {"budget": 1}, FEED.available_candidates
            )
        ),
        (structured(lambda body: RECOVERY),),
    ),
    "hnp-selector": (
        lambda tmp: lambda: ClaudeDelegatedChooser(api_key=API_KEY).choose(INTENT, SHORTLIST, None),
        (structured(shortlisted_choice),),
    ),
    "hnp-reviewer": (
        lambda tmp: lambda: reviewer(tmp).review(SHORTLIST, INTENT, None, "SON-01"),
        (structured(lambda body: {"decision": "confirmed", "reason": "matches_request"}),),
    ),
}


@pytest.mark.parametrize("path", list(PATHS))
def test_no_request_lets_the_model_call_a_tool_under_an_output_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str
) -> None:
    build, answers = PATHS[path]
    server = ModelServer(*answers)
    server.install(monkeypatch)

    build(tmp_path)()

    bodies = server.bodies()
    assert len(bodies) == len(answers)
    assert [lets_a_tool_be_called_with_a_schema(body) for body in bodies] == [False] * len(bodies)


@pytest.mark.parametrize(
    ("body", "combined"),
    [
        ({"tools": TOOLS, "tool_choice": {"type": "auto"}, "output_config": FORMAT}, True),
        ({"tools": TOOLS, "tool_choice": {"type": "any"}, "output_config": FORMAT}, True),
        (
            {
                "tools": TOOLS,
                "tool_choice": {"type": "tool", "name": "lookup_catalog"},
                "output_config": FORMAT,
            },
            True,
        ),
        # Without a tool_choice, tools are offered as with auto.
        ({"tools": TOOLS, "output_config": FORMAT}, True),
        ({"tools": TOOLS, "tool_choice": {"type": "none"}, "output_config": FORMAT}, False),
        (
            {"tools": TOOLS, "tool_choice": {"type": "auto"}, "output_config": {"effort": "low"}},
            False,
        ),
        ({"output_config": FORMAT}, False),
    ],
    ids=["auto", "any", "tool", "implicit-auto", "none", "no-schema", "no-tools"],
)
def test_the_guard_names_every_tool_choice_that_lets_a_call_through(
    body: dict[str, Any], combined: bool
) -> None:
    assert lets_a_tool_be_called_with_a_schema(body) is combined


def test_the_scripted_api_rejects_that_request_as_the_provider_did(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A regression fails every router test: the double answers the provider's 400."""
    server = ModelServer(structured(ROUTED))
    server.install(monkeypatch)
    client = anthropic.Anthropic(api_key=API_KEY.get_secret_value(), max_retries=1)

    with pytest.raises(anthropic.BadRequestError) as raised:
        client.messages.create(
            model=MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": "Hola"}],
            tools=[{"name": "lookup_catalog", "input_schema": {"type": "object"}}],
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            output_config={"format": {"type": "json_schema", "schema": {"type": "object"}}},
        )

    assert (raised.value.type, raised.value.api_message) == (
        "invalid_request_error",
        GRAMMAR_TOO_LARGE,
    )
    # The SDK does not retry a 400.
    assert len(server.requests) == 1
