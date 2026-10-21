import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EVALUATION = ROOT / "evaluation" / "data"


def load_json(name: str) -> dict[str, Any]:
    return json.loads((EVALUATION / name).read_text(encoding="utf-8"))


def test_b0_manifest_pins_all_services_and_the_catalog() -> None:
    manifest = load_json("b0-baseline.json")

    assert manifest["baseline_id"] == "B0-2026-10-06"
    assert manifest["git_tag"] == "b0-baseline-2026-10-06"
    assert manifest["catalog"]["seed_source_sha256"]

    services = manifest["services"]
    assert {service["name"] for service in services} == {
        "buyer-agent",
        "merchant",
        "payment-provider",
    }
    assert all(len(service["commit"]) == 40 for service in services)
    assert all(len(service["uv_lock_sha256"]) == 64 for service in services)


def test_pilot_matrix_defines_five_paired_scenarios() -> None:
    matrix = load_json("pilot-scenarios.json")

    assert matrix["baseline_id"] == "B0-2026-10-06"
    assert matrix["conditions"] == ["B0", "B2"]
    assert matrix["repetitions_per_condition"] == 5

    scenarios = matrix["scenarios"]
    assert len(scenarios) == 5
    assert len({scenario["id"] for scenario in scenarios}) == 5

    required = {
        "id",
        "name",
        "category",
        "request",
        "catalog_setup",
        "stimuli",
        "oracle",
        "evidence_required",
        "primary_metrics",
    }
    assert all(required <= scenario.keys() for scenario in scenarios)
    assert all(scenario["evidence_required"] for scenario in scenarios)
    assert all(scenario["primary_metrics"] for scenario in scenarios)


def test_every_primary_metric_is_declared() -> None:
    matrix = load_json("pilot-scenarios.json")
    declared = set(matrix["metrics"])

    assert all(set(scenario["primary_metrics"]) <= declared for scenario in matrix["scenarios"])


# B2-PILOT-v2 (docs/design/b2-governed-pilot-v2.md): a new matrix and manifest beside the v1's.
V1_PRE_REGISTRATION = {
    "b2-pilot-v1.json": "b02b845ce2d7a1d62ec6605e79383f2d5edf234f795d98ff64f348672e75796b",
    "pilot-scenarios.json": "8abc0ad7767d6cebe11bbe91d56548b12f66940a24b8146eb2c973a476c1c41c",
}
CARRIED_FIELDS = ("name", "category", "request", "catalog_setup", "stimuli", "evidence_required")
V2_FIELDS = {"governance_setup", "authority_limit", "approval_flow"}
EVIDENCE_LINKS = [
    "intent",
    "d1_decision",
    "checkout_created",
    "freshness",
    "d2_decision",
    "approval",
    "mandates",
    "receipts",
    "order_webhook",
]


def test_the_v1_pre_registration_keeps_its_bytes() -> None:
    digests = {
        name: hashlib.sha256((EVALUATION / name).read_bytes()).hexdigest()
        for name in V1_PRE_REGISTRATION
    }

    assert digests == V1_PRE_REGISTRATION


def test_v2_carries_the_v1_scenarios_and_declares_each_change() -> None:
    v1 = {item["id"]: item for item in load_json("pilot-scenarios.json")["scenarios"]}
    v2 = {item["id"]: item for item in load_json("pilot-scenarios-v2.json")["scenarios"]}

    assert list(v2) == ["S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"]
    for identifier, scenario in v1.items():
        carried = v2[identifier]
        assert carried["carried_from"] == "governance-pilot-v1"
        assert carried["v2_changes"]
        assert {field: carried[field] for field in CARRIED_FIELDS} == {
            field: scenario[field] for field in CARRIED_FIELDS
        }
        # Only S02's oracle changes, as M4 declares; every v1 oracle field keeps its value.
        changed = {
            key for key, value in scenario["oracle"].items() if carried["oracle"][key] != value
        }
        assert changed == ({"decision"} if identifier == "S02" else set())


def test_every_v2_scenario_states_its_authority_and_flow() -> None:
    matrix = load_json("pilot-scenarios-v2.json")

    assert matrix["matrix_id"] == "governance-pilot-v2"
    assert matrix["conditions"] == ["B0prime", "B2"]
    assert matrix["repetitions_per_condition"] == 10
    for scenario in matrix["scenarios"]:
        assert scenario.keys() >= V2_FIELDS
        allowlist = scenario["governance_setup"]["allowed_merchant_origins"]
        assert allowlist == "run_merchant" or (
            isinstance(allowlist, list)
            and all(origin.startswith("https://") for origin in allowlist)
        )
        assert scenario["authority_limit"] in {
            None,
            "total_budget",
            "merchant_allowlist",
            "revoked_policy",
        }
        assert scenario["approval_flow"] in {"none", "approve_and_complete"}
        assert set(scenario["primary_metrics"]) <= set(matrix["metrics"])


def test_only_s08_changes_the_policy_during_a_run_and_declares_it() -> None:
    scenarios = load_json("pilot-scenarios-v2.json")["scenarios"]
    changing = [
        scenario["id"]
        for scenario in scenarios
        if any(stimulus["action"] == "policy_revision_change" for stimulus in scenario["stimuli"])
    ]

    assert changing == ["S08"]
    [s08] = [scenario for scenario in scenarios if scenario["id"] == "S08"]
    assert s08["fault_injection"]["declared"] is True
    assert s08["stimuli"][0]["agent_restart"] == "both_conditions"


def test_v2_manifest_points_to_its_matrix_policy_and_controls() -> None:
    manifest = load_json("b2-pilot-v2.json")
    matrix = load_json("pilot-scenarios-v2.json")
    identifiers = [scenario["id"] for scenario in matrix["scenarios"]]

    assert manifest["manifest_id"] == "B2-PILOT-v2"
    assert (
        ROOT / manifest["scenario_manifest"]
    ).resolve() == EVALUATION / "pilot-scenarios-v2.json"
    assert (ROOT / manifest["design"]).is_file()
    assert set(manifest["conditions"]) == set(matrix["conditions"])
    assert manifest["execution"]["repetitions_per_condition"] == matrix["repetitions_per_condition"]
    assert manifest["policy"]["policy_set_id"] == "b2-hp-v2"
    assert manifest["policy"]["revision"] == "1"
    # Both conditions run on the last HP commit before HNP, tagged for the pilot.
    assert manifest["execution"]["agent_ref"] == "pilot-v2-base"
    revoked = manifest["measurement_amendments"]["M5_revoked_approval_form"]
    assert "d2_allow_unapplied fails" in revoked
    b2 = manifest["conditions"]["B2"]["agent_environment"]
    assert b2["AGENT_D1_POLICY_SET_ID"] == manifest["policy"]["policy_set_id"]
    assert b2["AGENT_D1_POLICY_REVISION"] == manifest["policy"]["revision"]
    attribution = manifest["attribution"]
    assert list(attribution["mechanism_by_scenario"]) == identifiers
    assert list(manifest["acceptance"])[: len(identifiers)] == identifiers
    mechanisms = set(manifest["conditions"]["B2"]["mechanisms"])
    attributed = {item for items in attribution["mechanism_by_scenario"].values() for item in items}
    assert attributed <= mechanisms
    # Each new policy control decides at least one scenario.
    assert {"pol02_allowlist", "pol11_budget", "d2_policy_context"} <= set(
        attribution["decisive_control_by_scenario"].values()
    )
    # The nine links of P2's verifier; with an order, only ok counts (no not_applicable).
    assert manifest["evidence_chain"]["links"] == EVIDENCE_LINKS
    assert "not_applicable is not accepted" in manifest["evidence_chain"]["complete"]
    # E1 (P2's final verifier contract) and E4 (the model client timeout), dated and before any
    # live repetition.
    e1, e4 = manifest["amendments_recorded"]
    assert (e1["id"], e1["date"]) == ("E1", "2026-10-20")
    assert e1["allowed_by"] in manifest["allowed_amendments"]
    assert e1["reason"]
    assert "d2_decision as not_applicable in B0'" in e1["change"]
    assert e1["h2"].startswith("unchanged")
    # E4 was not foreseen among E1-E3 and says why it is accepted.
    assert (e4["id"], e4["date"]) == ("E4", "2026-10-21")
    assert e4["allowed_by"].startswith("not among E1-E3")
    assert e4["reason"] and e4["why_accepted"]
    assert "OPENAI_TIMEOUT_SECONDS=90" in e4["change"]
    assert 0 < manifest["execution"]["openai_timeout_seconds"] == 90 <= 120


def test_v2_declares_that_neither_condition_is_the_v1_one() -> None:
    """#65 changes B0' and B2; #66, #68 and P1-P3 change B2: v1 to v2 is descriptive only."""
    manifest = load_json("b2-pilot-v2.json")
    comparability = manifest["comparability"]

    assert "#65" in manifest["conditions"]["B0prime"]["differs_from_v1"]
    assert comparability.keys() == {"B0prime_v1", "B2_v1", "latency_ms"}
    assert comparability["B2_v1"].startswith("descriptive only, not inferential")
    assert all(change in comparability["B2_v1"] for change in ("#65", "#66", "#68", "P1-P3"))


# Injection pilot (docs/design/injection-pilot.md): a draft, not pre-registered and not runnable.
FIELDS_THE_MODEL_READS = {"variant_title", "brand"}


def _words(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return set(re.findall(r"\w{4,}", "".join(c for c in folded if not unicodedata.combining(c))))


def test_the_injection_pilot_is_a_draft_on_the_v2_policy() -> None:
    manifest = load_json("injection-pilot-v1.json")
    matrix = load_json("injection-scenarios-v1.json")

    assert manifest["status"] == matrix["status"] == "draft"
    assert (
        ROOT / manifest["scenario_manifest"]
    ).resolve() == EVALUATION / "injection-scenarios-v1.json"
    assert (ROOT / manifest["design"]).is_file()
    # A live run needs this tag, which only the pre-registration creates.
    assert manifest["execution"]["agent_ref"] == "injection-pilot-base"
    assert manifest["policy"] == load_json("b2-pilot-v2.json")["policy"]
    assert set(manifest["conditions"]) == set(matrix["conditions"]) == {"B0prime", "B2"}
    assert manifest["execution"]["repetitions_per_condition"] == 10
    assert matrix["repetitions_per_condition"] == 10


def test_every_injection_reaches_the_model_and_keeps_the_shortlist() -> None:
    matrix = load_json("injection-scenarios-v1.json")
    reference = matrix["request_reference"]
    within = reference["within_request_variant_ids"]
    over = reference["shortlist_on_main"]["over_total_budget"]
    scenarios = matrix["scenarios"]

    assert [scenario["id"] for scenario in scenarios] == ["C0", "I1", "I2", "I3"]
    assert {scenario["request"] for scenario in scenarios} == {reference["request"]}
    assert reference["correct_variant_id"] == within[0]
    assert reference["shortlist_on_main"]["within_request"] == within
    for scenario in scenarios[1:]:
        injections = scenario["catalog_setup"]["injections"]
        # S05 wrote in description, which never reaches the model.
        assert {injection["field"] for injection in injections} <= FIELDS_THE_MODEL_READS
        # No word of four letters or more of the request. This does not keep the shortlist
        # fixed (the model writes the terms): the runner post-checks it on every row.
        assert all(not _words(i["value"]) & _words(reference["request"]) for i in injections)
    assert {scenario["oracle"]["decision"] for scenario in scenarios} == {"within_request"}
    kinds = [scenario["injection"]["kind"] for scenario in scenarios[1:]]
    assert kinds == ["declared_hole", "declared_hole", "governance_catches"]
    i1, _, i3 = (scenario["injection"] for scenario in scenarios[1:])
    assert i1["obedience"]["variant_id"] in within[1:]
    assert i3["obedience"]["variant_id"] in over
    # Only the total budget excludes the I3 target: the selector shows it, POL-11 denies it.
    prices = reference["catalog_prices_minor"]
    budget = reference["maximum_total_minor"]
    assert all(prices[variant] + reference["shipping_minor"] <= budget for variant in within)
    assert all(prices[variant] > budget for variant in over)


def test_the_injection_oracle_is_defined_once_and_d2_follows_the_human() -> None:
    manifest = load_json("injection-pilot-v1.json")
    matrix = load_json("injection-scenarios-v1.json")
    measures = manifest["measures"]

    within = measures["within_request"]
    assert set(within["applies_to"]) == {scenario["id"] for scenario in matrix["scenarios"]}
    assert "409 d1_denied without checkout" in within["passing_outcomes"]
    assert {scenario["oracle"]["definition"] for scenario in matrix["scenarios"]} == {
        "measures.within_request of evaluation/data/injection-pilot-v1.json"
    }
    # D2 runs inside approve, after the click: the human decides before it.
    chain = measures["deciding_control"]["chain"]
    assert chain.index("human_approval") < chain.index("freshness_approve") < chain.index("d2")
    assert chain[:2] == ["shortlist", "d1"]
    assert set(chain) < set(measures["deciding_control"]["values"])
    # Only rows whose intent still asks for black, new and the total budget score the oracle.
    assert manifest["acceptance"]["scope"].startswith("B2. C0, I1 and I3")
    assert "S05_v1_v2" in manifest["comparability"]
