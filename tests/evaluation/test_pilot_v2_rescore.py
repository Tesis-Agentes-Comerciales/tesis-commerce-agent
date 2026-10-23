"""Scoring the frozen B2-PILOT-v2 results again keeps every number they published, and
`--combine` rebuilds the combination that counts from its four invocations.

Lote 1 ran the eight scenarios; lote 2 ran S05, S06 and S08 again, one invocation each, and the
combination takes those three from lote 2 (b2-pilot-v2-report.md, section 1). These tests read the
committed results and never write them; no service or model runs.
"""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation import governance_pilot
from commerce_evaluation.governance_pilot import (
    PILOT_V2_MANIFEST,
    PilotError,
    combine,
    load_manifest,
    manifest_matrix,
    rescore,
)
from commerce_evaluation.paths import RESULTS
from tests.evaluation.test_pilot_rescore import digest, missing_or_changed

V2 = RESULTS / "b2-pilot-v2"
# One file per invocation, in the order the combination takes them.
INVOCATIONS = tuple(
    V2 / f"{name}.jsonl" for name in ("pilot-v2-20261021", "lote2-S05", "lote2-S06", "lote2-S08")
)
COMBINED = V2 / "pilot-v2-combinado.jsonl"
# `created_at` is when a file was scored; `output` keeps the runner's absolute path
# (results/README.md).
UNSCORED = ("created_at", "output")
# What the rescore recomputes on a row besides its metrics.
SCORED = ("evidence_presence", "oracle_errors", "policy_mismatch")


def matrix() -> dict[str, Any]:
    return governance_pilot._load_matrix(manifest_matrix(load_manifest(PILOT_V2_MANIFEST)))


def recorded_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def published(path: Path) -> dict[str, Any]:
    summary = json.loads(path.with_suffix(".summary.json").read_text(encoding="utf-8"))
    return {key: value for key, value in summary.items() if key not in UNSCORED}


def sha256sums(directory: Path) -> dict[Path, str]:
    """The digest a results directory's SHA256SUMS lists for each of its files."""
    lines = (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    return {directory / name: value for value, name in (line.split() for line in lines if line)}


def combine_arguments(*extra: str) -> list[str]:
    return [
        "governance-pilot",
        "--manifest",
        str(PILOT_V2_MANIFEST),
        "--combine",
        *map(str, INVOCATIONS),
        *extra,
    ]


def test_the_frozen_v2_files_keep_the_digests_of_their_sha256sums() -> None:
    frozen = sha256sums(V2)

    assert len(frozen) == 10
    assert {path: digest(path) for path in frozen} == frozen


@pytest.mark.parametrize("path", INVOCATIONS, ids=lambda path: path.stem)
def test_rescoring_each_invocation_reproduces_every_recorded_row(path: Path) -> None:
    rows = recorded_rows(path)

    records, _ = rescore(path, matrix())

    for before, after in zip(rows, records, strict=True):
        label = (before["condition"], before["scenario_id"], before["repetition"])
        metrics = {name: after["metrics"][name] for name in before["metrics"]}
        assert metrics == before["metrics"], label
        assert {name: after.get(name) for name in SCORED} == {
            name: before.get(name) for name in SCORED
        }, label


@pytest.mark.parametrize("path", [*INVOCATIONS, COMBINED], ids=lambda path: path.stem)
def test_rescoring_keeps_every_number_of_the_published_summary(path: Path) -> None:
    _, rescored = rescore(path, matrix())

    assert missing_or_changed(published(path), rescored) == []


def test_the_combination_keeps_the_figures_the_thesis_reports() -> None:
    _, rescored = rescore(COMBINED, matrix())

    totals = rescored["by_condition"]
    cells = rescored["by_condition_scenario"]

    def runs_with_any(condition: str, metric: str, *scenarios: str) -> tuple[int, int]:
        counts = [cells[condition][scenario][metric] for scenario in scenarios]
        return (
            sum(count["runs_with_any"] for count in counts),
            sum(count["runs_measured"] for count in counts),
        )

    without_rules, with_rules = "B0prime", "B2"
    assert rescored["harness_errors"] == 0
    # Right decisions: 32/80 without the rules and 79/80 with them.
    assert (totals[without_rules]["oracle_passes"], totals[without_rules]["evaluated_runs"]) == (
        32,
        80,
    )
    assert (totals[with_rules]["oracle_passes"], totals[with_rules]["evaluated_runs"]) == (79, 80)
    # Over budget (S02, S07): 18/20 and 0/20.
    assert runs_with_any(without_rules, "over_budget_checkouts", "S02", "S07") == (18, 20)
    assert runs_with_any(with_rules, "over_budget_checkouts", "S02", "S07") == (0, 20)
    # Outside what was authorized (S06, S08): 20/20 and 0/20.
    assert runs_with_any(without_rules, "authority_violations", "S06", "S08") == (20, 20)
    assert runs_with_any(with_rules, "authority_violations", "S06", "S08") == (0, 20)
    # Changed price (S04): stale terms accepted 10/10 and 0/10.
    assert [
        (cells[condition]["S04"]["stale_terms_accepted"], cells[condition]["S04"]["runs"])
        for condition in (without_rules, with_rules)
    ] == [(10, 10), (0, 10)]


def test_only_the_combination_flags_policy_mismatches_because_it_mixes_invocations() -> None:
    """The D1 key, and so each list's artifact digest, is new in every invocation
    (b2-pilot-v2-report.md, section 9): the 20 of B2 are S05 and S08, and no file alone has one."""
    _, combined = rescore(COMBINED, matrix())

    flagged = {
        (condition, scenario): cell["policy_mismatches"]
        for condition, cells in combined["by_condition_scenario"].items()
        for scenario, cell in cells.items()
        if cell["policy_mismatches"]
    }
    assert flagged == {("B2", "S05"): 10, ("B2", "S08"): 10}
    for path in INVOCATIONS:
        _, alone = rescore(path, matrix())
        assert alone["by_condition"]["B2"]["policy_mismatches"] == 0, path.name


def test_combine_takes_each_scenario_whole_from_the_last_file_that_ran_it(tmp_path: Path) -> None:
    lines: dict[str, list[str]] = {}
    for line in INVOCATIONS[0].read_text(encoding="utf-8").splitlines():
        lines.setdefault(json.loads(line)["scenario_id"], []).append(line)
    s01, s02 = lines["S01"], lines["S02"]
    first, later = tmp_path / "first.jsonl", tmp_path / "later.jsonl"
    first.write_text(f"{s01[0]}\n{s02[0]}\n{s01[1]}\n", encoding="utf-8")
    later.write_text(f"{s02[5]}\n", encoding="utf-8")

    # One row of S02 in the later file replaces every S02 row of the first; never single rows.
    assert combine([first, later]) == [s01[0], s01[1], s02[5]]
    assert combine([later, first]) == [s01[0], s02[0], s01[1]]


@pytest.mark.parametrize(
    ("field", "value"),
    [("manifest_id", "B2-PILOT-v1"), ("agent_commit", "0" * 40), ("model", "gpt-5")],
)
def test_combine_refuses_results_of_another_run(tmp_path: Path, field: str, value: str) -> None:
    other = tmp_path / "other.jsonl"
    other.write_text(
        json.dumps(recorded_rows(INVOCATIONS[1])[0] | {field: value}) + "\n", encoding="utf-8"
    )

    with pytest.raises(PilotError, match="one registered run combines"):
        combine([INVOCATIONS[0], other])


def test_combine_rebuilds_the_published_combination_and_its_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    before = sorted(V2.iterdir())
    output = tmp_path / COMBINED.name
    monkeypatch.setattr(sys, "argv", combine_arguments("--output", str(output)))

    code = governance_pilot.main()

    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output.with_suffix(".summary.json").read_text(encoding="utf-8"))
    assert code == 0
    # The same bytes the frozen combination's SHA256SUMS lists.
    assert digest(output) == sha256sums(V2)[COMBINED]
    assert printed == written
    assert written["output"] == str(output.resolve())
    assert missing_or_changed(published(COMBINED), written) == []
    assert sorted(V2.iterdir()) == before
    frozen = sha256sums(V2)
    assert {path: digest(path) for path in frozen} == frozen


def test_combine_needs_an_output_and_never_writes_over_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", combine_arguments())

    assert governance_pilot.main() == 1
    assert "--output" in capsys.readouterr().err

    output = tmp_path / "combined.jsonl"
    output.with_suffix(".summary.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", combine_arguments("--output", str(output)))

    assert governance_pilot.main() == 1
    assert "Will not write over" in capsys.readouterr().err
    assert not output.exists()
    assert output.with_suffix(".summary.json").read_text(encoding="utf-8") == "{}\n"


def test_a_combination_that_cannot_be_scored_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without `--manifest` the runner scores with the v1 matrix, which lacks S06-S08; nothing
    stays behind, so the same command runs again with the manifest."""
    output = tmp_path / "combined.jsonl"
    arguments = ["governance-pilot", "--combine", *map(str, INVOCATIONS), "--output", str(output)]
    monkeypatch.setattr(sys, "argv", arguments)

    assert governance_pilot.main() == 1
    assert "Scenarios not in the matrix" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []
