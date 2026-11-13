import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import SecretStr, ValidationError

from commerce_agent.acp import load_product_feed
from commerce_agent.acp.feed import ProductFeed
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
    ClaudeConversationRouter,
    CommerceDecision,
    ConversationMessage,
    UnknownVariantSelection,
)
from commerce_agent.llm import conversation_router as module
from commerce_agent.llm.catalog_tool import make_catalog_lookup
from commerce_agent.llm.claude import strict_schema
from commerce_agent.llm.conversation_router import (
    MAX_VOCABULARY_BRANDS,
    _candidate_payload,
    _catalog_vocabulary,
    format_minor_amount,
)
from commerce_agent.llm.reasoning import reasoning_listener
from tests.support.model_server import (
    ModelServer,
    function_call,
    output_schema,
    structured,
)

FEED = load_product_feed(Path(__file__).parent / "fixtures" / "feed")


def serve(
    monkeypatch: pytest.MonkeyPatch,
    *decisions: CommerceDecision | CandidateDecision | CatalogRecovery,
) -> ModelServer:
    """One answer per decision, in order."""
    server = ModelServer(*map(structured, decisions))
    server.install(monkeypatch)
    return server


def make_router(**options: Any) -> ClaudeConversationRouter:
    return ClaudeConversationRouter(api_key=SecretStr("sk-ant-test-key"), feed=FEED, **options)


def instructions(body: dict[str, Any]) -> str:
    return body["system"][0]["text"]


def payload(body: dict[str, Any]) -> str:
    """The JSON payload a selection or recovery request sends as its one user message."""
    (message,) = body["messages"]
    assert message["role"] == "user"
    return message["content"]


def variant_schema(body: dict[str, Any]) -> dict[str, Any]:
    """`selected_variant_id` as the request sends it: every property required, API-ready."""
    return output_schema(body)["properties"]["selected_variant_id"]


def test_router_uses_full_conversation_and_structured_decision(monkeypatch) -> None:
    decision = CommerceDecision(
        action="purchase",
        assistant_message="Buscaré una opción.",
        purchase_request="audífonos negros por máximo 800 USD",
        intent=PurchaseIntent(
            hard=HardConstraints(
                currency="USD",
                categories=("headphones",),
                max_total_amount_minor=80_000,
            )
        ),
    )
    server = serve(monkeypatch, decision)
    router = make_router()
    messages = (
        ConversationMessage(role="user", content="Quiero comprar algo"),
        ConversationMessage(role="assistant", content="¿Qué producto buscas?"),
        ConversationMessage(role="user", content="Audífonos negros por máximo 800 USD"),
    )

    result = router.route(messages)
    router.close()

    assert result.intent is not None
    assert result.intent.hard.currency == "USD"
    assert result.model_copy(update={"intent": decision.intent}) == decision
    (call,) = server.bodies()
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["effort"] == "low"
    assert output_schema(call)["title"] == "CommerceDecision"
    # Every property is required, so the schema stays within the API's optional limit.
    assert output_schema(call)["required"] == list(output_schema(call)["properties"])
    assert call["messages"] == [message.model_dump(mode="json") for message in messages]
    # The instructions are the stable prefix, read from the cache on the next turn.
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "clarify" in instructions(call)
    assert "headphones" in instructions(call)
    # El vocabulario publica marcas, no títulos: con miles de productos la lista completa
    # de títulos ocupaba casi todo el prompt y viajaba en cada turno.
    assert '"brands"' in instructions(call)
    assert "Audifonos Sonora SON-01" not in instructions(call)
    assert "never authorizes" in instructions(call).casefold()
    assert call["tools"][0]["name"] == "lookup_catalog"
    assert call["tools"][0]["strict"] is True
    assert call["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    # No summaries asked for: the model thinks, and the answer carries no thinking text.
    assert "thinking" not in call


def test_router_sends_a_leading_assistant_greeting_away(monkeypatch) -> None:
    """The API's first message is the user's: an opening greeting of the agent stays out."""
    decision = CommerceDecision(action="clarify", assistant_message="¿Qué producto buscas?")
    server = serve(monkeypatch, decision)
    messages = (
        ConversationMessage(role="assistant", content="Hola, ¿en qué te ayudo?"),
        ConversationMessage(role="user", content="Quiero comprar algo"),
    )

    make_router().route(messages)

    assert server.bodies()[0]["messages"] == [{"role": "user", "content": "Quiero comprar algo"}]


def test_catalog_tool_is_read_only_and_reports_observed_prices() -> None:
    output = json.loads(make_catalog_lookup(FEED).invoke({"query": "Sonora"}))

    assert output["exact_text_matches"] > 0
    assert all("Sonora" in item["title"] for item in output["products"])
    assert output["prices_are_authoritative"] is False
    assert "checkout_id" not in str(output)


def test_catalog_tool_never_exposes_minor_unit_prices_to_the_model() -> None:
    candidate = FEED.available_candidates[0].model_copy(
        update={"observed_unit_amount_minor": 46_000_000, "currency": "COP"}
    )
    feed = cast(ProductFeed, SimpleNamespace(available_candidates=(candidate,)))

    output = json.loads(make_catalog_lookup(feed).invoke({"query": candidate.product_title}))

    assert output["products"][0]["display_amount"] == "COP 460.000"
    assert "observed_unit_amount_minor" not in output["products"][0]
    assert "46000000" not in json.dumps(output)


def test_catalog_tool_ranks_natural_spanish_query_without_claiming_exact_match() -> None:
    output = json.loads(
        make_catalog_lookup(FEED).invoke({"query": "¿Qué audífonos Sonora tienes?"})
    )

    assert output["sampled_fallback"] is False
    assert output["partial_matches"] > 0
    assert output["exact_text_matches"] == 0
    assert output["products"][0]["variant_id"] == "SON-01"


def test_catalog_tool_marks_unmatched_examples_as_fallback() -> None:
    output = json.loads(make_catalog_lookup(FEED).invoke({"query": "televisores"}))

    assert output["exact_text_matches"] == 0
    assert output["partial_matches"] == 0
    assert output["sampled_fallback"] is True


def test_catalog_summary_stays_bounded_for_large_feeds() -> None:
    original = FEED.available_candidates[0]
    candidates = tuple(
        original.model_copy(
            update={"variant_id": f"variant-{index}", "product_title": f"Product {index:05d}"}
        )
        for index in range(2_000)
    )
    feed = cast(ProductFeed, SimpleNamespace(available_candidates=candidates))

    summary = json.loads(module._catalog_vocabulary(feed))

    assert summary["sampled"] is True
    assert summary["product_count"] == 2_000
    assert "product_titles" not in summary
    assert len(json.dumps(summary)) < 10_000


def test_router_executes_one_model_requested_catalog_lookup(monkeypatch) -> None:
    decision = CommerceDecision(action="respond", assistant_message="Tengo audífonos Sonora.")
    server = ModelServer(function_call("lookup_catalog", {"query": "Sonora"}), structured(decision))
    server.install(monkeypatch)
    question = ConversationMessage(role="user", content="¿Qué Sonora tienes?")

    result = make_router().route((question,))

    assert result == decision
    first, second = server.bodies()
    assert second["tool_choice"] == {"type": "none"}
    assert second["tools"] == first["tools"]
    asked, answer = second["messages"][-2:]
    assert second["messages"][0] == question.model_dump(mode="json")
    # The model's answer goes back whole, then the tool's output.
    assert asked == {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_test",
                "name": "lookup_catalog",
                "input": {"query": "Sonora"},
            }
        ],
    }
    (output,) = answer["content"]
    assert answer["role"] == "user"
    assert output["type"] == "tool_result"
    assert output["tool_use_id"] == "toolu_test"
    assert "SON-01" in output["content"]
    assert "checkout_id" not in output["content"]


def test_reasoning_summaries_keep_the_catalog_tool_available(monkeypatch) -> None:
    decision = CommerceDecision(action="respond", assistant_message="Tengo Sonora SON-01.")
    server = ModelServer(
        function_call("lookup_catalog", {"query": "Sonora"}),
        structured(decision, thinking=("Busco Sonora en el catálogo.", "SON-01 coincide.")),
    )
    server.install(monkeypatch)
    router = make_router(reasoning_summary=True)
    summaries: list[str] = []

    with reasoning_listener(summaries.append):
        result = router.route((ConversationMessage(role="user", content="¿Qué Sonora tienes?"),))

    assert result == decision
    first, second = server.bodies()
    assert first["thinking"] == second["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert first["tools"][0]["name"] == "lookup_catalog"
    assert first["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert second["tool_choice"] == {"type": "none"}
    assert "SON-01" in second["messages"][-1]["content"][0]["content"]
    assert summaries == ["Busco Sonora en el catálogo.", "\n\nSON-01 coincide."]


def test_reasoning_summaries_need_a_listener(monkeypatch) -> None:
    decision = CommerceDecision(action="respond", assistant_message="Hola.")
    server = serve(monkeypatch, decision)

    make_router(reasoning_summary=True).route((ConversationMessage(role="user", content="Hola"),))

    assert "thinking" not in server.bodies()[0]


def test_the_model_and_effort_are_configurable(monkeypatch) -> None:
    decision = CommerceDecision(action="respond", assistant_message="Hola.")
    server = serve(monkeypatch, decision)

    make_router(model="claude-sonnet-5-5", effort="medium").route(
        (ConversationMessage(role="user", content="Hola"),)
    )

    (call,) = server.bodies()
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_config"]["effort"] == "medium"


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
    serve(monkeypatch, decision)
    router = make_router()

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
    serve(monkeypatch, decision)
    router = make_router()

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
    serve(monkeypatch, decision)
    router = make_router()

    result = router.route((ConversationMessage(role="user", content="Sí"),))

    assert result.action == "clarify"
    assert result.intent is None


def test_router_chooses_from_deterministically_ranked_candidates(monkeypatch) -> None:
    decision = CandidateDecision(
        action="purchase",
        assistant_message="Elegí Sonora porque coincide con tu marca preferida.",
        selected_variant_id="SON-01",
    )
    server = serve(monkeypatch, decision)
    router = make_router()
    messages = (ConversationMessage(role="user", content="Quiero audífonos Sonora"),)
    intent = PurchaseIntent(
        hard=HardConstraints(categories=("headphones",), required_terms=("sonora",))
    )
    candidates = rank_candidates(FEED, intent).ranked

    result = router.choose_candidate(messages, intent, candidates)

    assert result == decision
    (call,) = server.bodies()
    assert output_schema(call)["title"] == "CandidateChoice"
    assert "SON-01" in payload(call)
    assert '"preference_score": 0' in payload(call)
    assert "authoritative total is unknown" in instructions(call)
    assert "Human approval" in instructions(call)


def test_router_names_the_variant_chosen_outside_the_shortlist(monkeypatch) -> None:
    decision = CandidateDecision(
        action="purchase",
        assistant_message="Elegí los Sonora blancos.",
        selected_variant_id="SON-07",
    )
    serve(monkeypatch, decision)
    router = make_router()
    intent = PurchaseIntent(
        hard=HardConstraints(categories=("headphones",), required_terms=("sonora",))
    )
    messages = (ConversationMessage(role="user", content="Quiero audífonos Sonora"),)

    with pytest.raises(UnknownVariantSelection) as raised:
        router.choose_candidate(messages, intent, rank_candidates(FEED, intent).ranked)

    assert raised.value.variant_id == "SON-07"
    # Same code and text as before, so B0 still answers 502 intent_model_error.
    assert raised.value.code == "intent_model_error"
    assert str(raised.value) == "The candidate model selected an unknown variant"


def test_a_single_candidate_choice_can_only_name_the_shortlisted_variant(monkeypatch) -> None:
    # S02: one candidate, so the action is forced to purchase. Without the enum the model
    # escaped by naming a variant that was not in the shortlist.
    decision = CandidateDecision(
        action="purchase", assistant_message="Elegí Sonora.", selected_variant_id="SON-01"
    )
    server = serve(monkeypatch, decision)
    router = make_router()
    intent = PurchaseIntent(
        hard=HardConstraints(categories=("headphones",), required_terms=("sonora",))
    )
    candidates = rank_candidates(FEED, intent).ranked
    assert len(candidates) == 1

    router.choose_candidate(
        (ConversationMessage(role="user", content="Quiero audífonos Sonora"),), intent, candidates
    )

    (call,) = server.bodies()
    assert output_schema(call)["title"] == "CandidateChoice"
    assert variant_schema(call) == {"enum": ["SON-01"], "title": "Selected Variant Id"}
    # The enum shapes only what the model can decode; validation still reaches the router's
    # own check, so an outside id keeps raising UnknownVariantSelection.
    restricted = module._restricted_to_shortlist(module.CandidateChoice, ("SON-01",))
    outside = {"action": "purchase", "assistant_message": "x", "selected_variant_id": "SON-07"}
    assert restricted.model_validate(outside).selected_variant_id == "SON-07"


def test_a_decision_that_may_clarify_names_a_shortlisted_variant_or_none(monkeypatch) -> None:
    decision = CandidateDecision(action="clarify", assistant_message="¿Te sirve otra opción?")
    server = serve(monkeypatch, decision)
    router = make_router()
    # A budget is a selection signal, and the unmatched preference allows a clarification.
    intent = PurchaseIntent.model_validate(
        {
            "hard": {"max_unit_amount_minor": 10_000_000},
            "preferences": {"terms": ["noise-cancelling"]},
        }
    )
    (first,) = rank_candidates(FEED, intent).ranked
    alternative = first.model_copy(
        update={"candidate": first.candidate.model_copy(update={"variant_id": "ALT-01"})}
    )
    candidates = (first, alternative, first)  # A repeated ID is offered once.
    shortlist = ["SON-01", "ALT-01"]

    router.choose_candidate(
        (ConversationMessage(role="user", content="Quiero cancelación de ruido"),),
        intent,
        candidates,
    )

    (call,) = server.bodies()
    assert output_schema(call)["title"] == "CandidateDecision"
    assert variant_schema(call)["anyOf"] == [{"enum": shortlist}, {"type": "null"}]
    restricted = module._restricted_to_shortlist(module.CandidateDecision, tuple(shortlist))
    with pytest.raises(ValidationError):
        restricted.model_validate({"action": "purchase", "assistant_message": "x"})


def test_each_selection_restricts_the_schema_to_its_own_shortlist(monkeypatch) -> None:
    decision = CandidateDecision(
        action="purchase", assistant_message="Elegí.", selected_variant_id="SON-01"
    )
    server = serve(monkeypatch, decision, decision)
    router = make_router()
    intent = PurchaseIntent(
        hard=HardConstraints(categories=("headphones",)),
        preferences=SoftPreferences(selection_strategy="lowest_observed_price"),
    )
    candidates = rank_candidates(FEED, intent).ranked
    messages = (ConversationMessage(role="user", content="Dame los más baratos"),)

    router.choose_candidate(messages, intent, candidates)
    router.choose_candidate(messages, intent, candidates[:1])

    first, second = server.bodies()
    assert variant_schema(first)["enum"] == [item.candidate.variant_id for item in candidates]
    assert variant_schema(second)["enum"] == [candidates[0].candidate.variant_id]
    # The shared models stay unrestricted.
    assert "enum" not in strict_schema(module.CandidateChoice)["properties"]["selected_variant_id"]


def test_broad_category_requires_candidate_clarification(monkeypatch) -> None:
    decision = CandidateDecision(
        action="clarify",
        assistant_message=(
            "Tengo audífonos de distintos colores, condiciones y precios. "
            "¿Tienes alguna preferencia o presupuesto?"
        ),
    )
    server = serve(monkeypatch, decision)
    router = make_router()
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
    (call,) = server.bodies()
    assert output_schema(call)["title"] == "CandidateClarification"
    assert "ask one focused question" in instructions(call)
    assert "arbitrary color" in instructions(call)


def test_lowest_price_strategy_allows_candidate_choice(monkeypatch) -> None:
    decision = CandidateDecision(
        action="purchase",
        assistant_message="Elegí la opción de menor precio observado.",
        selected_variant_id="SON-01",
    )
    server = serve(monkeypatch, decision)
    router = make_router()
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
    assert output_schema(server.bodies()[0])["title"] == "CandidateChoice"


def test_unmatched_preferences_allow_candidate_clarification(monkeypatch) -> None:
    decision = CandidateDecision(
        action="clarify",
        assistant_message="El catálogo no confirma cancelación de ruido.",
    )
    server = serve(monkeypatch, decision)
    router = make_router()
    messages = (ConversationMessage(role="user", content="Quiero cancelación de ruido"),)
    intent = PurchaseIntent.model_validate({"preferences": {"terms": ["noise-cancelling"]}})
    candidates = rank_candidates(FEED, intent).ranked

    result = router.choose_candidate(messages, intent, candidates)

    assert result == decision
    assert output_schema(server.bodies()[0])["title"] == "CandidateDecision"


def test_router_explains_real_alternatives_when_no_candidate_matches(monkeypatch) -> None:
    decision = CatalogRecovery(
        action="clarify",
        assistant_message="No hay opciones en ese presupuesto; tengo Sonora por 720 USD.",
    )
    server = serve(monkeypatch, decision)
    router = make_router()
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
    (call,) = server.bodies()
    assert output_schema(call)["title"] == "CatalogRecovery"
    assert '"budget": 1' in payload(call)
    assert "SON-01" in payload(call)


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


def test_candidate_payload_exposes_only_the_quotable_amount() -> None:
    feed = load_product_feed(Path(__file__).parent / "fixtures" / "assistant_feed")
    candidate = feed.candidate("SON-XM6-BLK")
    assert candidate is not None
    payload = _candidate_payload(candidate)
    assert payload["display_amount"] == "COP 1.899.000"
    assert "observed_unit_amount_minor" not in payload


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
        "sampled",
    }
    assert len(vocabulary["brands"]) <= MAX_VOCABULARY_BRANDS
    assert vocabulary["product_count"] == len(feed.available_candidates)
