"""One invocation interleaves B0' and B2 by repetition, as the pre-registered manifest orders."""

import io
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation import governance_pilot
from commerce_evaluation.governance_pilot import (
    CONDITIONS,
    ConditionMismatchError,
    D1RunKeys,
    PilotError,
    PlannedRun,
    RepositoryProvenance,
    build_plan,
    condition_environments,
    parse_conditions,
    pilot_manifest_id,
    repository_provenance,
    require_clean_worktree,
    run_directory,
    run_plan,
    summarize,
)
from commerce_evaluation.paths import DATA

B0_PRIME, B2 = CONDITIONS["B0prime"], CONDITIONS["B2"]
MERCHANT = "http://127.0.0.1:4120"
MATRIX = {"matrix_id": "governance-pilot-v1", "baseline_id": "B0-2026-10-06"}
METADATA = {
    "manifest_id": "B2-PILOT-v1",
    "agent_commit": "0" * 40,
    "agent_worktree_dirty": False,
}


def scenario(identifier: str) -> dict[str, Any]:
    return {"id": identifier, "name": f"scenario {identifier}"}


def steps(plan: list[PlannedRun]) -> list[tuple[str, int, str]]:
    return [(run.scenario["id"], run.repetition, run.condition.name) for run in plan]


def record(run: PlannedRun, *, passed: int, latency_ms: int = 100) -> dict[str, Any]:
    return {
        "condition": run.condition.name,
        "scenario_id": run.scenario["id"],
        "repetition": run.repetition,
        "metrics": {
            "valid_task_success": passed,
            "hard_constraint_violation": 0,
            "unnecessary_clarification": 0,
            "unsafe_effect_before_approval": 0,
            "stale_terms_accepted": 0,
            "prompt_injection_compliance": 0,
            "trace_completeness": 0.5,
            "latency_ms": latency_ms,
            "model_input_tokens": None,
            "model_output_tokens": None,
        },
    }


def test_one_condition_keeps_the_scenario_then_repetition_order() -> None:
    plan = build_plan([scenario("S01"), scenario("S02")], 2, (B0_PRIME,))

    assert steps(plan) == [
        ("S01", 1, "B0prime"),
        ("S01", 2, "B0prime"),
        ("S02", 1, "B0prime"),
        ("S02", 2, "B0prime"),
    ]


def test_conditions_interleave_and_alternate_which_goes_first() -> None:
    plan = build_plan([scenario("S01"), scenario("S04")], 3, (B0_PRIME, B2))

    assert steps(plan) == [
        ("S01", 1, "B0prime"),
        ("S01", 1, "B2"),
        ("S01", 2, "B2"),
        ("S01", 2, "B0prime"),
        ("S01", 3, "B0prime"),
        ("S01", 3, "B2"),
        ("S04", 1, "B0prime"),
        ("S04", 1, "B2"),
        ("S04", 2, "B2"),
        ("S04", 2, "B0prime"),
        ("S04", 3, "B0prime"),
        ("S04", 3, "B2"),
    ]


def test_the_given_order_decides_the_first_condition_of_odd_repetitions() -> None:
    plan = build_plan([scenario("S01")], 2, (B2, B0_PRIME))

    assert steps(plan) == [
        ("S01", 1, "B2"),
        ("S01", 1, "B0prime"),
        ("S01", 2, "B0prime"),
        ("S01", 2, "B2"),
    ]


def test_conditions_default_to_b0_prime_and_reject_repeats() -> None:
    assert parse_conditions(None) == (B0_PRIME,)
    assert parse_conditions(["B0", "B2"]) == (B0_PRIME, B2)
    # "B0" is B0' on this commit, so asking for both would run B0' twice per repetition.
    with pytest.raises(PilotError, match="B0prime"):
        parse_conditions(["B0prime", "B0"])


def test_d1_keys_are_generated_once_and_only_for_b2(monkeypatch: pytest.MonkeyPatch) -> None:
    generated: list[D1RunKeys] = []
    original = D1RunKeys.generate

    def counting() -> D1RunKeys:
        generated.append(original())
        return generated[-1]

    monkeypatch.setattr(D1RunKeys, "generate", staticmethod(counting))

    off_only = condition_environments((B0_PRIME,), MERCHANT)
    assert generated == []
    assert not any(name.startswith("AGENT_D1_") for name in off_only["B0prime"])

    both = condition_environments((B0_PRIME, B2), MERCHANT)
    assert len(generated) == 1
    assert both["B2"]["AGENT_D1_HMAC_KEY_HEX"] == generated[0].hmac_hex
    assert both["B2"]["AGENT_D1_AES_KEY_HEX"] == generated[0].aes_hex
    assert not any(name.startswith("AGENT_D1_") for name in both["B0prime"])


def test_each_condition_runs_its_agent_on_its_own_directory(tmp_path: Path) -> None:
    [first, second] = build_plan([scenario("S01")], 1, (B0_PRIME, B2))

    assert run_directory(tmp_path, first) != run_directory(tmp_path, second)
    assert run_directory(tmp_path, first).parent == tmp_path


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / "agent.py").write_text("print('agent')\n", encoding="utf-8")
    git(tmp_path, "add", "agent.py")
    git(
        tmp_path,
        "-c",
        "user.name=Pilot",
        "-c",
        "user.email=pilot@example.test",
        "commit",
        "-q",
        "-m",
        "agent",
    )
    return tmp_path


def test_provenance_reads_the_commit_and_a_clean_tree(repository: Path) -> None:
    # Result files the runner writes are untracked: they do not make the agent dirty.
    (repository / "results.jsonl").write_text("{}\n", encoding="utf-8")

    provenance = repository_provenance(repository)

    assert provenance == RepositoryProvenance(git(repository, "rev-parse", "HEAD"), False)
    require_clean_worktree(provenance, allow_dirty=False)


def test_a_modified_agent_aborts_unless_dirty_runs_are_allowed(repository: Path) -> None:
    (repository / "agent.py").write_text("print('changed')\n", encoding="utf-8")

    provenance = repository_provenance(repository)

    assert provenance.worktree_dirty is True
    with pytest.raises(PilotError, match="--allow-dirty"):
        require_clean_worktree(provenance, allow_dirty=False)
    require_clean_worktree(provenance, allow_dirty=True)


def test_provenance_outside_git_is_a_pilot_error(tmp_path: Path) -> None:
    with pytest.raises(PilotError, match="commit"):
        repository_provenance(tmp_path)


def test_manifest_id_needs_both_conditions_on_its_scenario_matrix(tmp_path: Path) -> None:
    matrix = DATA / "pilot-scenarios.json"

    assert pilot_manifest_id((B0_PRIME, B2), matrix) == "B2-PILOT-v1"
    assert pilot_manifest_id((B2, B0_PRIME), matrix) == "B2-PILOT-v1"
    # One condition alone is not the pre-registered paired design.
    assert pilot_manifest_id((B2,), matrix) is None
    assert pilot_manifest_id((B0_PRIME,), matrix) is None
    assert pilot_manifest_id((B0_PRIME, B2), tmp_path / "other-matrix.json") is None


def test_rows_carry_provenance_and_harness_errors_do_not_stop_the_plan() -> None:
    plan = build_plan([scenario("S01")], 2, (B0_PRIME, B2))
    executed: list[PlannedRun] = []

    def execute(run: PlannedRun) -> dict[str, Any]:
        executed.append(run)
        if (run.repetition, run.condition.name) == (1, "B2"):
            raise RuntimeError("merchant seed failed")
        return record(run, passed=1)

    output = io.StringIO()
    records = run_plan(plan, execute, matrix=MATRIX, run_metadata=METADATA, result_file=output)

    assert executed == plan
    assert [json.loads(line) for line in output.getvalue().splitlines()] == records
    assert all({key: row[key] for key in METADATA} == METADATA for row in records)
    failed = records[1]
    assert failed["harness_error"] == "RuntimeError: merchant seed failed"
    assert (failed["condition"], failed["scenario_id"], failed["repetition"]) == ("B2", "S01", 1)
    assert failed["governance_mode"] is None


def test_a_condition_mismatch_aborts_the_whole_invocation() -> None:
    plan = build_plan([scenario("S01")], 2, (B0_PRIME, B2))
    executed: list[PlannedRun] = []

    def execute(run: PlannedRun) -> dict[str, Any]:
        executed.append(run)
        if run.condition.name == "B2":
            raise ConditionMismatchError("Agent /health reports another condition")
        return record(run, passed=1)

    output = io.StringIO()
    with pytest.raises(ConditionMismatchError):
        run_plan(plan, execute, matrix=MATRIX, run_metadata=METADATA, result_file=output)

    assert steps(executed) == [("S01", 1, "B0prime"), ("S01", 1, "B2")]
    assert len(output.getvalue().splitlines()) == 1


def test_summary_reports_each_condition_and_scenario_over_evaluated_rows() -> None:
    plan = build_plan([scenario("S01"), scenario("S02")], 2, (B0_PRIME, B2))
    records = [record(run, passed=int(run.condition.name == "B2")) for run in plan]
    records[1] = {**records[1], "harness_error": "PilotError: seed failed"}
    records[1]["metrics"] = {**records[1]["metrics"], "valid_task_success": 0, "latency_ms": 0}

    summary = summarize(records, (B0_PRIME, B2))

    assert summary["runs"] == 8
    assert summary["harness_errors"] == 1
    b2 = summary["by_condition"]["B2"]
    assert (b2["runs"], b2["harness_errors"], b2["evaluated_runs"]) == (4, 1, 3)
    assert b2["oracle_passes"] == 3
    assert b2["oracle_pass_rate"] == 1.0
    assert b2["oracle_pass_rate_all_runs"] == 0.75
    assert b2["mean_latency_ms"] == 100
    s01 = summary["by_condition_scenario"]["B2"]["S01"]
    assert (s01["runs"], s01["harness_errors"], s01["oracle_pass_rate"]) == (2, 1, 1.0)
    assert summary["by_condition"]["B0prime"]["oracle_pass_rate"] == 0.0
    # Several conditions are never pooled into one oracle rate.
    assert "oracle_passes" not in summary


def test_summary_of_one_condition_keeps_todays_totals() -> None:
    plan = build_plan([scenario("S01")], 2, (B0_PRIME,))
    records = [record(run, passed=1) for run in plan]

    summary = summarize(records, (B0_PRIME,))

    assert summary["condition"] == "B0prime"
    assert summary["oracle_passes"] == 2
    assert summary["trace_completeness"] == "not_evaluated"
    assert summary["by_condition"]["B0prime"]["oracle_passes"] == 2


def test_rates_are_null_when_no_row_was_evaluated() -> None:
    [run] = build_plan([scenario("S01")], 1, (B2,))
    failed = {**record(run, passed=0), "harness_error": "PilotError: down"}

    summary = summarize([failed], (B2,))["by_condition"]["B2"]

    assert (summary["evaluated_runs"], summary["oracle_pass_rate"]) == (0, None)
    assert summary["mean_latency_ms"] is None


def run_main(arguments: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(sys, "argv", ["governance-pilot", *arguments])
    return governance_pilot.main()


def test_dry_run_prints_the_interleaved_plan(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        governance_pilot, "repository_provenance", lambda _: RepositoryProvenance("a" * 40, True)
    )

    code = run_main(
        ["--dry-run", "--condition", "B0prime", "--condition", "B2", "--repetitions", "2"],
        monkeypatch,
    )

    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["manifest_id"] == "B2-PILOT-v1"
    assert (output["agent_commit"], output["agent_worktree_dirty"]) == ("a" * 40, True)
    assert output["execution_order"] == "interleaved_by_repetition_alternating_first_condition"
    assert [item["condition"] for item in output["conditions"]] == ["B0prime", "B2"]
    assert output["runs"] == len(output["plan"]) == 20
    assert output["plan"][:4] == [
        {"scenario_id": "S01", "repetition": 1, "condition": "B0prime"},
        {"scenario_id": "S01", "repetition": 1, "condition": "B2"},
        {"scenario_id": "S01", "repetition": 2, "condition": "B2"},
        {"scenario_id": "S01", "repetition": 2, "condition": "B0prime"},
    ]


def test_dry_run_of_one_condition_keeps_its_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        governance_pilot, "repository_provenance", lambda _: RepositoryProvenance("b" * 40, False)
    )

    run_main(["--dry-run", "--condition", "B2", "--scenario", "S04"], monkeypatch)

    output = json.loads(capsys.readouterr().out)
    assert {key: output[key] for key in ("condition", "governance_mode", "hp_flow")} == {
        "condition": "B2",
        "governance_mode": "enforce",
        "hp_flow": "governed",
    }
    assert (output["runs"], output["scenarios"], output["repetitions"]) == (5, ["S04"], 5)


def test_a_dirty_agent_stops_a_real_run_before_any_service_starts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        governance_pilot, "repository_provenance", lambda _: RepositoryProvenance("c" * 40, True)
    )
    started: list[object] = []
    forbidden: Callable[..., object] = lambda *args, **kwargs: started.append(args)  # noqa: E731
    monkeypatch.setattr(governance_pilot, "_run", forbidden)
    monkeypatch.setattr(governance_pilot.subprocess, "Popen", forbidden)

    code = run_main(["--condition", "B2"], monkeypatch)

    assert code == 1
    assert "--allow-dirty" in capsys.readouterr().err
    assert started == []
