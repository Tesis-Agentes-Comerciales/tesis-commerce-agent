"""The agent's strict schemas stay within the API's complexity limits.

The Messages API counts, across every strict schema of one request (its output format and its
strict tools), at most 24 optional parameters and 16 parameters with a union type (`anyOf` or a
type array). Past them the request is a 400 before the model runs.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel

from commerce_agent.llm import CatalogRecovery, CommerceDecision, ConversationMessage
from commerce_agent.llm.claude import strict_schema
from commerce_agent.llm.conversation_router import (
    CandidateChoice,
    CandidateClarification,
    CandidateDecision,
)
from commerce_agent.llm.intent_review import IntentVerdict
from tests.support.model_server import ModelServer, structured
from tests.test_conversation_router import make_router

MAX_OPTIONAL = 24
MAX_UNIONS = 16


def _nodes(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, list):
        for item in node:
            yield from _nodes(item)
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _nodes(value)


def budget(*schemas: dict[str, Any]) -> tuple[int, int]:
    """Optional parameters and parameters with a union type, across `schemas`."""
    nodes = [node for schema in schemas for node in _nodes(schema)]
    optional = sum(
        len(set(node["properties"]) - set(node.get("required", ())))
        for node in nodes
        if node.get("type") == "object" and isinstance(node.get("properties"), dict)
    )
    unions = sum("anyOf" in node or isinstance(node.get("type"), list) for node in nodes)
    return optional, unions


def test_the_route_request_stays_within_the_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = CommerceDecision(action="respond", assistant_message="Hola.")
    server = ModelServer(structured(decision))
    server.install(monkeypatch)

    make_router().route((ConversationMessage(role="user", content="Hola"),))

    (body,) = server.bodies()
    schemas = [body["output_config"]["format"]["schema"]]
    schemas += [tool["input_schema"] for tool in body["tools"] if tool.get("strict")]
    optional, unions = budget(*schemas)
    # Every property is required; the nullable ones are the unions.
    assert optional == 0
    assert unions < MAX_UNIONS


@pytest.mark.parametrize(
    "output",
    [CandidateChoice, CandidateDecision, CandidateClarification, CatalogRecovery, IntentVerdict],
)
def test_each_other_output_stays_within_the_limits(output: type[BaseModel]) -> None:
    optional, unions = budget(strict_schema(output))
    assert optional == 0
    assert unions < MAX_UNIONS
