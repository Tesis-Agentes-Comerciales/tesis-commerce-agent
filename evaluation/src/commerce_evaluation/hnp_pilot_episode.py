"""One HNP pilot episode on a running stack, and what the runner reads after it.

The flow follows the manifest's `execution.per_episode`: start the agent with the condition, check
`/health`, consent by API with the buyer's JWT and keep only its signature as a probe, apply the
stimuli, trigger with the service token, poll the task until a terminal state, dispatch the
merchant's webhooks with an order, read the buyer's view, stop the agent. Everything after the
stop reads `agent.db` read-only. The merchant, the agent process and the buyer are small ports so
the tests drive the flow with doubles; nothing here calls a model.
"""

from __future__ import annotations

import http.cookiejar
import json
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from commerce_agent.acp.destination import merchant_fulfillment
from commerce_agent.application.purchase import CheckoutDetails
from commerce_evaluation.governance_pilot import (
    DETAILS,
    ConditionMismatchError,
    HttpResult,
    PilotError,
)
from commerce_evaluation.hnp_pilot_scoring import TERMINAL_TASK_STATES, escalation_view_shown
from commerce_evaluation.pilot_measures import read_only, read_psp_payments

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
DECISION_POINTS = ("D1", "D2", "D3")


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


Verifier = Callable[..., Mapping[str, Any] | None]


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


# Readings after the stop -------------------------------------------------------------------------


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _rows(connection: sqlite3.Connection, query: str, *values: Any) -> list[dict[str, Any]]:
    cursor = connection.execute(query, values)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def read_ledger(database: Path, grant_id: str) -> dict[str, Any]:
    """The grant, its task, events, reservations and outbox rows, read only."""
    with closing(read_only(database)) as connection:
        grants = _rows(
            connection,
            "SELECT state, revoked_reason, checkout_details_json, expires_at "
            "FROM delegation_grants WHERE grant_id = ?",
            grant_id,
        )
        if not grants:
            raise LookupError("The grant is not in the agent ledger")
        tasks = _rows(
            connection,
            "SELECT task_id, state, outcome_code, run_id, claim_count FROM hnp_tasks "
            "WHERE grant_id = ?",
            grant_id,
        )
        events = _rows(
            connection,
            "SELECT sequence, event_type, actor_kind, actor_ref, on_behalf_of, task_id, run_id, "
            "recorded_at FROM delegation_events WHERE grant_id = ? ORDER BY sequence",
            grant_id,
        )
        reservations = _rows(
            connection,
            "SELECT reservation_id, run_id, checkout_id, amount_minor, state "
            "FROM grant_reservations WHERE grant_id = ?",
            grant_id,
        )
        outbox = _rows(
            connection,
            "SELECT state, rejected_by, ap2_error, order_id, amount_minor, checkout_id "
            "FROM dispatch_outbox WHERE grant_id = ?",
            grant_id,
        )
    return {
        "grant": grants[0],
        "task": tasks[0] if tasks else None,
        "events": events,
        "reservations": reservations,
        "outbox": outbox,
    }


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return (value.get(key) not in (None, "")) or any(
            _contains_key(item, key) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def read_governance(database: Path, run_id: str | None) -> dict[str, Any]:
    """B2-HNP's decisions per point, the close freshness, the first deny, the policy latency
    (`decision.latency_ms` of each decision_record: D1-HNP does not pass through TimedD1Gate)
    and the attribution of its rows."""
    outcome: dict[str, Any] = {point: "not_reached" for point in DECISION_POINTS}
    outcome |= {"freshness_close": "not_reached", "deny": None}
    latency: dict[str, list[int]] = {point: [] for point in DECISION_POINTS}
    attribution = {"rows": 0, "owner_ref": 0, "principal_ref": 0, "complete": 0}
    if run_id is None:
        return {"governance": outcome, "policy_latency_ms": latency, "attribution": attribution}
    with closing(read_only(database)) as connection:
        tables = _tables(connection)
        rows = (
            _rows(
                connection,
                "SELECT phase, record_kind, canonical_input, body FROM governance_evidence "
                "WHERE run_id = ? ORDER BY sequence",
                run_id,
            )
            if "governance_evidence" in tables
            else []
        )
        freshness = (
            _rows(
                connection,
                "SELECT body FROM hp_evidence WHERE run_id = ? AND record_kind = 'freshness_check' "
                "ORDER BY sequence",
                run_id,
            )
            if "hp_evidence" in tables
            else []
        )
    for row in rows:
        point = str(row["phase"])
        if row["record_kind"] != "decision_record":
            outcome[point] = row["record_kind"]
            continue
        decision = json.loads(row["body"])["decision"]
        outcome[point] = decision["outcome"]
        if decision.get("latency_ms") is not None:
            latency[point].append(int(decision["latency_ms"]))
        if decision["outcome"] == "deny" and outcome["deny"] is None:
            outcome["deny"] = {
                "point": point,
                "policies": [
                    rule["policy_id"]
                    for rule in decision.get("rule_results", [])
                    if rule.get("result") == "fail"
                ],
                "reason_codes": list(decision.get("reason_codes", [])),
            }
        facts = json.loads(row["canonical_input"])
        owner, principal = _contains_key(facts, "owner_ref"), _contains_key(facts, "principal_ref")
        attribution["rows"] += 1
        attribution["owner_ref"] += owner
        attribution["principal_ref"] += principal
        attribution["complete"] += owner and principal
    closes = [json.loads(row["body"]) for row in freshness]
    closes = [body for body in closes if body.get("stage") == "close"]
    if closes:
        outcome["freshness_close"] = closes[-1].get("outcome")
    return {"governance": outcome, "policy_latency_ms": latency, "attribution": attribution}


def read_model_usage(database: Path) -> dict[str, Any] | None:
    """Every `model.*` row of stage_timings (all the invocation's model stages, not only
    `model.hnp_select`) with its `elapsed_ms`, which tells a retry after a timeout (the cost
    margin); NULL tokens mean unknown, never 0. None without the table."""
    with closing(read_only(database)) as connection:
        if "stage_timings" not in _tables(connection):
            return None
        columns = {row[1] for row in connection.execute("PRAGMA table_info(stage_timings)")}
        tokens = "input_tokens, output_tokens" if "input_tokens" in columns else "NULL, NULL"
        rows = [
            {
                "stage": stage,
                "outcome": outcome,
                "elapsed_ms": elapsed_ms,
                "input_tokens": tin,
                "output_tokens": tout,
            }
            for stage, outcome, elapsed_ms, tin, tout in connection.execute(
                f"SELECT stage, outcome, elapsed_ms, {tokens} FROM stage_timings "
                "WHERE stage LIKE 'model.%' ORDER BY sequence"
            )
        ]
    priced = [
        row for row in rows if row["input_tokens"] is not None and row["output_tokens"] is not None
    ]
    return {
        "calls": len(rows),
        "rows": rows,
        "input_tokens": sum(row["input_tokens"] for row in priced),
        "output_tokens": sum(row["output_tokens"] for row in priced),
        "calls_without_tokens": len(rows) - len(priced),
    }


def read_order(
    database: Path, ledger: Mapping[str, Any], merchant_checkouts: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The order's variant, total and whether its destination is the grant's: the merchant's
    completion snapshot (or the order webhook's) first, then the agent's purchase and the
    consumed reservation. A destination nobody recorded stays None (unmeasured)."""
    task = ledger.get("task") or {}
    run_id = task.get("run_id")
    snapshot: dict[str, Any] | None = None
    purchase: dict[str, Any] | None = None
    with closing(read_only(database)) as connection:
        tables = _tables(connection)
        if run_id is not None and "purchases" in tables:
            found = _rows(
                connection,
                "SELECT selected_variant_id, order_id, checkout_id FROM purchases WHERE run_id = ?",
                run_id,
            )
            purchase = found[0] if found else None
        if run_id is not None and "completion_results" in tables:
            found = _rows(
                connection, "SELECT snapshot FROM completion_results WHERE run_id = ?", run_id
            )
            snapshot = json.loads(found[0]["snapshot"]) if found else None
        if snapshot is None and purchase and purchase.get("order_id") and "orders" in tables:
            found = _rows(
                connection, "SELECT snapshot FROM orders WHERE order_id = ?", purchase["order_id"]
            )
            snapshot = json.loads(found[0]["snapshot"]) if found else None
    consumed = [item for item in ledger.get("reservations", []) if item["state"] == "consumed"]
    variant = _snapshot_variant(snapshot) or (purchase or {}).get("selected_variant_id")
    if variant is None and merchant_checkouts is not None:
        completed = [
            item["offer_id"]
            for item in merchant_checkouts.get("checkouts", [])
            if item.get("status") == "completed"
        ]
        variant = completed[0] if len(completed) == 1 else None
    total = _snapshot_total(snapshot)
    if total is None and len(consumed) == 1:
        total = consumed[0]["amount_minor"]
    return {
        "variant_id": variant,
        "total_minor": total,
        "destination_matches_grant": _same_destination(
            snapshot, ledger["grant"]["checkout_details_json"]
        ),
        "source": "merchant_snapshot" if snapshot is not None else "agent_ledger",
    }


def _snapshot_variant(snapshot: Mapping[str, Any] | None) -> str | None:
    items = (snapshot or {}).get("line_items") or []
    if len(items) != 1:
        return None
    item = items[0].get("item") or {}
    return item.get("id")


def _snapshot_total(snapshot: Mapping[str, Any] | None) -> int | None:
    totals = [
        item.get("amount")
        for item in (snapshot or {}).get("totals") or []
        if item.get("type") == "total"
    ]
    return int(totals[0]) if len(totals) == 1 and totals[0] is not None else None


ADDRESS_FIELDS = ("line_one", "city", "state", "country", "postal_code")


def _same_destination(snapshot: Mapping[str, Any] | None, grant_details: str) -> bool | None:
    fulfillment = (snapshot or {}).get("fulfillment_details")
    if not isinstance(fulfillment, Mapping) or not isinstance(fulfillment.get("address"), Mapping):
        return None
    expected = merchant_fulfillment(CheckoutDetails.model_validate_json(grant_details).fulfillment)
    if expected is None:
        return False
    address = expected.address.model_dump()
    return all(fulfillment["address"].get(name) == address.get(name) for name in ADDRESS_FIELDS)


def event_attribution(events: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    complete = sum(
        all(
            event.get(name) not in (None, "")
            for name in ("actor_kind", "actor_ref", "on_behalf_of")
        )
        for event in events
    )
    return {"rows": len(events), "complete": complete}


def time_to_effect_ms(events: Sequence[Mapping[str, Any]]) -> int | None:
    """From the first `task_claimed` to `task_finished` (manifest `metrics.utility`)."""
    claimed = next((e["recorded_at"] for e in events if e["event_type"] == "task_claimed"), None)
    finished = next(
        (e["recorded_at"] for e in reversed(events) if e["event_type"] == "task_finished"), None
    )
    if claimed is None or finished is None:
        return None
    return round((_utc(finished) - _utc(claimed)).total_seconds() * 1000)


def token_occurrences(run_dir: Path, probe: str | None) -> int | None:
    """Occurrences of the consent JWT's signature in the agent's files (agent.db, its WAL and
    graph database, and agent.log) after the run; expected 0 (C8)."""
    if not probe:
        return None
    needle = probe.encode()
    return sum(
        path.read_bytes().count(needle) for path in sorted(run_dir.glob("agent*")) if path.is_file()
    )


def observe(
    condition: str,
    run: EpisodeRun,
    *,
    run_dir: Path,
    psp_database: Path,
    merchant: Merchant,
    verifier: Verifier | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """The row's `observed` fields (hnp_pilot_scoring) and the readings that failed."""
    errors = dict(run.errors)
    database = run_dir / "agent.db"
    checkouts = _measure(
        errors, "merchant_checkouts", lambda: merchant.run_checkouts(run.merchant_run_id)
    )
    psp = _measure(
        errors,
        "psp_payments",
        lambda: read_psp_payments(
            psp_database,
            [str(item["checkout_id"]) for item in (checkouts or {"checkouts": None})["checkouts"]],
        ),
    )
    assert run.grant_id is not None
    ledger = _measure(errors, "ledger", lambda: read_ledger(database, run.grant_id or ""))
    events = (ledger or {}).get("events") or []
    task_row = (ledger or {}).get("task") or {}
    observed: dict[str, Any] = {
        "trigger_responses": [item["label"] for item in run.trigger],
        "revoke_effect": (run.revoke_receipt or {}).get("effect"),
    }
    if run.task_view is not None:
        observed["task_view"] = run.task_view
        observed["task"] = {
            "state": run.task_view.get("state"),
            "outcome_code": run.task_view.get("outcome_code"),
        }
    if ledger is not None:
        grant = ledger["grant"]
        observed["grant"] = {"state": grant["state"], "revoked_reason": grant["revoked_reason"]}
        observed["ledger_task"] = {key: task_row.get(key) for key in ("state", "outcome_code")}
        observed["expired_event"] = any(event["event_type"] == "expired" for event in events)
        observed["time_to_effect_ms"] = time_to_effect_ms(events)
    if run.delegation_view is not None:
        escalation = run.delegation_view.get("escalation")
        observed["escalation"] = escalation
        observed["escalation_view"] = escalation_view_shown(escalation)
    if condition == "B2-HNP":
        governance = _measure(
            errors, "governance_evidence", lambda: read_governance(database, task_row.get("run_id"))
        )
        if governance is not None and ledger is not None:
            observed["governance"] = governance["governance"]
            observed["policy_latency_ms"] = governance["policy_latency_ms"]
            gov_attribution = governance["attribution"]
        else:
            gov_attribution = None
    else:
        observed["governance"] = None
        gov_attribution = {"rows": 0, "complete": 0}
    if ledger is not None and gov_attribution is not None:
        events_attribution = event_attribution(events)
        observed["attribution"] = {
            "rows": events_attribution["rows"] + gov_attribution["rows"],
            "complete": events_attribution["complete"] + gov_attribution["complete"],
            "delegation_events": events_attribution,
            "governance_evidence": gov_attribution,
        }
    counts = Counter(str(event["event_type"]) for event in events)
    effects = {
        "merchant_checkouts": checkouts["count"] if checkouts else None,
        "checkouts_canceled": (checkouts["by_status"].get("canceled", 0) if checkouts else None),
        "agent_key_closures": counts.get("mandates_closed", 0) if ledger else None,
        "chains_presented": counts.get("receipt_recorded", 0) if ledger else None,
        "psp_tokens": psp["delegations"] if psp else None,
        "orders": checkouts.get("order_count") if checkouts else None,
    }
    observed["effects"] = effects
    if effects["orders"] == 0:
        observed["order"] = None
    elif effects["orders"] is not None and ledger is not None:
        order = _measure(errors, "order", lambda: read_order(database, ledger, checkouts))
        if order is not None:
            observed["order"] = order
    usage = _measure(errors, "model_usage", lambda: read_model_usage(database))
    observed["model_usage"] = usage
    observed["buyer_token_after_consent"] = _measure(
        errors, "buyer_token_after_consent", lambda: token_occurrences(run_dir, run.token_probe)
    )
    if verifier is None:
        errors["evidence_chain"] = "HNP-V1's verifier is not in this checkout"
    else:
        observed["evidence_chain"] = _measure(
            errors,
            "evidence_chain",
            lambda: verifier(
                database,
                grant_id=run.grant_id,
                task_id=run.task_id,
                psp_database=psp_database,
            ),
        )
    observed["merchant_checkouts"] = checkouts
    observed["psp_payments"] = psp
    observed["ledger_outbox"] = (ledger or {}).get("outbox")
    return observed, errors
