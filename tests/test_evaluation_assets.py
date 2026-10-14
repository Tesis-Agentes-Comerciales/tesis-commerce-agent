import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVALUATION = ROOT / "evaluation"


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
