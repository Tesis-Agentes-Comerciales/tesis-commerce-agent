"""Small EXP-01 recording adapter; services and faults remain the existing live harness."""

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "evaluation/scenarios/exp01-first-batch.json"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def sqlite_snapshot(path: Path) -> dict[str, Any]:
    """Independent read of PSP state, retaining counts/digests only, never token values."""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        result = {}
        for (table,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ):
            quoted = '"' + table.replace('"', '""') + '"'
            rows = db.execute("SELECT * FROM " + quoted).fetchall()
            result[table] = {"count": len(rows), "sha256": digest(sorted(rows, key=str))}
        return result


@contextmanager
def recorded_case(
    path: Path, case_id: str, inputs: dict[str, Any], *, protocol_path: Path = PROTOCOL
) -> Iterator[dict[str, Any]]:
    """Freeze input and oracle before executing Agent. A failed assertion stays failed."""
    protocol = json.loads(protocol_path.read_text())
    frozen = {
        "case_id": case_id,
        "oracle": protocol["cases"][case_id],
        "inputs": inputs,
        "protocol_sha256": hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
        "frozen_at_ns": time.time_ns(),
        "semantic_evaluation": False,
    }
    source = path / "exp01-frozen.json"
    source.write_text(json.dumps(frozen, indent=2, default=str) + "\n")
    facts: dict[str, Any] = {
        "case_id": case_id,
        "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "started_at_ns": time.time_ns(),
        "status": "error",
        "cleanup_verified": False,
        "observations": {},
    }
    try:
        yield facts
        if facts["cleanup_verified"] is not True:
            raise RuntimeError("EXP01_CLEANUP_NOT_VERIFIED")
        facts["status"] = "passed"
    except BaseException as error:
        facts["status"] = (
            "failed"
            if isinstance(error, AssertionError) and facts.get("stimulus_verified") is True
            else "error"
        )
        facts["exception_type"] = type(error).__name__
        raise
    finally:
        facts["finished_at_ns"] = time.time_ns()
        (path / "exp01-observed.json").write_text(json.dumps(facts, indent=2, default=str) + "\n")
