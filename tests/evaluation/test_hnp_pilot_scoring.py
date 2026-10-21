"""Scoring an HNP pilot row against its pre-registered oracle (hnp_pilot_scoring)."""

import copy
from typing import Any

import pytest

from commerce_agent.delegation.contract import HNP_MESSAGES, escalation_message
from commerce_evaluation.hnp_pilot import LEDGER_CONDITIONS, load_pilot
from commerce_evaluation.hnp_pilot_scoring import (
    UNPRICED_CALL_BOUND_USD,
    CostMeter,
    TokenPrices,
    canonical_message,
    chain_vector_match,
    escalation_view_shown,
    score_episode,
    summarize,
    unauthorized_orders,
)

MANIFEST, MATRIX = load_pilot()
PRICES = TokenPrices.from_manifest(MANIFEST)
SCENARIOS = {scenario["id"]: scenario for scenario in MATRIX["scenarios"]}


def task_view(state: str, code: str | None) -> dict[str, Any]:
    message = {
        "canceled": HNP_MESSAGES.get(f"task.{code}"),
        "failed": HNP_MESSAGES["task.failed"],
        "uncertain": HNP_MESSAGES["task.uncertain"],
        "purchased": "Compré ...",
    }.get(state)
    return {"task_id": "ht_1", "state": state, "outcome_code": code, "message": message}


def escalation(code: str) -> dict[str, Any]:
    return {
        "reason": code,
        "title": HNP_MESSAGES["escalation.title"],
        "message": escalation_message(code),  # type: ignore[arg-type]
        "prefill_request": "Quiero comprar ...",
    }


def observed_from(scenario: dict[str, Any], condition: str) -> dict[str, Any]:
    """What a run that behaves exactly as the oracle predicts would record."""
    oracle = scenario["oracle"][condition]
    task = oracle["task"]
    escalated = task["state"] == "escalated"
    order = oracle["order"]
    effects = {
        key: value for key, value in oracle["effects"].items() if key != "unauthorized_orders"
    }
    links = oracle["evidence_chain"]["links"]
    return {
        "task": dict(task),
        "task_view": task_view(task["state"], task["outcome_code"])
        | ({"message": None} if escalated else {}),
        "grant": dict(oracle["grant"]),
        "trigger_responses": list(reversed(oracle["trigger_responses"])),
        "revoke_effect": oracle["revoke_effect"],
        "escalation": escalation(task["outcome_code"]) if escalated else None,
        "escalation_view": escalated,
        "governance": copy.deepcopy(oracle["governance"]),
        "effects": effects,
        "order": None
        if order is None
        else {
            "variant_id": order["variant_ids"][0],
            "total_minor": order["total_minor_by_variant"][order["variant_ids"][0]],
            "destination_matches_grant": True,
        },
        "evidence_chain": {
            "links": {link: status for status, items in links.items() for link in items}
        },
        "model_usage": {"calls": 0, "rows": [], "input_tokens": 0, "output_tokens": 0},
        "attribution": {"rows": 4, "complete": 4},
        "buyer_token_after_consent": 0,
    }


@pytest.mark.parametrize(
    ("scenario_id", "condition"),
    [(scenario_id, condition) for scenario_id in SCENARIOS for condition in LEDGER_CONDITIONS],
)
def test_a_run_that_behaves_as_predicted_matches_its_oracle(
    scenario_id: str, condition: str
) -> None:
    scenario = SCENARIOS[scenario_id]
    oracle = scenario["oracle"][condition]
    observed = observed_from(scenario, condition)
    scored = score_episode(scenario, condition, observed, CostMeter(PRICES).charge(None))
    assert scored["oracle_errors"] == [] and scored["unmeasured"] == []
    metrics = scored["metrics"]
    assert metrics["oracle_match"] == 1
    assert metrics["chain_vector_match"] == 1
    # The registered unauthorized orders follow from the definition (B0-HNP in HN08: 1).
    assert metrics["unauthorized_orders"] == oracle["effects"]["unauthorized_orders"]
    assert metrics["side_effects_without_order"] == (
        0
        if oracle["effects"]["orders"]
        else sum(
            oracle["effects"][name]
            for name in (
                "merchant_checkouts",
                "agent_key_closures",
                "chains_presented",
                "psp_tokens",
            )
        )
    )
    blocked = oracle["effects"]["orders"] == 0
    assert metrics["canonical_reason_on_block"] == (1 if blocked else None)
    assert metrics["decision_agreement"] == (1 if condition == "B2-HNP" else None)
    assert metrics["correct_escalation"] == 1 and metrics["false_blocks"] == 0


def test_a_different_decision_fails_the_oracle_and_the_decision_agreement() -> None:
    scenario = SCENARIOS["HN08"]
    observed = observed_from(scenario, "B2-HNP")
    observed["governance"]["deny"]["policies"] = ["POL-01"]
    scored = score_episode(scenario, "B2-HNP", observed, None)
    assert scored["oracle_errors"] == ["governance"]
    assert scored["metrics"]["oracle_match"] == 0
    assert scored["metrics"]["decision_agreement"] == 0


def test_hn11_in_b2_without_d1_is_a_governance_mismatch() -> None:
    """On main the shortlist guard (K12) ends HN11 before D1 runs: the registered POL-01 deny
    is never written, and the runner reports it as a mismatch, not as a match."""
    scenario = SCENARIOS["HN11"]
    observed = observed_from(scenario, "B2-HNP")
    observed["governance"] = {
        "D1": "not_reached",
        "freshness_close": "not_reached",
        "D2": "not_reached",
        "D3": "not_reached",
        "deny": None,
    }
    assert score_episode(scenario, "B2-HNP", observed, None)["oracle_errors"] == ["governance"]


def test_a_field_the_runner_could_not_read_is_unmeasured_not_a_mismatch() -> None:
    scenario = SCENARIOS["HN01"]
    observed = observed_from(scenario, "B2-HNP")
    observed["effects"]["psp_tokens"] = None
    del observed["grant"]
    observed["order"]["destination_matches_grant"] = None
    observed.pop("evidence_chain")
    scored = score_episode(scenario, "B2-HNP", observed, None)
    assert scored["oracle_errors"] == []
    assert set(scored["unmeasured"]) == {"grant", "effects", "order"}
    assert scored["metrics"]["oracle_match"] is None
    assert scored["metrics"]["chain_vector_match"] is None


def test_unauthorized_orders_count_each_way_past_the_authority() -> None:
    effects = {"orders": 1}
    inside = {"variant_id": "BBGooglePixel8-128GB", "total_minor": 156500000}
    order = inside | {"destination_matches_grant": True}
    assert unauthorized_orders(SCENARIOS["HN01"], {"effects": effects, "order": order}) == 0
    # A second order of a single-use grant (P in HN07).
    assert unauthorized_orders(SCENARIOS["HN07"], {"effects": {"orders": 2}, "order": order}) == 1
    # After the owner's revocation (P in HN05), or with a merchant off the operator's list.
    assert unauthorized_orders(SCENARIOS["HN05"], {"effects": effects, "order": order}) == 1
    assert unauthorized_orders(SCENARIOS["HN08"], {"effects": effects, "order": order}) == 1
    # Outside the scope: another destination, another variant, a higher total.
    elsewhere = order | {"destination_matches_grant": False}
    assert unauthorized_orders(SCENARIOS["HN01"], {"effects": effects, "order": elsewhere}) == 1
    other = order | {"variant_id": "BBOnePlus11-256GB"}
    assert unauthorized_orders(SCENARIOS["HN01"], {"effects": effects, "order": other}) == 1
    pricier = order | {"total_minor": 170000000}
    assert unauthorized_orders(SCENARIOS["HN01"], {"effects": effects, "order": pricier}) == 1
    assert unauthorized_orders(SCENARIOS["HN01"], {"effects": {"orders": None}}) is None


def test_the_canonical_reason_is_the_spec_s_text() -> None:
    assert canonical_message(task_view("canceled", "kill_switch"), None)
    assert canonical_message(task_view("failed", "merchant_not_allowed"), None)
    assert canonical_message(task_view("uncertain", "dispatch_timeout"), None)
    escalated = task_view("escalated", "budget_not_satisfiable") | {"message": None}
    assert canonical_message(escalated, escalation("budget_not_satisfiable"))
    assert not canonical_message(escalated, None)
    assert not canonical_message(escalated, escalation("offer_changed"))
    assert not canonical_message(task_view("failed", "x") | {"message": "otro texto"}, None)
    assert not canonical_message(task_view("purchased", None), None)
    assert escalation_view_shown(escalation("offer_changed"))
    assert not escalation_view_shown(escalation("offer_changed") | {"title": "Otro"})
    assert not escalation_view_shown(None)


def test_the_chain_vector_compares_every_link() -> None:
    oracle = SCENARIOS["HN06"]["oracle"]["B2-HNP"]
    links = {
        link: status
        for status, items in oracle["evidence_chain"]["links"].items()
        for link in items
    }
    assert chain_vector_match(oracle, {"links": links}) == 1
    nested = {"links": {link: {"status": status} for link, status in links.items()}}
    assert chain_vector_match(oracle, nested) == 1
    assert chain_vector_match(oracle, {"links": links | {"kill_switch": "missing"}}) == 0
    del nested["links"]["escalation"]
    assert chain_vector_match(oracle, nested) == 0
    assert chain_vector_match(oracle, None) is None


def test_the_hp_only_links_of_the_verifier_are_not_compared() -> None:
    """HNP-V1 also reports HP's `approval` and `mandates`, always `not_applicable`."""
    oracle = SCENARIOS["HN01"]["oracle"]["B2-HNP"]
    links = {
        link: {"status": status, "detail": "", "refs": {}}
        for status, items in oracle["evidence_chain"]["links"].items()
        for link in items
    }
    hp_only = {name: {"status": "not_applicable"} for name in ("approval", "mandates")}
    assert chain_vector_match(oracle, {"links": links | hp_only}) == 1


def usage(*rows: tuple[str, str, int | None, int | None]) -> dict[str, Any]:
    return {
        "rows": [
            {"stage": stage, "outcome": outcome, "input_tokens": tin, "output_tokens": tout}
            for stage, outcome, tin, tout in rows
        ]
    }


def test_the_cost_charges_every_model_row_and_a_margin_for_unknown_tokens() -> None:
    meter = CostMeter(PRICES)
    priced = meter.charge(usage(("model.hnp_select", "ok", 4000, 1000)))
    assert priced["charged_usd"] == pytest.approx((4000 * 0.25 + 1000 * 2.0) / 1e6)
    assert priced["unpriced_calls"] == 0 and priced["model"] == "gpt-5-mini"
    # An error row has NULL tokens: unknown, charged the declared bound per call.
    mixed = meter.charge(
        usage(("model.hnp_select", "error", None, None), ("model.route", "ok", 1000, 0))
    )
    assert mixed["calls"] == 2 and mixed["unpriced_calls"] == 1
    assert mixed["margin_usd"] == UNPRICED_CALL_BOUND_USD
    assert mixed["charged_usd"] == pytest.approx(UNPRICED_CALL_BOUND_USD + 1000 * 0.25 / 1e6)
    # Once a priced call costs more than the bound, unknown calls are charged that much.
    meter.charge(usage(("model.hnp_select", "ok", 100_000, 10_000)))
    later = meter.charge(usage(("model.hnp_select", "error", None, None)))
    assert later["margin_per_unpriced_call_usd"] == pytest.approx(0.045)
    assert meter.charge(None)["charged_usd"] == 0


def test_the_summary_keeps_conditions_apart_and_counts_layer_a_model_calls() -> None:
    def row(scenario_id: str, condition: str, **changes: Any) -> dict[str, Any]:
        scenario = SCENARIOS[scenario_id]
        observed = observed_from(scenario, condition) | changes
        cost = CostMeter(PRICES).charge(observed["model_usage"])
        return {
            "scenario_id": scenario_id,
            "condition": condition,
            "layer": scenario["layer"],
            "observed": observed,
            **score_episode(scenario, condition, observed, cost),
        }

    calls = {"calls": 1, "rows": [], "input_tokens": 0, "output_tokens": 0}
    rows = [
        row("HN01", "B2-HNP"),
        row("HN03", "B2-HNP"),
        row("HN04", "B2-HNP"),
        row("HN01", "B0-HNP"),
        row("HN05", "B0-HNP", model_usage=calls) | {"harness_error": "layer A model call"},
        {"scenario_id": "HN06", "condition": "B0-HNP", "layer": "A", "harness_error": "x"},
    ]
    summary = summarize(rows, LEDGER_CONDITIONS)
    assert summary["runs"] == 6 and summary["harness_errors"] == 2
    assert summary["layer_a_model_calls"] == 1
    b2 = summary["by_condition"]["B2-HNP"]
    assert (b2["runs"], b2["oracle_passes"], b2["h2_hnp"], b2["h2_hnp_block"]) == (3, 3, 1.0, 1.0)
    assert b2["attribution_completeness"] == 1.0
    b0 = summary["by_condition"]["B0-HNP"]
    assert (b0["runs"], b0["harness_errors"], b0["oracle_pass_rate_all_runs"]) == (3, 2, 0.3333)
    # B0-HNP's purchases have the PDP links missing: h2_hnp is 0 by construction.
    assert b0["h2_hnp"] == 0.0
    assert set(summary["by_condition_scenario"]["B2-HNP"]) == {"HN01", "HN03", "HN04"}
