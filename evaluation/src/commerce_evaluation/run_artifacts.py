"""Each pilot row's run artifact, kept outside Git (docs/design/run-traceability.md §4.3, T1).

Once the agent of a row has stopped, `RunArtifacts.keep` copies what its run directory holds
before the runner's temporary directory is deleted: `agent.db` and `agent.graph.db` with
`sqlite3.Connection.backup` (a consistent copy, WAL included), the feed the run used or mutated,
and `agent.log` redacted. `MANIFEST.json` names the row, the commits and each file's sha256, and
the row keeps the artifact's relative directory and digests in `run_artifact`. The artifact holds
the sandbox's JWTs, mandates and idempotency keys, so it is never versioned: the default directory
is `.local/runs/`, and another one must be outside the checkout or ignored by Git. No D1 key is
ever written: the runners pass them only to the agent, and the log copy is redacted.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from commerce_evaluation.paths import LOCAL, ROOT
from commerce_evaluation.pilot_measures import read_only
from commerce_evaluation.redaction import redact, sweep_fields

SCHEMA = "run-artifact/1"
DEFAULT_ROOT = LOCAL / "runs"
# The agent's own files in a run directory. auth.db and payments.db are shared by every row of
# the invocation and hold its secrets, so they are never kept.
DATABASES = ("agent.db", "agent.graph.db")
LOG = "agent.log"
FEED = "feed"
MANIFEST = "MANIFEST.json"
# Row fields the runners fill from errors, and the HNP row's flow_trace (T3): swept before the row
# is written (§4.2).
SWEPT_FIELDS = ("harness_error", "error_response", "measurement_errors", "flow_trace")
# What a row shows instead of a local directory (§4.2): no row names an absolute local path.
ARTIFACTS_LABEL, TEMPORARY_LABEL, HOME_LABEL = "<artifacts-dir>", "<run-tmp>", "~"
SERVICES = ("agent", "merchant", "payment")


def artifacts_root(path: Path | None, *, checkout: Path = ROOT) -> Path:
    """Where an invocation keeps its artifacts: `.local/runs/` unless another directory is given,
    which must be outside the checkout or ignored by Git, so an artifact never reaches a commit."""
    root = (path or DEFAULT_ROOT).resolve()
    if root.is_relative_to(checkout.resolve()) and not _ignored(checkout, root):
        raise ValueError(
            f"{root} is inside the checkout and not ignored by Git; run artifacts hold sandbox "
            "JWTs and mandates: use .local/runs/ or a directory outside the checkout"
        )
    return root


def _ignored(checkout: Path, path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "check-ignore", "-q", "--", str(path)],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def commits(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """The commit of each service the row ran on (`<service>_commit` of the run metadata)."""
    return {name: metadata[f"{name}_commit"] for name in SERVICES if f"{name}_commit" in metadata}


def backup_database(source: Path, target: Path) -> None:
    """A consistent copy of a SQLite database, its WAL included, as one self-contained file."""
    with closing(read_only(source)) as origin, closing(sqlite3.connect(target)) as copy:
        origin.backup(copy)
        copy.execute("PRAGMA journal_mode=DELETE")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as content:
        for block in iter(lambda: content.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class RunArtifacts:
    """One invocation's artifacts, under `<root>/<invocation_id>/<row directory>/`."""

    root: Path
    invocation_id: str
    runner: str
    # The run metadata with each service's `<service>_commit`.
    provenance: Mapping[str, Any]
    # Every secret of the invocation: generated ones, and per row the agent's (its merchant
    # session) as the runner learns them.
    secrets: set[str] = field(default_factory=set, repr=False)

    def hide(self, *values: str | None) -> None:
        self.secrets.update(value for value in values if value)

    def local_paths(self, run_dir: Path) -> dict[str, str]:
        """The local directories a row's error may name, and the label it shows instead: the
        artifacts root, the invocation's temporary directory (the parent of each row's run
        directory) and the home directory."""
        temporary = run_dir.parent
        labels = {
            Path.home(): HOME_LABEL,
            temporary: TEMPORARY_LABEL,
            temporary.resolve(): TEMPORARY_LABEL,
            self.root: ARTIFACTS_LABEL,
            self.root.resolve(): ARTIFACTS_LABEL,
        }
        # A filesystem root (a home of `/`) would relabel every path separator.
        return {str(path): label for path, label in labels.items() if path.parent != path}

    def keep(self, run_dir: Path, key: Mapping[str, Any]) -> dict[str, Any]:
        """Copy the row's run directory, write its MANIFEST.json and return the row's link."""
        invocation = self.root / self.invocation_id
        invocation.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = invocation / run_dir.name
        target.mkdir()
        unavailable: list[str] = []
        for name in DATABASES:
            if (run_dir / name).is_file():
                backup_database(run_dir / name, target / name)
            else:
                unavailable.append(name)
        if (run_dir / LOG).is_file():
            text = (run_dir / LOG).read_text(encoding="utf-8", errors="replace")
            (target / LOG).write_text(redact(text, self.secrets), encoding="utf-8")
        else:
            unavailable.append(LOG)
        if (run_dir / FEED).is_dir():
            shutil.copytree(run_dir / FEED, target / FEED)
        else:
            unavailable.append(FEED)
        files = {
            path.relative_to(target).as_posix(): {
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in sorted(target.rglob("*"))
            if path.is_file()
        }
        manifest = {
            "schema_version": SCHEMA,
            "runner": self.runner,
            "invocation_id": self.invocation_id,
            "row_dir": run_dir.name,
            "row": dict(key),
            "commits": commits(self.provenance),
            "kept_at": datetime.now(UTC).isoformat(),
            "files": files,
            "unavailable": unavailable,
            "redacted": [LOG] if LOG in files else [],
        }
        (target / MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return {
            "dir": f"{self.invocation_id}/{run_dir.name}",
            "manifest_sha256": _sha256(target / MANIFEST),
            "sha256": {name: described["sha256"] for name, described in files.items()},
        }


def finished_row(
    artifacts: RunArtifacts, run_dir: Path, row: Mapping[str, Any], *, key: Mapping[str, Any]
) -> dict[str, Any]:
    """The row as the runner writes it: linked to its artifact, with its error fields and its
    flow_trace redacted and without an absolute local path.

    Failing to keep the artifact never loses the row nor fails the run, whose observation stands
    without it: `run_artifact` is null and the reason is the row's `run_artifact` measurement
    error, which the summaries count.
    """
    finished = dict(row)
    try:
        finished["run_artifact"] = artifacts.keep(run_dir, key)
    except Exception as error:
        finished["run_artifact"] = None
        finished["measurement_errors"] = dict(row.get("measurement_errors") or {}) | {
            "run_artifact": f"{type(error).__name__}: {error}"
        }
    return sweep_fields(
        finished, SWEPT_FIELDS, artifacts.secrets, paths=artifacts.local_paths(run_dir)
    )
