"""What a Human Present turn says when the model fails (docs/design/p0-model-failures.md §3.4).

REST keeps its 502 `intent_model_error` and its message, and names the `model_failure`; the chat,
which is what the buyer reads, explains a rate limit, a spent quota and an unreachable model with
their own copy. The real SDK over the scripted transport of tests/support/model_server.py, real
SQLite in tmp_path and the merchant double: no model is called and nothing is prepared.
"""

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from commerce_agent.api import create_app
from commerce_agent.assistant_transport import AssistantMessageRecorder
from commerce_agent.llm import CommerceDecision
from commerce_agent.llm.model_failures import ModelFailureKind
from tests.assistant_support import (
    PURCHASE_REQUEST,
    Merchant,
    add_message,
    assistant_body,
    checkout_details,
    counter_ids,
    post_assistant,
    purchase_intent,
    settings,
)
from tests.support.model_server import Answer, ModelServer, disconnect, failure, structured

API_KEY = "sk-test-surfaces-0123456789abcdef"
CONVERSATION_FAILED = "The conversation model request failed"
# The chat's copy before this change, which every other kind keeps.
CURRENT_COPY = "No pude interpretar la solicitud con el modelo en este momento."
RATE_LIMITED_COPY = (
    "El modelo está recibiendo demasiadas solicitudes. Intenta de nuevo en un momento. "
    "No se preparó ninguna compra."
)
QUOTA_COPY = (
    "El asistente no está disponible por ahora y reintentar no servirá hasta que se restablezca "
    "el servicio. No se preparó ninguna compra."
)
UNREACHABLE_COPY = (
    "No pude comunicarme con el modelo. Intenta de nuevo en un momento. "
    "No se preparó ninguna compra."
)
# Each failure of the interpretation, the HTTP attempts it costs and the chat's copy.
FAILURES: dict[str, tuple[tuple[Answer, ...], int, str]] = {
    "rate_limited": ((failure(429), failure(429)), 2, RATE_LIMITED_COPY),
    "quota_exhausted": ((failure(429, code="insufficient_quota"),), 1, QUOTA_COPY),
    "unreachable": ((disconnect(), disconnect()), 2, UNREACHABLE_COPY),
    "provider_error": ((failure(500), failure(500)), 2, CURRENT_COPY),
}
ROUTED = CommerceDecision(
    action="purchase",
    assistant_message="Buscaré audífonos negros dentro de tu presupuesto.",
    purchase_request=PURCHASE_REQUEST,
    intent=purchase_intent(2_000_000_00, black=True),
)


def app(database: Path, merchant: Merchant, *, model: bool = True) -> FastAPI:
    """The runtime's own OpenAI router, or none at all without `model`."""
    configured = settings(database)
    if model:
        configured = configured.model_copy(
            update={"openai_api_key": SecretStr(API_KEY), "openai_model": "test-model"}
        )
    return create_app(
        settings=configured,
        transport=merchant.transport(),
        payment_transport=httpx.MockTransport(
            lambda request: httpx.Response(201, json={"id": "vt_" + "a" * 64})
        ),
        id_factory=counter_ids(),
    )


def turn(client: TestClient) -> tuple[int, Any]:
    """The REST turn's status and body."""
    response = client.post(
        "/conversation/turn",
        json={
            "turn_id": "turn-model",
            "messages": [{"role": "user", "content": PURCHASE_REQUEST}],
            "details": checkout_details(),
        },
    )
    return response.status_code, response.json()


def chat(client: TestClient) -> dict[str, Any]:
    _, state = post_assistant(
        client, assistant_body([add_message(PURCHASE_REQUEST)], turn_id="turn-model")
    )
    return state


def steps(message: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {part["toolName"]: part for part in message["content"] if part["type"] == "tool-call"}


def checkouts(merchant: Merchant) -> int:
    return merchant.requests.count("POST /checkout_sessions")


# REST: same code and status, plus the kind


@pytest.mark.parametrize("kind", FAILURES)
def test_rest_keeps_its_502_and_names_the_model_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    answers, attempts, _ = FAILURES[kind]
    server = ModelServer(*answers)
    server.install(monkeypatch)
    merchant = Merchant()

    with TestClient(app(tmp_path / "agent.db", merchant)) as client:
        answered = turn(client)

    assert answered == (
        502,
        {"code": "intent_model_error", "message": CONVERSATION_FAILED, "model_failure": kind},
    )
    assert len(server.requests) == attempts
    assert checkouts(merchant) == 0


def test_rest_names_no_model_failure_when_the_model_was_not_the_cause(tmp_path: Path) -> None:
    """The graph raises the same error without a model configured: `model_failure` is null, so
    a runner tells it apart from a model that failed."""
    merchant = Merchant()

    with TestClient(app(tmp_path / "agent.db", merchant, model=False)) as client:
        answered = turn(client)

    assert answered == (
        502,
        {
            "code": "intent_model_error",
            "message": "No conversation model is configured",
            "model_failure": None,
        },
    )
    assert checkouts(merchant) == 0


# Chat: the buyer reads the cause


@pytest.mark.parametrize("kind", FAILURES)
def test_the_chat_explains_the_model_failure_and_prepares_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    answers, attempts, copy = FAILURES[kind]
    server = ModelServer(*answers)
    server.install(monkeypatch)
    merchant = Merchant()

    with TestClient(app(tmp_path / "agent.db", merchant)) as client:
        state = chat(client)

    message = state["messages"][-1]
    assert message["status"] == {"type": "incomplete", "reason": "error", "error": copy}
    interpret = steps(message)["interpret_request"]
    assert (interpret["isError"], interpret["result"]) == (
        True,
        {"error": {"code": "intent_model_error", "message": copy}},
    )
    assert tuple(steps(message)) == ("interpret_request",)
    assert state["purchase"] is None
    assert len(server.requests) == attempts
    assert checkouts(merchant) == 0


def test_a_selection_that_cannot_reach_the_model_says_so_in_its_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The interpretation answers and the connection drops in `choose_candidate`."""
    server = ModelServer(structured(ROUTED), disconnect(), disconnect())
    server.install(monkeypatch)
    merchant = Merchant()

    with TestClient(app(tmp_path / "agent.db", merchant)) as client:
        state = chat(client)

    message = state["messages"][-1]
    assert message["status"]["error"] == UNREACHABLE_COPY
    found = steps(message)
    assert tuple(found) == ("interpret_request", "search_catalog", "select_product")
    assert found["select_product"]["result"] == {
        "error": {"code": "intent_model_error", "message": UNREACHABLE_COPY}
    }
    assert state["purchase"] is None
    assert len(server.requests) == 3
    assert checkouts(merchant) == 0


CHAT_COPY: dict[ModelFailureKind | None, str] = {
    "rate_limited": RATE_LIMITED_COPY,
    "quota_exhausted": QUOTA_COPY,
    "unreachable": UNREACHABLE_COPY,
    "provider_error": CURRENT_COPY,
    "invalid_output": CURRENT_COPY,
    "rejected": CURRENT_COPY,
    None: CURRENT_COPY,
}


@pytest.mark.parametrize("kind", CHAT_COPY, ids=str)
def test_only_three_kinds_change_the_chat_copy(kind: ModelFailureKind | None) -> None:
    copy = CHAT_COPY[kind]
    recorder = AssistantMessageRecorder(index=0, new_id=counter_ids(), emit=lambda operations: None)
    recorder.step_started("interpret_request", {})

    recorder.fail("intent_model_error", kind)

    assert recorder.message is not None
    assert recorder.message["status"] == {"type": "incomplete", "reason": "error", "error": copy}
    assert recorder.message["content"][0]["result"] == {
        "error": {"code": "intent_model_error", "message": copy}
    }
