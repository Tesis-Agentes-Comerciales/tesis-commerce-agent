"""Process helpers of the live stack: requirements, ports, waits and a clean environment.

Nothing here depends on fixed commits, machine paths or ports.
"""

import os
import shutil
import socket
import sqlite3
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from commerce_evaluation.paths import ROOT

AGENT = ROOT
MERCHANT_REPO = "MERCHANT_REPO"


def missing_requirements() -> list[str]:
    """Why an opted-in run cannot start here; empty when it can. Never prints values."""
    problems = []
    if os.name != "posix":
        problems.append("macOS or Linux (the Merchant inherits its listener with pass_fds)")
    repository = os.environ.get(MERCHANT_REPO)
    if not repository:
        problems.append(f"{MERCHANT_REPO} (Merchant checkout)")
    elif not (Path(repository).expanduser() / "src" / "commerce_lab").is_dir():
        problems.append(f"{MERCHANT_REPO} pointing to a Merchant checkout")
    problems.extend(f"{tool} on PATH" for tool in ("docker", "uv") if shutil.which(tool) is None)
    return problems


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until(call: Callable[[], Any], what: str, timeout: float = 60) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return call()
        except (OSError, EOFError, httpx.HTTPError, AssertionError, RuntimeError):
            if time.monotonic() > deadline:
                raise RuntimeError("LIVE_STACK_NOT_READY:" + what) from None
            time.sleep(0.2)


def clean_environment() -> dict[str, str]:
    """Children get explicit settings only; no developer credentials or virtualenv leak in."""
    result = dict(os.environ)
    for key in tuple(result):
        if key.startswith(
            ("OPENAI_", "MERCHANT_", "PAYMENT_", "AGENT_", "LANG", "RUN_", "PG", "UV_PROJECT")
        ) or key in {
            "DATABASE_URL",
            "WEBHOOK_RECEIVER_URL",
            "STOREFRONT_ORIGIN",
            "ACP_API_BASE_URL",
            "VIRTUAL_ENV",
        }:
            result.pop(key)
    result.update(PYTHONDONTWRITEBYTECODE="1", LANGSMITH_TRACING="false", RUN_LIVE_LLM="0")
    return result


def connect(path):
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def uv_run(project: Path) -> list[str]:
    return ["uv", "run", "--frozen", "--project", str(project)]


def closed(number: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        if probe.connect_ex(("127.0.0.1", number)) == 0:
            raise RuntimeError("LIVE_STACK_PORT_STILL_OPEN")
    return True


def git_state(root: Path) -> dict[str, Any]:
    # Recorded for the evidence only; the suite runs on whatever is checked out.
    def git(*args: str) -> str | None:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None

    status = git("status", "--porcelain")
    return {"head": git("rev-parse", "HEAD"), "dirty": None if status is None else bool(status)}
