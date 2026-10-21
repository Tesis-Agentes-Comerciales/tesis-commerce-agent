"""HNP pilot v1 pre-registration (docs/design/hnp-pilot.md): manifest and scenario shape."""

import json
from typing import Any, get_args

import pytest

from commerce_agent.delegation.contract import (
    CancelCode,
    EscalationCode,
    FailureCode,
    GrantState,
    RevokedReason,
)
from commerce_evaluation.paths import DATA, ROOT

CONDITIONS = ["B0-HNP", "B2-HNP", "P"]
LEDGER_CONDITIONS = ("B0-HNP", "B2-HNP")
SCENARIO_IDS = {f"HN{number:02d}" for number in range(1, 12)}
# Spec §8.1: the outcome codes each terminal task state allows. B0-HNP never escalates, so a
# cause that escalates in B2-HNP fails there with the same code.
OUTCOMES: dict[str, set[str | None]] = {
    "purchased": {None},
    "escalated": set(get_args(EscalationCode)),
    "canceled": set(get_args(CancelCode)),
    "failed": set(get_args(FailureCode)) | set(get_args(EscalationCode)),
}
EFFECTS = {
    "merchant_checkouts",
    "checkouts_canceled",
    "agent_key_closures",
    "chains_presented",
    "psp_tokens",
    "orders",
    "unauthorized_orders",
}
ORACLE_FIELDS = {
    "run",
    "task",
    "grant",
    "trigger_responses",
    "revoke_effect",
    "escalation_view",
    "governance",
    "effects",
    "order",
    "verifier_rejection",
    "evidence_chain",
}
TRIGGER_RESPONSES = {"202", "200_terminal", "409 hnp_task_not_runnable"}
EVIDENCE_CHAIN = {"complete", "complete_to_block", "incomplete_by_construction", "no_terminal_link"}
# Spec §9.2, in order.
HNP_LINKS = [
    "intent",
    "delegation_consent",
    "open_mandates",
    "task_activation",
    "d1_decision",
    "checkout_created",
    "freshness",
    "d2_decision",
    "reservation",
    "closed_mandates",
    "d3_decision",
    "receipts",
    "order_webhook",
    "consumption",
    "revocation",
    "kill_switch",
    "escalation",
]
PURCHASE_LINKS = HNP_LINKS[:14]
TERMINAL_LINKS = HNP_LINKS[14:]
# Spec §9.2: the links the PDP writes; B0-HNP has none of them.
PDP_LINKS = {
    "intent",
    "d1_decision",
    "checkout_created",
    "freshness",
    "d2_decision",
    "d3_decision",
}
# Races and crash points belong to HNP-I1: no stimulus of the matrix injects one.
STIMULI = {
    "merchant_update_offer",
    "wait_until_grant_expired",
    "owner_revoke",
    "operator_kill_switch",
}


def load(name: str) -> dict[str, Any]:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return load("hnp-pilot-v1.json")


@pytest.fixture(scope="module")
def matrix() -> dict[str, Any]:
    return load("hnp-scenarios.json")


@pytest.fixture(scope="module")
def scenarios(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    return matrix["scenarios"]


def test_the_manifest_points_to_its_design_spec_and_matrix(manifest: dict[str, Any]) -> None:
    assert manifest["manifest_id"] == "HNP-PILOT-v1"
    assert manifest["experiment"] == "EC-HNP-03"
    assert manifest["status"] == "pre_registered"
    assert (ROOT / manifest["design"]).is_file()
    assert (ROOT / manifest["spec"]).is_file()
    assert (ROOT / manifest["scenario_manifest"]).resolve() == DATA / "hnp-scenarios.json"
    assert list(manifest["conditions"]) == CONDITIONS
    assert manifest["policy"]["policy_set_id"] == "b2-hnp-v1"
    assert manifest["policy"]["rules"] == {
        "D1": ["POL-01", "POL-02", "POL-11"],
        "D2": ["POL-03", "POL-04", "POL-05", "POL-06", "POL-07", "POL-08", "POL-09"],
        "D3": ["POL-09", "POL-10"],
    }


def test_yair_approves_the_oracles_before_any_live_run(manifest: dict[str, Any]) -> None:
    approval = manifest["approval"]

    assert approval["required_from"] == "Yair"
    assert approval["status"] in {"pending", "approved"}
    assert (approval["status"] == "approved") == (approval["approved_at"] is not None)
    assert manifest["decisions_pending"]


def test_the_conditions_differ_only_in_the_governance_mode(manifest: dict[str, Any]) -> None:
    b0 = manifest["conditions"]["B0-HNP"]["agent_environment"]
    b2 = manifest["conditions"]["B2-HNP"]["agent_environment"]

    assert b0["AGENT_HNP_ENABLED"] == b2["AGENT_HNP_ENABLED"] == "true"
    assert (b0["AGENT_GOVERNANCE_MODE"], b2["AGENT_GOVERNANCE_MODE"]) == ("off", "enforce")
    assert {key: value for key, value in b2.items() if key in b0} == b0 | {
        "AGENT_GOVERNANCE_MODE": "enforce"
    }
    # The ablation arm lives in commerce_evaluation; the product gains no unsafe switch.
    assert "commerce_evaluation" in manifest["conditions"]["P"]["implementation"]
    assert "agent_environment" not in manifest["conditions"]["P"]


def test_the_cost_cap_is_at_most_two_dollars(manifest: dict[str, Any]) -> None:
    cost = manifest["cost"]

    assert cost["cap_variable"] == "HNP_PILOT_MAX_COST_USD"
    assert 0 < cost["cap_usd"] <= 2


def test_scenario_ids_are_unique_and_exactly_hn01_to_hn11(scenarios: list[dict[str, Any]]) -> None:
    identifiers = [scenario["id"] for scenario in scenarios]

    assert len(identifiers) == len(set(identifiers))
    assert set(identifiers) == SCENARIO_IDS


def test_the_matrix_and_the_manifest_agree(
    manifest: dict[str, Any], matrix: dict[str, Any], scenarios: list[dict[str, Any]]
) -> None:
    assert matrix["matrix_id"] == "hnp-pilot-v1"
    assert (ROOT / matrix["manifest"]).resolve() == DATA / "hnp-pilot-v1.json"
    assert matrix["conditions"] == CONDITIONS
    assert set(manifest["acceptance"]) >= SCENARIO_IDS
    for layer in ("A", "B"):
        assert manifest["layers"][layer]["scenarios"] == [
            scenario["id"] for scenario in scenarios if scenario["layer"] == layer
        ]
    declared = set(matrix["metrics"])
    assert all(set(scenario["primary_metrics"]) <= declared for scenario in scenarios)


def test_every_scenario_has_an_oracle_for_the_three_conditions(
    scenarios: list[dict[str, Any]],
) -> None:
    for scenario in scenarios:
        oracle = scenario["oracle"]
        assert sorted(oracle) == sorted(CONDITIONS), scenario["id"]
        for condition in CONDITIONS:
            cell = oracle[condition]
            assert cell.keys() >= ORACLE_FIELDS, (scenario["id"], condition)
            assert cell["run"] == (scenario["runs"][condition] > 0), (scenario["id"], condition)


def test_ledger_oracles_name_states_the_contract_allows(scenarios: list[dict[str, Any]]) -> None:
    for scenario in scenarios:
        for condition in LEDGER_CONDITIONS:
            cell = scenario["oracle"][condition]
            task, grant = cell["task"], cell["grant"]
            assert task["outcome_code"] in OUTCOMES[task["state"]], scenario["id"]
            assert grant["state"] in get_args(GrantState)
            assert (grant["state"] == "revoked") == (grant["revoked_reason"] is not None)
            assert grant["revoked_reason"] in {None, *get_args(RevokedReason)}
            assert set(cell["trigger_responses"]) <= TRIGGER_RESPONSES
            assert cell["evidence_chain"]["class"] in EVIDENCE_CHAIN
            assert set(cell["effects"]) == EFFECTS
            assert all(isinstance(value, int) for value in cell["effects"].values())
            assert (cell["effects"]["orders"] > 0) == (task["state"] == "purchased")
            assert cell["escalation_view"] == (task["state"] == "escalated")


def test_b0_hnp_never_escalates_and_never_completes_a_purchase_chain(
    scenarios: list[dict[str, Any]],
) -> None:
    for scenario in scenarios:
        cell = scenario["oracle"]["B0-HNP"]
        assert cell["task"]["state"] != "escalated"
        assert cell["governance"] is None
        assert cell["evidence_chain"]["class"] != "complete", scenario["id"]
        if cell["task"]["state"] == "purchased":
            assert cell["evidence_chain"]["class"] == "incomplete_by_construction"


def links_of(cell: dict[str, Any]) -> dict[str, list[str]]:
    return cell["evidence_chain"]["links"]


def test_every_ledger_oracle_registers_a_vector_of_the_seventeen_links(
    scenarios: list[dict[str, Any]],
) -> None:
    for scenario in scenarios:
        assert scenario["oracle"]["P"]["evidence_chain"] is None, scenario["id"]
        for condition in LEDGER_CONDITIONS:
            cell = scenario["oracle"][condition]
            links = links_of(cell)
            where = (scenario["id"], condition)
            assert set(links) == {"ok", "missing", "not_applicable"}, where
            listed = [link for status in links.values() for link in status]
            # Every link in exactly one status, each list in the spec's order.
            assert sorted(listed) == sorted(HNP_LINKS), where
            for status in links.values():
                assert status == [link for link in HNP_LINKS if link in status], where
            assert set(links["missing"]) <= PDP_LINKS, where
            chain_class = cell["evidence_chain"]["class"]
            assert (chain_class == "incomplete_by_construction") == bool(links["missing"]), where
            assert (chain_class == "no_terminal_link") == (scenario["id"] == "HN04"), where
            purchased = cell["task"]["state"] == "purchased"
            if purchased:
                assert set(PURCHASE_LINKS) - PDP_LINKS <= set(links["ok"]), where
                assert set(TERMINAL_LINKS) <= set(links["not_applicable"]), where
            if chain_class == "complete":
                assert purchased and links["ok"] == PURCHASE_LINKS, where
            if chain_class == "complete_to_block":
                assert not purchased, where


def test_b2_hnp_records_every_link_it_reaches(scenarios: list[dict[str, Any]]) -> None:
    for scenario in scenarios:
        cell = scenario["oracle"]["B2-HNP"]
        assert links_of(cell)["missing"] == [], scenario["id"]
        if cell["task"]["state"] == "purchased":
            assert cell["evidence_chain"]["class"] == "complete", scenario["id"]
        # A block before D1 leaves the same vector in B0-HNP: the PDP never ran.
        if not PDP_LINKS & set(links_of(cell)["ok"]):
            assert links_of(scenario["oracle"]["B0-HNP"]) == links_of(cell), scenario["id"]


def test_no_oracle_depends_on_how_far_a_run_got(manifest: dict[str, Any]) -> None:
    for name in ("hnp-pilot-v1.json", "hnp-scenarios.json"):
        text = (DATA / name).read_text(encoding="utf-8")
        assert "if_reached" not in text, name
        assert "if_d1_reached" not in text, name
    assert "E2_hn11_b0_code" not in manifest["allowed_amendments"]


def test_declared_ties_have_the_same_effects_and_outcome(scenarios: list[dict[str, Any]]) -> None:
    ties = [scenario for scenario in scenarios if scenario["b0_vs_b2"] == "tie_by_construction"]

    # D-2: B0-HNP and B2-HNP tie on safety in all but HN03 (earlier containment) and HN08.
    assert {scenario["id"] for scenario in ties} == SCENARIO_IDS - {"HN03", "HN08"}
    for scenario in ties:
        b0, b2 = scenario["oracle"]["B0-HNP"], scenario["oracle"]["B2-HNP"]
        assert (b0["effects"], b0["task"]) == (b2["effects"], b2["task"]), scenario["id"]


def test_b2_hnp_never_orders_outside_the_authority(scenarios: list[dict[str, Any]]) -> None:
    for scenario in scenarios:
        assert scenario["oracle"]["B2-HNP"]["effects"]["unauthorized_orders"] == 0


def test_orders_stay_within_the_delegated_variants_and_total(
    matrix: dict[str, Any], scenarios: list[dict[str, Any]]
) -> None:
    catalog = matrix["catalog"]["variants"]
    for scenario in scenarios:
        delegation = scenario["delegation"]
        assert delegation["currency"] == "COP"
        assert 1 <= len(delegation["variant_ids"]) <= 5
        assert set(delegation["variant_ids"]) <= set(catalog)
        assert 60 <= delegation["valid_for_seconds"] <= 86400
        for condition in CONDITIONS:
            order = scenario["oracle"][condition]["order"]
            if order is None:
                continue
            assert set(order["variant_ids"]) <= set(delegation["variant_ids"]), scenario["id"]
            assert set(order["total_minor_by_variant"]) == set(order["variant_ids"])
            assert all(
                total <= delegation["total_max_minor"]
                for total in order["total_minor_by_variant"].values()
            )


def test_the_registered_prices_are_the_catalog_s(
    matrix: dict[str, Any], scenarios: list[dict[str, Any]]
) -> None:
    catalog = matrix["catalog"]["variants"]
    for variant in catalog.values():
        assert variant["item_price_minor"] + variant["shipping_minor"] == variant["total_minor"]
    for scenario in scenarios:
        changed = {
            stimulus["variant_id"]: stimulus["new_item_price_minor"]
            for stimulus in scenario["stimuli"]
            if stimulus["action"] == "merchant_update_offer"
        }
        for variant_id, price in scenario["catalog_setup"]["feed_item_price_minor"].items():
            assert price == catalog[variant_id]["item_price_minor"], scenario["id"]
        for condition in CONDITIONS:
            order = scenario["oracle"][condition]["order"]
            for variant_id, total in (order or {}).get("total_minor_by_variant", {}).items():
                item = changed.get(variant_id, catalog[variant_id]["item_price_minor"])
                assert total == item + catalog[variant_id]["shipping_minor"], scenario["id"]


def test_races_and_crash_points_stay_out_of_the_matrix(
    manifest: dict[str, Any], scenarios: list[dict[str, Any]]
) -> None:
    for scenario in scenarios:
        assert {stimulus["action"] for stimulus in scenario["stimuli"]} <= STIMULI
        assert scenario["trigger"]["mode"] in {"once", "twice_concurrently"}
    assert manifest["out_of_matrix"]["owner"] == "HNP-I1"


def test_repetitions_and_the_episode_count(
    manifest: dict[str, Any], scenarios: list[dict[str, Any]]
) -> None:
    by_condition = {
        condition: sum(scenario["runs"][condition] for scenario in scenarios)
        for condition in CONDITIONS
    }

    assert manifest["episodes"]["by_condition"] == by_condition
    assert manifest["episodes"]["total"] == sum(by_condition.values()) == 133
    for scenario in scenarios:
        runs = scenario["runs"]
        assert runs["B0-HNP"] == runs["B2-HNP"], scenario["id"]
        if scenario["layer"] == "A":
            assert runs["B2-HNP"] == (10 if scenario["id"] == "HN07" else 3)
        else:
            assert (runs["B2-HNP"], runs["P"]) == (10, 0)
        assert runs["P"] in {0, runs["B2-HNP"]}
    with_p = [scenario["id"] for scenario in scenarios if scenario["runs"]["P"]]
    assert with_p == manifest["conditions"]["P"]["runs_only_in"]
    assert set(with_p) == {"HN04", "HN05", "HN06", "HN07", "HN08", "HN11"}


def test_the_evidence_chain_has_the_spec_s_seventeen_links(manifest: dict[str, Any]) -> None:
    chain = manifest["evidence_chain"]

    assert chain["links"] == HNP_LINKS
    assert chain["statuses"] == ["ok", "missing", "mismatch", "not_applicable"]


def test_no_secret_is_registered(manifest: dict[str, Any]) -> None:
    for condition in ("B0-HNP", "B2-HNP"):
        environment = manifest["conditions"][condition]["agent_environment"]
        for key in ("AGENT_HNP_AGENT_KEY_PEM", "AGENT_HNP_SERVICE_TOKEN", "AGENT_D1_HMAC_KEY_HEX"):
            assert environment[key] == "generated_per_invocation_never_logged"
    text = (DATA / "hnp-pilot-v1.json").read_text(encoding="utf-8")
    assert "BEGIN" not in text
