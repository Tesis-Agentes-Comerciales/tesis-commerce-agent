"""One HNP pilot episode on a running stack.

The flow follows the manifest's `execution.per_episode`: start the agent with the condition, check
`/health`, consent by API with the buyer's JWT and keep only its signature as a probe, apply the
stimuli, trigger with the service token, poll the task until a terminal state, dispatch the
merchant's webhooks with an order, read the buyer's view, stop the agent. The merchant, the agent
process and the buyer are small ports so the tests drive the flow with doubles; nothing here calls
a model.
"""

from __future__ import annotations

import http.cookiejar
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from commerce_evaluation.governance_pilot import (
    DETAILS,
    ConditionMismatchError,
    HttpResult,
    PilotError,
)
from commerce_evaluation.hnp_pilot_scoring import TERMINAL_TASK_STATES

GOVERNANCE_MODES = {"B0-HNP": "off", "B2-HNP": "enforce"}
MERCHANT_LABELS = {"B0-HNP": "B0", "B2-HNP": "B2"}
HEALTH_HNP_FIELDS = (
    "hnp_enabled",
    "hnp_kill_switch",
    "hnp_active_grants",
    "hnp_running_tasks",
    "hnp_uncertain_tasks",
)
TRIGGER_STATUSES = frozenset({200, 202, 409})


class HarnessError(PilotError):
    """The episode could not be measured; its row records why and is never a result."""


class EpisodeTimeout(HarnessError):
    """The task was not terminal within the manifest's poll timeout (never a result)."""


# HTTP --------------------------------------------------------------------------------------------


def send_json(
    method: str,
    url: str,
    *,
    body: Mapping[str, Any] | None = None,
    bearer: str | None = None,
    timeout_seconds: float = 30,
) -> HttpResult:
    """One JSON request to the agent. Never sends an `Origin` header: the internal routes refuse
    any request with one (spec §5.2)."""
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw, status = response.read(), response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {"unparseable_body": raw.decode(errors="replace")[:500]}
    return HttpResult(status, parsed if isinstance(parsed, dict) else {"value": parsed}, elapsed_ms)


Send = Callable[..., HttpResult]


class AgentApi:
    """The routes an HNP episode uses (spec §5.1, §5.2). Tokens stay in memory."""

    def __init__(
        self, origin: str, *, service_token: str, operator_token: str, send: Send = send_json
    ) -> None:
        self.origin = origin
        self._service = service_token
        self._operator = operator_token
        self._send = send

    def _call(self, method: str, path: str, **options: Any) -> HttpResult:
        return self._send(method, f"{self.origin}{path}", **options)

    def health(self) -> HttpResult:
        return self._call("GET", "/health")

    def propose(self, buyer_token: str, body: Mapping[str, Any]) -> HttpResult:
        return self._call("POST", "/delegations/proposals", body=body, bearer=buyer_token)

    def consent(self, buyer_token: str, body: Mapping[str, Any]) -> HttpResult:
        return self._call("POST", "/delegations", body=body, bearer=buyer_token)

    def delegation(self, buyer_token: str, grant_id: str) -> HttpResult:
        return self._call("GET", f"/delegations/{grant_id}", bearer=buyer_token)

    def revoke(self, buyer_token: str, grant_id: str) -> HttpResult:
        return self._call("POST", f"/delegations/{grant_id}/revoke", bearer=buyer_token)

    def run_task(self, task_id: str) -> HttpResult:
        """HNP-09b's trigger: 202 running, 200 terminal, 409 `hnp_task_not_runnable`."""
        return self._call("POST", f"/internal/hnp/tasks/{task_id}/run", bearer=self._service)

    def task(self, task_id: str) -> HttpResult:
        return self._call("GET", f"/internal/hnp/tasks/{task_id}", bearer=self._service)

    def kill_switch(self, enabled: bool) -> HttpResult:
        return self._call(
            "POST", "/internal/hnp/kill-switch", body={"enabled": enabled}, bearer=self._operator
        )


class BuyerAccount:
    """The invocation's synthetic buyer on the local auth server. Each `session_token` signs in
    again, so the consent JWT, the revoking session and the later view are distinct sessions.
    The password is generated here and never leaves memory; `Origin` goes only to auth."""

    def __init__(self, auth_origin: str) -> None:
        self.origin = auth_origin
        self.email = f"hnp-pilot-{uuid4().hex[:12]}@example.test"
        self._password = f"HnpPilot-{secrets.token_urlsafe(18)}"
        self.user_id: str | None = None

    def _post(self, opener: urllib.request.OpenerDirector, path: str, body: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            f"{self.origin}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Origin": self.origin},
            method="POST",
        )
        with opener.open(request, timeout=30) as response:
            return json.load(response)

    @staticmethod
    def _opener() -> urllib.request.OpenerDirector:
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def signup(self) -> str:
        payload = self._post(
            self._opener(),
            "/api/auth/sign-up/email",
            {"name": "HNP Pilot Buyer", "email": self.email, "password": self._password},
        )
        try:
            self.user_id = str(payload["user"]["id"])
        except (KeyError, TypeError) as error:
            raise PilotError("Auth signup response did not contain a user id") from error
        return self.user_id

    def session_token(self) -> str:
        opener = self._opener()
        self._post(
            opener, "/api/auth/sign-in/email", {"email": self.email, "password": self._password}
        )
        with opener.open(f"{self.origin}/api/auth/token", timeout=30) as response:
            token = json.load(response).get("token")
        if not isinstance(token, str):
            raise PilotError("Auth token response did not contain a token")
        return token


# Ports -------------------------------------------------------------------------------------------


class Merchant(Protocol):
    def seed(self, label: str) -> str: ...
    def export_feed(self, run_id: str, directory: Path) -> None: ...
    def issue_session(self, run_id: str) -> str: ...
    def update_offer(self, run_id: str, variant_id: str, price_minor: int) -> dict[str, Any]: ...
    def run_checkouts(self, run_id: str) -> dict[str, Any]: ...
    def dispatch_webhooks(self) -> dict[str, Any]: ...


class AgentProcess(Protocol):
    def start(self, variables: Mapping[str, str]) -> dict[str, Any]:
        """Start the agent and return its `/health` body."""
        ...

    def stop(self) -> None: ...


class Buyer(Protocol):
    def session_token(self) -> str: ...


def verify_health(condition: str, health: Mapping[str, Any]) -> dict[str, Any]:
    """The condition `/health` reports; another one aborts the invocation (manifest)."""
    reported = {"governance_mode": health.get("governance_mode"), "hp_flow": health.get("hp_flow")}
    reported |= {name: health.get(name) for name in HEALTH_HNP_FIELDS}
    if (
        reported["governance_mode"] != GOVERNANCE_MODES[condition]
        or reported["hnp_enabled"] is not True
        or reported["hnp_kill_switch"] is not False
        or any(reported[name] is None for name in HEALTH_HNP_FIELDS)
    ):
        raise ConditionMismatchError(
            f"Agent /health reports {reported}, but {condition} requires governance_mode="
            f"{GOVERNANCE_MODES[condition]}, hnp_enabled=true and hnp_kill_switch=false"
        )
    return reported


def trigger_label(result: HttpResult) -> str:
    """The oracle's spelling: `202`, `200_terminal` or `409 <code>`."""
    if result.status == 202:
        return "202"
    if result.status == 200:
        return "200_terminal"
    return f"{result.status} {result.body.get('code')}"


def _expect(result: HttpResult, status: int, what: str) -> dict[str, Any]:
    if result.status != status:
        raise HarnessError(f"{what} answered {result.status} {result.body.get('code')}")
    return result.body


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def proposal_body(scenario: Mapping[str, Any]) -> dict[str, Any]:
    delegation = scenario["delegation"]
    body: dict[str, Any] = {
        "variant_ids": list(delegation["variant_ids"]),
        "total_max_minor": delegation["total_max_minor"],
        "currency": delegation["currency"],
        "valid_for_seconds": delegation["valid_for_seconds"],
        "details": DETAILS,
    }
    if delegation.get("preference_text") is not None:
        body["preference_text"] = delegation["preference_text"]
    return body


@dataclass
class EpisodeRun:
    """What happened against the live services; the readings after the stop come apart."""

    merchant_run_id: str
    health: list[dict[str, Any]] = field(default_factory=list)
    grant_id: str | None = None
    task_id: str | None = None
    consented_at: str | None = None
    stimuli: list[dict[str, Any]] = field(default_factory=list)
    revoke_receipt: dict[str, Any] | None = None
    trigger: list[dict[str, Any]] = field(default_factory=list)
    task_view: dict[str, Any] | None = None
    delegation_view: dict[str, Any] | None = None
    webhooks: dict[str, Any] | None = None
    errors: dict[str, str] = field(default_factory=dict)
    # The consent JWT's signature segment only: enough to find the token, useless as one.
    token_probe: str | None = field(default=None, repr=False)


def _measure[T](errors: dict[str, str], name: str, read: Callable[[], T]) -> T | None:
    try:
        return read()
    except Exception as error:
        errors[name] = f"{type(error).__name__}: {error}"
        return None


def run_episode(
    scenario: Mapping[str, Any],
    condition: str,
    *,
    api: AgentApi,
    buyer: Buyer,
    merchant: Merchant,
    agent: AgentProcess,
    variables: Mapping[str, str],
    run_dir: Path,
    poll_timeout_seconds: float,
    poll_interval_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EpisodeRun:
    """Seed, start, consent, stimulate, trigger, poll; the agent is stopped on the way out."""
    run_dir.mkdir(parents=True, exist_ok=True)
    feed = run_dir / "feed"
    run = EpisodeRun(merchant_run_id=merchant.seed(MERCHANT_LABELS[condition]))
    try:
        merchant.export_feed(run.merchant_run_id, feed)
        agent_variables = {
            **variables,
            "AGENT_DATABASE_PATH": str(run_dir / "agent.db"),
            "AGENT_FEED_PATH": str(feed),
            "MERCHANT_BEARER_TOKEN": merchant.issue_session(run.merchant_run_id),
        }
    except Exception as error:
        raise SeededEpisodeError(run, error) from error
    try:
        run.health.append(verify_health(condition, agent.start(agent_variables)))
        _consent(run, scenario, api, buyer)
        for stimulus in scenario.get("stimuli", []):
            _apply(
                run,
                stimulus,
                condition,
                api,
                buyer,
                merchant,
                agent,
                agent_variables,
                feed,
                sleep,
                now,
            )
        assert run.task_id is not None
        results = _trigger(
            api, run.task_id, twice=scenario["trigger"]["mode"] == "twice_concurrently"
        )
        run.trigger = [
            {
                "status": result.status,
                "code": result.body.get("code"),
                "label": trigger_label(result),
            }
            for result in results
        ]
        unexpected = [result for result in results if result.status not in TRIGGER_STATUSES]
        if unexpected:
            raise HarnessError(f"The trigger answered {trigger_label(unexpected[0])}")
        run.task_view = _poll(api, run.task_id, poll_timeout_seconds, poll_interval_seconds, sleep)
        if run.task_view["state"] == "purchased" or run.task_view.get("order_id") is not None:
            # The order's `order_create` reaches this agent only while it is still running.
            run.webhooks = _measure(run.errors, "merchant_webhooks", merchant.dispatch_webhooks)
        run.delegation_view = _measure(
            run.errors, "delegation_view", lambda: _delegation_view(api, buyer, run)
        )
    except ConditionMismatchError:
        raise
    except Exception as error:
        raise SeededEpisodeError(run, error) from error
    finally:
        agent.stop()
    return run


class SeededEpisodeError(HarnessError):
    """A harness error after the merchant seeded the run: its row keeps what happened."""

    def __init__(self, run: EpisodeRun, cause: Exception) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.run = run
        self.cause = cause


def _consent(run: EpisodeRun, scenario: Mapping[str, Any], api: AgentApi, buyer: Buyer) -> None:
    """Consent with a fresh buyer session and drop its JWT: only the signature stays, as the
    probe of `buyer_token_after_consent` (C8)."""
    token = buyer.session_token()
    try:
        proposal = _expect(api.propose(token, proposal_body(scenario)), 201, "The proposal")
        view = _expect(
            api.consent(
                token,
                {"proposal_id": proposal["proposal_id"], "scope_digest": proposal["scope_digest"]},
            ),
            201,
            "The consent",
        )
    finally:
        run.token_probe = token.rsplit(".", 1)[-1]
        del token
    run.grant_id = str(view["grant_id"])
    run.task_id = str(view["task"]["task_id"])
    run.consented_at = str(view["consented_at"])
    run.stimuli.append({"after": "consented", "expires_at": view["expires_at"]})


def _apply(
    run: EpisodeRun,
    stimulus: Mapping[str, Any],
    condition: str,
    api: AgentApi,
    buyer: Buyer,
    merchant: Merchant,
    agent: AgentProcess,
    variables: Mapping[str, str],
    feed: Path,
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
) -> None:
    action = stimulus["action"]
    assert run.grant_id is not None
    record: dict[str, Any] = {"action": action}
    if action == "merchant_update_offer":
        record["offer"] = merchant.update_offer(
            run.merchant_run_id, str(stimulus["variant_id"]), int(stimulus["new_item_price_minor"])
        )
        if stimulus.get("reexport_feed"):
            merchant.export_feed(run.merchant_run_id, feed)
        if stimulus.get("agent_restart") == "same_database_all_conditions":
            # HN03, declared: both conditions restart on the same agent.db to load the new feed.
            agent.stop()
            run.health.append(verify_health(condition, agent.start(variables)))
            record["restarted"] = True
    elif action == "wait_until_grant_expired":
        expires_at = _utc(str(run.stimuli[0]["expires_at"]))
        delay = (expires_at - now()).total_seconds() + float(stimulus["margin_seconds"])
        sleep(max(0.0, delay))
        record["waited_seconds"] = round(max(0.0, delay), 3)
    elif action == "owner_revoke":
        # The human comes back with a new session of the same buyer.
        run.revoke_receipt = _expect(
            api.revoke(buyer.session_token(), run.grant_id), 200, "The revocation"
        )
        record["effect"] = run.revoke_receipt.get("effect")
    elif action == "operator_kill_switch":
        record["state"] = _expect(
            api.kill_switch(bool(stimulus["enabled"])), 200, "The kill switch"
        )
    else:
        raise HarnessError(f"Unsupported stimulus {action}")
    run.stimuli.append(record)


def _trigger(api: AgentApi, task_id: str, *, twice: bool) -> list[HttpResult]:
    if not twice:
        return [api.run_task(task_id)]
    barrier = threading.Barrier(2)
    results: list[HttpResult | BaseException | None] = [None, None]

    def fire(slot: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[slot] = api.run_task(task_id)
        except BaseException as error:  # reported below, in the caller's thread
            results[slot] = error

    threads = [threading.Thread(target=fire, args=(slot,)) for slot in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    answered = [item for item in results if isinstance(item, HttpResult)]
    if len(answered) != 2:
        raise HarnessError("One of the two concurrent triggers did not answer")
    return answered


def _poll(
    api: AgentApi,
    task_id: str,
    timeout_seconds: float,
    interval_seconds: float,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        view = _expect(api.task(task_id), 200, "GET /internal/hnp/tasks")
        if view.get("state") in TERMINAL_TASK_STATES:
            return view
        if time.monotonic() >= deadline:
            raise EpisodeTimeout(
                f"The task was still {view.get('state')} after {timeout_seconds:g} s"
            )
        sleep(interval_seconds)


def _delegation_view(api: AgentApi, buyer: Buyer, run: EpisodeRun) -> dict[str, Any]:
    """The buyer's own view after the end (escalation and its text), from a new session."""
    assert run.grant_id is not None
    return _expect(api.delegation(buyer.session_token(), run.grant_id), 200, "The delegation")
