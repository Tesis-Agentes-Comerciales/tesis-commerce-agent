"""Portable live stack: disposable PostgreSQL in Docker, the real Merchant, a fault proxy.

The Merchant runs from its own checkout through `uv run --project`; no Agent process imports it.
purchase_lane.py adds the payment provider and the purchase lanes on top of this stack.
Every Agent request crosses FaultProxy, which stays transparent unless a test arms a
labelled fault. Nothing here depends on fixed commits, machine paths or ports.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from commerce_evaluation.stack.environment import (
    AGENT,
    MERCHANT_REPO,
    clean_environment,
    closed,
    git_state,
    uv_run,
    wait_until,
)
from commerce_evaluation.stack.fault_proxy import FaultProxy
from commerce_evaluation.stack.postgres import POSTGRES_DATA, POSTGRES_IMAGE, PostgresContainer
from commerce_evaluation.stack.service_server import ServiceServer

# Run with `python -m` under the Merchant's interpreter, from this checkout's source.
CONTROL = "commerce_evaluation.stack.merchant_control"
SOURCE = AGENT / "evaluation" / "src"


class MerchantControl:
    """`merchant_control` under the Merchant's interpreter; JSON lines, private pipe."""

    def __init__(self, root: Path, merchant: Path, env: dict[str, str]):
        self.log = (root / "merchant-control.log").open("ab")
        self.process = subprocess.Popen(
            [*uv_run(merchant), "python", "-B", "-m", CONTROL],
            cwd=root,
            # Only this package's source; the Merchant's interpreter never sees the Agent's.
            env={**env, "PYTHONPATH": str(SOURCE)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            text=True,
            encoding="utf-8",
        )
        self._lock = threading.Lock()

    def __call__(self, operation: str, **values: Any) -> Any:
        assert self.process.stdin and self.process.stdout
        with self._lock:
            self.process.stdin.write(json.dumps({"operation": operation, **values}) + "\n")
            self.process.stdin.flush()
            line = self.process.stdout.readline()
        assert line, "LIVE_STACK_CONTROL_STOPPED"
        result = json.loads(line)
        assert result["ok"], f"LIVE_STACK_CONTROL_FAILED:{operation}:{result.get('error_type')}"
        return result["data"]

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self("shutdown")
                self.process.wait(timeout=20)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=10)
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()
            self.log.close()


class LiveStack:
    """One disposable PostgreSQL, one Merchant control worker and server, one FaultProxy."""

    def __init__(self, root: Path):
        self.root = root
        self.merchant = Path(os.environ[MERCHANT_REPO]).expanduser().resolve()
        self.postgres = PostgresContainer()
        self.proxy = FaultProxy()
        self.merchant_env = clean_environment()
        self.merchant_env.update(
            DATABASE_URL=self.postgres.dsn,
            # Feed URLs must name the origin the Agent talks to: the proxy.
            ACP_API_BASE_URL=self.proxy.origin,
            LIVE_STACK_CLUSTER=self.postgres.name,
            PGCONNECT_TIMEOUT="5",
        )
        self.server = ServiceServer(
            root,
            "merchant",
            self.merchant,
            ("commerce_lab.api:app",),
            self.merchant_env,
            "/health/ready",
            self.proxy,
        )
        self.control: MerchantControl | None = None
        self.evidence: list[dict[str, Any]] = []
        self._catalog: dict[str, Any] | None = None

    @property
    def origin(self) -> str:
        return self.proxy.origin

    def rpc(self, operation: str, **values: Any) -> Any:
        assert self.control is not None
        return self.control(operation, **values)

    def start(self) -> None:
        self.postgres.start()
        self.control = MerchantControl(self.root, self.merchant, self.merchant_env)
        identity = wait_until(lambda: self.rpc("identity"), "merchant control")
        assert identity["database"] == "tesis_lab"
        assert identity["user"] == self.postgres.app_user
        assert identity["cluster_name"] == self.postgres.name
        assert identity["data_directory"] == POSTGRES_DATA
        migrations = self.rpc("prepare")
        self.server.start()
        self.record(
            "bootstrap",
            postgres_image=POSTGRES_IMAGE,
            container=self.postgres.name,
            merchant_identity=identity,
            migrations=migrations,
            agent_python=sys.version.split()[0],
            agent_checkout=git_state(AGENT),
            merchant_checkout=git_state(self.merchant),
        )

    def catalog(self) -> dict[str, Any]:
        """The Merchant-managed catalog P/Q observe; imported once per stack."""
        if self._catalog is None:
            self._catalog = dict(self.rpc("managed_catalog"))
        return self._catalog

    def record(self, scenario: str, **data: Any) -> None:
        # Public facts only: counts, outcomes, digests. Never tokens, keys or DSNs.
        self.evidence.append({"scenario": scenario, **data})
        with (self.root / "evidence.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps({"scenario": scenario, **data}, default=str) + "\n")

    def close(self) -> None:
        try:
            self.server.stop()
            if self.control is not None:
                self.control.close()
        finally:
            self.proxy.close()
            self.postgres.remove()
            self.record(
                "shutdown",
                merchant_pids=self.server.pids,
                proxy_closed=closed(self.proxy.port),
                postgres_removed=not self.postgres.created,
            )
