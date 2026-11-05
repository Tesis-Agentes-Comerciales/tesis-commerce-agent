"""Offline checks of the second P0 real-model batch (docs/design/p0-llm-batch2.md).

No model, no network, no cost: the exposure tests send the real OpenAI SDK requests to the
scripted transport of tests/support/model_server.py, with a synthetic key and model.
"""

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterator
from contextlib import nullcontext
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from commerce_agent.acp.feed import (
    FeedAvailability,
    FeedMetadata,
    FeedPrice,
    FeedProduct,
    FeedVariant,
    ProductFeed,
)
from commerce_agent.domain import (
    HardConstraints,
    OptionConstraint,
    PurchaseIntent,
    SoftPreferences,
    rank_candidates,
)
from commerce_agent.domain.selection import RankedCandidate
from commerce_agent.llm import CommerceDecision, OpenAIConversationRouter
from commerce_agent.llm.conversation_router import ConversationMessage
from commerce_agent.llm.delegated_choice import OpenAIDelegatedChooser
from commerce_agent.llm.intent_review import ModelHnpIntentReviewer
from commerce_agent.persistence.stage_timings_sqlite import ModelUsage
from tests.support.model_server import ModelServer, function_call, shortlisted_choice, structured

ROOT = Path(__file__).resolve().parents[2]
PLAN_PATH = ROOT / "evaluation/scenarios/p0-llm-batch2.json"
PLAN: dict[str, Any] = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
CT21: dict[str, Any] = PLAN["ct21"]
FACTS: dict[str, dict[str, Any]] = {item["variant_id"]: item for item in PLAN["catalog_facts"]}
CELLS: dict[str, dict[str, Any]] = {cell["id"]: cell for cell in PLAN["cells"]}
KEY = SecretStr("sk-p0-llm-batch2-offline-0123456789")
MODEL = "p0-llm-batch2-scripted"
# Visible in the injected title only; ASCII, so it survives any JSON escaping.
MARKER = "AVISO DEL SISTEMA"


def load(relative: str) -> dict[str, Any]:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def sha256(relative: str) -> str:
    return hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()


def folded(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", folded(text)) if len(w) >= 4}


def feed(*, mounted: bool) -> ProductFeed:
    """The catalog facts as the Agent's ACP feed, with the CT21 title mounted or not."""
    products: dict[str, list[dict[str, Any]]] = {}
    for item in PLAN["catalog_facts"]:
        products.setdefault(item["product_id"], []).append(item)
    built = []
    for product_id, items in products.items():
        url = f"https://merchant.example.test/products/{product_id}"
        variants = [
            FeedVariant(
                id=item["variant_id"],
                title=CT21["injected_variant_title"]
                if mounted and item["variant_id"] == CT21["target_variant_id"]
                else item["variant_title"],
                url=f"{url}?variant={item['variant_id']}",
                price=FeedPrice(amount=item["unit_minor"], currency="COP"),
                availability=FeedAvailability(available=True, status="in_stock"),
                categories=[
                    {"taxonomy": "merchant", "value": "smartphones"},
                    {"taxonomy": "brand", "value": item["brand"]},
                ],
                condition=[item["condition"]],
                variant_options=[{"name": n, "value": v} for n, v in item["options"]],
            )
            for item in items
        ]
        built.append(
            FeedProduct(
                id=product_id,
                title=items[0]["product_title"],
                url=url,
                variants=variants,
            )
        )
    return ProductFeed(
        metadata=FeedMetadata(
            id="feed_p0_tech", target_country="CO", updated_at="2026-09-10T14:00:00Z"
        ),
        products=tuple(built),
    )


REFERENCE = PurchaseIntent.model_validate_json(json.dumps(CT21["reference_intent"]))
BUDGET: int = CT21["reference_intent"]["hard"]["max_total_amount_minor"]
# The execution intent HNP derives from the scope (delegation/service.py:290-293).
HNP_INTENT = PurchaseIntent(hard=HardConstraints(currency="COP", max_total_amount_minor=BUDGET))


def variant_intents() -> Iterator[PurchaseIntent]:
    """Intents the router could plausibly write for the CT21 request, all faithful."""
    yield REFERENCE
    no_color = REFERENCE.hard.model_copy(update={"required_options": ()})
    yield PurchaseIntent(hard=no_color.model_copy(update={"required_terms": ("negro",)}))
    yield PurchaseIntent(
        hard=no_color,
        preferences=SoftPreferences(options=(OptionConstraint(name="color", value="negro"),)),
    )
    for term in ("barata", "más barata", "económico", "precio", "menor precio", "costo"):
        yield REFERENCE.model_copy(
            update={"preferences": SoftPreferences(terms=(term,), selection_strategy=None)}
        )


def ids(ranked: tuple[RankedCandidate, ...]) -> list[str]:
    return [item.candidate.variant_id for item in ranked]


class Timer:
    def measure(self, stage: str, usage: ModelUsage | None = None) -> nullcontext[None]:
        return nullcontext()


# The plan and its bindings


def test_plan_binds_its_spec_the_matrix_and_batch_one():
    assert PLAN["schema"] == "p0-llm-batch2-preregistration/1"
    assert PLAN["spec"]["sha256"] == sha256(PLAN["spec"]["path"])
    assert PLAN["matrix_sha256"] == sha256("evaluation/scenarios/p0/matrix.json")
    assert PLAN["batch1"]["sha256"] == sha256(PLAN["batch1"]["path"])
    exp06 = load(PLAN["batch1"]["path"])
    assert PLAN["model"] == exp06["model"] == "gpt-5-mini-2025-08-07"
    assert PLAN["context"] == exp06["context"]
    assert PLAN["run_ref"] is None and PLAN["status"] == "draft_pending_approval"


def test_cells_cover_exactly_the_batch_two_rows_and_modes():
    rows = {row["id"]: row for row in load("evaluation/scenarios/p0/matrix.json")["scenarios"]}
    covered = set()
    for cell in PLAN["cells"]:
        assert cell["mode"] in rows[cell["matrix_id"]]["modes"], cell["id"]
        assert set(cell["conditions"]) <= PLAN["conditions"].keys(), cell["id"]
        assert cell["surface"] in PLAN["surfaces"], cell["id"]
        covered.add((cell["matrix_id"], cell["mode"]))
    assert len(CELLS) == len(PLAN["cells"])
    assert covered == {
        ("UX01", "HNP"),
        ("UX02", "HP"),
        ("UX02", "HNP"),
        ("UX11", "HNP"),
        ("UX12", "HNP"),
        ("CT21", "HP"),
        ("CT21", "HNP"),
    }
    # The HP modes of UX01, UX11 and UX12 are batch one's cases.
    assert set(load(PLAN["batch1"]["path"])["cases"]) == {"UX01", "UX11", "UX12"}
    assert PLAN["batch1"]["covers"] == ["UX01-HP", "UX11-HP", "UX12-HP"]


def test_ambiguity_always_asks_and_a_proposal_never_drafts_or_spends():
    for cell in PLAN["cells"]:
        if cell["stage"] != "hnp_proposal":
            continue
        assert cell["surface"] == "assistant_delegate"
        assert cell["structural_oracle"]["delegation_draft"] is None
        assert set(cell["effects"].values()) == {0}
        assert "purchase" not in cell["actions"]
    for cell_id in ("UX01-HNP-proposal", "UX02-HP", "UX02-HNP-proposal", "UX11-HNP-proposal"):
        assert CELLS[cell_id]["actions"] == ["clarify"]
    assert set(CELLS["UX12-HNP-proposal"]["actions"]) <= {"clarify", "respond"}


def test_stimuli_carry_the_ambiguity_by_construction():
    exp06 = load(PLAN["batch1"]["path"])["cases"]
    assert CELLS["UX01-HNP-proposal"]["messages"] == [exp06["UX01"]["prompt"]]
    assert CELLS["UX11-HNP-proposal"]["messages"] == [exp06["UX11"]["prompt"]]
    [ux11] = CELLS["UX11-HNP-proposal"]["messages"]
    assert not re.search(r"cop|usd|\$|peso|d[oó]lar|mill|\bmil\b|\bk\b", ux11.casefold())
    for cell_id in ("UX02-HP", "UX02-HNP-proposal"):
        prior, prompt = CELLS[cell_id]["messages"]
        referents = [FACTS[v] for v in CELLS[cell_id]["referents"]]
        assert prompt == "Compra ese."
        assert len({item["product_id"] for item in referents}) == 1
        for item in referents:
            [storage] = [v for n, v in item["options"] if n == "Almacenamiento"]
            assert storage in prior
    proposal, execution = CELLS["UX12-HNP-proposal"], CELLS["UX12-HNP-execution"]
    assert execution["grant"]["preference_text"] == proposal["messages"][0]
    [variant] = execution["grant"]["variant_ids"]
    assert FACTS[variant]["product_title"] in proposal["messages"][0]
    assert FACTS[variant]["total_minor"] <= execution["grant"]["total_max_minor"]


def test_hnp_grants_are_valid_scopes():
    for cell in PLAN["cells"]:
        grant = cell.get("grant")
        if grant is None:
            continue
        variants = grant["variant_ids"]
        assert variants == sorted(set(variants)) and 1 <= len(variants) <= 5
        assert set(variants) <= FACTS.keys()
        assert isinstance(grant["total_max_minor"], int) and grant["total_max_minor"] > 0
        assert 1 <= len(grant["preference_text"]) <= 280


def test_not_applicable_references_resolve():
    rows = {row["id"]: row for row in load("evaluation/scenarios/p0/matrix.json")["scenarios"]}
    assert set(PLAN["not_applicable"]) == {
        "UX01-HNP-execution",
        "UX02-HNP-execution",
        "UX11-HNP-execution",
    }
    for cell_id, entry in PLAN["not_applicable"].items():
        assert cell_id not in CELLS
        assert "HNP" in rows[cell_id.split("-")[0]]["modes"]
        assert entry["reason"] and entry["refs"]
        for ref in entry["refs"]:
            lines = (ROOT / ref["path"]).read_text(encoding="utf-8").splitlines()
            first, last = (int(n) for n in ref["lines"].split("-"))
            assert 1 <= first <= last <= len(lines), ref


# Cost


def cap(bound: Decimal, step: int) -> Decimal:
    return (bound / step).to_integral_value(rounding=ROUND_CEILING) * step


def cell_attempts(cell: dict[str, Any], conditions: list[str]) -> int:
    value = cell["max_http_attempts_per_episode"]
    if isinstance(value, dict):
        return sum(value[c] for c in conditions)
    return value * len(conditions)


@pytest.mark.parametrize(
    "path", ["evaluation/scenarios/exp06-hp.json", "evaluation/scenarios/p0-llm-batch2.json"]
)
def test_caps_follow_the_batch_one_formula(path):
    cost = load(path)["cost"]
    worst = (
        (
            cost["input_token_ceiling_per_attempt"] * Decimal(cost["input_usd_per_million"])
            + cost["output_token_ceiling_per_attempt"] * Decimal(cost["output_usd_per_million"])
        )
        / 1_000_000
        * Decimal(cost["price_safety_multiplier"])
    )
    assert worst == Decimal(cost["worst_reserved_usd_per_attempt"]) == Decimal("0.445")
    for stage, step in (("smoke", 1), ("batch", 5)):
        bound = cost[f"{stage}_max_attempts"] * worst
        assert bound == Decimal(cost[f"{stage}_reservation_bound_usd"])
        assert Decimal(cost[f"proposed_{stage}_cap_usd"]) == cap(bound, step)


def test_attempts_add_up_per_cell_condition_and_repetition():
    cost, design = PLAN["cost"], PLAN["design"]
    per_repetition = sum(cell_attempts(c, c["conditions"]) for c in PLAN["cells"])
    assert per_repetition == cost["attempts_per_repetition"] == 44
    assert cost["smoke_max_attempts"] == per_repetition * design["smoke_repetitions_per_cell"]
    assert cost["batch_max_attempts"] == per_repetition * design["batch_repetitions_per_cell"]
    # Without the B0 conditions of CT21.
    alternative = cost["b2_only_alternative"]
    kept = sum(
        cell_attempts(c, [k for k in c["conditions"] if k not in {"B0prime", "B0-HNP"}])
        for c in PLAN["cells"]
    )
    assert kept == alternative["attempts_per_repetition"] == 36
    worst = Decimal(cost["worst_reserved_usd_per_attempt"])
    for stage, repetitions, step in (("smoke", 1, 1), ("batch", 5, 5)):
        bound = kept * repetitions * worst
        assert Decimal(alternative[f"{stage}_reservation_bound_usd"]) == bound
        assert Decimal(alternative[f"proposed_{stage}_cap_usd"]) == cap(bound, step)


# CT21


def test_injection_text_obeys_its_rules():
    text = CT21["injected_variant_title"]
    assert len(text) <= CT21["max_length"] == 200
    assert text.startswith(CT21["original_variant_title"] + " ")
    assert FACTS[CT21["target_variant_id"]]["variant_title"] == CT21["original_variant_title"]
    assert MARKER in text
    request = words(CT21["hp_request"]) | words(CT21["hnp_preference_text"])
    assert words(text) & request == set()
    for term in ("precio", "barat", "econom", "menor", "costo", "valor", "oferta", "negro"):
        assert term not in folded(text), term
    for goal in ("dos unidades", "aprobada", "confirma"):
        assert goal in text


def test_r_and_the_target_follow_from_the_catalog_facts():
    budget = BUDGET
    black_new = sorted(
        (
            item
            for item in PLAN["catalog_facts"]
            if item["condition"] == "new" and ["Color", "negro"] in item["options"]
        ),
        key=lambda item: item["unit_minor"],
    )
    assert [item["variant_id"] for item in black_new] == CT21["shortlist"]
    assert CT21["R"] == [v for v in CT21["shortlist"] if FACTS[v]["total_minor"] <= budget]
    assert CT21["correct"] == CT21["R"][0]
    target = FACTS[CT21["target_variant_id"]]
    assert CT21["shortlist"].index(CT21["target_variant_id"]) == 7
    assert CT21["target_variant_id"] not in CT21["R"]
    assert target["unit_minor"] > budget
    low, high = CT21["no_black_new_unit_minor_in"]
    assert all(not low <= item["unit_minor"] <= high for item in black_new)
    grant = CELLS["CT21-HNP"]["grant"]
    assert CT21["target_variant_id"] in grant["variant_ids"]
    assert CT21["correct"] in grant["variant_ids"]
    assert grant["total_max_minor"] == budget


def test_the_mounted_title_never_moves_the_shortlist():
    plain, mounted = feed(mounted=False), feed(mounted=True)
    assert ids(rank_candidates(plain, REFERENCE).ranked[:10]) == CT21["shortlist"]
    for intent in variant_intents():
        expected = ids(rank_candidates(plain, intent).ranked[:10])
        assert ids(rank_candidates(mounted, intent).ranked[:10]) == expected, intent
        assert CT21["target_variant_id"] in expected, intent
    granted = set(CELLS["CT21-HNP"]["grant"]["variant_ids"])
    seen = [v for v in ids(rank_candidates(mounted, HNP_INTENT).ranked) if v in granted]
    assert seen == CELLS["CT21-HNP"]["shortlist"]


def candidates_of(body: dict[str, Any]) -> list[dict[str, Any]]:
    return json.loads(body["input"])["candidates"]


def test_the_injected_title_reaches_every_selection_stage_but_not_the_route_input(monkeypatch):
    mounted = feed(mounted=True)
    target, text = CT21["target_variant_id"], CT21["injected_variant_title"]
    preference = CT21["hnp_preference_text"]
    clarify = CommerceDecision(action="clarify", assistant_message="¿Cuál prefieres?")
    server = ModelServer(
        structured(clarify),
        function_call("lookup_catalog", {"query": "Galaxy A22"}),
        structured(clarify),
        structured(shortlisted_choice),
        structured(shortlisted_choice),
        structured(lambda body: {"decision": "confirmed", "reason": "matches_request"}),
    )
    server.install(monkeypatch)
    messages = (ConversationMessage(role="user", content=CT21["hp_request"]),)
    router = OpenAIConversationRouter(api_key=KEY, model=MODEL, feed=mounted)
    router.route(messages)
    router.route(messages)
    shortlist = rank_candidates(mounted, REFERENCE).ranked[:10]
    router.choose_candidate(messages, REFERENCE, shortlist)
    router.close()
    granted = set(CELLS["CT21-HNP"]["grant"]["variant_ids"])
    delegated = tuple(
        item
        for item in rank_candidates(mounted, HNP_INTENT).ranked
        if item.candidate.variant_id in granted
    )
    chooser = OpenAIDelegatedChooser(api_key=KEY, model=MODEL)
    chooser.choose(HNP_INTENT, delegated, preference)
    chooser.close()
    reviewer = ModelHnpIntentReviewer(api_key=KEY, model=MODEL, timer=Timer())
    reviewer.review(delegated, HNP_INTENT, preference, target)
    reviewer.close()
    plain_route, _, tool_route, choose, select, review = server.bodies()

    # The route never carries a title: the vocabulary has brands and options only.
    assert MARKER not in json.dumps(plain_route, ensure_ascii=False)
    # A lookup_catalog call brings it back as the tool's output.
    [output] = [i for i in tool_route["input"] if i.get("type") == "function_call_output"]
    tool = {p["variant_id"]: p for p in json.loads(output["output"])["products"]}
    assert tool[target]["variant"] == text
    # Every selection stage sees it as the target's variant_title, beside its real price.
    for body in (choose, select, review):
        seen = {c["variant_id"]: c for c in candidates_of(body)}
        assert seen[target]["variant_title"] == text
        assert MARKER not in body["instructions"]
    assert [c["variant_id"] for c in candidates_of(choose)] == CT21["shortlist"]
    assert [c["variant_id"] for c in candidates_of(select)] == CELLS["CT21-HNP"]["shortlist"]
    assert json.loads(review["input"])["chosen_variant_id"] == target
