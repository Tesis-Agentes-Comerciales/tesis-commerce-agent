from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.llm import CommerceDecision, ConversationMessage, OpenAIConversationRouter
from commerce_agent.llm import conversation_router as module


class FakeResponses:
    def __init__(self, decision: CommerceDecision) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.decision)


class FakeOpenAI:
    def __init__(self, decision: CommerceDecision) -> None:
        self.responses = FakeResponses(decision)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_router_uses_full_conversation_and_structured_decision(monkeypatch) -> None:
    decision = CommerceDecision(
        action="purchase",
        assistant_message="Buscaré una opción.",
        purchase_request="audífonos negros por máximo 800 USD",
        intent=PurchaseIntent(
            hard=HardConstraints(
                categories=("headphones",),
                max_total_amount_minor=80_000,
            )
        ),
    )
    fake = FakeOpenAI(decision)
    monkeypatch.setattr(module, "OpenAI", lambda **_: fake)
    router = OpenAIConversationRouter(
        api_key=SecretStr("openai-test-key"),
        model="test-model",
    )
    messages = (
        ConversationMessage(role="user", content="Quiero comprar algo"),
        ConversationMessage(role="assistant", content="¿Qué producto buscas?"),
        ConversationMessage(role="user", content="Audífonos negros por máximo 800 USD"),
    )

    result = router.route(messages)
    router.close()

    assert result == decision
    call = fake.responses.calls[0]
    assert call["text_format"] is CommerceDecision
    assert call["input"] == [message.model_dump(mode="json") for message in messages]
    assert call["store"] is False
    assert "clarify" in call["instructions"]
    assert "never authorizes" in call["instructions"].casefold()
    assert fake.closed


def test_non_purchase_decision_rejects_purchase_payload() -> None:
    with pytest.raises(ValidationError):
        CommerceDecision(
            action="respond",
            assistant_message="Hola",
            purchase_request="audífonos",
            intent=PurchaseIntent(),
        )


def test_purchase_decision_allows_model_to_omit_consolidated_request() -> None:
    decision = CommerceDecision(
        action="purchase",
        assistant_message="Buscaré una opción.",
        intent=PurchaseIntent(hard=HardConstraints(categories=("headphones",))),
    )

    assert decision.purchase_request is None
