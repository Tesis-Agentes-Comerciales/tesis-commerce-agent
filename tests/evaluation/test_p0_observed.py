import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from commerce_evaluation.p0_observed import P0, load, unique_object, validate

ROOT = Path(__file__).resolve().parents[2]


def test_historical_register_preserves_denominators_and_gaps():
    result = validate(ROOT)
    assert result["denominators"] == {
        "scenarios": 33,
        "scenario_modes": 56,
        "preregistered_cells": 37,
    }
    assert result["runs"] == 18 and result["internal_hashes"] == 362
    assert result["scenario_modes"] == {
        "passed": 6,
        "partial": 3,
        "failed": 0,
        "blocked": 1,
        "harness_error": 0,
        "not_executed": 46,
    }
    assert len(result["scenarios_without_execution"]) == 24
    assert {"CT19/HP", "CT19/HNP", "UX01/HP", "UX11/HP", "UX12/HP"} <= set(result["not_executed"])


def test_inf31_block_is_the_native_status_not_an_agent_commercial_failure():
    observed = load(ROOT / P0 / "matrix-observed.json")
    cell = observed["observations"]["INF31"]["modes"]["HP"]
    assert cell["status"] == "blocked"
    with ZipFile(ROOT / observed["runs"][str(cell["run_id"])]["archive"]) as archive:
        report = json.loads(archive.read("exp01-report.json"))
    native = report["controls"]["INF31:merchant_503"]["observations"]
    assert native["upstream_answered"] is True
    assert native["merchant_http_status"] == 500
    assert native["agent_result"] == "d1_dispatch_uncertain"


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("bad_hash", "Archive hash mismatch"),
        ("wrong_mode", "No attributable evidence"),
        ("infrastructure", "No attributable evidence"),
        ("promoted_block", "Outcome mismatch"),
        ("wrong_protocol", "Frozen protocol mismatch"),
    ],
)
def test_validator_rejects_misattributed_or_changed_evidence(mutation, error):
    data = load(ROOT / P0 / "matrix-observed.json")
    cells = data["observations"]
    if mutation == "bad_hash":
        data["runs"]["41895460657"]["sha256"] = "0" * 64
    elif mutation == "wrong_mode":
        cells["CT15"]["modes"]["HNP"]["run_id"] = 41895460657
    elif mutation == "infrastructure":
        cells["CT15"]["modes"]["HP"]["run_id"] = 41915169656
    elif mutation == "promoted_block":
        cells["INF31"]["modes"]["HP"].update(status="passed", coverage="bounded")
    else:
        data["runs"]["41895460657"]["protocol"] = "evaluation/scenarios/exp03-stock-shipping.json"
    with pytest.raises(ValueError, match=error):
        validate(ROOT, data)


def test_duplicate_modes_cannot_silently_overwrite_evidence():
    with pytest.raises(ValueError, match="Duplicate JSON key: HP"):
        json.loads(
            '{"HP": {"status": "failed"}, "HP": {"status": "passed"}}',
            object_pairs_hook=unique_object,
        )
