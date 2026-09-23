import json
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
    SoftPreferences,
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
from commerce_agent.llm.conversation_router import (
    MAX_VOCABULARY_BRANDS,
    _candidate_payload,
    _catalog_vocabulary,
    format_minor_amount,
)

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
    # El vocabulario publica marcas, no títulos: con miles de productos la lista completa
    # de títulos ocupaba casi todo el prompt y viajaba en cada turno.
    assert '"brands"' in call["instructions"]
    assert "Audifonos Sonora SON-01" not in call["instructions"]
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


def test_router_turns_ungrounded_purchase_into_clarification(monkeypatch) -> None:
    decision = CommerceDecision(
        action="purchase",
        assistant_message="Procederé con ese artículo.",
        intent=PurchaseIntent(),
    )
    fake = FakeOpenAI(decision)
    monkeypatch.setattr(module, "OpenAI", lambda **_: fake)
    router = OpenAIConversationRouter(
        api_key=SecretStr("openai-test-key"),
        model="test-model",
        feed=FEED,
    )

    result = router.route((ConversationMessage(role="user", content="Sí, ese está bien"),))

    assert result.action == "clarify"
    assert result.intent is None
    assert "qué producto" in result.assistant_message.casefold()


@pytest.mark.parametrize(
    "intent",
    [
        PurchaseIntent(hard=HardConstraints(max_unit_amount_minor=80_000)),
        PurchaseIntent(hard=HardConstraints(conditions=("new",))),
        PurchaseIntent(
            hard=HardConstraints(required_options=(OptionConstraint(name="color", value="black"),))
        ),
        PurchaseIntent(preferences=SoftPreferences(categories=("cellphones",))),
    ],
    ids=("budget-only", "condition-only", "option-only", "unknown-category"),
)
def test_router_requires_product_identity_for_purchase(monkeypatch, intent) -> None:
    decision = CommerceDecision(
        action="purchase",
        assistant_message="Buscaré una opción.",
        intent=intent,
    )
    fake = FakeOpenAI(decision)
    monkeypatch.setattr(module, "OpenAI", lambda **_: fake)
    router = OpenAIConversationRouter(
        api_key=SecretStr("openai-test-key"),
        model="test-model",
        feed=FEED,
    )

    result = router.route((ConversationMessage(role="user", content="Sí"),))

    assert result.action == "clarify"
    assert result.intent is None


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
    intent = PurchaseIntent(
        hard=HardConstraints(categories=("headphones",), required_terms=("sonora",))
    )
    candidates = rank_candidates(FEED, intent).ranked

    result = router.choose_candidate(messages, intent, candidates)

    assert result == decision
    call = fake.responses.calls[0]
    assert call["text_format"] is module.CandidateChoice
    assert "SON-01" in call["input"]
    assert '"preference_score": 0' in call["input"]
    assert "authoritative total is unknown" in call["instructions"]
    assert "Human approval" in call["instructions"]


def test_broad_category_requires_candidate_clarification(monkeypatch) -> None:
    decision = CandidateDecision(
        action="clarify",
        assistant_message=(
            "Tengo audífonos de distintos colores, condiciones y precios. "
            "¿Tienes alguna preferencia o presupuesto?"
        ),
    )
    fake = FakeOpenAI(decision)
    monkeypatch.setattr(module, "OpenAI", lambda **_: fake)
    router = OpenAIConversationRouter(
        api_key=SecretStr("openai-test-key"),
        model="test-model",
        feed=FEED,
    )
    messages = (ConversationMessage(role="user", content="Quiero comprar unos audífonos"),)
    intent = PurchaseIntent(
        hard=HardConstraints(categories=("headphones",)),
        preferences=SoftPreferences(terms=("audífonos",)),
    )
    first = rank_candidates(FEED, intent).ranked[0]
    alternative = first.model_copy(
        update={
            "candidate": first.candidate.model_copy(
                update={
                    "variant_id": "ALT-01",
                    "variant_title": "Audifonos Alterna ALT-01",
                    "observed_unit_amount_minor": 60_000,
                    "options": (("Color", "blue"),),
                }
            )
        }
    )
    candidates = (alternative, first)

    result = router.choose_candidate(messages, intent, candidates)

    assert result == decision
    call = fake.responses.calls[0]
    assert call["text_format"] is module.CandidateClarification
    assert "ask one focused question" in call["instructions"]
    assert "arbitrary color" in call["instructions"]


def test_lowest_price_strategy_allows_candidate_choice(monkeypatch) -> None:
    decision = CandidateDecision(
        action="purchase",
        assistant_message="Elegí la opción de menor precio observado.",
        selected_variant_id="SON-01",
    )
    fake = FakeOpenAI(decision)
    monkeypatch.setattr(module, "OpenAI", lambda **_: fake)
    router = OpenAIConversationRouter(
        api_key=SecretStr("openai-test-key"),
        model="test-model",
        feed=FEED,
    )
    intent = PurchaseIntent(
        hard=HardConstraints(categories=("headphones",)),
        preferences=SoftPreferences(selection_strategy="lowest_observed_price"),
    )
    candidates = rank_candidates(FEED, intent).ranked

    result = router.choose_candidate(
        (ConversationMessage(role="user", content="Dame los más baratos"),),
        intent,
        candidates,
    )

    assert result == decision
    assert fake.responses.calls[0]["text_format"] is module.CandidateChoice


def test_unmatched_preferences_allow_candidate_clarification(monkeypatch) -> None:
    decision = CandidateDecision(
        action="clarify",
        assistant_message="El catálogo no confirma cancelación de ruido.",
    )
    fake = FakeOpenAI(decision)
    monkeypatch.setattr(module, "OpenAI", lambda **_: fake)
    router = OpenAIConversationRouter(
        api_key=SecretStr("openai-test-key"),
        model="test-model",
        feed=FEED,
    )
    messages = (ConversationMessage(role="user", content="Quiero cancelación de ruido"),)
    intent = PurchaseIntent.model_validate({"preferences": {"terms": ["noise-cancelling"]}})
    candidates = rank_candidates(FEED, intent).ranked

    result = router.choose_candidate(messages, intent, candidates)

    assert result == decision
    assert fake.responses.calls[0]["text_format"] is CandidateDecision


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


@pytest.mark.parametrize(
    ("amount_minor", "currency", "expected"),
    [
        (1_129_900_000, "cop", "COP 11.299.000"),
        (899_900_000, "COP", "COP 8.999.000"),
        (54_900_000, "cop", "COP 549.000"),
        (0, "cop", "COP 0"),
        (1_129_900_050, "cop", "COP 11.299.000,50"),
    ],
)
def test_display_amount_never_asks_the_model_to_divide_by_one_hundred(
    amount_minor: int, currency: str, expected: str
) -> None:
    """Con COP los montos tienen nueve dígitos: un error de factor cien parece un precio real."""
    assert format_minor_amount(amount_minor, currency) == expected


def test_candidate_payload_carries_a_quotable_amount_next_to_the_filterable_one() -> None:
    feed = load_product_feed(Path(__file__).parent / "fixtures" / "assistant_feed")
    candidate = feed.candidate("SON-XM6-BLK")
    assert candidate is not None
    payload = _candidate_payload(candidate)
    assert payload["observed_unit_amount_minor"] == 1_899_000_00
    assert payload["display_amount"] == "COP 1.899.000"


def test_catalog_vocabulary_stays_bounded_as_the_catalog_grows() -> None:
    """Viaja en cada turno del router: no puede crecer con el número de productos."""
    feed = load_product_feed(Path(__file__).parent / "fixtures" / "assistant_feed")
    vocabulary = json.loads(_catalog_vocabulary(feed))

    assert set(vocabulary) == {
        "brands",
        "categories",
        "conditions",
        "currencies",
        "options",
        "product_count",
    }
    assert len(vocabulary["brands"]) <= MAX_VOCABULARY_BRANDS
    assert vocabulary["product_count"] == len(feed.available_candidates)
