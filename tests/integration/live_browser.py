"""The buyer UI over a live purchase lane: auth service, Agent API, Vite and Chromium.

The browser signs up through this checkout's auth service, talks to the Agent API with the
short-lived JWT it issues and approves the purchase the Agent prepares against the lane's
real Merchant and payment provider. auth/ and ui/ run with `node` after `npm ci`; the Agent
API runs in this process so the model boundary can be scripted. Replaces the September
browser smoke; the mapping is in tests/integration/README.md.
"""

import os
import secrets
import shutil
import signal
import socket
import subprocess
import threading
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from commerce_agent.api import create_app
from commerce_agent.config import Settings
from commerce_agent.llm import ConversationRouter
from tests.integration.live_purchase import SANDBOX_CLOCK, Episode, PurchaseLane
from tests.integration.live_stack import AGENT, clean_environment, closed, free_port, wait_until

AUTH = AGENT / "auth"
UI = AGENT / "ui"


def browser_requirements() -> list[str]:
    """Why the browser test cannot start here; empty when it can."""
    problems = [] if shutil.which("node") else ["node on PATH"]
    problems += [
        f"`npm ci` in {directory.name}/"
        for directory in (AUTH, UI)
        if not (directory / "node_modules").is_dir()
    ]
    return problems


class NodeServer:
    """A Node service of this checkout, in a session of its own, logging to the run directory."""

    def __init__(
        self,
        root: Path,
        name: str,
        directory: Path,
        command: tuple[str, ...],
        env: dict[str, str],
        port: int,
        ready: str,
    ):
        self.root = root
        self.name = name
        self.directory = directory
        self.command = command
        self.env = env
        self.port = port
        self.ready = ready
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        assert self.process is None
        with (self.root / f"{self.name}.log").open("ab") as log:
            self.process = subprocess.Popen(
                ["node", *self.command],
                cwd=self.directory,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )

        def ready():
            assert self.process is not None and self.process.poll() is None
            with httpx.Client(trust_env=False, timeout=2) as client:
                assert client.get(self.origin + self.ready).status_code == 200

        wait_until(ready, self.name)

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=15)
        self.process = None
        wait_until(lambda: closed(self.port), f"{self.name} listener closed", timeout=15)


class AgentServer:
    """The Agent API under uvicorn in a thread of this process, on a listener bound here."""

    def __init__(self, app: FastAPI):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        config = uvicorn.Config(app, log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.listener]}, daemon=True
        )

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.listener.getsockname()[1]}"

    def start(self) -> None:
        self.thread.start()

        def ready():
            assert self.thread.is_alive()
            with httpx.Client(trust_env=False, timeout=2) as client:
                assert client.get(self.origin + "/health").status_code == 200

        wait_until(ready, "agent")

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread.ident is not None:
            self.thread.join(timeout=15)
        self.listener.close()


class BuyerUi:
    """Auth service, Agent API and Vite for one episode; the UI's origin is the JWT issuer."""

    def __init__(self, lane: PurchaseLane, episode: Episode, router: ConversationRouter | None):
        self.root = lane.root / f"browser-{episode.actor_id}"
        self.root.mkdir()
        self.database = self.root / "agent.sqlite"
        ui_port, auth_port = free_port(), free_port()
        self.origin = f"http://127.0.0.1:{ui_port}"
        env = clean_environment()
        self.auth = NodeServer(
            self.root,
            "auth",
            AUTH,
            ("server.js",),
            env
            | {
                "BETTER_AUTH_URL": self.origin,
                "BETTER_AUTH_SECRET": secrets.token_urlsafe(32),
                "AUTH_DATABASE_PATH": str(self.root / "auth.sqlite"),
                "PORT": str(auth_port),
            },
            auth_port,
            "/api/auth/jwks",
        )
        settings = Settings(
            merchant_base_url=lane.origin,
            merchant_bearer_token=episode.bearer,
            merchant_webhook_secret=lane.webhook_secret,
            agent_approval_secret=lane.approval_secret,
            agent_ap2_required=lane.ap2,
            agent_ap2_private_key_pem=lane.agent_key,
            payment_provider_url=lane.payment.origin,
            payment_agent_bearer_token=lane.payment_token,
            agent_database_path=self.database,
            agent_feed_source="file",
            agent_feed_path=episode.feed,
            agent_auth_mode="jwt",
            agent_auth_issuer=self.origin,
            agent_ui_origin=self.origin,
            agent_sandbox_clock_at=datetime.fromisoformat(SANDBOX_CLOCK),
        )
        self.agent = AgentServer(create_app(settings=settings, conversation_router=router))
        vite = ("node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--strictPort")
        self.ui = NodeServer(
            self.root,
            "ui",
            UI,
            (*vite, "--port", str(ui_port)),
            env
            | {
                "AUTH_DEV_UPSTREAM": self.auth.origin,
                "VITE_AGENT_API_URL": self.agent.origin,
                "VITE_GOVERNANCE_MONITOR": "false",
            },
            ui_port,
            "/",
        )

    def __enter__(self) -> "BuyerUi":
        try:
            self.auth.start()
            self.agent.start()
            self.ui.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.ui.stop()
            self.auth.stop()
        finally:
            self.agent.stop()
