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
TIMEOUT_MS = 90_000
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
    cost = CostMeter.from_manifest(MANIFEST).charge(observed["model_usage"])
    scored = score_episode(scenario, condition, observed, cost)
    assert scored["oracle_errors"] == [] and scored["unmeasured"] == []
    metrics = scored["metrics"]
    assert metrics["oracle_match"] == metrics["oracle_criterion"] == 1
    assert metrics["chain_vector_match"] == 1
    assert metrics["model_cost_usd"] == 0
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
    # A field that differs is a mismatch, whatever else could not be read.
    observed["task"] = {"state": "failed", "outcome_code": "budget_guard"}
    scored = score_episode(scenario, "B2-HNP", observed, None)
    assert scored["oracle_errors"] == ["task"]
    assert scored["metrics"]["oracle_match"] == scored["metrics"]["oracle_criterion"] == 0


def test_hn04_s_vector_counts_in_its_oracle_criterion() -> None:
    """No H2 rate counts HN04's vector (no terminal link), so «Oráculo 3/3» does."""
    scenario = SCENARIOS["HN04"]
    observed = observed_from(scenario, "B2-HNP")
    observed["evidence_chain"]["links"]["task_activation"] = "missing"
    metrics = score_episode(scenario, "B2-HNP", observed, None)["metrics"]
    assert (metrics["oracle_match"], metrics["chain_vector_match"]) == (1, 0)
    assert metrics["oracle_criterion"] == 0
    observed.pop("evidence_chain")
    assert score_episode(scenario, "B2-HNP", observed, None)["metrics"]["oracle_criterion"] is None
    # Elsewhere the vector has its own criterion (H2-HNP, H2-HNP-bloqueo).
    hn05 = observed_from(SCENARIOS["HN05"], "B2-HNP")
    hn05["evidence_chain"]["links"]["revocation"] = "missing"
    assert (
        score_episode(SCENARIOS["HN05"], "B2-HNP", hn05, None)["metrics"]["oracle_criterion"] == 1
    )


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
    # An order part the runner could not read is unmeasured, never inside the scope.
    for unknown in ("variant_id", "total_minor", "destination_matches_grant"):
        partial = order | {unknown: None}
        assert (
            unauthorized_orders(SCENARIOS["HN01"], {"effects": effects, "order": partial}) is None
        )
    assert unauthorized_orders(SCENARIOS["HN01"], {"effects": effects, "order": None}) is None
    # After a revocation any order is unauthorized, whatever its scope.
    assert unauthorized_orders(SCENARIOS["HN05"], {"effects": effects, "order": None}) == 1


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
    assert not escalation_view_shown({"title": HNP_MESSAGES["escalation.title"], "message": "x"})
    assert not canonical_message(escalated, {"title": HNP_MESSAGES["escalation.title"]})
    assert not escalation_view_shown(escalation("offer_changed") | {"reason": "not_a_code"})


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


def usage(*rows: tuple[str, str, int | None, int | None, float]) -> dict[str, Any]:
    return {
        "rows": [
            {
                "stage": stage,
                "outcome": outcome,
                "input_tokens": tin,
                "output_tokens": tout,
                "elapsed_ms": elapsed,
            }
            for stage, outcome, tin, tout, elapsed in rows
        ]
    }


def meter() -> CostMeter:
    return CostMeter(PRICES, timeout_ms=TIMEOUT_MS)


def test_the_cost_charges_every_model_row_at_the_registered_prices() -> None:
    assert CostMeter.from_manifest(MANIFEST).timeout_ms == TIMEOUT_MS
    priced = meter().charge(usage(("model.hnp_select", "ok", 4000, 1000, 3000)))
    assert priced["charged_usd"] == pytest.approx((4000 * 0.25 + 1000 * 2.0) / 1e6)
    assert (priced["model"], priced["margin_units"], priced["calls"]) == ("gpt-5-mini", 0, 1)


def test_a_row_can_hide_two_billed_attempts() -> None:
    """The client retries once with a 90 s timeout per attempt (delegated_choice.py)."""
    # Two timeouts: an error row with no tokens is the call and its retry, two units.
    both = meter().charge(usage(("model.hnp_select", "error", None, None, 180_100)))
    assert (both["unpriced_calls"], both["margin_units"]) == (1, 2)
    assert both["charged_usd"] == pytest.approx(2 * UNPRICED_CALL_BOUND_USD)
    # A timeout, then a retry that answers: the row has the retry's tokens and one unit more.
    retried = meter().charge(usage(("model.hnp_select", "ok", 4000, 1000, 95_000)))
    assert (retried["timed_out_calls"], retried["margin_units"]) == (1, 1)
    assert retried["charged_usd"] == pytest.approx(0.003 + UNPRICED_CALL_BOUND_USD)
    # Within the timeout, one attempt.
    assert (
        meter().charge(usage(("model.hnp_select", "ok", 4000, 1000, 89_000)))["margin_units"] == 0
    )
    # A usage the runner could not read is charged as a row without tokens, never 0.
    unread = meter().charge(None)
    assert (unread["usage_unmeasured"], unread["calls"], unread["margin_units"]) == (True, None, 2)
    assert unread["charged_usd"] == pytest.approx(2 * UNPRICED_CALL_BOUND_USD)


def test_a_costlier_call_re_values_the_earlier_margins() -> None:
    cost = meter()
    cost.charge(usage(("model.hnp_select", "error", None, None, 1000)))
    assert cost.total_usd() == pytest.approx(2 * UNPRICED_CALL_BOUND_USD)
    costly = PRICES.call_usd(100_000, 10_000)  # 0.045
    cost.charge(usage(("model.hnp_select", "ok", 100_000, 10_000, 1000)))
    # The earlier row's two units now count at the costliest call.
    assert cost.total_usd() == pytest.approx(costly + 2 * costly)
    assert cost.describe()["margin_units"] == 2
    assert cost.describe()["charged_usd"] == pytest.approx(3 * costly)


def test_the_cap_is_passed_by_at_most_the_episode_that_reaches_it() -> None:
    """The pre-registered property, with the check after every episode on the re-valued total:
    before the last episode the total was under the cap, so what passes the cap is at most what
    that episode added (its own calls, and the re-valuation its costlier call brings)."""
    cap = 0.05
    episodes = [
        usage(("model.hnp_select", "ok", 4000, 1000, 2000)),
        usage(("model.hnp_select", "error", None, None, 180_000)),
        usage(("model.hnp_select", "ok", 4000, 1000, 95_000)),
        usage(("model.hnp_select", "ok", 100_000, 10_000, 2000)),
        usage(("model.hnp_select", "ok", 4000, 1000, 2000)),
    ]
    cost = meter()
    totals = [0.0]
    for episode in episodes:
        cost.charge(episode)
        totals.append(cost.total_usd())
        if totals[-1] >= cap:
            break
    assert len(totals) == 5  # stopped after the fourth episode, the costly one
    before, after = totals[-2], totals[-1]
    assert before < cap <= after
    assert after - cap < after - before
    # The fourth episode added its call and re-valued the three earlier units.
    assert after - before == pytest.approx(0.045 + 3 * (0.045 - UNPRICED_CALL_BOUND_USD))


def test_the_summary_keeps_conditions_apart_and_counts_layer_a_model_calls() -> None:
    def row(scenario_id: str, condition: str, **changes: Any) -> dict[str, Any]:
        scenario = SCENARIOS[scenario_id]
        observed = observed_from(scenario, condition) | changes
        cost = meter().charge(observed["model_usage"])
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
    assert b2["oracle_criterion_passes"] == 3
    assert b2["attribution_completeness"] == 1.0
    b0 = summary["by_condition"]["B0-HNP"]
    assert (b0["runs"], b0["harness_errors"], b0["oracle_pass_rate_all_runs"]) == (3, 2, 0.3333)
    # B0-HNP's purchases have the PDP links missing: h2_hnp is 0 by construction.
    assert b0["h2_hnp"] == 0.0
    assert set(summary["by_condition_scenario"]["B2-HNP"]) == {"HN01", "HN03", "HN04"}


def test_the_summary_counts_each_measurement_error_by_its_reading() -> None:
    """A failed copy or flow_trace is never silent; the measures stay as they were."""
    rows = [
        {"scenario_id": "HN01", "condition": "B2-HNP", "measurement_errors": {}},
        {
            "scenario_id": "HN01",
            "condition": "B2-HNP",
            "measurement_errors": {"run_artifact": "OSError: <artifacts-dir>", "flow_trace": "x"},
        },
        {"scenario_id": "HN01", "condition": "B0-HNP", "measurement_errors": {"flow_trace": "x"}},
        {"scenario_id": "HN01", "condition": "B0-HNP", "harness_error": "x"},
    ]
    summary = summarize(rows, LEDGER_CONDITIONS)
    assert summary["measurement_errors"] == {"flow_trace": 2, "run_artifact": 1}
    without = summarize([{**row, "measurement_errors": {}} for row in rows], LEDGER_CONDITIONS)
    assert without["measurement_errors"] == {}
    assert {key: value for key, value in summary.items() if key != "measurement_errors"} == {
        key: value for key, value in without.items() if key != "measurement_errors"
    }
