"""Portable live stack: disposable PostgreSQL in Docker, the real Merchant, a fault proxy.

Opt-in only (RUN_LIVE_STACK=1 plus MERCHANT_REPO; see tests/integration/README.md). The
Merchant runs from its own checkout through `uv run --project`; no Agent process imports it.
live_purchase.py adds the payment provider and the purchase lanes on top of this stack.
Every Agent request crosses FaultProxy, which stays transparent unless a test arms a
labelled fault. Nothing here depends on fixed commits, machine paths or ports.
"""

import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.creation_lookup import BackendLookupSession, CreationLookupClient
from commerce_agent.acp.feed import ProductFeed
from commerce_agent.application import PurchaseService, PurchaseServiceError
from commerce_agent.approval import ApprovalService
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.dispatch_envelope import ExpectedRemoteBinding
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.guarded_creation import CREATE_PATH as GUARDED_CREATE_PATH
from commerce_agent.governance.guarded_creation import LOOKUP_PATH as GUARDED_LOOKUP_PATH
from commerce_agent.governance.models import ObjectRef
from commerce_agent.governance.provenance import (
    ClockProfile,
    ClockReader,
    D1EnforceConfiguration,
    ProvenanceKeys,
)
from commerce_agent.governance.remote_creation import LOOKUP_PATH, SessionScope, VerificationRequest
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.d1_envelope_sqlite import SqliteD1EnvelopeStore
from commerce_agent.persistence.remote_creation_sqlite import RemoteCreationVerifier
from tests.test_purchase_service import FakeCheckoutGateway, details

AGENT = Path(__file__).resolve().parents[2]
MERCHANT_REPO = "MERCHANT_REPO"
# Same image and digest as the Merchant CI and the joint E2E workflow.
POSTGRES_IMAGE = (
    "postgres:18.4-alpine@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15"
)
# The image's PGDATA; the Merchant D17 suites refuse any other data_directory.
POSTGRES_DATA = "/var/lib/postgresql/18/docker"
CONTAINER_PREFIX = "tesis-live-"
CONTROL = Path(__file__).with_name("live_merchant_control.py")
AGENT_WORKER = Path(__file__).with_name("live_agent_worker.py")
CREATE_PATH = "/checkout_sessions"
# Routes the governance suites exercise; FaultProxy answers 405 to anything else, by
# construction. `*` in a route stands for exactly one path segment.
ROUTES = frozenset(
    {
        ("GET", "/feeds/current"),
        ("POST", "/lab/offers/get"),
        ("POST", CREATE_PATH),
        ("POST", LOOKUP_PATH),
        ("POST", GUARDED_CREATE_PATH),
        ("POST", GUARDED_LOOKUP_PATH),
    }
)


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


def route_allowed(routes: frozenset[tuple[str, str]], method: str, path: str) -> bool:
    segments = path.split("/")
    return any(
        method == allowed
        and len(template := pattern.split("/")) == len(segments)
        and all(part in {"*", segment} for part, segment in zip(template, segments, strict=True))
        for allowed, pattern in routes
    )


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


class PostgresContainer:
    """The CI image in a new container; the Merchant's role owns tesis_lab and nothing else."""

    def __init__(self):
        self.name = CONTAINER_PREFIX + secrets.token_hex(6)
        self.port = free_port()
        self.app_user = "live_app"
        self._admin_password = secrets.token_hex(20)
        self._app_password = secrets.token_hex(20)
        self.created = False
        self.running = False

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.app_user}:{self._app_password}@127.0.0.1:{self.port}/tesis_lab"

    def docker(self, *args: str, input: str | None = None, timeout: float = 120) -> str:
        result = subprocess.run(
            ["docker", *args],
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
            # Passed by name with --env, so the password never appears in an argument list.
            env={**os.environ, "POSTGRES_PASSWORD": self._admin_password},
        )
        if result.returncode:
            # Do not echo commands, passwords or server output.
            raise RuntimeError(f"LIVE_STACK_DOCKER_FAILED:{args[0]}:{result.returncode}")
        return result.stdout.strip()

    def start(self) -> None:
        self.docker(
            "run",
            "--detach",
            "--name",
            self.name,
            "--label",
            "tesis-live=1",
            "--env",
            "POSTGRES_USER=live_admin",
            "--env",
            "POSTGRES_PASSWORD",
            "--env",
            "POSTGRES_DB=postgres",
            "--env",
            f"PGDATA={POSTGRES_DATA}",
            "--publish",
            f"127.0.0.1:{self.port}:5432",
            POSTGRES_IMAGE,
            # The unique name lets every fixture connection prove it reached this cluster.
            "-c",
            f"cluster_name={self.name}",
            "-c",
            "timezone=UTC",
        )
        self.created = self.running = True
        self.wait_ready()
        self.admin_sql(
            f"CREATE ROLE {self.app_user} LOGIN PASSWORD '{self._app_password}' "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE;\n"
            f"CREATE DATABASE tesis_lab OWNER {self.app_user};\n"
            # Only for the data_directory identity checks of the Merchant's own suites.
            f"GRANT pg_read_all_settings TO {self.app_user};\n"
        )

    def wait_ready(self) -> None:
        # TCP inside the container: the image's init server listens on its socket only.
        wait_until(
            lambda: self.docker(
                "exec", self.name, "pg_isready", "-q", "-h", "127.0.0.1", "-U", "live_admin"
            ),
            "postgres",
        )

    def admin_sql(self, statements: str) -> str:
        # SQL travels on stdin; the container's local socket trusts its own superuser.
        return self.docker(
            "exec",
            "--interactive",
            self.name,
            "psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "live_admin",
            "-d",
            "postgres",
            input=statements,
        )

    def stop(self) -> None:
        self.docker("stop", "--time", "10", self.name)
        self.running = False

    def restart(self) -> None:
        self.docker("start", self.name)
        self.running = True
        self.wait_ready()

    def remove(self) -> None:
        if self.created:
            self.docker("rm", "--force", "--volumes", self.name)
            self.created = self.running = False


def uv_run(project: Path) -> list[str]:
    return ["uv", "run", "--frozen", "--project", str(project)]


class ServiceServer:
    """`uvicorn <app>` from another checkout (Merchant or PSP), on a listener made here.

    The listener is bound in this process and inherited (pass_fds + `uvicorn --fd`), so no
    other process can take the port between choosing and serving it. The parent closes its
    copy at once: a stopped service refuses connections instead of queueing them. With a
    proxy, the proxy is the service's public origin and follows it across restarts.
    """

    def __init__(
        self,
        root: Path,
        name: str,
        project: Path,
        app: tuple[str, ...],
        env: dict[str, str],
        ready: str,
        proxy: "FaultProxy | None" = None,
    ):
        self.root = root
        self.name = name
        self.project = project
        self.app = app
        self.env = env
        self.ready = ready
        self.proxy = proxy
        self.process: subprocess.Popen[bytes] | None = None
        self.port: int | None = None
        self.pids: list[int] = []

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        assert self.process is None
        listener = socket.socket()
        log = (self.root / f"{self.name}.log").open("ab")
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            self.port = listener.getsockname()[1]
            descriptor = listener.fileno()
            self.process = subprocess.Popen(
                [
                    *uv_run(self.project),
                    "uvicorn",
                    *self.app,
                    "--fd",
                    str(descriptor),
                    "--log-level",
                    "warning",
                    "--no-access-log",
                ],
                pass_fds=(descriptor,),
                # The run directory, never the checkout: no service reads a developer .env.
                cwd=self.root,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        finally:
            listener.close()
            log.close()
        self.pids.append(self.process.pid)

        def ready():
            assert self.process is not None and self.process.poll() is None
            with httpx.Client(trust_env=False, timeout=2) as client:
                response = client.get(self.origin + self.ready)
            assert response.status_code == 200 and response.json()["status"] == "ok"

        wait_until(ready, self.name)
        if self.proxy is not None:
            self.proxy.upstream = self.origin

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            # uv and uvicorn share a session of their own; stop both.
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=15)
        self.process = None
        port = self.port
        assert port is not None
        wait_until(lambda: closed(port), f"{self.name} listener closed", timeout=15)


def closed(number: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        if probe.connect_ex(("127.0.0.1", number)) == 0:
            raise RuntimeError("LIVE_STACK_PORT_STILL_OPEN")
    return True


class MerchantControl:
    """live_merchant_control.py under the Merchant's interpreter; JSON lines, private pipe."""

    def __init__(self, root: Path, merchant: Path, env: dict[str, str]):
        self.log = (root / "merchant-control.log").open("ab")
        self.process = subprocess.Popen(
            [*uv_run(merchant), "python", "-B", str(CONTROL)],
            cwd=root,
            env=env,
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


@dataclass
class Call:
    method: str
    path: str
    fault: str
    request_body: bytes = field(repr=False)
    status: int | None = None
    response_body: bytes | None = field(default=None, repr=False)
    # Set once the Merchant answered, even when the Agent never sees that answer; never for
    # answers the proxy makes up (405, status_503).
    answered: threading.Event = field(default_factory=threading.Event, repr=False)


class FaultProxy:
    """Loopback HTTP hop between Agent and Merchant; the Merchant's public origin.

    Faults are labelled and path-scoped. `drop`, `reset` and `delay` forward first, so the
    Merchant really processes the request and only its answer is lost, reset or late.
    `status_503` answers without forwarding. `scope`, `digest`, `profile` and `malformed`
    rewrite a genuine lookup answer. An unreachable Merchant resets the Agent's connection.
    """

    FAULTS = frozenset(
        {
            "pass",
            "drop",
            "reset",
            "delay",
            "timeout_before",
            "status_503",
            "scope",
            "digest",
            "profile",
            "malformed",
        }
    )

    def __init__(self, routes: frozenset[tuple[str, str]] = ROUTES):
        self.routes = routes
        self.upstream: str | None = None
        self.calls: list[Call] = []
        self.faults: dict[str, str] = {}
        self.delay_seconds = 1.5
        self.before_forward: dict[str, Callable[[bytes, dict[str, str]], None]] = {}
        self.after_answer: dict[str, Callable[[], None]] = {}
        self.response_transform: dict[str, Callable[[bytes], bytes]] = {}
        self.errors: list[str] = []
        # One client for the hop; still a fresh upstream connection per request.
        self.client = httpx.Client(
            trust_env=False, timeout=30, limits=httpx.Limits(max_keepalive_connections=0)
        )
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            # Headers and body are separate writes; Nagle would add delayed-ACK latency.
            disable_nagle_algorithm = True

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                proxy._handle(self)

            def do_POST(self) -> None:
                proxy._handle(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def reset(self) -> None:
        self.calls.clear()
        self.faults.clear()
        self.before_forward.clear()
        self.after_answer.clear()
        self.response_transform.clear()
        self.errors.clear()
        self.delay_seconds = 1.5

    def count(self, method: str, path: str) -> int:
        return sum((call.method, call.path) == (method, path) for call in self.calls)

    def requests(self) -> list[tuple[str, str]]:
        return [(call.method, call.path) for call in self.calls]

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        method, path = handler.command, handler.path.split("?", 1)[0]
        body = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
        fault = self.faults.get(path, "pass")
        call = Call(method, path, fault, body)
        self.calls.append(call)
        if not route_allowed(self.routes, method, path):
            call.fault = "blocked"
            self._respond(handler, call, 405, {}, b"")
            return
        assert fault in self.FAULTS
        headers = {
            key: value
            for key, value in handler.headers.items()
            if key.lower() not in {"host", "connection", "content-length", "keep-alive"}
        }
        try:
            if hook := self.before_forward.get(path):
                hook(body, headers)
        except BaseException as error:
            # Fail closed: a broken expectation never reaches the Merchant.
            self.errors.append(f"before_forward:{path}:{type(error).__name__}:{error}")
            self._abort(handler, reset=True)
            return
        if fault == "timeout_before":
            # Deliberate timeout without ever forwarding, including after the client gives up.
            time.sleep(self.delay_seconds)
            self._abort(handler, reset=False)
            return
        if fault == "status_503":
            raw = json.dumps({"type": "error", "code": "proxy_unavailable"}).encode()
            self._respond(
                handler,
                call,
                503,
                {"Content-Type": "application/json", "Cache-Control": "no-store"},
                raw,
            )
            return
        assert self.upstream is not None
        try:
            request = self.client.build_request(
                method, self.upstream + handler.path, content=body, headers=headers
            )
            response = self.client.send(request, stream=True)
            try:
                raw = b"".join(response.iter_raw())
            finally:
                response.close()
        except httpx.TransportError:
            call.fault = "merchant_unreachable"
            self._abort(handler, reset=True)
            return
        call.status, call.response_body = response.status_code, raw
        call.answered.set()
        try:
            if hook := self.after_answer.get(path):
                hook()
        except BaseException as error:
            self.errors.append(f"after_answer:{path}:{type(error).__name__}:{error}")
            self._abort(handler, reset=True)
            return
        if fault in {"drop", "reset"}:
            self._abort(handler, reset=fault == "reset")
            return
        if fault == "delay":
            time.sleep(self.delay_seconds)
        if fault in {"scope", "digest", "profile"}:
            payload = json.loads(raw)
            key, value = {
                "scope": ("actor_id", "injected-foreign"),
                "digest": ("original_response_sha256", "0" * 64),
                "profile": ("original_response_profile", "injected/unknown"),
            }[fault]
            payload[key] = value
            raw = json.dumps(payload).encode()
        elif fault == "malformed":
            raw = b"{"
        if transform := self.response_transform.get(path):
            raw = transform(raw)
        forwarded = {
            key: value
            for key, value in response.headers.items()
            if key.lower()
            not in {"content-length", "connection", "keep-alive", "transfer-encoding", "date"}
        }
        self._respond(handler, call, response.status_code, forwarded, raw, record=False)

    def _respond(
        self,
        handler: BaseHTTPRequestHandler,
        call: Call,
        status: int,
        headers: dict[str, str],
        raw: bytes,
        *,
        record: bool = True,
    ) -> None:
        if record:
            call.status, call.response_body = status, raw
        try:
            handler.send_response(status)
            for key, value in headers.items():
                handler.send_header(key, value)
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # The Agent already gave up (delay beyond its timeout).

    @staticmethod
    def _abort(handler: BaseHTTPRequestHandler, *, reset: bool) -> None:
        handler.close_connection = True
        connection = handler.connection
        with suppress(OSError):
            if reset:
                # Zero linger turns the close into a TCP RST.
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                connection.close()
            else:
                connection.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.client.close()


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


def git_state(root: Path) -> dict[str, Any]:
    # Recorded for the evidence only; the suite runs on whatever is checked out.
    def git(*args: str) -> str | None:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None

    status = git("status", "--porcelain")
    return {"head": git("rev-parse", "HEAD"), "dirty": None if status is None else bool(status)}


class CutpointReached(RuntimeError):
    pass


class AgentProcess:
    """live_agent_worker.py under this checkout's interpreter; killable at named cutpoints."""

    def __init__(self, stack: LiveStack, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.log = (directory / ("agent-" + secrets.token_hex(4) + ".log")).open("ab")
        env = clean_environment()
        env.update(LIVE_STACK_ROOT=str(directory), LIVE_STACK_PORT=str(stack.proxy.port))
        self.process = subprocess.Popen(
            [sys.executable, "-B", str(AGENT_WORKER)],
            cwd=directory,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            text=True,
            encoding="utf-8",
        )
        self.pid = self.process.pid

    def _send(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        assert self.process.stdin and self.process.stdout
        self.process.stdin.write(
            json.dumps(
                {"action": action, **data},
                default=lambda value: (
                    value.get_secret_value() if isinstance(value, SecretStr) else None
                ),
            )
            + "\n"
        )
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        assert line, "LIVE_STACK_AGENT_STOPPED"
        return json.loads(line)

    def call(self, action: str, *, required: bool = True, **data: Any) -> Any:
        reply = self._send(action, data)
        if "cutpoint" in reply:
            raise CutpointReached(reply["cutpoint"])
        if required:
            assert reply["ok"], (
                f"Agent worker rejected {action}: {reply.get('error_type')}:"
                f"{reply.get('error_code')}"
            )
            return reply["result"]
        return reply

    def kill_at(self, cutpoint: str, action: str, **data: Any) -> int:
        """Run an action that parks at a cutpoint, then SIGKILL the process there."""
        reply = self._send(action, {**data, "cutpoint": cutpoint})
        assert reply == {"cutpoint": cutpoint}, "Agent did not reach the cutpoint"
        self.process.kill()
        code = self.process.wait(timeout=20)
        self._release()
        return code

    def close(self) -> None:
        if self.process.poll() is None:
            with suppress(OSError, AssertionError):
                self._send("close", {})
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self._release()

    def _release(self) -> None:
        for stream in (self.process.stdin, self.process.stdout):
            if stream and not stream.closed:
                with suppress(OSError):
                    stream.close()
        self.log.close()


class NoEffectsGateway(FakeCheckoutGateway):
    def create_checkout(self, *args, **kwargs):
        raise AssertionError("Recovery fixture Agent must never create")

    def get_checkout(self, *args, **kwargs):
        raise AssertionError("Recovery fixture Agent must never GET commercial state")

    def update_checkout(self, *args, **kwargs):
        raise AssertionError("Recovery fixture Agent must never update")

    def cancel_checkout(self, *args, **kwargs):
        raise AssertionError("Recovery fixture Agent must never cancel")


def single_offer_feed(offer: dict[str, Any], origin: str) -> ProductFeed:
    # JSON mode is needed for the frozen tuple-bearing ProductFeed, not coercion.
    return ProductFeed.model_validate_json(
        json.dumps(
            {
                "metadata": {
                    "id": "live-joint-feed",
                    "target_country": "CO",
                    "updated_at": "2026-09-10T14:00:00Z",
                },
                "products": [
                    {
                        "id": offer["product_id"],
                        "title": offer["name"],
                        "url": origin + "/fixture/product",
                        "variants": [
                            {
                                "id": offer["id"],
                                "title": offer["name"],
                                "url": origin + "/fixture/variant",
                                "price": {
                                    "amount": offer["pricing"]["items_total_minor"],
                                    "currency": "COP",
                                },
                                "availability": {"available": True, "status": "in_stock"},
                                "condition": ["new"],
                            }
                        ],
                    }
                ],
            }
        )
    )


def ephemeral_keys(key_id: str) -> ProvenanceKeys:
    return ProvenanceKeys(
        key_id=key_id,
        pseudonym_key=SecretBytes(secrets.token_bytes(32)),
        encryption_key=SecretBytes(secrets.token_bytes(32)),
    )


FIXED_CLOCK = ClockProfile(
    authority_source="system_utc",
    commercial_source="fixed_sandbox",
    authority_provider=None,
    commercial_provider=None,
    fixed_commercial_at="2026-09-10T14:00:00.000Z",
)


@dataclass
class JointCase:
    """Real Agent D1 state for one Merchant episode; the verifier runs in this process."""

    stack: LiveStack
    index: str
    origin: str
    episode: dict[str, Any]
    credential: dict[str, Any] = field(repr=False)
    path: Path
    keys: ProvenanceKeys = field(repr=False)
    owner: str
    local_run: str
    operation_id: str
    envelope: Any = field(repr=False)
    original: dict[str, Any] | None = None
    setup_creations: int = 0

    @classmethod
    def open(cls, stack: LiveStack, prefix: str) -> "JointCase":
        index = secrets.token_hex(6)
        episode = stack.rpc("episode", actor=f"{prefix}-owner-{index}")
        credential = stack.rpc("session", run_id=episode["run_id"], actor_id=episode["actor_id"])
        return cls(
            stack,
            index,
            stack.origin,
            episode,
            credential,
            stack.root / (f"{prefix}-agent-{index}.sqlite"),
            ephemeral_keys(prefix + "-ephemeral"),
            f"local-owner:{index}",
            f"local-run:{index}",
            "",
            None,
        )

    @classmethod
    def prepare(cls, stack: LiveStack, *, binding: bool = True) -> "JointCase":
        """06D-C: the gate commits a D1 reservation and stops before any Agent dispatch."""
        case = cls.open(stack, "06dc")
        store = SqliteAgentStore(case.path)
        store.initialize()
        gate = SqliteD1EnforceGate(
            case.path,
            case.configuration("06dc-cop-d1"),
            case.keys,
            ClockReader(FIXED_CLOCK),
            expected_remote_binding=case.expected_binding() if binding else None,
        )
        service = case.service(store, gate)
        try:
            case.start(service, "Isolated recovery fixture")
        except PurchaseServiceError as error:
            assert error.code == "d1_dispatch_blocked"
        else:
            raise AssertionError("Fixture must stop before Agent dispatch")
        case.recover_envelope()
        assert case.local_run != case.credential["run_id"]
        return case

    def configuration(self, policy_set_id: str) -> D1EnforceConfiguration:
        return D1EnforceConfiguration(
            governance_mode="enforce",
            merchant_base_url=self.origin,
            policy_set_id=policy_set_id,
            policy_revision=1,
            currency="COP",
            quantity=1,
            clock=FIXED_CLOCK,
        )

    def expected_binding(self) -> ExpectedRemoteBinding:
        return ExpectedRemoteBinding(
            assurance="backend_expected_not_authenticated",
            remote_run_id=self.credential["run_id"],
            remote_actor_id=self.credential["actor_id"],
            source_ref=self.source_ref(self.credential),
        )

    def service(self, store: SqliteAgentStore, gate: SqliteD1EnforceGate) -> PurchaseService:
        return PurchaseService(
            feed=single_offer_feed(self.episode["offer"], self.origin),
            checkout=NoEffectsGateway(),
            store=store,
            approvals=ApprovalService(store, SecretStr(secrets.token_hex(32))),
            d1_gate=gate,
        )

    def start(self, service: PurchaseService, request: str) -> None:
        service.start(
            run_id=self.local_run,
            owner_id=self.owner,
            request=request,
            intent=PurchaseIntent(hard=HardConstraints(currency="COP")),
            selected_variant_id=self.episode["offer"]["id"],
            details=details(),
        )

    def recover_envelope(self) -> None:
        with closing(connect(self.path)) as connection:
            self.operation_id = connection.execute(
                "SELECT operation_id FROM d1_checkout_operations"
            ).fetchone()[0]
            self.envelope = SqliteD1EnvelopeStore(connection, self.keys).recover(
                self.operation_id, run_id=self.local_run, owner_id=self.owner
            )
        assert self.envelope is not None

    @staticmethod
    def source_ref(credential: dict[str, Any]) -> ObjectRef:
        # Registry data came from issue + authenticate in the Merchant control worker.
        public = {key: credential[key] for key in ("run_id", "actor_id", "session_sha256")}
        digest = sha256_digest(canonical_bytes(public))
        return ObjectRef(
            schema_version="commerce-policy/1.0",
            id="provision:" + digest[7:],
            version=1,
            digest=digest,
        )

    def scope(self, credential: dict[str, Any] | None = None) -> SessionScope:
        credential = credential or self.credential
        return SessionScope(
            assurance="backend_session_registry/1",
            remote_run_id=credential["run_id"],
            remote_actor_id=credential["actor_id"],
            source_ref=self.source_ref(credential),
        )

    def request(self, query: str = "query:one", version: int = 0) -> VerificationRequest:
        return VerificationRequest(
            query_id=query,
            operation_id=self.operation_id,
            run_id=self.local_run,
            owner_id=self.owner,
            expected_version=version,
        )

    def client(
        self, *, credential: dict[str, Any] | None = None, timeout: float = 5.0
    ) -> CreationLookupClient:
        credential = credential or self.credential
        return CreationLookupClient(
            BackendLookupSession(
                self.origin, self.scope(credential), SecretStr(credential["bearer"])
            ),
            timeout_seconds=timeout,
        )

    def verifier(self, client: CreationLookupClient, factory=None) -> RemoteCreationVerifier:
        return RemoteCreationVerifier(
            factory or (lambda: connect(self.path)),
            self.keys,
            client,
            clock=lambda: datetime.now(UTC),
        )

    def setup_create(self) -> dict[str, Any]:
        """The one fixture POST of 06D-C: the creation an Agent sent and lost the answer to."""
        assert self.setup_creations == 0
        with httpx.Client(trust_env=False, timeout=10) as client:
            result = client.post(
                self.origin + CREATE_PATH,
                content=self.envelope.body_canonical,
                headers={
                    "Authorization": "Bearer " + self.credential["bearer"],
                    "API-Version": "2026-04-17",
                    "Content-Type": "application/json",
                    "Idempotency-Key": self.envelope.idempotency_key.get_secret_value(),
                },
            )
        assert result.status_code == 201, f"Setup creation rejected: HTTP {result.status_code}"
        value = result.json()
        assert isinstance(value, dict)
        self.original = value
        self.setup_creations = 1
        return value

    def key_sha256(self) -> str:
        return hashlib.sha256(self.envelope.idempotency_key.get_secret_value().encode()).hexdigest()

    def raw_lookup(
        self, *, credential: dict[str, Any] | None = None, digest: str | None = None
    ) -> httpx.Response:
        with httpx.Client(trust_env=False, timeout=5) as client:
            return client.post(
                self.origin + LOOKUP_PATH,
                headers={
                    "Authorization": "Bearer " + (credential or self.credential)["bearer"],
                    "API-Version": "2026-04-17",
                    "Request-Id": "client-is-not-authority",
                },
                json={
                    "schema_version": "merchant-acp-creation-lookup/1",
                    "idempotency_key_sha256": digest or self.key_sha256(),
                },
            )

    def verify_zero(
        self,
        *,
        credential: dict[str, Any] | None = None,
        request: VerificationRequest | None = None,
        expected: str = "found_verified",
        timeout: float = 5.0,
        factory=None,
    ):
        """One verification; the Merchant's tables must be byte-identical before and after."""
        before = self.stack.rpc("snapshot")
        client = self.client(credential=credential, timeout=timeout)
        started = time.perf_counter()
        try:
            verifier = self.verifier(client, factory)
            result = verifier.verify(request or self.request())
            elapsed = (time.perf_counter() - started) * 1000
            assert result.verification.outcome == expected
            assert (
                not result.verification.replay_allowed
                and not result.verification.continuation_allowed
            )
            if result.durable:
                assert result.event in verifier.inspect(self.request())
        finally:
            client.close()
        after = self.stack.rpc("snapshot")
        assert before == after, "Lookup mutated Merchant tables"
        self.stack.record(
            f"06dc:{self.index}:{expected}",
            observed=result.verification.outcome,
            durable=result.durable,
            total_verifier_ms=elapsed,
            setup_creations=self.setup_creations,
            new_checkouts=0,
            commercial_mutations=0,
        )
        return result

    def probe(self, *, crash: bool = False) -> dict[str, Any] | None:
        """A separate Agent process verifies or inspects this case's SQLite, then exits."""
        agent = AgentProcess(self.stack, self.stack.root)
        try:
            agent.call(
                "verifier",
                database=str(self.path),
                key_id=self.keys.key_id,
                hmac=self.keys.pseudonym_key.get_secret_value().hex(),
                aes=self.keys.encryption_key.get_secret_value().hex(),
                request=self.request().model_dump(mode="json"),
                origin=self.origin,
                scope=self.scope().model_dump(mode="json"),
                bearer=self.credential["bearer"],
            )
            if crash:
                assert agent.kill_at("verification_commit", "verify") == -signal.SIGKILL
                return None
            return agent.call("inspect")
        finally:
            agent.close()
