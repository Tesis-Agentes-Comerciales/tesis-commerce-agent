from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from commerce_agent.acp import load_product_feed
from commerce_agent.domain import (
    HardConstraints,
    OptionConstraint,
    PurchaseIntent,
    rank_candidates,
)
from commerce_agent.llm import (
    CandidateDecision,
    CatalogRecovery,
    CommerceDecision,
    ConversationMessage,
    OpenAIConversationRouter,
)
from commerce_agent.llm import conversation_router as module

FEED = load_product_feed(Path(__file__).parent / "fixtures" / "feed")


class FakeResponses:
    def __init__(self, decision: CommerceDecision | CandidateDecision | CatalogRecovery) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.decision)


class FakeOpenAI:
    def __init__(self, decision: CommerceDecision | CandidateDecision | CatalogRecovery) -> None:
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
        feed=FEED,
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
    assert "headphones" in call["instructions"]
    assert "Audifonos Sonora SON-01" in call["instructions"]
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


def test_router_moves_unknown_catalog_attributes_to_preferences(monkeypatch) -> None:
    decision = CommerceDecision(
        action="purchase",
        assistant_message="Buscaré una opción.",
        intent=PurchaseIntent(
            hard=HardConstraints(
                categories=("headphones",),
                required_options=(OptionConstraint(name="color", value="blue"),),
                required_terms=("wireless", "noise-cancelling"),
            )
        ),
    )
    fake = FakeOpenAI(decision)
    monkeypatch.setattr(module, "OpenAI", lambda **_: fake)
    router = OpenAIConversationRouter(
        api_key=SecretStr("openai-test-key"),
        model="test-model",
        feed=FEED,
    )

    result = router.route((ConversationMessage(role="user", content="Quiero audífonos"),))

    assert result.intent is not None
    assert result.intent.hard.categories == ("headphones",)
    assert result.intent.hard.required_options == ()
    assert result.intent.hard.required_terms == ()
    assert result.intent.preferences.options == (OptionConstraint(name="color", value="blue"),)
    assert result.intent.preferences.terms == ("wireless", "noise-cancelling")


def test_router_chooses_from_deterministically_ranked_candidates(monkeypatch) -> None:
    decision = CandidateDecision(
        action="purchase",
        assistant_message="Elegí Sonora porque coincide con tu marca preferida.",
        selected_variant_id="SON-01",
    )
    fake = FakeOpenAI(decision)
    monkeypatch.setattr(module, "OpenAI", lambda **_: fake)
    router = OpenAIConversationRouter(
        api_key=SecretStr("openai-test-key"),
        model="test-model",
        feed=FEED,
    )
    messages = (ConversationMessage(role="user", content="Quiero audífonos Sonora"),)
    intent = PurchaseIntent(hard=HardConstraints(categories=("headphones",)))
    candidates = rank_candidates(FEED, intent).ranked

    result = router.choose_candidate(messages, intent, candidates)

    assert result == decision
    call = fake.responses.calls[0]
    assert call["text_format"] is CandidateDecision
    assert "SON-01" in call["input"]
    assert '"preference_score": 0' in call["input"]
    assert "authoritative total is unknown" in call["instructions"]
    assert "Human approval" in call["instructions"]


def test_router_explains_real_alternatives_when_no_candidate_matches(monkeypatch) -> None:
    decision = CatalogRecovery(
        action="clarify",
        assistant_message="No hay opciones en ese presupuesto; tengo Sonora por 720 USD.",
    )
    fake = FakeOpenAI(decision)
    monkeypatch.setattr(module, "OpenAI", lambda **_: fake)
    router = OpenAIConversationRouter(
        api_key=SecretStr("openai-test-key"),
        model="test-model",
        feed=FEED,
    )
    messages = (ConversationMessage(role="user", content="Quiero audífonos por un centavo"),)
    intent = PurchaseIntent(hard=HardConstraints(max_unit_amount_minor=1))
    selection = rank_candidates(FEED, intent)

    result = router.recover_no_candidate(
        messages,
        intent,
        selection.rejection_counts,
        FEED.available_candidates,
    )

    assert result == decision
    call = fake.responses.calls[0]
    assert call["text_format"] is CatalogRecovery
    assert '"budget": 1' in call["input"]
    assert "SON-01" in call["input"]
