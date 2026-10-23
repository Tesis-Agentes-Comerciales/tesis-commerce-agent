"""The HP pilot's buyer waits out auth's rate limit and its row says so (governance_pilot).

Better Auth limits each route to 100 requests in 60 s (auth/auth.js) and answers 429 with
`X-Retry-After`. The auth double serves on loopback and the waits go to an injected `sleep`: no
test waits, starts a service or calls a model.
"""

import io
import json
import threading
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from commerce_evaluation.governance_pilot import (
    AUTH_ATTEMPTS,
    AUTH_WINDOW_SECONDS,
    CONDITIONS,
    AuthClient,
    AuthRateLimitedError,
    PilotError,
    PlannedRun,
    run_plan,
)
from tests.evaluation.test_pilot_evidence import (
    MERCHANT_RUN,
    checkout_report,
    execute_s04,
    s04,
)
from tests.evaluation.test_pilot_followups import agent_writes_evidence

B2 = CONDITIONS["B2"]
MATRIX = {"matrix_id": "governance-pilot-v1", "baseline_id": "B0-2026-10-06"}
# What Better Auth 1.7.5 answers past its limit: the seconds until its window resets.
LIMITED = (429, {"X-Retry-After": "7"})
# The harness error once every attempt was refused with LIMITED.
GAVE_UP = (
    f"Auth answered 429 to /api/auth/token on all {AUTH_ATTEMPTS} attempts "
    f"(waited {', '.join(['7'] * (AUTH_ATTEMPTS - 1))} s)"
)


class RateLimitedAuth(BaseHTTPRequestHandler):
    """Auth's sign-up and token routes. Each request first takes the next scripted refusal, if
    any is left; `requests` keeps the path of every request auth received."""

    refusals: ClassVar[list[tuple[int, dict[str, str]]]] = []
    requests: ClassVar[list[str]] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, status: int, body: object, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _refused(self) -> bool:
        type(self).requests.append(self.path)
        if not type(self).refusals:
            return False
        status, headers = type(self).refusals.pop(0)
        self._send(status, {"message": "Refused."}, headers)
        return True

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        if not self._refused():
            self._send(200, {"user": {"id": "buyer-1"}}, {"Set-Cookie": "session=s1; Path=/"})

    def do_GET(self) -> None:
        if not self._refused():
            signed_in = "session=s1" in (self.headers.get("Cookie") or "")
            self._send(200 if signed_in else 401, {"token": "buyer.jwt" if signed_in else None})


@pytest.fixture
def auth_server() -> Any:
    RateLimitedAuth.refusals, RateLimitedAuth.requests = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), RateLimitedAuth)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def auth_against(server: ThreadingHTTPServer) -> tuple[AuthClient, list[float]]:
    waits: list[float] = []
    return AuthClient(f"http://127.0.0.1:{server.server_address[1]}", sleep=waits.append), waits


def signed_up(
    server: ThreadingHTTPServer, *refusals: tuple[int, dict[str, str]]
) -> tuple[AuthClient, list[float]]:
    """The invocation's buyer, signed up; auth then refuses its next requests with `refusals`."""
    auth, waits = auth_against(server)
    auth.signup()
    RateLimitedAuth.refusals, RateLimitedAuth.requests = list(refusals), []
    return auth, waits


def test_the_runner_waits_out_auth_s_rate_limit_and_gets_its_token(auth_server: Any) -> None:
    auth, waits = signed_up(auth_server, LIMITED, LIMITED)

    assert auth.token() == "buyer.jwt"
    assert waits == [7.0, 7.0]
    assert RateLimitedAuth.requests == ["/api/auth/token"] * 3
    assert [(retry["path"], retry["attempt"], retry["wait_seconds"]) for retry in auth.retries] == [
        ("/api/auth/token", 1, 7.0),
        ("/api/auth/token", 2, 7.0),
    ]
    assert all(datetime.fromisoformat(retry["refused_at"]).tzinfo for retry in auth.retries)


def test_the_sign_up_waits_out_the_rate_limit_too(
    auth_server: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    auth, waits = auth_against(auth_server)
    RateLimitedAuth.refusals = [LIMITED]

    assert auth.signup() == "buyer-1"
    assert waits == [7.0]
    assert [retry["path"] for retry in auth.retries] == ["/api/auth/sign-up/email"]
    # The invocation's log says why the run paused.
    assert "Auth answered 429 to /api/auth/sign-up/email; retrying in 7 s" in (
        capsys.readouterr().err
    )


@pytest.mark.parametrize(
    ("headers", "wait"),
    [
        ({"Retry-After": "3"}, 3.0),
        ({"X-Retry-After": "600"}, AUTH_WINDOW_SECONDS),
        ({}, AUTH_WINDOW_SECONDS),
        ({"Retry-After": "Fri, 23 Oct 2026 15:00:00 GMT"}, AUTH_WINDOW_SECONDS),
        ({"X-Retry-After": "-5"}, AUTH_WINDOW_SECONDS),
    ],
)
def test_the_wait_follows_retry_after_and_never_exceeds_auth_s_window(
    auth_server: Any, headers: dict[str, str], wait: float
) -> None:
    auth, waits = signed_up(auth_server, (429, headers))

    assert auth.token() == "buyer.jwt"
    assert waits == [wait]


def test_the_runner_gives_up_after_its_attempts_and_says_why(auth_server: Any) -> None:
    auth, waits = signed_up(auth_server, *[LIMITED] * AUTH_ATTEMPTS)

    with pytest.raises(AuthRateLimitedError) as refused:
        auth.token()

    assert isinstance(refused.value, PilotError)
    assert str(refused.value) == GAVE_UP
    assert len(waits) == len(auth.retries) == AUTH_ATTEMPTS - 1
    assert RateLimitedAuth.requests == ["/api/auth/token"] * AUTH_ATTEMPTS


@pytest.mark.parametrize("status", [400, 401, 403, 500, 502, 503])
def test_any_other_auth_refusal_is_not_retried(auth_server: Any, status: int) -> None:
    auth, waits = signed_up(auth_server, (status, {"Retry-After": "1"}))

    with pytest.raises(urllib.error.HTTPError) as refused:
        auth.token()

    assert refused.value.code == status
    assert waits == []
    assert auth.retries == []
    assert RateLimitedAuth.requests == ["/api/auth/token"]


def test_the_row_records_only_the_auth_retries_of_its_repetition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth_server: Any
) -> None:
    auth, _ = auth_against(auth_server)
    RateLimitedAuth.refusals = [LIMITED]
    auth.signup()  # The invocation's sign-up: its retry belongs to no row.
    RateLimitedAuth.refusals = [LIMITED, LIMITED]

    record = execute_s04(tmp_path, monkeypatch, agent_writes_evidence, checkout_report(), auth=auth)

    assert [(retry["path"], retry["attempt"]) for retry in record["auth_retries"]] == [
        ("/api/auth/token", 1),
        ("/api/auth/token", 2),
    ]
    assert record["metrics"]["valid_task_success"] == 1


def test_a_repetition_without_a_refusal_records_no_auth_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = execute_s04(tmp_path, monkeypatch, agent_writes_evidence, checkout_report())

    assert record["auth_retries"] == []


def test_a_repetition_whose_auth_stays_rate_limited_is_a_harness_error_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth_server: Any
) -> None:
    auth, waits = signed_up(auth_server, *[LIMITED] * AUTH_ATTEMPTS)

    [failed] = run_plan(
        [PlannedRun(s04(), 1, B2)],
        lambda _: execute_s04(
            tmp_path, monkeypatch, agent_writes_evidence, checkout_report(), auth=auth
        ),
        matrix=MATRIX,
        run_metadata={},
        result_file=io.StringIO(),
    )

    assert failed["harness_error"] == f"AuthRateLimitedError: {GAVE_UP}"
    assert failed["merchant_run_id"] == MERCHANT_RUN
    assert len(waits) == AUTH_ATTEMPTS - 1
