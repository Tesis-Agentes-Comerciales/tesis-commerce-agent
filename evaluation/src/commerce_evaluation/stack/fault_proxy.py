"""FaultProxy: the loopback HTTP hop every Agent request to the Merchant crosses.

It stays transparent unless a test or a case arms a labelled fault, and answers 405 to any
route outside its list.
"""

import json
import socket
import struct
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from commerce_agent.governance.guarded_creation import CREATE_PATH as GUARDED_CREATE_PATH
from commerce_agent.governance.guarded_creation import LOOKUP_PATH as GUARDED_LOOKUP_PATH
from commerce_agent.governance.remote_creation import LOOKUP_PATH
from commerce_evaluation.stack.environment import wait_until

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


def route_allowed(routes: frozenset[tuple[str, str]], method: str, path: str) -> bool:
    segments = path.split("/")
    return any(
        method == allowed
        and len(template := pattern.split("/")) == len(segments)
        and all(part in {"*", segment} for part, segment in zip(template, segments, strict=True))
        for allowed, pattern in routes
    )


@dataclass
class Call:
    method: str
    path: str
    fault: str
    request_body: bytes = field(repr=False)
    status: int | None = None
    response_body: bytes | None = field(default=None, repr=False)
    response_request_id: str | None = None
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
        call.response_request_id = response.headers.get("Request-Id")
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


def await_call(proxy: FaultProxy, method: str, path: str, timeout: float = 30) -> None:
    """Until the Merchant answered `method path` through the proxy, even if the Agent never saw
    that answer (a dropped, delayed or orphaned response)."""

    def answered() -> None:
        calls = [call for call in proxy.calls if (call.method, call.path) == (method, path)]
        assert calls and calls[-1].answered.is_set()

    wait_until(answered, f"{method} {path}", timeout=timeout)


def on_checkout(proxy: FaultProxy, prepare: Callable[[str], None]) -> None:
    """Once the Merchant answered the run's `POST /checkout_sessions`, `prepare(checkout_id)`
    runs in the proxy, before the Agent sees the answer: paths with the id get their hooks and
    faults before the Agent can call them."""

    def created() -> None:
        [call] = [item for item in proxy.calls if item.path == "/checkout_sessions"]
        assert call.status == 201 and call.response_body is not None, call.status
        prepare(json.loads(call.response_body)["id"])

    proxy.after_answer["/checkout_sessions"] = created
