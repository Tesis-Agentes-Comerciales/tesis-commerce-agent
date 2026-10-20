"""Opt-in real processes and disposable databases. No production imports from Merchant."""

import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing.connection import Client
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.creation_lookup import BackendLookupSession, CreationLookupClient
from commerce_agent.acp.feed import ProductFeed
from commerce_agent.application import PurchaseService, PurchaseServiceError
from commerce_agent.approval import ApprovalService
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.dispatch_envelope import ExpectedRemoteBinding
from commerce_agent.governance.enforce import SqliteD1EnforceGate
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
# Machine locations come only from the environment (tests/integration/README.md).
MERCHANT_REPO = "MERCHANT_REPO"
PG_BIN_DIR = "PG_BIN_DIR"
POSTGRES_TOOLS = ("initdb", "pg_ctl", "psql", "postgres")
AGENT_HEAD = "4b890cb468d29e658d4c79987ec59d86394c4582"
MERCHANT_HEAD = "c5bd8113998f26928f7d2b62771285fe4040d07f"
WORKER = Path(__file__).with_name("gov06dc_merchant_worker.py")


def executable(directory: Path, name: str) -> Path:
    """Program inside a directory; only Windows adds the .exe suffix."""
    return directory / (name + ".exe" if os.name == "nt" else name)


def merchant_root() -> Path:
    return Path(os.environ[MERCHANT_REPO]).expanduser().resolve()


def merchant_python() -> Path:
    """The frozen Merchant's own virtual environment (uv sync), never the Agent's."""
    return executable(
        merchant_root() / ".venv" / ("Scripts" if os.name == "nt" else "bin"), "python"
    )


def pg_tool(name: str) -> Path:
    return executable(Path(os.environ[PG_BIN_DIR]).expanduser().resolve(), name)


def missing_locations(*, windows_listener: bool = False) -> list[str]:
    """Why an opted-in live run cannot start here; empty when it can. Never prints values."""
    problems = []
    if not os.environ.get(MERCHANT_REPO):
        problems.append(f"{MERCHANT_REPO} (frozen Merchant checkout)")
    elif not merchant_python().is_file():
        problems.append(f"{MERCHANT_REPO} with a synced .venv (uv sync --frozen)")
    if not os.environ.get(PG_BIN_DIR):
        problems.append(f"{PG_BIN_DIR} (PostgreSQL 17 bin directory)")
    elif absent := [name for name in POSTGRES_TOOLS if not pg_tool(name).is_file()]:
        problems.append(f"{PG_BIN_DIR} containing {', '.join(absent)}")
    if windows_listener and os.name != "nt":
        problems.append("Windows socket.share for the audited Merchant listener handoff")
    return problems


def live_requirements(opt_in: str, reason: str, *, windows_listener: bool = False):
    """Opt-in first, then explicit locations; both hold before any real process starts."""
    problems = missing_locations(windows_listener=windows_listener)
    return [
        pytest.mark.skipif(os.environ.get(opt_in) != "1", reason=reason),
        pytest.mark.skipif(
            bool(problems), reason=f"{opt_in}=1 also requires " + "; ".join(problems)
        ),
    ]


def port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        value = listener.getsockname()[1]
    assert value not in {55432, 55433, 62281}
    return value


def connect(path):
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def wait_ready(call, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return call()
        except (OSError, EOFError, httpx.HTTPError, AssertionError):
            time.sleep(0.1)
    raise RuntimeError("G06_PROCESS_NOT_READY")


def _require_windows_socket_sharing() -> None:
    """Require Windows without making other class attributes unreachable on Linux."""
    if os.name != "nt":
        raise RuntimeError("This audited harness currently requires Windows socket sharing")


def clean_environment():
    result = dict(os.environ)
    for key in tuple(result):
        if key.startswith(
            ("OPENAI_", "MERCHANT_", "PAYMENT_", "AGENT_", "LANG", "RUN_", "PG")
        ) or key in {
            "DATABASE_URL",
            "WEBHOOK_RECEIVER_URL",
            "STOREFRONT_ORIGIN",
            "ACP_API_BASE_URL",
        }:
            result.pop(key)
    result.update(PYTHONDONTWRITEBYTECODE="1", LANGSMITH_TRACING="false", RUN_LIVE_LLM="0")
    return result


class LiveEnvironment:
    def __init__(self, *, agent_head=AGENT_HEAD, allowed_agent_changes=frozenset()):
        _require_windows_socket_sharing()
        assert os.environ.get("RUN_GOV06DC_REAL") == "1", "Explicit real-test opt-in required"
        self.agent_head = agent_head
        self.merchant = merchant_root()
        for root, commit in ((AGENT, agent_head), (self.merchant, MERCHANT_HEAD)):
            actual = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
            ).strip()
            assert actual == commit
            changed = subprocess.check_output(
                ["git", "-C", str(root), "diff", "HEAD", "--name-only"], text=True
            ).splitlines()
            assert set(changed) <= (set(allowed_agent_changes) if root == AGENT else set())
        assert not subprocess.check_output(
            ["git", "-C", str(self.merchant), "status", "--porcelain"]
        )
        self.root = AGENT / ".local" / ("gov06dc-" + secrets.token_hex(6))
        self.root.mkdir(parents=True, exist_ok=False)
        self.data = self.root / "pgdata"
        self.pg_port, self.http_port, self.control_port = port(), port(), port()
        assert len({self.pg_port, self.http_port, self.control_port}) == 3
        self.authkey = secrets.token_bytes(32)
        self.env = clean_environment()
        self.env.update(
            DATABASE_URL=f"postgresql://gov06dc_app:{secrets.token_hex(20)}@127.0.0.1:{self.pg_port}/tesis_lab",
            ACP_API_BASE_URL=f"http://127.0.0.1:{self.http_port}",
            G06_MERCHANT=str(self.merchant),
            G06_AGENT=str(AGENT),
            G06_ROOT=str(self.root),
            G06_PGDATA=str(self.data),
            G06_HTTP=str(self.http_port),
            G06_CONTROL=str(self.control_port),
            G06_IPC_KEY=self.authkey.hex(),
            PGCONNECT_TIMEOUT="2",
        )
        self.server: subprocess.Popen | None = None
        self.server_pid: int | None = None
        self.control: subprocess.Popen | None = None
        self.pg_started = False
        self.evidence: list[dict[str, Any]] = []
        self.processes: list[int] = []

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.http_port}"

    def command(self, executable, *args, input=None):
        # pg_ctl descendants inherit handles on Windows. Real files avoid an
        # indefinitely open PIPE after the launcher has already exited.
        log = self.root / ("command-" + secrets.token_hex(4))
        with (
            log.with_suffix(".out").open("wb") as stdout,
            log.with_suffix(".err").open("wb") as stderr,
        ):
            result = subprocess.run(
                [str(executable), *map(str, args)],
                input=input,
                text=True,
                stdout=stdout,
                stderr=stderr,
                timeout=40,
                env=self.env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        if result.returncode:
            # Do not echo DSNs/commands/environment or accidental DB data.
            raise RuntimeError(f"G06_SUBPROCESS_FAILED:{Path(executable).name}:{result.returncode}")
        return log.with_suffix(".out").read_text(encoding="utf-8", errors="replace").strip()

    def psql(self, statement, database="postgres"):
        return self.command(
            pg_tool("psql"),
            "-X",
            "-qAt",
            "-h",
            "127.0.0.1",
            "-p",
            self.pg_port,
            "-U",
            "gov06dc_admin",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            statement,
        )

    def assert_cluster(self):
        value = self.psql(
            "SELECT json_build_array(current_database(),current_user,"
            "host(inet_server_addr()),inet_server_port(),current_setting('data_directory'))"
        )
        row = json.loads(value)
        assert row[:4] == ["postgres", "gov06dc_admin", "127.0.0.1", self.pg_port]
        assert Path(row[4]).resolve() == self.data.resolve()
        assert self.psql("SHOW timezone") == "UTC"

    def start_pg(self):
        self.pg_started = True
        self.command(
            pg_tool("pg_ctl"),
            "-D",
            self.data,
            "-l",
            self.root / "postgres.log",
            "-o",
            f"-h 127.0.0.1 -p {self.pg_port}",
            "-w",
            "start",
        )
        self.assert_cluster()

    def stop_pg(self):
        if self.pg_started:
            self.command(pg_tool("pg_ctl"), "-D", self.data, "-m", "fast", "-w", "stop")
            self.pg_started = False

    def spawn(self, mode, **extra_env):
        stream = (self.root / (mode + ".log")).open("ab")
        try:
            child = subprocess.Popen(
                [str(merchant_python()), "-B", str(WORKER), mode],
                cwd=self.merchant,
                env={**self.env, **extra_env},
                stdin=subprocess.PIPE if mode == "server" else subprocess.DEVNULL,
                stdout=stream,
                stderr=stream,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        finally:
            stream.close()
        self.processes.append(child.pid)
        return child

    def start_server(self):
        assert self.server is None
        # Keep this exact kernel listener under our ownership across process start.
        # There is no close/rebind interval in which another process can take it.
        with socket.socket() as listener:
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            share = getattr(listener, "share", None)
            if not isinstance(exclusive, int) or not callable(share):
                raise RuntimeError("Windows socket sharing API unavailable")
            listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            listener.bind(("127.0.0.1", self.http_port))
            listener.listen(128)
            ticket = secrets.token_hex(12)
            self.server = self.spawn("server", G06_LAUNCH_TICKET=ticket)

            def child_identity():
                assert self.server and self.server.poll() is None
                birth = json.loads((self.root / f"server-boot-{ticket}.json").read_text())
                assert birth["parent_pid"] == self.server.pid
                return birth

            birth = wait_ready(child_identity)
            actual_pid = birth["pid"]
            assert isinstance(actual_pid, int) and actual_pid > 0
            self.server_pid = actual_pid
            self.processes.append(actual_pid)
            assert self.server.stdin is not None
            shared_bytes = share(actual_pid)
            if not isinstance(shared_bytes, bytes):
                raise RuntimeError("Invalid Windows socket sharing payload")
            self.server.stdin.write(
                json.dumps({"socket_share": shared_bytes.hex()}).encode() + b"\n"
            )
            self.server.stdin.flush()
            self.server.stdin.close()

        def health():
            assert self.server and self.server.poll() is None
            with httpx.Client(trust_env=False, timeout=1) as client:
                result = client.get(self.origin + "/health/live")
                assert result.status_code == 200

        wait_ready(health)
        marker = json.loads((self.root / f"server-socket-{self.server_pid}.json").read_text())
        assert marker == {
            "pid": self.server_pid,
            "address": ["127.0.0.1", self.http_port],
            "source": "targeted_windows_socket_share",
        }

    def stop_server(self):
        if self.server is not None:
            if self.server.poll() is None:
                self.server.terminate()
            self.server.wait(timeout=15)
            self.server = None

    def rpc(self, operation, **values):
        with Client(("127.0.0.1", self.control_port), authkey=self.authkey) as channel:
            channel.send({"operation": operation, **values})
            assert channel.poll(30), "G06_CONTROL_TIMEOUT"
            result = channel.recv()
        assert result["ok"], f"G06_CONTROL_FAILED:{operation}:{result.get('error_type')}"
        return result["data"]

    def start(self):
        self.command(
            pg_tool("initdb"),
            "-D",
            self.data,
            "-U",
            "gov06dc_admin",
            "-A",
            "trust",
            "--encoding=UTF8",
            "--no-locale",
        )
        # initdb is brand new; bind only loopback and trust only the two synthetic roles.
        (self.data / "pg_hba.conf").write_text(
            "host all gov06dc_admin 127.0.0.1/32 trust\n"
            "host tesis_lab gov06dc_app 127.0.0.1/32 trust\n",
            encoding="utf-8",
        )
        with (self.data / "postgresql.conf").open("a", encoding="utf-8") as config:
            config.write("\ntimezone='UTC'\nlog_timezone='UTC'\n")
        self.start_pg()
        self.psql("CREATE ROLE gov06dc_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        self.psql("CREATE DATABASE tesis_lab OWNER gov06dc_app")
        # Required for the per-connection identity probe, not arbitrary superuser access.
        self.psql("GRANT pg_read_all_settings TO gov06dc_app")
        self.control = self.spawn("control")

        # An authenticated connection is required: raw readiness probes would kill Listener.accept.
        def probe():
            return self.rpc("snapshot")

        wait_ready(probe)
        prepared = self.rpc("prepare")
        self.start_server()
        assert self.server is not None
        self.record(
            "bootstrap",
            data_directory=str(self.data),
            pg_port=self.pg_port,
            http_port=self.http_port,
            control_port=self.control_port,
            migrations=prepared,
            postgres_version=self.command(pg_tool("postgres"), "--version"),
            merchant_versions=self.rpc("versions"),
            agent_python=sys.version.split()[0],
            agent_commit=self.agent_head,
            merchant_commit=MERCHANT_HEAD,
            merchant_listener_owned_pid=self.server_pid,
        )

    def record(self, scenario, **data):
        # Only callers' public assertions/digests/counts. Never tokens, raw keys or DSNs.
        record = {"scenario": scenario, **data}
        self.evidence.append(record)
        (self.root / "evidence.json").write_text(
            json.dumps(self.evidence, indent=2) + "\n", encoding="utf-8"
        )

    def close(self):
        try:
            self.stop_server()
            if self.control is not None:
                if self.control.poll() is None:
                    with suppress(Exception):
                        self.rpc("shutdown")
                    try:
                        self.control.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.control.terminate()
                        self.control.wait(timeout=10)
                self.control = None
        finally:
            self.stop_pg()
            closed = {}
            for number in (self.pg_port, self.http_port, self.control_port):
                with socket.socket() as probe:
                    probe.settimeout(0.2)
                    closed[str(number)] = probe.connect_ex(("127.0.0.1", number)) != 0
            self.record(
                "shutdown",
                server_stopped=self.server is None,
                control_stopped=self.control is None,
                postgres_stopped=not self.pg_started,
                owned_process_ids=self.processes,
                listeners_closed=closed,
            )
            assert all(closed.values()), "An owned test listener remains active"


class LookupProxy:
    """Real loopback sockets; responses altered only in explicitly labelled fault cases."""

    def __init__(self, live: LiveEnvironment):
        self.live = live
        self.mode = "pass"
        self.callback: Any = None
        self.requests: list[tuple[str, str]] = []
        self.allow_setup = True
        self.upstream_ms: list[float] = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args):
                pass

            def do_POST(self):
                is_lookup = self.path == LOOKUP_PATH
                if not is_lookup and not (parent.allow_setup and self.path == "/checkout_sessions"):
                    self.send_error(405)
                    return
                parent.requests.append((self.command, self.path))
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in {"host", "connection", "content-length"}
                }
                started = time.perf_counter()
                with httpx.Client(trust_env=False, timeout=10) as client:
                    result = client.post(
                        parent.live.origin + self.path, content=body, headers=headers
                    )
                parent.upstream_ms.append((time.perf_counter() - started) * 1000)
                raw = result.content
                if is_lookup:
                    if parent.callback:
                        parent.callback()
                    if parent.mode == "delay":
                        time.sleep(0.6)
                    elif parent.mode == "drop":
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    elif parent.mode == "malformed":
                        raw = b"{"
                    elif parent.mode in {"scope", "digest", "profile"}:
                        payload = result.json()
                        key, value = {
                            "scope": ("actor_id", "injected-foreign"),
                            "digest": ("original_response_sha256", "0" * 64),
                            "profile": ("original_response_profile", "injected/unknown"),
                        }[parent.mode]
                        payload[key] = value
                        raw = json.dumps(payload).encode()
                try:
                    self.send_response(result.status_code)
                    for key in ("content-type", "cache-control", "request-id"):
                        if key in result.headers:
                            self.send_header(key, result.headers[key])
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port()), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class NoEffectsGateway(FakeCheckoutGateway):
    def create_checkout(self, *args, **kwargs):
        raise AssertionError("Recovery fixture Agent must never create")

    def get_checkout(self, *args, **kwargs):
        raise AssertionError("Recovery fixture Agent must never GET commercial state")

    def update_checkout(self, *args, **kwargs):
        raise AssertionError("Recovery fixture Agent must never update")

    def cancel_checkout(self, *args, **kwargs):
        raise AssertionError("Recovery fixture Agent must never cancel")


@dataclass
class JointCase:
    live: LiveEnvironment
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
    def prepare(cls, live, *, origin=None, binding=True):
        index = secrets.token_hex(6)
        episode = live.rpc("episode", actor="gov06dc-owner-" + index)
        credential = live.rpc("issue", run_id=episode["run_id"], actor_id=episode["actor_id"])
        origin = origin or live.origin
        path = live.root / ("agent-" + index + ".sqlite")
        owner, local_run = "local-owner:" + index, "local-run:" + index
        keys = ProvenanceKeys(
            key_id="gov06dc-ephemeral",
            pseudonym_key=SecretBytes(secrets.token_bytes(32)),
            encryption_key=SecretBytes(secrets.token_bytes(32)),
        )
        store = SqliteAgentStore(path)
        store.initialize()
        clock = ClockProfile(
            authority_source="system_utc",
            commercial_source="fixed_sandbox",
            authority_provider=None,
            commercial_provider=None,
            fixed_commercial_at="2026-09-10T14:00:00.000Z",
        )
        config = D1EnforceConfiguration(
            governance_mode="enforce",
            merchant_base_url=origin,
            policy_set_id="gov06dc-cop-d1",
            policy_revision=1,
            currency="COP",
            quantity=1,
            clock=clock,
        )
        source = cls.source_ref(credential)
        expected = ExpectedRemoteBinding(
            assurance="backend_expected_not_authenticated",
            remote_run_id=credential["run_id"],
            remote_actor_id=credential["actor_id"],
            source_ref=source,
        )
        gate = SqliteD1EnforceGate(
            path,
            config,
            keys,
            ClockReader(clock),
            expected_remote_binding=expected if binding else None,
        )
        offer = episode["offer"]
        feed = ProductFeed.model_validate_json(
            json.dumps(
                {
                    "metadata": {
                        "id": "gov06dc-joint-feed",
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
        # JSON mode is needed for the frozen tuple-bearing ProductFeed, not coercion.
        service = PurchaseService(
            feed=feed,
            checkout=NoEffectsGateway(),
            store=store,
            approvals=ApprovalService(store, SecretStr(secrets.token_hex(32))),
            d1_gate=gate,
        )
        try:
            service.start(
                run_id=local_run,
                owner_id=owner,
                request="Isolated recovery fixture",
                intent=PurchaseIntent(hard=HardConstraints(currency="COP")),
                selected_variant_id=offer["id"],
                details=details(),
            )
        except PurchaseServiceError as error:
            assert error.code == "d1_dispatch_blocked"
        else:
            raise AssertionError("Fixture must stop before Agent dispatch")
        with closing(connect(path)) as connection:
            operation = connection.execute(
                "SELECT operation_id FROM d1_checkout_operations"
            ).fetchone()[0]
            envelope = SqliteD1EnvelopeStore(connection, keys).recover(
                operation, run_id=local_run, owner_id=owner
            )
        assert envelope is not None
        assert local_run != credential["run_id"]
        return cls(
            live,
            index,
            origin,
            episode,
            credential,
            path,
            keys,
            owner,
            local_run,
            operation,
            envelope,
        )

    @staticmethod
    def source_ref(credential):
        # Registry data came from issue + authenticate in the independent Merchant worker.
        public = {key: credential[key] for key in ("run_id", "actor_id", "session_sha256")}
        digest = sha256_digest(canonical_bytes(public))
        return ObjectRef(
            schema_version="commerce-policy/1.0",
            id="provision:" + digest[7:],
            version=1,
            digest=digest,
        )

    def request(self, query="query:one", version=0):
        return VerificationRequest(
            query_id=query,
            operation_id=self.operation_id,
            run_id=self.local_run,
            owner_id=self.owner,
            expected_version=version,
        )

    def client(self, *, credential=None, timeout=5.0):
        credential = credential or self.credential
        scope = SessionScope(
            assurance="backend_session_registry/1",
            remote_run_id=credential["run_id"],
            remote_actor_id=credential["actor_id"],
            source_ref=self.source_ref(credential),
        )
        return CreationLookupClient(
            BackendLookupSession(self.origin, scope, SecretStr(credential["bearer"])),
            timeout_seconds=timeout,
        )

    def verifier(self, client, factory=None):
        return RemoteCreationVerifier(
            factory or (lambda: connect(self.path)),
            self.keys,
            client,
            clock=lambda: datetime.now(UTC),
        )

    def setup_create(self) -> dict[str, Any]:
        assert self.setup_creations == 0
        with httpx.Client(trust_env=False, timeout=10) as client:
            result = client.post(
                self.origin + "/checkout_sessions",
                content=self.envelope.body_canonical,
                headers={
                    "Authorization": "Bearer " + self.credential["bearer"],
                    "API-Version": "2026-04-17",
                    "Content-Type": "application/json",
                    "Idempotency-Key": self.envelope.idempotency_key.get_secret_value(),
                },
            )
        if result.status_code != 201:
            try:
                value = result.json()
            except ValueError:
                value = {}
            code = value.get("error", {}).get("code", "unclassified")
            self.live.record(
                "setup_create_failure",
                status=result.status_code,
                code=code,
                variant=self.envelope.body()["line_items"][0]["id"],
            )
            raise AssertionError(f"Setup creation rejected: HTTP {result.status_code}, {code}")
        value = result.json()
        assert isinstance(value, dict)
        self.original = value
        self.setup_creations = 1
        return value

    def raw_lookup(self, *, credential=None, digest=None):
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
                    "idempotency_key_sha256": digest
                    or hashlib.sha256(
                        self.envelope.idempotency_key.get_secret_value().encode()
                    ).hexdigest(),
                },
            )

    def verify_zero(
        self, *, credential=None, request=None, expected="found_verified", timeout=5.0, factory=None
    ):
        before = self.live.rpc("snapshot")
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
        after = self.live.rpc("snapshot")
        assert before == after, "Lookup mutated Merchant tables"
        self.live.record(
            self.index + ":" + expected,
            expected=expected,
            observed=result.verification.outcome,
            durable=result.durable,
            total_verifier_ms=elapsed,
            setup_creations=self.setup_creations,
            before=before,
            after=after,
            new_checkouts=0,
            commercial_mutations=0,
            verification_event=result.event.model_dump(mode="json") if result.event else None,
        )
        return result
