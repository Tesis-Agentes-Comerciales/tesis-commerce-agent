"""Each pilot row keeps its run's artifact outside Git (run-traceability.md §4.3, T1).

The databases are real SQLite files in `tmp_path`; the agent, the merchant, the buyer and every
HTTP call are doubles. No test starts a service or calls a model.
"""

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from commerce_evaluation.agent_condition import D1RunKeys, ap2_private_key
from commerce_evaluation.paths import LOCAL, RESULTS, ROOT
from commerce_evaluation.redaction import REDACTED
from commerce_evaluation.run_artifacts import (
    DEFAULT_ROOT,
    RunArtifacts,
    artifacts_root,
    finished_row,
)

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
    (run_dir / "feed").mkdir(parents=True)
    (run_dir / "feed" / "products.jsonl").write_text('{"id": "BBGooglePixel8-128GB"}\n', "utf-8")
    with closing(sqlite3.connect(run_dir / "agent.db")) as database:
        database.execute("CREATE TABLE purchases (run_id TEXT)")
    (run_dir / "agent.log").write_text("INFO started\n", encoding="utf-8")
    key = {
        "scenario_id": "S05",
        "condition": "B2",
        "repetition": 8,
        "purchase_run_id": None,
        "merchant_run_id": "6f1c2d3e-4b5a-4c6d-8e7f-0a1b2c3d4e5f",
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
    pem = ap2_private_key()
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

    finished = finished_row(artifacts(root), run_dir, row, key={"scenario_id": "S05"})

    assert finished["run_artifact"] is None
    assert finished["measurement_errors"]["run_artifact"].startswith(
        ("FileExistsError", "NotADirectoryError")
    )
    assert finished["metrics"] == row["metrics"]


def test_no_row_names_a_local_directory(tmp_path: Path) -> None:
    """An error names the artifacts root, the invocation's temporary directory or the home
    directory by their labels, never by the absolute path (T3)."""
    run_dir = tmp_path / "temp" / "001-b2-hnp-hn01"
    run_dir.mkdir(parents=True)
    root = tmp_path / "runs"
    root.write_text("a file where the artifacts directory should be", encoding="utf-8")
    merchant = Path.home() / "repos" / "tesis-commerce-merchant"
    row = {
        "harness_error": f"PilotError: {merchant} answered 500",
        "measurement_errors": {"ledger": f"OperationalError: {run_dir / 'agent.db'}"},
    }

    finished = finished_row(artifacts(root), run_dir, row, key={})

    assert "<artifacts-dir>" in finished["measurement_errors"]["run_artifact"]
    assert finished["measurement_errors"]["ledger"] == (
        "OperationalError: <run-tmp>/001-b2-hnp-hn01/agent.db"
    )
    assert finished["harness_error"] == "PilotError: ~/repos/tesis-commerce-merchant answered 500"
    text = json.dumps(finished)
    assert str(tmp_path) not in text and str(Path.home()) not in text


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
