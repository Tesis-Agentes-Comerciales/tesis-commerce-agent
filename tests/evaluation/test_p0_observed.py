import json
import shutil
from hashlib import sha256
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from commerce_evaluation.p0_observed import (
    CT19_RUN,
    P0,
    load,
    unique_object,
    validate,
    validate_ct19_archive,
    validate_local_cell,
    validate_local_run,
)

ROOT = Path(__file__).resolve().parents[2]
LOCAL_RUN = "p0-model-failures-corrida-2"
LOCAL_RESULTS = Path("evaluation/results/p0-model-failures")
MODEL_FAILURE_CELLS = {
    ("INF27", "HP"): ["INF27-HP"],
    ("INF27", "HNP"): ["INF27-HNP"],
    ("INF28", "HP"): ["INF28-HP"],
    ("INF28", "HNP"): ["INF28-HNP"],
    ("INF29", "HP"): ["INF29-HP"],
    ("INF29", "HNP"): ["INF29-HNP"],
    ("INF33", "HNP"): ["INF33-HNP-a", "INF33-HNP-b"],
}


def test_historical_register_preserves_denominators_and_gaps():
    result = validate(ROOT)
    assert result["denominators"] == {
        "scenarios": 33,
        "scenario_modes": 56,
        "measurable_scenario_modes": 55,
        "preregistered_cells": 37,
    }
    assert result["runs"] == 23 and result["internal_hashes"] == 446
    assert result["local_runs"] == 2 and result["local_hashes"] == 6
    assert result["scenario_modes"] == {
        "passed": 14,
        "partial": 3,
        "failed": 0,
        "blocked": 1,
        "harness_error": 0,
        "not_executed": 37,
        "not_applicable": 1,
    }
    assert len(result["scenarios_without_execution"]) == 19
    pending = set(result["not_executed"])
    assert {"CT19/HNP", "INF30/HNP", "UX01/HP", "UX11/HP", "UX12/HP"} <= pending
    assert {"CT19/HP", "INF33/HP", "INF33/HNP"}.isdisjoint(pending)


def test_measurable_denominator_changes_with_an_approved_exclusion():
    data = load(ROOT / P0 / "matrix-observed.json")
    del data["observations"]["INF33"]
    result = validate(ROOT, data)
    assert result["denominators"]["measurable_scenario_modes"] == 56
    assert result["scenario_modes"]["not_applicable"] == 0
    assert result["scenario_modes"]["not_executed"] == 39


def test_model_failure_cells_are_attributed_to_the_registered_local_run():
    data = load(ROOT / P0 / "matrix-observed.json")
    pending = set(validate(ROOT, data)["not_executed"])
    for (scenario, mode), cases in MODEL_FAILURE_CELLS.items():
        cell = data["observations"][scenario]["modes"][mode]
        assert (cell["status"], cell["local_run"], cell["cases"]) == ("passed", LOCAL_RUN, cases)
        assert cell["prior_attempts"] == ["p0-model-failures-corrida-1"]
        assert f"{scenario}/{mode}" not in pending
    assert data["observations"]["INF33"]["modes"]["HP"]["status"] == "not_applicable"
    assert data["local_runs"]["p0-model-failures-corrida-1"]["kind"] == "harness_error"


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("protocol_sha256", "Local protocol hash"),
        ("summary_sha256", "Local evidence hash"),
        ("junit_sha256", "Local evidence hash"),
    ],
)
def test_local_run_rejects_a_changed_register_hash(field, message):
    data = load(ROOT / P0 / "matrix-observed.json")
    data["local_runs"][LOCAL_RUN][field] = "0" * 64
    with pytest.raises(ValueError, match=message):
        validate(ROOT, data)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("harness_run", "No attributable local evidence"),
        ("unknown_run", "No attributable local evidence"),
        ("mode_without_cases", "No attributable local evidence"),
        ("wrong_cases", "No attributable local evidence"),
        ("partial_cases", "No attributable local evidence"),
        ("control_as_cell", "No attributable local evidence"),
        ("no_evidence", "No attributable evidence"),
        ("claims_actions_run", "Inconsistent local evidence"),
        ("partial_status", "Inconsistent local evidence"),
        ("same_prior", "Unrelated prior attempt"),
    ],
)
def test_local_cells_only_attribute_the_measured_cells_of_their_pair(mutation, message):
    data = load(ROOT / P0 / "matrix-observed.json")
    rows = data["observations"]
    cell = rows["INF27"]["modes"]["HP"]
    if mutation == "harness_run":
        rows["INF29"]["modes"]["HP"]["local_run"] = "p0-model-failures-corrida-1"
    elif mutation == "unknown_run":
        cell["local_run"] = "p0-model-failures-corrida-3"
    elif mutation == "mode_without_cases":
        rows["INF31"]["modes"]["HNP"] = rows["INF29"]["modes"]["HNP"]
    elif mutation == "wrong_cases":
        rows["INF33"]["modes"]["HNP"]["cases"] = ["INF27-HNP"]
    elif mutation == "partial_cases":
        rows["INF33"]["modes"]["HNP"]["cases"] = ["INF33-HNP-a"]
    elif mutation == "control_as_cell":
        cell["cases"] = ["INF27-HP", "INF27-HP-control"]
    elif mutation == "no_evidence":
        del cell["local_run"], cell["cases"]
    elif mutation == "claims_actions_run":
        cell["run_id"] = 41846571407
    elif mutation == "partial_status":
        cell.update(status="partial", coverage="partial")
    else:
        cell["prior_attempts"] = [LOCAL_RUN]
    with pytest.raises(ValueError, match=message):
        validate(ROOT, data)


def local_copy(root: Path) -> dict[str, Any]:
    """Copy the evidence of the local runs under `root` and return their register rows."""
    for ref in ("evaluation/scenarios/p0-model-failures.json", "docs/design/p0-model-failures.md"):
        (root / ref).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / ref, root / ref)
    shutil.copytree(ROOT / LOCAL_RESULTS, root / LOCAL_RESULTS)
    return load(ROOT / P0 / "matrix-observed.json")["local_runs"]


def reseal(root: Path, row: dict[str, Any], name: str, data: bytes) -> None:
    """Rewrite an evidence file together with its SHA256SUMS line and its register hash."""
    directory = root / LOCAL_RESULTS
    (directory / name).write_bytes(data)
    new = sha256(data).hexdigest()
    ledger = directory / "SHA256SUMS"
    lines = [
        f"{new}  {name}" if line.split("  ", 1)[1] == name else line
        for line in ledger.read_text(encoding="utf-8").splitlines()
    ]
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
    row["summary_sha256" if name == row["summary"] else "junit_sha256"] = new


def summary_of(root: Path) -> dict[str, Any]:
    return json.loads((root / LOCAL_RESULTS / "summary.json").read_bytes())


@pytest.mark.parametrize("name", ["summary.json", "README.md"])
def test_local_run_rejects_a_file_that_differs_from_its_ledger(tmp_path, name):
    row = local_copy(tmp_path)[LOCAL_RUN]
    path = tmp_path / LOCAL_RESULTS / name
    path.write_bytes(path.read_bytes() + b" ")
    if name == row["summary"]:  # even with the register hash rewritten, the ledger rejects it
        row["summary_sha256"] = sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match=f"Local checksum: {LOCAL_RUN}/{name}"):
        validate_local_run(tmp_path, LOCAL_RUN, row)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("spec_mismatch", "Local run provenance mismatch"),
        ("dirty_checkout", "Local run provenance mismatch"),
        ("provider_call", "Local run provenance mismatch"),
        ("execution_order", "Local execution order mismatch"),
        ("status_without_junit", "Local JUnit/summary mismatch"),
    ],
)
def test_local_run_requires_protocol_match_and_its_own_junit(tmp_path, mutation, message):
    row = local_copy(tmp_path)[LOCAL_RUN]
    summary = summary_of(tmp_path)
    if mutation == "spec_mismatch":
        summary["spec"]["matches_protocol"] = False
    elif mutation == "dirty_checkout":
        summary["checkouts"]["agent"]["dirty"] = True
    elif mutation == "provider_call":
        summary["model"]["real_provider_calls"] = 1
    elif mutation == "execution_order":
        summary["execution_order"]["matches_protocol"] = False
    else:
        summary["cells"]["INF33-HNP-b"]["runs"][0]["status"] = "failed"
    reseal(tmp_path, row, "summary.json", json.dumps(summary).encode())
    with pytest.raises(ValueError, match=message):
        validate_local_run(tmp_path, LOCAL_RUN, row)


@pytest.mark.parametrize("case", ["INF27-HP", "CTRL-HP"])
def test_local_cell_requires_its_cases_and_mode_controls_in_every_repetition(tmp_path, case):
    rows = local_copy(tmp_path)
    cell = load(ROOT / P0 / "matrix-observed.json")["observations"]["INF27"]["modes"]["HP"]
    verified = {name: validate_local_run(tmp_path, name, row) for name, row in rows.items()}
    validate_local_cell(("INF27", "HP"), cell, rows, verified)
    # A consistent third-repetition failure: summary, JUnit, ledger and register all agree.
    summary = summary_of(tmp_path)
    result = summary["cells"][case]
    oracle = next(iter(result["oracles"]))
    result["oracles"][oracle] = result["repetitions_passed"] = "2/3"
    result["runs"][2].update(status="failed", failed_oracles=[oracle])
    summary["totals"].update(passed=35, failed=1)
    junit = ElementTree.fromstring((tmp_path / LOCAL_RESULTS / "junit.xml").read_bytes())
    node = next(n for n in junit.iter("testcase") if n.get("name", "").endswith(f"[{case}-r3]"))
    ElementTree.SubElement(node, "failure", message=oracle)
    row = rows[LOCAL_RUN]
    reseal(tmp_path, row, "summary.json", json.dumps(summary).encode())
    reseal(tmp_path, row, "junit.xml", ElementTree.tostring(junit, encoding="utf-8"))
    row["junit_counts"].update(passed=35, failed=1)
    verified[LOCAL_RUN] = validate_local_run(tmp_path, LOCAL_RUN, row)
    with pytest.raises(ValueError, match=f"Not passed in every repetition: {case}"):
        validate_local_cell(("INF27", "HP"), cell, rows, verified)


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
