"""The evaluation buyer on the local auth server, as the live suites sign it up.

Better Auth allows 100 requests per route in this window (auth/auth.js) and past it answers 429
with `X-Retry-After`, the seconds until the window resets. The buyer waits that long, never more
than the window (the whole window without a readable value), and tries again, at most this many
times. Any other status is never retried.
"""

import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import Message
from typing import Any
from uuid import uuid4

AUTH_WINDOW_SECONDS = 60.0
AUTH_ATTEMPTS = 3


class AuthError(RuntimeError):
    """Auth answered without the user or the token the buyer needs."""


class AuthRateLimitedError(AuthError):
    """Auth still answered 429 on the last attempt: the run is a harness error."""


def _retry_after_seconds(headers: Message) -> float:
    """Better Auth's `X-Retry-After`, else the standard `Retry-After`, in seconds."""
    value = headers.get("X-Retry-After") or headers.get("Retry-After")
    try:
        seconds = float(value) if value is not None else -1.0
    except ValueError:
        seconds = -1.0
    return min(seconds, AUTH_WINDOW_SECONDS) if seconds >= 0 else AUTH_WINDOW_SECONDS


class AuthClient:
    """The invocation's buyer on the local auth server. A 429 from auth's rate limiter is waited
    out (`AUTH_ATTEMPTS`): `retries` keeps each wait."""

    def __init__(self, origin: str, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.origin = origin
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self._sleep = sleep
        self.retries: list[dict[str, Any]] = []

    def _open(self, request: urllib.request.Request) -> Any:
        waits: list[float] = []
        while True:
            try:
                with self.opener.open(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code != 429:
                    raise
                if len(waits) + 1 == AUTH_ATTEMPTS:
                    raise AuthRateLimitedError(
                        f"Auth answered 429 to {request.selector} on all {AUTH_ATTEMPTS} attempts "
                        f"(waited {', '.join(f'{wait:g}' for wait in waits)} s)"
                    ) from error
                waits.append(_retry_after_seconds(error.headers))
                self.retries.append(
                    {
                        "path": request.selector,
                        "attempt": len(waits),
                        "wait_seconds": waits[-1],
                        "refused_at": datetime.now(UTC).isoformat(),
                    }
                )
                print(
                    f"Auth answered 429 to {request.selector}; retrying in {waits[-1]:g} s "
                    f"(attempt {len(waits) + 1} of {AUTH_ATTEMPTS})",
                    file=sys.stderr,
                    flush=True,
                )
                self._sleep(waits[-1])

    def signup(self) -> str:
        request = urllib.request.Request(
            f"{self.origin}/api/auth/sign-up/email",
            data=json.dumps(
                {
                    "name": "Governance Pilot Buyer",
                    "email": f"governance-pilot-{uuid4().hex[:12]}@example.test",
                    "password": "GovernancePilot!2026-Temporary",
                }
            ).encode(),
            headers={"Content-Type": "application/json", "Origin": self.origin},
            method="POST",
        )
        payload = self._open(request)
        try:
            return str(payload["user"]["id"])
        except (KeyError, TypeError) as error:
            raise AuthError("Auth signup response did not contain a user id") from error

    def token(self) -> str:
        payload = self._open(urllib.request.Request(f"{self.origin}/api/auth/token", method="GET"))
        token = payload.get("token")
        if not isinstance(token, str):
            raise AuthError("Auth token response did not contain a token")
        return token
