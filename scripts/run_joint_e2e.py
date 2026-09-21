#!/usr/bin/env python3
"""Run the buyer-agent/merchant boundary test against separate processes."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MERCHANT_REVISION = "361bfe7436dd0167cad3b4b3fedbf79c82a9c76b"


class HarnessError(RuntimeError):
    pass


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=capture,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() if capture else "see command output above"
        raise HarnessError(f"Command failed ({command[0]}): {detail}")
    return result


def _run_json(command: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    result = _run(command, cwd=cwd, env=env, capture=True)
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise HarnessError(f"Command returned invalid JSON: {command[0]}") from error
    if not isinstance(value, dict):
        raise HarnessError(f"Command returned a non-object JSON value: {command[0]}")
    return value


def _verify_merchant_revision(merchant: Path, env: dict[str, str]) -> None:
    result = _run(["git", "rev-parse", "HEAD"], cwd=merchant, env=env, capture=True)
    if result.stdout.strip() != MERCHANT_REVISION:
        raise HarnessError(
            f"Merchant checkout is not the pinned integration revision {MERCHANT_REVISION}"
        )


def _wait_until_ready(url: str, process: subprocess.Popen[str], timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise HarnessError("Merchant API stopped before becoming ready")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                payload = json.load(response)
            if response.status == 200 and payload.get("status") == "ok":
                return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.25)
    raise HarnessError("Merchant API readiness check timed out")


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _redact(value: str, secrets_to_hide: tuple[str, ...]) -> str:
    for secret in secrets_to_hide:
        if secret:
            value = value.replace(secret, "[redacted]")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--merchant-repo",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "tesis-commerce-merchant",
    )
    parser.add_argument("--merchant-port", type=int, default=4120)
    parser.add_argument("--webhook-port", type=int, default=43120)
    parser.add_argument(
        "--skip-postgres-start",
        action="store_true",
        help="Use an already-running PostgreSQL service instead of Docker Compose.",
    )
    parser.add_argument(
        "--live-llm",
        action="store_true",
        help="Use the configured OpenAI model for the conversational scenario.",
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    agent = Path(__file__).resolve().parents[1]
    merchant = arguments.merchant_repo.resolve()
    if not (merchant / "pyproject.toml").is_file():
        raise HarnessError(f"Merchant repository not found: {merchant}")

    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    environment.update(
        {
            "ACP_API_BASE_URL": f"http://127.0.0.1:{arguments.merchant_port}",
            "WEBHOOK_RECEIVER_URL": f"http://127.0.0.1:{arguments.webhook_port}",
            "MERCHANT_WEBHOOK_SECRET": secrets.token_urlsafe(32),
            "AGENT_APPROVAL_SECRET": secrets.token_urlsafe(32),
            "AGENT_PAYMENT_SECRET": secrets.token_urlsafe(32),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    _verify_merchant_revision(merchant, environment)
    _run(["uv", "sync", "--frozen", "--all-groups"], cwd=merchant, env=environment)
    if not arguments.skip_postgres_start:
        _run(
            ["docker", "compose", "up", "-d", "--wait", "postgres"],
            cwd=merchant,
            env=environment,
        )

    _run(["uv", "run", "python", "-m", "commerce_lab.db", "migrate"], cwd=merchant, env=environment)
    actor_id = "joint-e2e-agent"
    seeded = _run_json(
        [
            "uv",
            "run",
            "python",
            "-m",
            "commerce_lab.db",
            "seed",
            "--actor-id",
            actor_id,
            "--variant",
            "B0",
        ],
        cwd=merchant,
        env=environment,
    )
    run_id = seeded.get("run_id")
    if not isinstance(run_id, str):
        raise HarnessError("Merchant seed did not return a run_id")

    with tempfile.TemporaryDirectory(prefix="commerce-joint-e2e-") as temporary:
        temp = Path(temporary)
        feed = temp / "feed"
        environment["AGENT_DATABASE_PATH"] = str(temp / "agent.db")
        _run(
            [
                "uv",
                "run",
                "python",
                "-m",
                "commerce_lab.feed",
                "export",
                "--output-dir",
                str(feed),
                "--run-id",
                run_id,
            ],
            cwd=merchant,
            env=environment,
        )
        session = _run_json(
            [
                "uv",
                "run",
                "python",
                "-m",
                "commerce_lab.db",
                "issue-session",
                run_id,
                actor_id,
                "--ttl-seconds",
                "3600",
            ],
            cwd=merchant,
            env=environment,
        )
        token = session.get("session_token")
        if not isinstance(token, str):
            raise HarnessError("Merchant session command did not return a token")
        environment.update(
            {
                "RUN_JOINT_E2E": "1",
                "MERCHANT_BASE_URL": environment["ACP_API_BASE_URL"],
                "MERCHANT_BEARER_TOKEN": token,
                "MERCHANT_REPO": str(merchant),
                "MERCHANT_RUN_ID": run_id,
                "MERCHANT_ACTOR_ID": actor_id,
                "PRODUCT_FEED_PATH": str(feed),
                "AGENT_WEBHOOK_PORT": str(arguments.webhook_port),
            }
        )
        if arguments.live_llm:
            environment["RUN_LIVE_LLM"] = "1"
        if os.getenv("GITHUB_ACTIONS") == "true":
            print(f"::add-mask::{token}")

        log_path = temp / "merchant-api.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    "uv",
                    "run",
                    "uvicorn",
                    "commerce_lab.api:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(arguments.merchant_port),
                ],
                cwd=merchant,
                env=environment,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                _wait_until_ready(
                    f"{environment['ACP_API_BASE_URL']}/health/ready",
                    process,
                    timeout_seconds=30,
                )
                _run(
                    ["uv", "run", "pytest", "tests/e2e", "-q"],
                    cwd=agent,
                    env=environment,
                )
            except Exception:
                log.flush()
                safe_log = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                safe_log = _redact(
                    safe_log,
                    (
                        token,
                        environment["MERCHANT_WEBHOOK_SECRET"],
                        environment["AGENT_APPROVAL_SECRET"],
                        environment["AGENT_PAYMENT_SECRET"],
                    ),
                )
                if safe_log:
                    print("Merchant API log tail:", file=sys.stderr)
                    print(safe_log, file=sys.stderr)
                raise
            finally:
                _terminate(process)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HarnessError as error:
        print(f"joint E2E failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
