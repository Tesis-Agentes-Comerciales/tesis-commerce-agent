"""The pilot scores governed evidence, merchant checkouts (M3) and policy latency per run.

Evidence comes from SQLite files written by the agent's real stores; the merchant, the agent
process and every HTTP call are doubles. No test starts a service or calls a model.
"""

import hashlib
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from commerce_agent.persistence.hp_evidence_sqlite import HpRecordKind, SqliteHpEvidenceStore
from commerce_evaluation import governance_pilot
from commerce_evaluation.governance_pilot import (
    CONDITIONS,
    AuthClient,
    HttpResult,
    PilotError,
    PlannedRun,
    RepositoryProvenance,
    dispatch_merchant_webhooks,
    mechanism_evidence,
    mechanisms_by_scenario,
    merchant_checkouts,
    read_governed_evidence,
    require_merchant_run_checkouts,
    summarize,
    summary_document,
)
from commerce_evaluation.paths import DATA, ROOT
from tests.governance.test_shadow_integration import configuration, observer, service, start

B0_PRIME, B2 = CONDITIONS["B0prime"], CONDITIONS["B2"]
MERCHANT_RUN = "6f1c2d3e-4b5a-4c6d-8e7f-0a1b2c3d4e5f"
FRESHNESS_BODY = {
    "run_id": "run-real",
    "checkout_id": "chk_private",
    "stage": "approve",
    "observed_revision": 2,
    "compared": {"total_minor": {"checkout": 100, "offer": 120}},
    "outcome": "changed",
    "changed_fields": ["subtotal_minor", "total_minor"],
    "reason": None,
    "governance_mode": "enforce",
    "decision": "block",
    "checked_at": "2026-09-10T14:05:00Z",
}
RECOVERY_BODY = {
    "event": "governed_recovery",
    "control": "application",
    "kind": "selection_outside_shortlist",
    "cause": "budget",
    "run_id": "run-real",
    "variant_id": "SON-02",
    "checkout_id": None,
    "currency": "COP",
    "budget_minor": 50_000,
    "observed_unit_amount_minor": 80_000,
    "rejection_counts": {"budget": 3},
    "intent_sha256": "a" * 64,
    "explanation_sha256": "b" * 64,
}


def write_d1_decision(database: Path) -> None:
    """A real D1 decision_record, written by the agent's purchase service and ledger."""
    settings = configuration(database)
    purchases, _, _ = service(settings, observer(settings))
    start(purchases)


def write_hp_evidence(database: Path, *records: tuple[HpRecordKind, dict[str, Any]]) -> None:
    store = SqliteHpEvidenceStore(database)
    store.initialize()
    for kind, body in records:
        store.append("run-real", kind, body)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_missing_database_reports_nothing_available(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"

    assert read_governed_evidence(database) == {
        "d1_decisions": None,
        "hp_evidence": None,
        "hp_evidence_chain_verified": None,
    }
    assert not database.exists()


def test_database_without_governed_tables_reports_them_as_unavailable(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE purchases (run_id TEXT)")

    evidence = read_governed_evidence(database)

    assert evidence == {
        "d1_decisions": None,
        "hp_evidence": None,
        "hp_evidence_chain_verified": None,
    }


def test_reads_d1_decisions_and_hp_results_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    write_d1_decision(database)
    write_hp_evidence(
        database, ("freshness_check", FRESHNESS_BODY), ("governed_recovery", RECOVERY_BODY)
    )
    before = digest(database)

    evidence = read_governed_evidence(database)

    [decision] = evidence["d1_decisions"]
    assert decision["record_kind"] == "decision_record"
    assert (decision["outcome"], decision["origin"]) == ("allow", "policy")
    assert decision["reason_codes"] == ["ALL_APPLICABLE_POLICIES_SATISFIED"]
    assert type(decision["latency_ms"]) is int
    assert evidence["hp_evidence"] == [
        {
            "record_kind": "freshness_check",
            "stage": "approve",
            "outcome": "changed",
            "decision": "block",
            "changed_fields": ["subtotal_minor", "total_minor"],
            "reason": None,
            "governance_mode": "enforce",
            "observed_revision": 2,
        },
        {
            "record_kind": "governed_recovery",
            "control": "application",
            "kind": "selection_outside_shortlist",
            "cause": "budget",
        },
    ]
    assert evidence["hp_evidence_chain_verified"] is True
    assert digest(database) == before


def test_an_empty_hp_table_has_no_chain_to_verify(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    SqliteHpEvidenceStore(database).initialize()

    evidence = read_governed_evidence(database)

    assert (evidence["hp_evidence"], evidence["hp_evidence_chain_verified"]) == ([], None)


def test_a_tampered_hp_chain_does_not_verify(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    write_hp_evidence(database, ("freshness_check", FRESHNESS_BODY))
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TRIGGER hp_evidence_no_update")
        tampered = json.dumps(FRESHNESS_BODY | {"outcome": "fresh", "decision": "allow"})
        connection.execute("UPDATE hp_evidence SET body = ?", (tampered,))

    evidence = read_governed_evidence(database)

    assert evidence["hp_evidence"][0]["outcome"] == "fresh"
    assert evidence["hp_evidence_chain_verified"] is False


def test_rejected_d1_inputs_keep_their_reason_without_a_decision() -> None:
    row = governance_pilot._d1_decision(
        "input_rejected",
        json.dumps({"record_kind": "input_rejected", "reason_code": "GOV_INPUT_INVALID"}),
    )

    assert row == {
        "record_kind": "input_rejected",
        "outcome": None,
        "origin": None,
        "reason_codes": ["GOV_INPUT_INVALID"],
        "latency_ms": None,
    }


def governed(
    *, d1: list[tuple[str, str | None]] | None = None, hp: list[str] | None = None
) -> dict[str, Any]:
    return {
        "d1_decisions": None
        if d1 is None
        else [{"record_kind": kind, "outcome": outcome} for kind, outcome in d1],
        "hp_evidence": None if hp is None else [{"record_kind": kind} for kind in hp],
        "hp_evidence_chain_verified": True if hp else None,
    }


MECHANISMS = mechanisms_by_scenario()


def test_mechanisms_come_from_the_pre_registered_manifest() -> None:
    manifest = json.loads((DATA / "b2-pilot-v1.json").read_text(encoding="utf-8"))

    assert manifest["attribution"]["mechanism_by_scenario"] == MECHANISMS


def test_b0_prime_has_no_mechanism_to_attribute() -> None:
    assert mechanism_evidence(B0_PRIME, "S04", governed(), MECHANISMS) == "not_applicable"


def test_b2_reports_which_mechanism_left_its_own_record() -> None:
    evidence = governed(d1=[("decision_record", "allow")], hp=["freshness_check"])

    assert mechanism_evidence(B2, "S04", evidence, MECHANISMS) == {
        "d1_dispatch": "recorded",
        "governed_continuation": "not_recorded",
        "offer_freshness": "recorded",
    }


def test_b2_reports_missing_evidence_and_unavailable_tables() -> None:
    # Recovery still logs instead of writing hp_evidence: the scorer says so, it does not infer.
    assert mechanism_evidence(B2, "S02", governed(hp=["freshness_check"]), MECHANISMS) == {
        "governed_recovery": "missing"
    }
    assert mechanism_evidence(B2, "S01", governed(d1=[("input_rejected", None)]), MECHANISMS) == {
        "d1_dispatch": "missing",
        "governed_continuation": "not_recorded",
    }
    assert mechanism_evidence(B2, "S04", governed(), MECHANISMS) == {
        "d1_dispatch": "unavailable",
        "governed_continuation": "not_recorded",
        "offer_freshness": "unavailable",
    }
    assert mechanism_evidence(B2, "S03", governed(), MECHANISMS) == {}


def test_a_d1_deny_is_not_a_dispatch() -> None:
    denied = governed(d1=[("decision_record", "deny")])

    assert mechanism_evidence(B2, "S01", denied, MECHANISMS) == {
        "d1_dispatch": "missing",
        "governed_continuation": "not_recorded",
    }


def checkout_report(*statuses: str, orders: int = 0) -> dict[str, Any]:
    checkouts = [
        {
            "checkout_id": f"chk_{index}",
            "status": status,
            "offer_id": "SON-01",
            "created_at": "2026-09-10T14:05:00Z",
            "updated_at": "2026-09-10T14:05:00Z",
        }
        for index, status in enumerate(statuses)
    ]
    return {
        "operation": "run-checkouts",
        "run_id": MERCHANT_RUN,
        "count": len(checkouts),
        "checkouts": checkouts,
        "order_count": orders,
    }


def test_merchant_checkouts_count_the_run_by_status(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def run_json(command: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
        commands.append(command)
        return checkout_report("canceled", "completed", "canceled", orders=1)

    monkeypatch.setattr(governance_pilot, "_run_json", run_json)

    result = merchant_checkouts(Path("merchant"), {}, MERCHANT_RUN)

    assert commands == [
        ["uv", "run", "python", "-m", "commerce_lab.db", "run-checkouts", MERCHANT_RUN]
    ]
    assert result["count"] == 3
    assert result["by_status"] == {"canceled": 2, "completed": 1}
    assert result["order_count"] == 1
    assert [item["status"] for item in result["checkouts"]] == ["canceled", "completed", "canceled"]


def dispatch_answers(*batches: list[dict[str, Any]], calls: list[list[str]]) -> Callable[..., Any]:
    """`webhook-dispatch` printing one JSON list per call, as the merchant's CLI does."""
    answers = list(batches)

    def run(command: list[str], *, cwd: Path, env: dict[str, str], capture: bool = False) -> Any:
        assert capture and cwd == Path("merchant") and env == {"WEBHOOK_RECEIVER_URL": "agent"}
        calls.append(command)
        stdout = json.dumps(answers.pop(0)) + "\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    return run


def test_the_runner_drains_the_merchant_webhook_outbox_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full = [{"delivery_id": f"whd_{index}", "outcome": "delivered"} for index in range(20)]
    last = [{"delivery_id": "whd_x", "outcome": "terminal", "error_code": "HTTP_401"}]
    calls: list[list[str]] = []
    monkeypatch.setattr(governance_pilot, "_run", dispatch_answers(full, last, calls=calls))

    result = dispatch_merchant_webhooks(Path("merchant"), {"WEBHOOK_RECEIVER_URL": "agent"})

    command = ["uv", "run", "python", "-m", "commerce_lab.db", "webhook-dispatch", "--limit", "20"]
    assert calls == [command, command]
    assert result == {"count": 21, "by_outcome": {"delivered": 20, "terminal": 1}}


def test_an_unexpected_webhook_dispatch_answer_is_a_pilot_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        governance_pilot,
        "_run",
        dispatch_answers({"ok": True}, calls=calls),  # type: ignore[arg-type]
    )

    with pytest.raises(PilotError, match="webhook-dispatch"):
        dispatch_merchant_webhooks(Path("merchant"), {"WEBHOOK_RECEIVER_URL": "agent"})


def test_a_report_for_another_run_is_a_pilot_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        governance_pilot, "_run_json", lambda *_, **__: checkout_report() | {"run_id": "other"}
    )

    with pytest.raises(PilotError, match="run-checkouts"):
        merchant_checkouts(Path("merchant"), {}, MERCHANT_RUN)


def test_an_old_merchant_without_run_checkouts_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def old_merchant(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert command[-2:] == ["run-checkouts", "--help"]
        return subprocess.CompletedProcess(command, 2, "", "invalid choice: 'run-checkouts'")

    monkeypatch.setattr(governance_pilot.subprocess, "run", old_merchant)

    with pytest.raises(PilotError, match=r"run-checkouts.*M3"):
        require_merchant_run_checkouts(Path("merchant"), {})


def run_main(arguments: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(sys, "argv", ["governance-pilot", *arguments])
    return governance_pilot.main()


def test_the_pilot_checks_the_merchant_before_starting_any_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("merchant", "payment"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-mini")
    monkeypatch.setattr(
        governance_pilot, "repository_provenance", lambda _: RepositoryProvenance("d" * 40, False)
    )
    commands: list[list[str]] = []

    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        failed = "run-checkouts" in command
        return subprocess.CompletedProcess(command, 2 if failed else 0, "", "invalid choice")

    def no_service(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"no service may start: {args}")

    monkeypatch.setattr(governance_pilot.subprocess, "run", run)
    monkeypatch.setattr(governance_pilot.subprocess, "Popen", no_service)

    code = run_main(
        [
            "--condition",
            "B2",
            "--merchant-repo",
            str(tmp_path / "merchant"),
            "--payment-repo",
            str(tmp_path / "payment"),
            "--output",
            str(tmp_path / "out.jsonl"),
        ],
        monkeypatch,
    )

    assert code == 1
    assert "run-checkouts" in capsys.readouterr().err
    assert not any("migrate" in command for command in commands)


class FakeAgentProcess:
    """Stands in for uvicorn: on start it writes what the real agent would leave in agent.db."""

    def __init__(self, write: Callable[[Path], None], environment: dict[str, str]) -> None:
        self.returncode: int | None = None
        write(Path(environment["AGENT_DATABASE_PATH"]))

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class FakeAuth(AuthClient):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:4215")

    def token(self) -> str:
        return "buyer-jwt"


# The PSP's own table (tesis_payment_provider/store.py), which the runner reads for payments.
PSP_DELEGATIONS = """CREATE TABLE IF NOT EXISTS delegations (
    key_hash TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    checkout_id TEXT NOT NULL,
    max_amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    confirmation_key_hash TEXT,
    confirmation_hash TEXT,
    result_json TEXT,
    payment_mandate_hash TEXT)"""


def psp_database(path: Path, *delegations: tuple[str, str | None]) -> Path:
    """A PSP database with one delegated token per (checkout_id, confirmed status or None)."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(PSP_DELEGATIONS)
        for index, (checkout_id, status) in enumerate(delegations):
            result = (
                None
                if status is None
                else json.dumps({"id": f"pay_{index}", "status": status, "amount": 100})
            )
            connection.execute(
                "INSERT INTO delegations (key_hash, request_hash, token_hash, created_at, "
                "merchant_id, checkout_id, max_amount, currency, expires_at, outcome, result_json) "
                "VALUES (?, 'request', ?, '2026-09-10T14:05:00+00:00', 'tesis_merchant', ?, 100, "
                "'cop', '2026-09-10T15:05:00+00:00', 'approved', ?)",
                (f"key-{index}", f"token-{index}", checkout_id, result),
            )
    return path


def s04() -> dict[str, Any]:
    matrix = json.loads((DATA / "pilot-scenarios.json").read_text(encoding="utf-8"))
    return next(item for item in matrix["scenarios"] if item["id"] == "S04")


def fake_merchant(report: dict[str, Any], calls: list[str]) -> Callable[..., dict[str, Any]]:
    def run_json(command: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
        calls.append(command[-2] if "run-checkouts" in command else command[-1])
        if "seed" in command:
            return {"run_id": MERCHANT_RUN}
        if "issue-session" in command:
            return {"session_token": "merchant-session"}
        if "run-checkouts" in command:
            assert command[-1] == MERCHANT_RUN
            return report
        return {"operation": command[-1]}

    return run_json


def fake_agent_http(
    condition_health: dict[str, str],
    final_stage: str,
    calls: list[str],
    on_request: Callable[[str], None] = lambda _: None,
    order_id: str | None = None,
) -> Callable[..., HttpResult]:
    terms = {"fingerprint": "f" * 64, "checkout_jwt": "jwt", "total_minor": 1}
    responses = {
        "/health": HttpResult(200, {"status": "ok", **condition_health}, 1),
        "/conversation/turn": HttpResult(
            200,
            {
                "action": "purchase",
                "assistant_message": "Revisa la compra.",
                "purchase": {"run_id": "run-real", "checkout": {"id": "chk_0"}},
                "approval_terms": terms,
            },
            900,
        ),
        "/purchases/run-real/approval": HttpResult(409, {"code": "checkout_terms_changed"}, 30),
        "/purchases/run-real": HttpResult(200, {"stage": final_stage, "order_id": order_id}, 5),
        "/purchases/run-real/cancel": HttpResult(200, {"stage": "canceled"}, 5),
    }

    def request_json(url: str, **_: Any) -> HttpResult:
        path = url.removeprefix("http://127.0.0.1:43125")
        calls.append(path)
        on_request(path)
        return responses[path]

    return request_json


def execute_s04(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write: Callable[[Path], None],
    report: dict[str, Any],
    *,
    final_stage: str = "awaiting_confirmation",
    calls: list[str] | None = None,
    on_request: Callable[[str], None] = lambda _: None,
    payment_database: Path | None = None,
    order_id: str | None = None,
) -> dict[str, Any]:
    processes: list[FakeAgentProcess] = []
    calls = [] if calls is None else calls

    def popen(command: list[str], *, env: dict[str, str], **_: Any) -> FakeAgentProcess:
        processes.append(FakeAgentProcess(write, env))
        return processes[-1]

    reader = governance_pilot.read_governed_evidence

    def read_after_exit(database: Path) -> dict[str, Any]:
        assert processes and all(process.returncode is not None for process in processes)
        return reader(database)

    monkeypatch.setattr(governance_pilot.subprocess, "Popen", popen)
    monkeypatch.setattr(governance_pilot, "_run_json", fake_merchant(report, calls))
    monkeypatch.setattr(
        governance_pilot,
        "_request_json",
        fake_agent_http(
            {"governance_mode": "enforce", "hp_flow": "governed"},
            final_stage,
            calls,
            on_request,
            order_id,
        ),
    )
    monkeypatch.setattr(governance_pilot, "_wait_http", lambda *_, **__: None)
    monkeypatch.setattr(governance_pilot, "read_governed_evidence", read_after_exit)
    run = PlannedRun(s04(), 1, B2)
    return governance_pilot._execute_repetition(
        run=run,
        condition_environment={"AGENT_GOVERNANCE_MODE": "enforce", "AGENT_HP_FLOW": "governed"},
        mechanisms=MECHANISMS,
        agent=tmp_path / "agent-repo",
        merchant=tmp_path / "merchant-repo",
        environment={"BETTER_AUTH_URL": "http://127.0.0.1:4215"},
        run_dir=governance_pilot.run_directory(tmp_path, run),
        actor_id="buyer",
        auth=FakeAuth(),
        agent_port=43125,
        payment_database=payment_database or psp_database(tmp_path / "payments.db"),
    )


def test_a_b2_repetition_records_checkouts_evidence_and_mechanisms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def agent_writes(database: Path) -> None:
        write_d1_decision(database)
        write_hp_evidence(database, ("freshness_check", FRESHNESS_BODY))

    calls: list[str] = []
    record = execute_s04(
        tmp_path, monkeypatch, agent_writes, checkout_report("ready_for_payment"), calls=calls
    )

    # The merchant is read after the approval and before the runner cancels the open purchase.
    assert (
        calls.index("/purchases/run-real/approval")
        < calls.index("run-checkouts")
        < calls.index("/purchases/run-real/cancel")
    )
    assert record["runner_canceled"] is True
    assert record["metrics"]["valid_task_success"] == 1
    assert record["approval_response_code"] == "checkout_terms_changed"
    assert record["merchant_checkouts"]["count"] == 1
    assert record["merchant_checkouts"]["by_status"] == {"ready_for_payment": 1}
    assert [row["record_kind"] for row in record["governed_evidence"]["d1_decisions"]] == [
        "decision_record"
    ]
    assert record["governed_evidence"]["hp_evidence"][0]["decision"] == "block"
    assert record["governed_evidence"]["hp_evidence_chain_verified"] is True
    assert record["mechanism_evidence"] == {
        "d1_dispatch": "recorded",
        "governed_continuation": "not_recorded",
        "offer_freshness": "recorded",
    }
    serialized = json.dumps(record)
    assert "chk_private" not in serialized
    assert "compared" not in serialized


def test_a_repetition_without_agent_evidence_says_it_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    record = execute_s04(
        tmp_path,
        monkeypatch,
        lambda _: None,
        checkout_report(),
        final_stage="canceled",
        calls=calls,
    )

    assert record["governed_evidence"] == {
        "d1_decisions": None,
        "hp_evidence": None,
        "hp_evidence_chain_verified": None,
    }
    assert record["mechanism_evidence"]["offer_freshness"] == "unavailable"
    assert record["merchant_checkouts"]["count"] == 0
    # The agent already closed the purchase, so the runner did not cancel anything.
    assert record["runner_canceled"] is False
    assert "/purchases/run-real/cancel" not in calls


def recording_dispatch(
    monkeypatch: pytest.MonkeyPatch, calls: list[str], *, fails: bool = False
) -> None:
    """Replaces the merchant's dispatcher and notes when the agent is stopped, in `calls`."""
    terminate = governance_pilot._terminate

    def dispatch(merchant: Path, environment: dict[str, str]) -> dict[str, Any]:
        calls.append("webhook-dispatch")
        assert merchant.name == "merchant-repo"
        if fails:
            raise PilotError("Command failed (uv): webhook receiver unreachable")
        return {"count": 1, "by_outcome": {"delivered": 1}}

    def stop(process: Any) -> None:
        calls.append("terminate")
        terminate(process)

    monkeypatch.setattr(governance_pilot, "dispatch_merchant_webhooks", dispatch)
    monkeypatch.setattr(governance_pilot, "_terminate", stop)


def test_a_repetition_with_an_order_delivers_its_webhook_before_the_agent_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    recording_dispatch(monkeypatch, calls)

    record = execute_s04(
        tmp_path,
        monkeypatch,
        lambda _: None,
        checkout_report("completed"),
        final_stage="completed",
        calls=calls,
        order_id="ord_1",
    )

    assert calls.index("/purchases/run-real") < calls.index("webhook-dispatch")
    assert calls.index("webhook-dispatch") < calls.index("terminate")
    assert record["merchant_webhooks"] == {"count": 1, "by_outcome": {"delivered": 1}}
    assert "merchant_webhooks" not in record["measurement_errors"]


def test_a_repetition_without_an_order_does_not_dispatch_webhooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    recording_dispatch(monkeypatch, calls)

    record = execute_s04(tmp_path, monkeypatch, lambda _: None, checkout_report(), calls=calls)

    assert "webhook-dispatch" not in calls
    assert record["merchant_webhooks"] is None


def test_a_failed_webhook_dispatch_is_a_measurement_error_and_keeps_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    recording_dispatch(monkeypatch, calls, fails=True)

    record = execute_s04(
        tmp_path,
        monkeypatch,
        lambda _: None,
        checkout_report("completed"),
        final_stage="completed",
        calls=calls,
        order_id="ord_1",
    )

    assert record["merchant_webhooks"] is None
    assert "webhook receiver unreachable" in record["measurement_errors"]["merchant_webhooks"]


def row(condition: str, scenario_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "condition": condition,
        "scenario_id": scenario_id,
        "http_status": 200,
        "approval_http_status": None,
        "approval_response_code": None,
        "metrics": {
            "valid_task_success": 1,
            "hard_constraint_violation": 0,
            "stale_terms_accepted": 0,
            "prompt_injection_compliance": 0,
            "trace_completeness": 1.0,
            "latency_ms": 2000,
        },
        **fields,
    }


def decisions(*latencies: int, origin: str = "policy") -> dict[str, Any]:
    return {
        "d1_decisions": [
            {"record_kind": "decision_record", "origin": origin, "latency_ms": value}
            for value in latencies
        ],
        "hp_evidence": [],
        "hp_evidence_chain_verified": True,
    }


def test_summary_reports_5xx_checkouts_approvals_latency_and_coverage() -> None:
    records = [
        row(
            "B0prime",
            "S02",
            http_status=502,
            merchant_checkouts={"count": 0, "by_status": {}, "order_count": 0},
            runner_canceled=False,
            governed_evidence=decisions(),
            mechanism_evidence="not_applicable",
        ),
        row(
            "B0prime",
            "S02",
            # The agent created and canceled it itself: M3 reports this case apart.
            merchant_checkouts={"count": 1, "by_status": {"canceled": 1}, "order_count": 0},
            runner_canceled=False,
            governed_evidence=decisions(),
            mechanism_evidence="not_applicable",
        ),
        row(
            "B2",
            "S04",
            approval_http_status=409,
            approval_response_code="checkout_terms_changed",
            merchant_checkouts={
                "count": 1,
                "by_status": {"ready_for_payment": 1},
                "order_count": 0,
            },
            runner_canceled=True,
            governed_evidence=decisions(3, 1),
            mechanism_evidence={"d1_dispatch": "recorded", "offer_freshness": "recorded"},
        ),
        row(
            "B2",
            "S04",
            approval_http_status=409,
            approval_response_code="offer_freshness_unavailable",
            merchant_checkouts={"count": 1, "by_status": {"completed": 1}, "order_count": 1},
            runner_canceled=False,
            governed_evidence=decisions(2)
            | {
                "d1_decisions": [
                    {"record_kind": "decision_record", "origin": "policy", "latency_ms": 2},
                    {"record_kind": "decision_record", "origin": "runtime_guard", "latency_ms": 0},
                ]
            },
            mechanism_evidence={"d1_dispatch": "recorded", "offer_freshness": "missing"},
        ),
        {**row("B2", "S04"), "harness_error": "PilotError: down"},
    ]

    summary = summarize(records, (B0_PRIME, B2))

    s02 = summary["by_condition_scenario"]["B0prime"]["S02"]
    assert s02["http_5xx"] == 1
    assert s02["merchant_checkouts"] == {
        "runs_measured": 2,
        "runs_runner_canceled": 0,
        "runs_with_checkouts": 1,
        "total": 1,
        "by_status": {"canceled": 1},
        "orders": 0,
    }
    assert s02["policy_latency_ms"] == {
        "n": 0,
        "median": None,
        "max": None,
        "resolution_ms": 1,
    }
    assert s02["mechanism_evidence"] == "not_applicable"
    s04 = summary["by_condition_scenario"]["B2"]["S04"]
    assert s04["http_5xx"] == 0
    assert s04["approval_response_codes"] == {
        "checkout_terms_changed": 1,
        "offer_freshness_unavailable": 1,
    }
    assert s04["merchant_checkouts"]["by_status"] == {"ready_for_payment": 1, "completed": 1}
    assert s04["merchant_checkouts"]["orders"] == 1
    assert s04["merchant_checkouts"]["runs_runner_canceled"] == 1
    # Only the policy evaluator's own measurements; runtime guards and model latency stay apart.
    assert s04["policy_latency_ms"] == {"n": 3, "median": 2, "max": 3, "resolution_ms": 1}
    assert s04["mean_latency_ms"] == 2000
    assert s04["mechanism_evidence"] == {
        "d1_dispatch": {"recorded": 2},
        "offer_freshness": {"recorded": 1, "missing": 1},
    }
    assert s04["harness_errors"] == 1
    # Amendment M1: the legacy trace completeness is not reported, not even as a mean.
    assert s04["trace_completeness"] == "not_evaluated"
    assert "mean_trace_completeness" not in s04
    assert summary["by_condition"]["B2"]["trace_completeness"] == "not_evaluated"


def test_summary_file_records_the_planned_scenarios_and_repetitions() -> None:
    document = summary_document(
        [],
        matrix={"matrix_id": "governance-pilot-v1", "baseline_id": "B0-2026-10-06"},
        run_metadata={"manifest_id": "B2-PILOT-v1", "agent_commit": "e" * 40},
        conditions=(B0_PRIME, B2),
        scenarios=["S02", "S04"],
        repetitions=5,
        output=ROOT / "evaluation" / "results" / "b0prime-b2.jsonl",
    )

    assert (document["scenarios"], document["repetitions"]) == (["S02", "S04"], 5)
    assert document["conditions"] == ["B0prime", "B2"]
    assert document["execution_order"] == "interleaved_by_repetition_alternating_first_condition"
    assert document["output"] == "evaluation/results/b0prime-b2.jsonl"
    assert document["manifest_id"] == "B2-PILOT-v1"
