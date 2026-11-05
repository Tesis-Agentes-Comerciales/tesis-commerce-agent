import json
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

import pytest

from commerce_evaluation.p0_observed import (
    CT19_RUN,
    P0,
    load,
    unique_object,
    validate,
    validate_ct19_archive,
)

ROOT = Path(__file__).resolve().parents[2]


def test_historical_register_preserves_denominators_and_gaps():
    result = validate(ROOT)
    assert result["denominators"] == {
        "scenarios": 33,
        "scenario_modes": 56,
        "measurable_scenario_modes": 55,
        "preregistered_cells": 37,
    }
    assert result["runs"] == 23 and result["internal_hashes"] == 446
    assert result["scenario_modes"] == {
        "passed": 7,
        "partial": 3,
        "failed": 0,
        "blocked": 1,
        "harness_error": 0,
        "not_executed": 44,
        "not_applicable": 1,
    }
    assert len(result["scenarios_without_execution"]) == 23
    pending = set(result["not_executed"])
    assert {"CT19/HNP", "INF33/HNP", "UX01/HP", "UX11/HP", "UX12/HP"} <= pending
    assert {"CT19/HP", "INF33/HP"}.isdisjoint(pending)


def test_measurable_denominator_changes_with_an_approved_exclusion():
    data = load(ROOT / P0 / "matrix-observed.json")
    del data["observations"]["INF33"]
    result = validate(ROOT, data)
    assert result["denominators"]["measurable_scenario_modes"] == 56
    assert result["scenario_modes"]["not_applicable"] == 0
    assert result["scenario_modes"]["not_executed"] == 45


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


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("scope", "Unauthorized not_applicable pair"),
        ("wrong_mode", "Unauthorized not_applicable pair"),
        ("missing_justification", "requires justification"),
        ("claims_execution", "no execution evidence"),
        ("wrong_source", "Unapproved not_applicable source"),
    ],
)
def test_not_applicable_requires_approved_pair_and_versioned_justification(mutation, message):
    data = load(ROOT / P0 / "matrix-observed.json")
    rows = data["observations"]
    cell = rows["INF33"]["modes"]["HP"]
    if mutation == "scope":
        rows["INF31"]["modes"]["HP"] = cell
    elif mutation == "wrong_mode":
        rows["INF33"]["modes"]["HNP"] = cell
    elif mutation == "missing_justification":
        cell["justification"] = " "
    elif mutation == "claims_execution":
        cell["run_id"] = 42033289049
    else:
        cell["source"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match=message):
        validate(ROOT, data)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("case", "CT19:HP:stock_zero", "Invalid CT19 HP attribution"),
        ("run_id", 41968564766, "Invalid CT19 HP attribution"),
        ("agent_sha", "0" * 40, "CT19 terminal attribution"),
    ],
)
def test_ct19_only_attributes_the_terminal_removal_control(field, value, message):
    data = load(ROOT / P0 / "matrix-observed.json")
    data["observations"]["CT19"]["modes"]["HP"][field] = value
    with pytest.raises(ValueError, match=message):
        validate(ROOT, data)


@pytest.mark.parametrize("update_outer_hash", [False, True])
def test_ct19_rejects_corruption_even_with_rewritten_outer_hash(tmp_path, update_outer_hash):
    directory = Path("evaluation/results/ct19-hp")
    row = load(ROOT / directory / "runs.json")[CT19_RUN]
    archive = tmp_path / directory / row["original_zip"]
    archive.parent.mkdir(parents=True)
    with (
        ZipFile(ROOT / directory / row["original_zip"]) as original,
        ZipFile(archive, "w") as changed,
    ):
        for name in original.namelist():
            data = original.read(name)
            changed.writestr(name, data + b" " if name == "exp01-junit.xml" else data)
    if update_outer_hash:
        row["bytes"] = archive.stat().st_size
        row["zip_sha256"] = sha256(archive.read_bytes()).hexdigest()
    message = "CT19 internal hash" if update_outer_hash else "CT19 ZIP hash"
    with pytest.raises(ValueError, match=message):
        validate_ct19_archive(tmp_path, CT19_RUN, row, ROOT / "evaluation/scenarios/ct19-hp.json")
