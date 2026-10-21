"""Each pilot row keeps its run's artifact outside Git (run-traceability.md §4.3, T1).

The databases are real SQLite files in `tmp_path`; the agent, the merchant, the buyer and every
HTTP call are doubles. No test starts a service or calls a model.
"""

import argparse
import hashlib
import io
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation import governance_pilot, hnp_pilot_live
from commerce_evaluation.governance_pilot import (
    CONDITIONS,
    D1RunKeys,
    HttpResult,
    PilotError,
    PlannedRun,
    agent_environment,
    artifact_key,
    keeping,
    run_plan,
)
from commerce_evaluation.hnp_pilot import prepare, stack_variables
from commerce_evaluation.hnp_pilot_episode import EpisodeRun, SeededEpisodeError
from commerce_evaluation.paths import DATA, LOCAL, RESULTS, ROOT
from commerce_evaluation.redaction import REDACTED
from commerce_evaluation.run_artifacts import (
    DEFAULT_ROOT,
    RunArtifacts,
    artifacts_root,
    finished_row,
)
from tests.evaluation.test_pilot_evidence import (
    MECHANISMS,
    MERCHANT_RUN,
    FakeAgentProcess,
    FakeAuth,
    checkout_report,
    fake_agent_http,
    fake_merchant,
    psp_database,
    s04,
    write_d1_decision,
    write_feed,
)

B2 = CONDITIONS["B2"]
MERCHANT = "http://127.0.0.1:4120"
JWT = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJidXllciJ9.c2lnbmF0dXJlLXNpZ25hdHVyZQ"
COMMITS = {"agent_commit": "a" * 40, "merchant_commit": "m" * 40, "payment_commit": "p" * 40}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifacts(
    root: Path, *hidden: str, invocation_id: str = "governance-pilot-test"
) -> RunArtifacts:
    kept = RunArtifacts(
        root=root, invocation_id=invocation_id, runner="governance-pilot", provenance=COMMITS
    )
    kept.hide(*hidden)
    return kept


def files_under(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*") if path.is_file())


def assert_nowhere(directory: Path, *values: str) -> None:
    """No file of the artifact carries any of the values, in any of its lines."""
    for path in files_under(directory):
        content = path.read_bytes()
        for value in values:
            for line in value.splitlines() or [value]:
                if len(line) >= 16 and not line.startswith("-----"):
                    assert line.encode() not in content, (path.name, line[:8])


@contextmanager
def wal_database(path: Path, rows: int) -> Iterator[sqlite3.Connection]:
    """A WAL database whose rows are still only in its -wal file while the writer is open."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE purchases (run_id TEXT)")
    connection.executemany("INSERT INTO purchases VALUES (?)", [(f"run-{n}",) for n in range(rows)])
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


# --- The helper -----------------------------------------------------------------------------------


def test_the_databases_are_copied_with_backup_wal_included(tmp_path: Path) -> None:
    run_dir = tmp_path / "temp" / "b2-s05-8"
    run_dir.mkdir(parents=True)
    with (
        wal_database(run_dir / "agent.db", rows=3),
        wal_database(run_dir / "agent.graph.db", rows=2),
    ):
        assert (run_dir / "agent.db-wal").stat().st_size > 0
        link = artifacts(tmp_path / "runs").keep(run_dir, {"scenario_id": "S05"})

    kept = tmp_path / "runs" / link["dir"]
    for name, rows in (("agent.db", 3), ("agent.graph.db", 2)):
        with closing(sqlite3.connect(kept / name)) as copy:
            assert copy.execute("SELECT count(*) FROM purchases").fetchone() == (rows,)
            assert copy.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert not (kept / f"{name}-wal").exists()


def test_the_manifest_names_the_row_its_commits_and_each_file_digest(tmp_path: Path) -> None:
    run_dir = tmp_path / "temp" / "b2-s05-8"
    write_feed(run_dir / "feed")
    with closing(sqlite3.connect(run_dir / "agent.db")) as database:
        database.execute("CREATE TABLE purchases (run_id TEXT)")
    (run_dir / "agent.log").write_text("INFO started\n", encoding="utf-8")
    key = {
        "scenario_id": "S05",
        "condition": "B2",
        "repetition": 8,
        "purchase_run_id": None,
        "merchant_run_id": MERCHANT_RUN,
    }

    link = artifacts(tmp_path / "runs").keep(run_dir, key)

    assert link["dir"] == "governance-pilot-test/b2-s05-8"
    kept = tmp_path / "runs" / link["dir"]
    assert link["manifest_sha256"] == sha256(kept / "MANIFEST.json")
    assert link["sha256"] == {
        name: sha256(kept / name) for name in ("agent.db", "agent.log", "feed/products.jsonl")
    }
    manifest = json.loads((kept / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "run-artifact/1"
    assert (manifest["runner"], manifest["invocation_id"], manifest["row_dir"]) == (
        "governance-pilot",
        "governance-pilot-test",
        "b2-s05-8",
    )
    assert manifest["row"] == key
    assert manifest["commits"] == {"agent": "a" * 40, "merchant": "m" * 40, "payment": "p" * 40}
    assert sorted(manifest["files"]) == [
        "agent.db",
        "agent.log",
        "feed/products.jsonl",
    ]
    for name, described in manifest["files"].items():
        assert described == {"sha256": sha256(kept / name), "bytes": (kept / name).stat().st_size}
    assert manifest["unavailable"] == ["agent.graph.db"]
    assert manifest["redacted"] == ["agent.log"]
    # The feed is the one the run used, byte for byte; the log went through redaction.
    assert (kept / "feed" / "products.jsonl").read_bytes() == (
        run_dir / "feed" / "products.jsonl"
    ).read_bytes()


def test_the_log_is_redacted_and_no_d1_key_reaches_the_artifact(tmp_path: Path) -> None:
    keys = D1RunKeys.generate()
    merchant_token = secrets.token_urlsafe(32)
    pem = governance_pilot._ap2_private_key()
    run_dir = tmp_path / "temp" / "b2-s05-8"
    run_dir.mkdir(parents=True)
    (run_dir / "agent.log").write_text(
        "\n".join(
            [
                f"AGENT_D1_HMAC_KEY_HEX={keys.hmac_hex}",
                f"settings {json.dumps({'aes': keys.aes_hex, 'pem': pem})}",
                f"GET /checkouts Authorization: Bearer {merchant_token}",
                f"buyer {JWT}",
                "INFO kept line",
            ]
        ),
        encoding="utf-8",
    )

    link = artifacts(tmp_path / "runs", keys.hmac_hex, keys.aes_hex, merchant_token, pem).keep(
        run_dir, {}
    )

    kept = tmp_path / "runs" / link["dir"]
    assert_nowhere(kept, keys.hmac_hex, keys.aes_hex, merchant_token, pem, JWT)
    log = (kept / "agent.log").read_text(encoding="utf-8")
    assert "INFO kept line" in log and log.count(REDACTED) >= 5


def test_a_run_without_files_keeps_a_manifest_and_creates_no_database(tmp_path: Path) -> None:
    run_dir = tmp_path / "temp" / "b2-s05-8"

    link = artifacts(tmp_path / "runs").keep(run_dir, {"merchant_run_id": None})

    manifest = json.loads((tmp_path / "runs" / link["dir"] / "MANIFEST.json").read_text("utf-8"))
    assert manifest["files"] == {}
    assert manifest["unavailable"] == ["agent.db", "agent.graph.db", "agent.log", "feed"]
    assert not run_dir.exists()


def test_a_failure_to_keep_is_a_measurement_error_and_the_row_survives(tmp_path: Path) -> None:
    run_dir = tmp_path / "temp" / "b2-s05-8"
    run_dir.mkdir(parents=True)
    root = tmp_path / "runs"
    root.write_text("a file where the artifacts directory should be", encoding="utf-8")
    row = {"scenario_id": "S05", "metrics": {"valid_task_success": 1}, "measurement_errors": {}}

    finished = finished_row(artifacts(root), run_dir, row, key=artifact_key(row))

    assert finished["run_artifact"] is None
    assert finished["measurement_errors"]["run_artifact"].startswith(
        ("FileExistsError", "NotADirectoryError")
    )
    assert finished["metrics"] == row["metrics"]


def test_the_row_s_error_fields_are_swept_with_the_invocation_secrets(tmp_path: Path) -> None:
    token = secrets.token_urlsafe(32)
    row = {
        "harness_error": f"PilotError: merchant answered with {token}",
        "error_response": {"code": "d1_denied", "message": "Tu compra no cumple la política"},
        "measurement_errors": {"psp_payments": f"OperationalError {JWT}"},
    }

    finished = finished_row(artifacts(tmp_path / "runs", token), tmp_path / "x", row, key={})

    assert finished["harness_error"] == f"PilotError: merchant answered with {REDACTED}"
    assert finished["error_response"] == row["error_response"]
    assert finished["measurement_errors"] == {"psp_payments": f"OperationalError {REDACTED}"}


def test_the_artifacts_go_to_local_runs_and_never_to_a_versioned_directory(
    tmp_path: Path,
) -> None:
    assert DEFAULT_ROOT == LOCAL / "runs"
    assert artifacts_root(None) == DEFAULT_ROOT.resolve()
    assert artifacts_root(tmp_path / "elsewhere") == (tmp_path / "elsewhere").resolve()
    assert artifacts_root(ROOT / ".local" / "other") == (ROOT / ".local" / "other").resolve()
    for versioned in (RESULTS / "runs", ROOT / "docs" / "runs"):
        with pytest.raises(ValueError, match="not ignored by Git"):
            artifacts_root(versioned)
    assert not DEFAULT_ROOT.resolve().is_relative_to(RESULTS.resolve())


# --- governance-pilot -----------------------------------------------------------------------------


def run_s04(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kept: RunArtifacts,
    keys: D1RunKeys,
    *,
    turn_error: str | None = None,
) -> tuple[dict[str, Any], str]:
    """One B2 repetition of S04 through run_plan, as `_run_pilot` wires it."""
    calls: list[str] = []

    def agent_writes(env: dict[str, str]) -> None:
        database = Path(env["AGENT_DATABASE_PATH"])
        write_d1_decision(database)
        with (database.parent / "agent.log").open("a", encoding="utf-8") as log:
            log.write(f"INFO merchant token {env['MERCHANT_BEARER_TOKEN']}\n")
            log.write(f"DEBUG hmac {env['AGENT_D1_HMAC_KEY_HEX']} buyer {JWT}\n")

    def popen(command: list[str], *, env: dict[str, str], **_: Any) -> FakeAgentProcess:
        agent_writes(env)
        return FakeAgentProcess(lambda _: None, env)

    request_json = fake_agent_http(
        {"governance_mode": "enforce", "hp_flow": "governed"}, "awaiting_confirmation", calls
    )

    def agent_http(url: str, **options: Any) -> HttpResult:
        if turn_error is not None and url.endswith("/conversation/turn"):
            raise PilotError(turn_error)
        return request_json(url, **options)

    monkeypatch.setattr(governance_pilot.subprocess, "Popen", popen)
    monkeypatch.setattr(
        governance_pilot, "_run_json", fake_merchant(checkout_report("ready_for_payment"), calls)
    )
    monkeypatch.setattr(governance_pilot, "_request_json", agent_http)
    monkeypatch.setattr(governance_pilot, "_wait_http", lambda *_, **__: None)
    temp = tmp_path / "temp"
    run = PlannedRun(s04(), 1, B2)

    def execute(planned: PlannedRun) -> dict[str, Any]:
        return governance_pilot._execute_repetition(
            run=planned,
            condition_environment=agent_environment(B2, MERCHANT, keys),
            mechanisms=MECHANISMS,
            agent=tmp_path / "agent-repo",
            merchant=tmp_path / "merchant-repo",
            environment={"BETTER_AUTH_URL": "http://127.0.0.1:4215"},
            run_dir=governance_pilot.run_directory(temp, planned),
            actor_id="buyer",
            auth=FakeAuth(),
            agent_port=43125,
            payment_database=psp_database(tmp_path / "payments.db"),
            hide=kept.hide,
        )

    output = io.StringIO()
    [row] = run_plan(
        [run],
        execute,
        matrix={"matrix_id": "governance-pilot-v1", "baseline_id": "B0-2026-10-06"},
        run_metadata=COMMITS,
        result_file=output,
        finish=keeping(kept, temp),
    )
    return row, output.getvalue()


def test_each_governance_pilot_row_links_its_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = D1RunKeys.generate()
    kept = artifacts(tmp_path / "runs", keys.hmac_hex, keys.aes_hex)

    row, written = run_s04(tmp_path, monkeypatch, kept, keys)

    assert row["metrics"]["valid_task_success"] == 1
    assert row["run_artifact"]["dir"] == "governance-pilot-test/b2-s04-1"
    directory = tmp_path / "runs" / row["run_artifact"]["dir"]
    assert row["run_artifact"]["manifest_sha256"] == sha256(directory / "MANIFEST.json")
    manifest = json.loads((directory / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["row"] == {
        "scenario_id": "S04",
        "condition": "B2",
        "repetition": 1,
        "purchase_run_id": "run-real",
        "merchant_run_id": MERCHANT_RUN,
    }
    assert sorted(manifest["files"]) == ["agent.db", "agent.log", "feed/products.jsonl"]
    with closing(sqlite3.connect(directory / "agent.db")) as database:
        assert database.execute("SELECT count(*) FROM governance_evidence").fetchone()[0] >= 1
    # The merchant session is issued per run: the runner hides it when the agent starts with it.
    assert_nowhere(directory, keys.hmac_hex, keys.aes_hex, "merchant-session", JWT)
    assert "merchant-session" not in written and keys.hmac_hex not in written
    assert json.loads(written)["run_artifact"] == row["run_artifact"]


def test_a_harness_error_row_keeps_its_artifact_and_a_redacted_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = D1RunKeys.generate()
    kept = artifacts(tmp_path / "runs", keys.hmac_hex, keys.aes_hex)

    row, _ = run_s04(
        tmp_path, monkeypatch, kept, keys, turn_error=f"turn failed: Bearer {'x' * 40}"
    )

    assert row["harness_error"] == f"PilotError: turn failed: Bearer {REDACTED}"
    assert row["merchant_run_id"] == MERCHANT_RUN
    directory = tmp_path / "runs" / row["run_artifact"]["dir"]
    assert (directory / "agent.db").is_file()
    assert_nowhere(directory, keys.hmac_hex, "merchant-session", JWT)


def test_the_dry_runs_create_no_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from commerce_evaluation import hnp_pilot

    runs = tmp_path / "runs"
    documents: list[str] = []
    for extra in ([], ["--artifacts-dir", str(runs)]):
        monkeypatch.setattr("sys.argv", ["governance-pilot", "--dry-run", *extra])
        assert governance_pilot.main() == 0
        assert hnp_pilot.main(["--dry-run", *extra]) == 0
        documents.append(capsys.readouterr().out)
    # The option changes nothing a dry run prints, and a dry run keeps nothing.
    assert documents[0] == documents[1]
    assert not runs.exists()


# --- hnp-pilot ------------------------------------------------------------------------------------


class FakeBuyer:
    def __init__(self, origin: str) -> None:
        self.origin = origin

    def signup(self) -> str:
        return "user-1"


def test_each_hnp_pilot_row_links_its_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    merchant_session = secrets.token_urlsafe(32)
    episodes: list[Path] = []
    started_with: list[str] = [merchant_session]

    def run_episode(scenario: Any, condition: str, **options: Any) -> EpisodeRun:
        run_dir, variables, agent = options["run_dir"], options["variables"], options["agent"]
        episodes.append(run_dir)
        write_feed(run_dir / "feed")
        with closing(sqlite3.connect(run_dir / "agent.db")) as database:
            database.execute("CREATE TABLE hnp_tasks (task_id TEXT)")
        # As UvicornAgent.start does with what the agent was started with.
        agent.secrets.add(merchant_session)
        generated = [
            variables[name]
            for name in (
                "AGENT_HNP_SERVICE_TOKEN",
                "AGENT_HNP_OPERATOR_TOKEN",
                "AGENT_HNP_AGENT_KEY_PEM",
                "AGENT_D1_AES_KEY_HEX",
            )
            if variables.get(name)
        ]
        started_with.extend(generated)
        agent.log.write(f"merchant {merchant_session} started with {json.dumps(generated)}\n")
        if len(episodes) == 2:
            agent.log.flush()
            raise RuntimeError("seed failed")
        # Only the consent JWT's signature stays in the runner, as C8's probe.
        probe = secrets.token_urlsafe(64)
        started_with.append(probe)
        agent.log.write(f"consent signature {probe}\n")
        agent.log.flush()
        run = EpisodeRun(merchant_run_id="mr-1", token_probe=probe)
        raise SeededEpisodeError(run, RuntimeError(f"poll failed with {merchant_session}"))

    monkeypatch.setattr(hnp_pilot_live, "run_episode", run_episode)
    monkeypatch.setattr(hnp_pilot_live, "prepare_services", lambda *_, **__: None)
    monkeypatch.setattr(
        hnp_pilot_live,
        "services_provenance",
        lambda *_, **__: {key: value for key, value in COMMITS.items() if key != "agent_commit"},
    )

    @contextmanager
    def running_services(**_: Any) -> Iterator[None]:
        yield

    monkeypatch.setattr(hnp_pilot_live, "running_services", running_services)
    monkeypatch.setattr(hnp_pilot_live, "BuyerAccount", FakeBuyer)
    preparation = prepare(
        DATA / "hnp-pilot-v1.json",
        scenario_ids=["HN10"],
        smoke=True,
        stack=stack_variables(merchant_origin=MERCHANT, auth_origin="http://127.0.0.1:4215"),
        environ={"HNP_PILOT_MAX_COST_USD": "1"},
    )
    arguments = argparse.Namespace(
        merchant_repo=tmp_path / "merchant",
        payment_repo=tmp_path / "payment",
        merchant_port=4120,
        payment_port=4130,
        agent_port=43125,
        auth_port=4215,
        skip_postgres_start=True,
    )
    output = tmp_path / "hnp-pilot.jsonl"
    temp = tmp_path / "temp"
    temp.mkdir()

    hnp_pilot_live.run_live(
        preparation,
        arguments=arguments,
        output=output,
        temp=temp,
        invocation_id="hnp-pilot-test",
        artifacts_root=tmp_path / "runs",
    )

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len(episodes) == 2
    assert rows[0]["harness_error"] == f"RuntimeError: poll failed with {REDACTED}"
    assert rows[1]["harness_error"] == "RuntimeError: seed failed"
    assert merchant_session not in output.read_text(encoding="utf-8")
    for row, run_dir in zip(rows, episodes, strict=True):
        assert row["run_artifact"]["dir"] == f"hnp-pilot-test/{run_dir.name}"
        directory = tmp_path / "runs" / row["run_artifact"]["dir"]
        assert row["run_artifact"]["manifest_sha256"] == sha256(directory / "MANIFEST.json")
        manifest = json.loads((directory / "MANIFEST.json").read_text(encoding="utf-8"))
        assert manifest["runner"] == "hnp-pilot"
        assert manifest["row"] == {
            "layer": row["layer"],
            "scenario_id": "HN10",
            "repetition": 1,
            "condition": row["condition"],
            "plan_index": row["plan_index"],
            "merchant_run_id": row.get("merchant_run_id"),
            "grant_id": None,
            "task_id": None,
            "purchase_run_id": None,
        }
        assert manifest["commits"] == {"agent": preparation.provenance["agent_commit"]} | {
            "merchant": "m" * 40,
            "payment": "p" * 40,
        }
        assert sorted(manifest["files"]) == ["agent.db", "agent.log", "feed/products.jsonl"]
        assert_nowhere(directory, *started_with)
        assert REDACTED in (directory / "agent.log").read_text(encoding="utf-8")
    assert rows[0]["merchant_run_id"] == "mr-1"
