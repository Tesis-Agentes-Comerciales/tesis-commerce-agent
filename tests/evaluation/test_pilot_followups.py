"""Follow-ups of the pilot v1 review, for the runs that come after it.

Provenance of the three services, merchant-side violations (M3), PSP payments, stage timings,
and rows that survive a failed reading. Real SQLite files and git repositories; the merchant,
the agent process and every HTTP call are doubles. No service starts and no model is called.
"""

import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import urllib.error
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from commerce_agent.persistence.stage_timings_sqlite import SqliteStageTimings
from commerce_evaluation import governance_pilot
from commerce_evaluation.governance_pilot import (
    CONDITIONS,
    ConditionMismatchError,
    PilotError,
    PlannedRun,
    RepositoryProvenance,
    run_plan,
    services_provenance,
    summarize,
)
from commerce_evaluation.paths import DATA
from commerce_evaluation.pilot_measures import (
    merchant_side_violations,
    read_psp_payments,
    stage_timings_ms,
)
from tests.evaluation.test_pilot_evidence import (
    FRESHNESS_BODY,
    MERCHANT_RUN,
    checkout_report,
    decisions,
    execute_s04,
    psp_database,
    row,
    s04,
    write_d1_decision,
    write_hp_evidence,
)

B0_PRIME, B2 = CONDITIONS["B0prime"], CONDITIONS["B2"]
SCENARIOS = {
    scenario["id"]: scenario
    for scenario in json.loads((DATA / "pilot-scenarios.json").read_text(encoding="utf-8"))[
        "scenarios"
    ]
}
MATRIX = {"matrix_id": "governance-pilot-v1", "baseline_id": "B0-2026-10-06"}


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments], capture_output=True, text=True, check=True
    ).stdout.strip()


def committed_repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    (path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    git(path, "add", "pyproject.toml")
    git(path, "-c", "user.name=Pilot", "-c", "user.email=pilot@example.test", "commit", "-qm", "x")
    return path


def test_rows_record_the_merchant_and_psp_commits(tmp_path: Path) -> None:
    merchant = committed_repository(tmp_path / "merchant")
    payment = committed_repository(tmp_path / "payment")

    metadata = services_provenance({"merchant": merchant, "payment": payment}, allow_dirty=False)

    assert metadata == {
        "merchant_commit": git(merchant, "rev-parse", "HEAD"),
        "merchant_worktree_dirty": False,
        "payment_commit": git(payment, "rev-parse", "HEAD"),
        "payment_worktree_dirty": False,
    }


def test_a_modified_service_stops_the_run_unless_dirty_runs_are_allowed(tmp_path: Path) -> None:
    payment = committed_repository(tmp_path / "payment")
    (payment / "pyproject.toml").write_text("[project]\nname = 'changed'\n", encoding="utf-8")

    with pytest.raises(PilotError, match=r"payment worktree.*--allow-dirty"):
        services_provenance({"payment": payment}, allow_dirty=False)
    assert services_provenance({"payment": payment}, allow_dirty=True)["payment_worktree_dirty"]


def test_a_dirty_merchant_stops_a_live_run_before_any_service_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("merchant", "payment"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-mini")
    monkeypatch.setattr(
        governance_pilot,
        "repository_provenance",
        lambda path: RepositoryProvenance("e" * 40, path.name == "merchant"),
    )
    started: list[object] = []
    monkeypatch.setattr(governance_pilot, "_run", lambda *args, **_: started.append(args))
    monkeypatch.setattr(
        governance_pilot.subprocess, "Popen", lambda *args, **_: started.append(args)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "governance-pilot",
            "--merchant-repo",
            str(tmp_path / "merchant"),
            "--payment-repo",
            str(tmp_path / "payment"),
        ],
    )

    code = governance_pilot.main()

    assert code == 1
    assert "merchant worktree has uncommitted changes" in capsys.readouterr().err
    assert started == []


def merchant(*items: tuple[str, str]) -> dict[str, Any]:
    return {
        "count": len(items),
        "checkouts": [
            {"checkout_id": f"chk_{index}", "status": status, "offer_id": offer}
            for index, (offer, status) in enumerate(items)
        ],
    }


def test_every_s02_checkout_at_the_merchant_breaks_the_budget_even_if_canceled() -> None:
    s02 = SCENARIOS["S02"]

    assert merchant_side_violations(s02, merchant(("BBGooglePixel8-128GB", "canceled"))) == 1
    assert merchant_side_violations(s02, merchant()) == 0


def test_other_scenarios_count_forbidden_or_unrequested_variants_only() -> None:
    injected = ("BBOnePlus11-256GB", "ready_for_payment")
    safe = ("Q112159052-128GB", "ready_for_payment")
    pixel = ("BBGooglePixel8-128GB", "ready_for_payment")

    assert merchant_side_violations(SCENARIOS["S05"], merchant(injected, safe)) == 1
    assert merchant_side_violations(SCENARIOS["S01"], merchant(pixel, safe)) == 1
    assert merchant_side_violations(SCENARIOS["S04"], merchant(pixel)) == 0
    # S03 has no hard constraint: a checkout there fails the oracle, not a constraint.
    assert merchant_side_violations(SCENARIOS["S03"], merchant(pixel)) == 0


def test_an_unread_merchant_is_not_zero_violations() -> None:
    assert merchant_side_violations(SCENARIOS["S02"], None) is None


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_psp_payments_count_only_the_checkouts_of_the_run(tmp_path: Path) -> None:
    database = psp_database(
        tmp_path / "payments.db",
        ("chk_0", "approved"),
        ("chk_1", None),
        ("chk_other_run", "declined"),
    )
    before = digest(database)

    assert read_psp_payments(database, ["chk_0", "chk_1"]) == {
        "delegations": 2,
        "payments": 1,
        "by_status": {"approved": 1},
    }
    assert read_psp_payments(database, []) == {"delegations": 0, "payments": 0, "by_status": {}}
    assert digest(database) == before


def test_a_missing_psp_database_is_an_error_not_zero_payments(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="PSP database"):
        read_psp_payments(tmp_path / "payments.db", ["chk_0"])


WINDOW = {"started_at": "2026-10-21T10:00:00+00:00", "finished_at": "2026-10-21T10:00:40+00:00"}


def timing(stage: str, second: int, elapsed_ms: float) -> dict[str, Any]:
    return {
        "stage": stage,
        "started_at": f"2026-10-21T10:00:{second:02d}.000001+00:00",
        "elapsed_ms": elapsed_ms,
        "outcome": "ok",
    }


def test_stage_timings_split_the_turn_by_agent_stage_and_add_the_runner_calls() -> None:
    record = row(
        "B2",
        "S02",
        turn_window=WINDOW,
        agent_stage_timings=[
            timing("model.route", 0, 9_000.5),
            timing("model.choose_candidate", 10, 20_000.25),
            timing("d1.capture", 31, 2.0),
            timing("merchant.create_checkout", 31, 80.0),
            timing("merchant.get_checkout", 31, 10.0),
            timing("d1.create_checkout", 31, 120.0),
            timing("merchant.cancel_checkout", 33, 40.0),
            # The runner's cleanup after the turn is not the agent's cancellation.
            timing("merchant.cancel_checkout", 50, 35.0),
        ],
        runner_http_ms={"turn": 2000, "approval": None, "purchase_status": 4, "cancel": 60},
        governed_evidence=decisions(1, 14)
        | {
            "d1_decisions": [
                {"record_kind": "decision_record", "origin": "policy", "latency_ms": 1},
                {"record_kind": "decision_record", "origin": "runtime_guard", "latency_ms": 0},
            ]
        },
    )

    assert stage_timings_ms(record) == {
        "turn": 2000,
        "model": 29_000.75,
        "d1": 122.0,
        "d1_policy": 1,
        "checkout_create": 80.0,
        "checkout_cancel": 40.0,
        "approval": None,
        "runner_cancel": 60,
    }


def test_rows_without_agent_timings_only_have_the_turn_and_the_policy() -> None:
    pilot_v1_row = row("B0prime", "S01", governed_evidence=decisions())

    assert stage_timings_ms(pilot_v1_row) == {
        "turn": 2000,
        "model": None,
        "d1": None,
        "d1_policy": None,
        "checkout_create": None,
        "checkout_cancel": None,
        "approval": None,
        "runner_cancel": None,
    }
    assert stage_timings_ms({**pilot_v1_row, "harness_error": "PilotError: down"}) is None


def agent_writes_evidence(database: Path) -> None:
    write_d1_decision(database)
    write_hp_evidence(database, ("freshness_check", FRESHNESS_BODY))
    SqliteStageTimings(database).initialize()


def agent_times(database: Path, *stages: tuple[str, float]) -> None:
    """What the timed agent appends while it serves a request."""
    with closing(sqlite3.connect(database)) as connection, connection:
        for stage, elapsed_ms in stages:
            connection.execute(
                "INSERT INTO stage_timings (stage, started_at, elapsed_ms, outcome) "
                "VALUES (?, ?, ?, 'ok')",
                (stage, datetime.now(UTC).isoformat(), elapsed_ms),
            )


def s04_checkout() -> dict[str, Any]:
    """The merchant's report for S04: one checkout of the variant the oracle requires."""
    report = checkout_report("ready_for_payment")
    report["checkouts"][0]["offer_id"] = "BBGooglePixel8-128GB"
    return report


def test_a_b2_repetition_records_payments_timings_and_runner_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = governance_pilot.run_directory(tmp_path, PlannedRun(s04(), 1, B2)) / "agent.db"

    def agent_serves(path: str) -> None:
        if path == "/conversation/turn":
            agent_times(database, ("model.route", 900.0), ("merchant.create_checkout", 70.0))
        if path == "/purchases/run-real/cancel":
            agent_times(database, ("merchant.cancel_checkout", 30.0))

    record = execute_s04(
        tmp_path,
        monkeypatch,
        agent_writes_evidence,
        s04_checkout(),
        on_request=agent_serves,
        # A token of another run's checkout: never this run's payment.
        payment_database=psp_database(tmp_path / "payments.db", ("chk_other_run", "approved")),
    )

    assert record["psp_payments"] == {"delegations": 0, "payments": 0, "by_status": {}}
    assert record["metrics"]["merchant_side_violations"] == 0
    assert record["measurement_errors"] == {}
    assert record["runner_http_ms"] == {
        "turn": 900,
        "approval": 30,
        "purchase_status": 5,
        "cancel": 5,
    }
    assert [item["stage"] for item in record["agent_stage_timings"]] == [
        "model.route",
        "merchant.create_checkout",
        "merchant.cancel_checkout",
    ]
    timings = record["stage_timings_ms"]
    assert (timings["model"], timings["checkout_create"]) == (900.0, 70.0)
    # The runner's cleanup cancel stays out of the agent's stages.
    assert (timings["checkout_cancel"], timings["runner_cancel"]) == (None, 5)
    assert (timings["approval"], timings["turn"]) == (30, 900)
    assert type(timings["d1_policy"]) is int


def test_a_failed_merchant_reading_keeps_the_evaluated_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = execute_s04(
        tmp_path, monkeypatch, agent_writes_evidence, checkout_report() | {"run_id": "other"}
    )

    assert record["metrics"]["valid_task_success"] == 1
    assert record["merchant_checkouts"] is None
    assert record["psp_payments"] is None
    assert record["metrics"]["merchant_side_violations"] is None
    assert set(record["measurement_errors"]) == {"merchant_checkouts", "psp_payments"}
    assert "run-checkouts" in record["measurement_errors"]["merchant_checkouts"]
    assert record["runner_canceled"] is True
    assert record["mechanism_evidence"]["offer_freshness"] == "recorded"


def corrupt_database(database: Path) -> None:
    database.write_bytes(b"not a database")


def test_an_unreadable_agent_database_keeps_the_evaluated_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = execute_s04(
        tmp_path,
        monkeypatch,
        corrupt_database,
        checkout_report("ready_for_payment"),
    )

    assert record["metrics"]["valid_task_success"] == 1
    assert record["merchant_checkouts"]["count"] == 1
    assert (record["governed_evidence"], record["agent_stage_timings"]) == (None, None)
    assert set(record["measurement_errors"]) == {"governed_evidence", "agent_stage_timings"}
    assert record["mechanism_evidence"] == {
        "d1_dispatch": "read_error",
        "governed_continuation": "not_recorded",
        "offer_freshness": "read_error",
    }


def test_a_harness_error_after_the_seed_keeps_the_merchant_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def agent_down(path: str) -> None:
        if path == "/conversation/turn":
            raise urllib.error.URLError("agent down")

    [failed] = run_plan(
        [PlannedRun(s04(), 1, B2)],
        lambda _: execute_s04(
            tmp_path, monkeypatch, agent_writes_evidence, checkout_report(), on_request=agent_down
        ),
        matrix=MATRIX,
        run_metadata={},
        result_file=io.StringIO(),
    )

    assert failed["merchant_run_id"] == MERCHANT_RUN
    assert failed["harness_error"] == "URLError: <urlopen error agent down>"
    assert failed["metrics"]["merchant_side_violations"] is None


def test_an_error_before_the_seed_has_no_merchant_run() -> None:
    def seed_fails(_: PlannedRun) -> dict[str, Any]:
        raise PilotError("Merchant seed did not return a run_id")

    [failed] = run_plan(
        [PlannedRun(s04(), 1, B2)],
        seed_fails,
        matrix=MATRIX,
        run_metadata={},
        result_file=io.StringIO(),
    )

    assert failed["merchant_run_id"] is None
    assert failed["harness_error"] == "PilotError: Merchant seed did not return a run_id"


def test_a_condition_mismatch_after_the_seed_still_stops_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def another_condition(*_: Any) -> dict[str, str]:
        raise ConditionMismatchError("Agent /health reports another condition")

    monkeypatch.setattr(governance_pilot, "verify_agent_condition", another_condition)

    with pytest.raises(ConditionMismatchError):
        execute_s04(tmp_path, monkeypatch, agent_writes_evidence, checkout_report())


def test_summary_reports_violations_payments_timings_and_measurement_errors() -> None:
    timed = {
        "turn": 2000,
        "model": 1500.0,
        "d1": None,
        "d1_policy": None,
        "checkout_create": 80.0,
        "checkout_cancel": None,
        "approval": 30,
        "runner_cancel": None,
    }
    records = [
        row(
            "B2",
            "S04",
            psp_payments={"delegations": 0, "payments": 0, "by_status": {}},
            stage_timings_ms=timed,
            measurement_errors={},
        ),
        row(
            "B2",
            "S04",
            psp_payments={"delegations": 1, "payments": 1, "by_status": {"approved": 1}},
            stage_timings_ms=timed | {"model": 2500.0, "approval": 50},
            measurement_errors={},
        ),
        row(
            "B2",
            "S04",
            psp_payments=None,
            stage_timings_ms=timed | {"model": None},
            measurement_errors={"psp_payments": "FileNotFoundError: PSP database not found"},
        ),
        {**row("B2", "S04"), "harness_error": "PilotError: down"},
    ]
    for record, violations in zip(records, (0, 2, None, None), strict=True):
        record["metrics"] = record["metrics"] | {"merchant_side_violations": violations}

    s04_summary = summarize(records, (B2,))["by_condition_scenario"]["B2"]["S04"]

    assert s04_summary["merchant_side_violations"] == {
        "runs_measured": 2,
        "runs_with_violations": 1,
        "checkouts": 2,
    }
    assert s04_summary["psp_payments"] == {
        "runs_measured": 2,
        "delegations": 1,
        "payments": 1,
        "by_status": {"approved": 1},
    }
    assert s04_summary["stage_timings_ms"]["model"] == {"n": 2, "median": 2000.0, "max": 2500.0}
    assert s04_summary["stage_timings_ms"]["approval"] == {"n": 3, "median": 30, "max": 50}
    assert s04_summary["stage_timings_ms"]["d1"] == {"n": 0, "median": None, "max": None}
    assert s04_summary["measurement_errors"] == 1
    # The oracle numbers keep their meaning.
    assert s04_summary["hard_constraint_violations"] == 0
    assert s04_summary["evaluated_runs"] == 3
