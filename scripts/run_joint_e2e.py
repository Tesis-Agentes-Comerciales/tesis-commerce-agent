#!/usr/bin/env python3
"""Run the buyer-agent, merchant, and payment-provider boundary test."""

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

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwcrypto.jwk import JWK


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


def _wait_until_ready(url: str, process: subprocess.Popen[str], timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise HarnessError(f"Service stopped before becoming ready: {url}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                payload = json.load(response)
            if response.status == 200 and payload.get("status") == "ok":
                return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.25)
    raise HarnessError(f"Service readiness check timed out: {url}")


def _wait_http(url: str, process: subprocess.Popen[str], timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise HarnessError(f"Service stopped before becoming ready: {url}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.25)
    raise HarnessError(f"Service readiness check timed out: {url}")


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
    parser.add_argument(
        "--payment-repo",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "tesis-payment-provider-sandbox",
    )
    parser.add_argument("--payment-port", type=int, default=4130)
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
    parser.add_argument(
        "--ap2",
        action="store_true",
        help="Run the signed AP2 Human Present path instead of the legacy ACP path.",
    )
    parser.add_argument("--ui", action="store_true", help="Exercise AP2 through the React UI.")
    parser.add_argument("--ui-port", type=int, default=5175)
    parser.add_argument("--auth-port", type=int, default=4215)
    parser.add_argument("--agent-port", type=int, default=43125)
    return parser


def _ap2_private_key() -> str:
    return (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.ui and not arguments.ap2:
        raise HarnessError("--ui requires --ap2")
    agent = Path(__file__).resolve().parents[1]
    merchant = arguments.merchant_repo.resolve()
    payment = arguments.payment_repo.resolve()
    if not (merchant / "pyproject.toml").is_file():
        raise HarnessError(f"Merchant repository not found: {merchant}")
    if not (payment / "pyproject.toml").is_file():
        raise HarnessError(f"Payment repository not found: {payment}")

    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    environment.update(
        {
            "ACP_API_BASE_URL": f"http://127.0.0.1:{arguments.merchant_port}",
            "WEBHOOK_RECEIVER_URL": f"http://127.0.0.1:{arguments.webhook_port}",
            "MERCHANT_WEBHOOK_SECRET": secrets.token_urlsafe(32),
            "AGENT_APPROVAL_SECRET": secrets.token_urlsafe(32),
            "PAYMENT_PROVIDER_URL": f"http://127.0.0.1:{arguments.payment_port}",
            "PAYMENT_AGENT_BEARER_TOKEN": secrets.token_urlsafe(32),
            "PAYMENT_MERCHANT_BEARER_TOKEN": secrets.token_urlsafe(32),
            "PAYMENT_TOKEN_SECRET": secrets.token_urlsafe(32),
            "PAYMENT_SANDBOX_CLOCK_AT": "2026-09-10T14:05:00Z",
            "AGENT_SANDBOX_CLOCK_AT": "2026-09-10T14:05:00Z",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    if arguments.ap2:
        agent_key = _ap2_private_key()
        merchant_key = _ap2_private_key()
        payment_key = _ap2_private_key()
        agent_jwk = JWK.from_pem(agent_key.encode()).export_public()
        merchant_jwk = JWK.from_pem(merchant_key.encode()).export_public()
        environment.update(
            {
                "AGENT_AP2_REQUIRED": "true",
                "AGENT_AP2_PRIVATE_KEY_PEM": agent_key,
                "MERCHANT_AP2_REQUIRED": "true",
                "MERCHANT_AP2_PRIVATE_KEY_PEM": merchant_key,
                "MERCHANT_AP2_AGENT_JWK_JSON": agent_jwk,
                "PAYMENT_AP2_REQUIRED": "true",
                "PAYMENT_AP2_AGENT_JWK_JSON": agent_jwk,
                "PAYMENT_AP2_MERCHANT_JWK_JSON": merchant_jwk,
                "PAYMENT_AP2_MERCHANT_ISSUER": environment["ACP_API_BASE_URL"],
                "PAYMENT_AP2_PRIVATE_KEY_PEM": payment_key,
            }
        )
    _run(["uv", "sync", "--frozen", "--all-groups"], cwd=merchant, env=environment)
    _run(["uv", "sync", "--frozen", "--all-groups"], cwd=payment, env=environment)
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
        environment["AGENT_FEED_PATH"] = str(feed)
        environment["PAYMENT_DATABASE_PATH"] = str(temp / "payments.db")
        if arguments.ui:
            ui_origin = f"http://127.0.0.1:{arguments.ui_port}"
            environment.update(
                {
                    "AGENT_AUTH_MODE": "jwt",
                    "AGENT_AUTH_ISSUER": ui_origin,
                    "AGENT_UI_ORIGIN": ui_origin,
                    "BETTER_AUTH_URL": ui_origin,
                    "BETTER_AUTH_SECRET": secrets.token_urlsafe(32),
                    "AUTH_DATABASE_PATH": str(temp / "auth.db"),
                    "PORT": str(arguments.auth_port),
                    "AUTH_DEV_UPSTREAM": f"http://127.0.0.1:{arguments.auth_port}",
                    "VITE_AGENT_API_URL": f"http://127.0.0.1:{arguments.agent_port}",
                    "UI_AP2_SMOKE_URL": ui_origin,
                }
            )
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
        if arguments.ap2:
            environment["RUN_JOINT_AP2_E2E"] = "1"
        if os.getenv("GITHUB_ACTIONS") == "true":
            print(f"::add-mask::{token}")

        log_path = temp / "merchant-api.log"
        payment_log_path = temp / "payment-api.log"
        with (
            log_path.open("w", encoding="utf-8") as log,
            payment_log_path.open("w", encoding="utf-8") as payment_log,
        ):
            payment_process = subprocess.Popen(
                [
                    "uv",
                    "run",
                    "uvicorn",
                    "tesis_payment_provider.api:app_factory",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(arguments.payment_port),
                ],
                cwd=payment,
                env=environment,
                text=True,
                stdout=payment_log,
                stderr=subprocess.STDOUT,
            )
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
                    f"{environment['PAYMENT_PROVIDER_URL']}/health",
                    payment_process,
                    timeout_seconds=30,
                )
                _wait_until_ready(
                    f"{environment['ACP_API_BASE_URL']}/health/ready",
                    process,
                    timeout_seconds=30,
                )
                if arguments.ui:
                    extra_processes: list[subprocess.Popen[str]] = []
                    extra_logs = []
                    try:
                        for name, command, cwd, port in (
                            (
                                "auth",
                                ["node", "server.js"],
                                agent / "auth",
                                arguments.auth_port,
                            ),
                            (
                                "agent",
                                [
                                    "uv",
                                    "run",
                                    "uvicorn",
                                    "commerce_agent.api:app_factory",
                                    "--factory",
                                    "--host",
                                    "127.0.0.1",
                                    "--port",
                                    str(arguments.agent_port),
                                ],
                                agent,
                                arguments.agent_port,
                            ),
                            (
                                "ui",
                                [
                                    "node",
                                    "node_modules/vite/bin/vite.js",
                                    "--host",
                                    "127.0.0.1",
                                    "--port",
                                    str(arguments.ui_port),
                                    "--strictPort",
                                ],
                                agent / "ui",
                                arguments.ui_port,
                            ),
                        ):
                            log_file = (temp / f"{name}.log").open("w", encoding="utf-8")
                            extra_logs.append(log_file)
                            extra_processes.append(
                                subprocess.Popen(
                                    command,
                                    cwd=cwd,
                                    env=environment,
                                    text=True,
                                    stdout=log_file,
                                    stderr=subprocess.STDOUT,
                                )
                            )
                            ready_url = (
                                f"http://127.0.0.1:{port}/health"
                                if name == "agent"
                                else f"http://127.0.0.1:{port}/"
                            )
                            if name == "agent":
                                _wait_until_ready(ready_url, extra_processes[-1], 30)
                            elif name == "ui":
                                _wait_http(ready_url, extra_processes[-1], 30)
                            else:
                                _wait_http(
                                    f"http://127.0.0.1:{port}/api/auth/jwks",
                                    extra_processes[-1],
                                    30,
                                )
                        _run(
                            ["uv", "run", "python", "tests/e2e/ui_ap2_smoke.py"],
                            cwd=agent,
                            env=environment,
                        )
                    finally:
                        for extra_process in reversed(extra_processes):
                            _terminate(extra_process)
                        for extra_log in extra_logs:
                            extra_log.close()
                    return 0
                _run(
                    [
                        "uv",
                        "run",
                        "pytest",
                        "tests/e2e/test_ap2_joint_purchase.py" if arguments.ap2 else "tests/e2e",
                        "-q",
                    ],
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
                        environment["PAYMENT_AGENT_BEARER_TOKEN"],
                        environment["PAYMENT_MERCHANT_BEARER_TOKEN"],
                        environment["PAYMENT_TOKEN_SECRET"],
                        environment.get("AGENT_AP2_PRIVATE_KEY_PEM", ""),
                        environment.get("MERCHANT_AP2_PRIVATE_KEY_PEM", ""),
                        environment.get("PAYMENT_AP2_PRIVATE_KEY_PEM", ""),
                    ),
                )
                if safe_log:
                    print("Merchant API log tail:", file=sys.stderr)
                    print(safe_log, file=sys.stderr)
                payment_log.flush()
                safe_payment_log = _redact(
                    payment_log_path.read_text(encoding="utf-8", errors="replace")[-4000:],
                    (
                        environment["PAYMENT_AGENT_BEARER_TOKEN"],
                        environment["PAYMENT_MERCHANT_BEARER_TOKEN"],
                        environment["PAYMENT_TOKEN_SECRET"],
                        environment.get("AGENT_AP2_PRIVATE_KEY_PEM", ""),
                        environment.get("MERCHANT_AP2_PRIVATE_KEY_PEM", ""),
                        environment.get("PAYMENT_AP2_PRIVATE_KEY_PEM", ""),
                    ),
                )
                if safe_payment_log:
                    print("Payment API log tail:", file=sys.stderr)
                    print(safe_payment_log, file=sys.stderr)
                raise
            finally:
                _terminate(process)
                _terminate(payment_process)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HarnessError as error:
        print(f"joint E2E failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
