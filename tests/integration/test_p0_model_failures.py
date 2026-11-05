"""P0 evidence of model failures on the live stack (docs/design/p0-model-failures.md §4 and §5).

Every cell of §4, three times, against the live stack's real Merchant, payment provider and
PostgreSQL, with the real OpenAI SDK over the scripted transport of tests/support/model_server.py:
no model, no request to a provider, no cost. The protocol, frozen before the first run, is
evaluation/scenarios/p0-model-failures.json: cases, stimuli, scripts, oracles and repetitions.
This module reads the oracles from it, records each case's inputs before the Agent runs
(`recorded_case`) and then checks every oracle one by one, so a failed oracle stays failed in the
evidence. Human Present is B2-HP on the `b0` lane through `AgentServer`; Human Not Present is
B2-HNP on the `hnp` lane through `ModelHnpAgent` (tests/integration/live_model.py). When the
module ends it writes `p0-model-failures/summary.json` in the run directory.
"""

import hashlib
import json
import os
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from commerce_agent.api import create_app
from commerce_agent.llm import CommerceDecision
from commerce_agent.llm.model_failures import NO_RETRY_HEADER
from tests.assistant_support import add_message, assistant_body, parse_sse, rebuild_state, sse_body
from tests.delegation.test_hnp_model_failure_kind import read_timeout
from tests.integration.exp01_support import ROOT, recorded_case, sqlite_snapshot
from tests.integration.live_browser import AgentServer, ExplicitSettings
from tests.integration.live_model import MODEL_SETTINGS, ModelHnpAgent
from tests.integration.live_purchase import (
    PAYMENT_REPO,
    PHONE,
    PHONE_INTENT,
    PHONE_REQUEST,
    Episode,
    EpisodeBuyer,
    PurchaseLane,
    buyer_details,
    delegation_request,
)
from tests.integration.live_stack import AGENT, CREATE_PATH, LiveStack, git_state
from tests.integration.test_exp02_commercial import local_effects, observe
from tests.integration.test_live_b2_d2_authorization import settings_for
from tests.integration.test_live_hnp import SHIPPING_ROOM_MINOR, catalog, effects
from tests.support.model_server import (
    Answer,
    ModelServer,
    disconnect,
    failure,
    shortlisted_choice,
    structured,
)

PROTOCOL = ROOT / "evaluation/scenarios/p0-model-failures.json"
PLAN: dict[str, Any] = json.loads(PROTOCOL.read_text(encoding="utf-8"))
CASES: dict[str, dict[str, Any]] = PLAN["cases"]
REPETITIONS = range(1, PLAN["repetitions"] + 1)

ROUTED = CommerceDecision(
    action="purchase",
    assistant_message="Voy a preparar la compra.",
    purchase_request=PHONE_REQUEST,
    intent=PHONE_INTENT,
)
# The stimuli of the protocol, by name.
ANSWERS: dict[str, Answer] = {
    "route_ok": structured(ROUTED),
    "choice_ok": structured(shortlisted_choice),
    "review_confirmed": structured(
        lambda body: {"decision": "confirmed", "reason": "matches_request"}
    ),
    "review_outside_contract": structured(
        lambda body: {"decision": "not_confirmed", "reason": "review_unavailable"}
    ),
    "http_429_rate_limit": failure(429, code="rate_limit_exceeded", error_type="requests"),
    "http_429_insufficient_quota": failure(
        429, code="insufficient_quota", error_type="insufficient_quota"
    ),
    "disconnect": disconnect(),
    "read_timeout": read_timeout(),
}
# The model_requests columns an oracle compares; payloads and hashes stay out of the summary.
REQUEST_FIELDS = ("stage", "outcome", "error_kind", "http_status", "provider_error_code", "retries")
# What an effect leaves in the Agent's own SQLite (`local_effects` plus these).
LEDGER = (
    "approvals",
    "completion_results",
    "grant_reservations",
    "dispatch_outbox",
    "orders",
    "hp_evidence",
    "governance_evidence",
    "ap2_evidence",
    "closed_mandates",
)


class ScriptedModel(ModelServer):
    """`ModelServer` serving a named script of the protocol whose last answer persists: an attempt
    beyond the oracle gets the same stimulus and is counted, instead of breaking the double. Each
    served response keeps its `x-should-retry` as scripted, before the Agent's hooks see it.
    `restart` starts the script again: the protocol runs it per surface (HP) or per task run."""

    def __init__(self, script: list[str]) -> None:
        super().__init__()
        self.script = script
        self.served: list[tuple[str, httpx.Response | None, str | None]] = []
        self.origin = 0

    def restart(self) -> None:
        self.origin = len(self.requests)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        name = self.script[min(len(self.requests) - self.origin, len(self.script)) - 1]
        try:
            response = ANSWERS[name](json.loads(request.content))
        except httpx.TransportError:
            self.served.append((name, None, None))
            raise
        self.served.append((name, response, response.headers.get(NO_RETRY_HEADER)))
        return response

    def since(self, start: int) -> dict[str, Any]:
        """The attempts from `start` on: how many, which answers and each 429's retry header as
        sent and after the Agent's httpx response hooks (`refuse_quota_retry`)."""
        served = self.served[start:]
        return {
            "model_http_attempts": len(served),
            "answers": [name for name, _, _ in served],
            "scripted_429": [
                {
                    "x_should_retry_as_sent": sent,
                    "x_should_retry_after_agent_hooks": response.headers.get(NO_RETRY_HEADER),
                }
                for _, response, sent in served
                if response is not None and response.status_code == 429
            ],
        }


def plan(*cells: str) -> list[Any]:
    assert all(cell in CASES for cell in cells)
    return [
        pytest.param(cell, repetition, id=f"{cell}-r{repetition}")
        for cell in cells
        for repetition in REPETITIONS
    ]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """No OPENAI_* variable of the developer's shell reaches the SDK (a base URL included)."""
    for name in list(os.environ):
        if name.startswith("OPENAI_"):
            monkeypatch.delenv(name)


@pytest.fixture(scope="module")
def p0_cases(stack: LiveStack) -> Iterator[list[dict[str, Any]]]:
    """Every case's result; the summary of the module is written when it ends."""
    cases: list[dict[str, Any]] = []
    yield cases
    summary = summarize(cases, stack)
    directory = stack.root / "p0-model-failures"
    directory.mkdir(exist_ok=True)
    (directory / "summary.json").write_text(
        json.dumps(summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    stack.record("p0:summary", totals=summary["totals"])


@contextmanager
def p0_case(
    cases: list[dict[str, Any]],
    lane: PurchaseLane,
    path: Path,
    cell: str,
    repetition: int,
    inputs: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """`recorded_case` with the protocol of this module; its result also goes to the summary."""
    record: dict[str, Any] | None = None
    frozen = {"cell": cell, "repetition": repetition, "script": CASES[cell]["script"], **inputs}
    try:
        with recorded_case(path, cell, frozen, protocol_path=PROTOCOL) as facts:
            record = facts
            yield facts
    finally:
        if record is not None:
            observations = record["observations"]
            entry = {
                "cell": cell,
                "repetition": repetition,
                "status": record["status"],
                "exception_type": record.get("exception_type"),
                "checks": observations.get("checks", {}),
                "observed": observations.get("summary", {}),
            }
            cases.append(entry)
            lane.stack.record("p0:case", **{k: v for k, v in entry.items() if k != "observed"})


def verdict(record: dict[str, Any], checks: dict[str, bool]) -> None:
    """Every oracle goes to the evidence first; then the case fails with the ones that failed."""
    record["observations"]["checks"] = checks
    failed = [name for name, passed in checks.items() if not passed]
    assert not failed, failed


# Observations -----------------------------------------------------------------------------------


def route(path: str) -> str:
    """A Merchant path without the ids of one run, so repetitions compare."""
    return re.sub(r"/(checkout_sessions|orders)/[^/]+", r"/\1/*", path)


def calls(proxy_calls: list[Any]) -> list[list[Any]]:
    return [[call.method, route(call.path), call.status] for call in proxy_calls]


def outside(lane: PurchaseLane, episode: Episode) -> dict[str, Any]:
    """What the Merchant and the PSP hold, read by themselves, never through the Agent."""
    merchant = observe(lane, episode)
    return {
        "merchant_tables": merchant["tables"],
        "merchant_checkouts": merchant["checkouts"],
        "psp": sqlite_snapshot(lane.root / "payments.sqlite"),
        "psp_tokens": lane.payment_tokens(),
    }


def ledger(path: Path) -> dict[str, int]:
    """Rows an effect leaves in the Agent's SQLite; a table that does not exist counts zero."""
    found = local_effects(path)
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def count(query: str, table: str) -> int:
            return db.execute(query).fetchone()[0] if table in tables else 0

        for name in ("purchases", "orders", "hp_evidence", "governance_evidence", "ap2_evidence"):
            found[name] = count('SELECT count(*) FROM "' + name + '"', name)
        found["closed_mandates"] = count(
            "SELECT count(*) FROM delegation_mandates "
            "WHERE kind IN ('closed_checkout', 'closed_payment')",
            "delegation_mandates",
        )
        found["governed_recovery"] = count(
            "SELECT count(*) FROM hp_evidence WHERE record_kind = 'governed_recovery'",
            "hp_evidence",
        )
    return {name: found.get(name, 0) for name in (*LEDGER, "purchases", "governed_recovery")}


def last_request(path: Path) -> int:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        return db.execute("SELECT coalesce(max(sequence), 0) FROM model_requests").fetchone()[0]


def model_requests(path: Path, after: int) -> list[dict[str, Any]]:
    """The Agent's model_requests rows after `after`; never headers, keys or payloads."""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT run_id, model, input_tokens, request_sha256, latency_ms, "
            + ", ".join(REQUEST_FIELDS)
            + " FROM model_requests WHERE sequence > ? ORDER BY sequence",
            (after,),
        ).fetchall()
    return [dict(row) for row in rows]


def projected(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{name: row[name] for name in REQUEST_FIELDS} for row in rows]


def zero_effects(
    before: dict[str, Any],
    after: dict[str, Any],
    ledger_before: dict[str, int],
    ledger_after: dict[str, int],
    merchant_calls: list[list[Any]],
    psp_calls: list[list[Any]] | None,
) -> tuple[dict[str, bool], dict[str, Any]]:
    """The two halves of «0 efectos» (spec §4): outside the Agent and in its ledger."""
    grown = {name: ledger_after[name] - ledger_before[name] for name in LEDGER}
    facts = {
        "merchant_posts": sum(call[0] == "POST" for call in merchant_calls),
        "merchant_checkouts": [before["merchant_checkouts"], after["merchant_checkouts"]],
        "merchant_snapshot_equal": before["merchant_tables"] == after["merchant_tables"],
        "psp_tokens_delta": after["psp_tokens"] - before["psp_tokens"],
        "psp_snapshot_equal": before["psp"] == after["psp"],
        "ledger_delta": grown,
    }
    checks = {
        "zero_effects.merchant_no_post": facts["merchant_posts"] == 0,
        "zero_effects.merchant_no_checkout": facts["merchant_checkouts"] == [[], []],
        "zero_effects.merchant_snapshot_equal": facts["merchant_snapshot_equal"],
        "zero_effects.psp_no_token": facts["psp_tokens_delta"] == 0,
        "zero_effects.psp_snapshot_equal": facts["psp_snapshot_equal"],
        "zero_effects.ledger_no_new_rows": all(value == 0 for value in grown.values()),
    }
    if psp_calls is not None:
        facts["psp_posts"] = sum(call[0] == "POST" for call in psp_calls)
        checks["zero_effects.psp_no_post"] = facts["psp_posts"] == 0
    return checks, facts


def scripted_429(oracle: dict[str, Any], seen: dict[str, Any]) -> bool:
    """Every scripted 429 went out without `x-should-retry`, as the oracle states, and the
    Agent's hook left the header the oracle expects (`false` for a spent quota)."""
    return bool(seen["scripted_429"]) and all(
        item == oracle["scripted_429"] for item in seen["scripted_429"]
    )


# Human Present ----------------------------------------------------------------------------------


def chat_view(state: dict[str, Any]) -> dict[str, Any]:
    message = state["messages"][-1]
    steps = [part for part in message["content"] if part["type"] == "tool-call"]
    failed = [part for part in steps if part.get("isError")]
    purchase = state.get("purchase")
    return {
        "status": message["status"],
        "steps": [part["toolName"] for part in steps],
        "failed_step": {
            "name": failed[-1]["toolName"],
            "isError": True,
            "result": failed[-1]["result"],
        }
        if failed
        else None,
        "purchase": purchase,
        "purchase_stage": None if purchase is None else purchase.get("stage"),
    }


class HpAgent:
    """The B2-HP Agent of one Merchant episode, in this process with the synthetic model: the
    production `create_app` over `settings_for` (enforce, governed flow, b2-hp-v2, AP2) read by
    `ExplicitSettings`. The episode's buyer authenticates with a token of this run."""

    def __init__(
        self, lane: PurchaseLane, episode: Episode, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root.mkdir(parents=True)
        self.lane = lane
        self.episode = episode
        values: dict[str, Any] = settings_for(lane, episode, root, monkeypatch).model_dump()
        values.update({name.lower(): value for name, value in MODEL_SETTINGS.items()})
        values.update(
            openai_reasoning_effort=None, openai_reasoning_summary=False, openai_timeout_seconds=30
        )
        self.settings = ExplicitSettings(**values)
        condition = (
            self.settings.agent_governance_mode,
            self.settings.agent_hp_flow,
            self.settings.agent_d1_policy_set_id,
        )
        assert condition == ("enforce", "governed", "b2-hp-v2"), condition
        self.database = self.settings.agent_database_path
        token = secrets.token_urlsafe(32)
        self.server = AgentServer(
            create_app(
                settings=self.settings,
                buyer_authenticator=EpisodeBuyer(episode.actor_id, token),
            )
        )
        self.http = httpx.Client(
            trust_env=False, timeout=60, headers={"Authorization": f"Bearer {token}"}
        )
        self.details = buyer_details("p0").model_dump(mode="json", exclude_none=True)

    def start(self) -> None:
        self.server.start()

    def close(self) -> None:
        try:
            self.server.stop()
        finally:
            self.http.close()

    def stopped(self) -> bool:
        return not self.server.thread.is_alive()

    def turn(self, surface: str, model: ScriptedModel, turn_id: str) -> dict[str, Any]:
        """One turn of the phone request through REST or the chat, with what it cost. The
        script starts again on each surface."""
        model.restart()
        attempts, sequence = len(model.served), last_request(self.database)
        start = len(self.lane.proxy.calls)
        if surface == "rest":
            response = self.http.post(
                self.server.origin + "/conversation/turn",
                json={
                    "turn_id": turn_id,
                    "messages": [{"role": "user", "content": PHONE_REQUEST}],
                    "details": self.details,
                },
            )
            body = response.json()
            seen: dict[str, Any] = {"status": response.status_code, "body": body}
        else:
            request = assistant_body(
                [add_message(PHONE_REQUEST)], turn_id=turn_id, details=self.details
            )
            response = self.http.post(self.server.origin + "/assistant", json=request)
            assert response.status_code == 200, response.text
            state = rebuild_state(parse_sse(sse_body(response)), request["state"])
            seen = {"view": chat_view(state)}
        rows = model_requests(self.database, sequence)
        return (
            seen
            | model.since(attempts)
            | {
                "model_requests": projected(rows),
                "model_request_rows": rows,
                "merchant_calls": calls(self.lane.proxy.calls[start:]),
            }
        )


def hp_surface_checks(oracle: dict[str, Any], surface: str, seen: dict[str, Any]) -> dict:
    checks = {
        f"{surface}.model_http_attempts": seen["model_http_attempts"]
        == oracle["model_http_attempts_per_surface"],
        f"{surface}.model_requests": seen["model_requests"] == oracle["model_requests_per_surface"],
    }
    if "scripted_429" in oracle:
        checks[f"{surface}.scripted_429"] = scripted_429(oracle, seen)
    return checks


def hp_summary(seen: dict[str, Any]) -> dict[str, Any]:
    """The deterministic part of a surface: what repetitions must repeat."""
    kept = ("status", "body", "view", "model_http_attempts", "answers", "scripted_429")
    summary = {key: seen[key] for key in kept if key in seen}
    summary["model_requests"] = seen["model_requests"]
    summary["merchant_calls"] = seen["merchant_calls"]
    if isinstance(summary.get("body"), dict) and "purchase" in summary["body"]:
        body = summary["body"]
        summary["body"] = {
            "action": body.get("action"),
            "purchase_stage": (body.get("purchase") or {}).get("stage"),
        }
    if "view" in summary:
        summary["view"] = {k: v for k, v in summary["view"].items() if k != "purchase"}
    return summary


@pytest.mark.parametrize(("cell", "repetition"), plan("INF27-HP", "INF28-HP", "INF29-HP"))
def test_p0_hp_model_failure(b0, tmp_path, monkeypatch, p0_cases, cell, repetition):
    """INF27-HP, INF28-HP, INF29-HP: REST, then the chat, in one B2-HP Agent; no effect."""
    case = CASES[cell]
    oracle = case["oracle"]
    model = ScriptedModel(case["script"])
    model.install(monkeypatch)
    episode = b0.episode(f"p0-{cell.lower()}")
    inputs = {"run_id": episode.run_id, "actor_id": episode.actor_id, "lane": "b0"}
    with p0_case(p0_cases, b0, tmp_path, cell, repetition, inputs) as record:
        facts = record["observations"]
        agent = HpAgent(b0, episode, tmp_path / "agent", monkeypatch)
        try:
            agent.start()
            before, ledger_before = outside(b0, episode), ledger(agent.database)
            surfaces = {
                surface: agent.turn(surface, model, f"p0-{cell}-r{repetition}-{surface}")
                for surface in case["surfaces"]
            }
            after, ledger_after = outside(b0, episode), ledger(agent.database)
            record["stimulus_verified"] = model.since(0)["answers"][:1] == case["script"][:1]
            checks: dict[str, bool] = {}
            for surface, seen in surfaces.items():
                checks |= hp_surface_checks(oracle, surface, seen)
            rest, chat = surfaces["rest"], surfaces["chat"]["view"]
            checks["rest.status_and_body"] = [rest["status"], rest["body"]] == [
                oracle["rest"]["status"],
                oracle["rest"]["body"],
            ]
            for key in ("status", "steps", "failed_step", "purchase"):
                checks[f"chat.{key}"] = chat[key] == oracle["chat"][key]
            checks["governed_recovery_rows"] = (
                ledger_after["governed_recovery"] == oracle["governed_recovery_rows"]
            )
            checks["no_purchase"] = ledger_after["purchases"] == ledger_before["purchases"] == 0
            merchant_calls = [call for seen in surfaces.values() for call in seen["merchant_calls"]]
            effect_checks, effect_facts = zero_effects(
                before, after, ledger_before, ledger_after, merchant_calls, None
            )
            checks |= effect_checks
            facts.update(
                surfaces=surfaces,
                merchant_before=before,
                merchant_after=after,
                ledger_before=ledger_before,
                ledger_after=ledger_after,
                summary={
                    "surfaces": {name: hp_summary(seen) for name, seen in surfaces.items()},
                    "effects": effect_facts,
                    "governed_recovery_rows": ledger_after["governed_recovery"],
                    "purchases": ledger_after["purchases"],
                },
            )
        finally:
            agent.close()
            record["cleanup_verified"] = agent.stopped()
        verdict(record, checks)


# Human Not Present ------------------------------------------------------------------------------


def hnp_run(
    lane: PurchaseLane, agent: ModelHnpAgent, model: ScriptedModel, name: str
) -> dict[str, Any]:
    """Consent, the trigger and the terminal task, with what the run cost and left."""
    assert lane.psp_proxy is not None
    _, price = catalog(agent.episode.feed, PHONE)
    _, view = agent.delegate(delegation_request([PHONE], price + SHIPPING_ROOM_MINOR, name=name))
    grant_id, task_id = view["grant_id"], view["task"]["task_id"]
    before, ledger_before = outside(lane, agent.episode), ledger(agent.database)
    counts_before = effects(lane)
    attempts, sequence = len(model.served), last_request(agent.database)
    start, psp_start = len(lane.proxy.calls), len(lane.psp_proxy.calls)
    triggered = agent.trigger(task_id)
    task = agent.finished(task_id)
    after, ledger_after = outside(lane, agent.episode), ledger(agent.database)
    counts_after = effects(lane)
    events = agent.rows(
        "SELECT event_type, body FROM delegation_events WHERE grant_id = ? ORDER BY sequence",
        grant_id,
    )
    purchases = agent.rows(
        "SELECT stage, checkout_id FROM purchases WHERE run_id = ?", task["run_id"]
    )
    delegation = agent.delegation(grant_id)
    rows = model_requests(agent.database, sequence)
    return model.since(attempts) | {
        "trigger_status": triggered.status_code,
        "task": {"state": task["state"], "outcome_code": task["outcome_code"]},
        "task_finished_failure_kinds": [
            json.loads(body).get("failure_kind") for kind, body in events if kind == "task_finished"
        ],
        "choice_reviewed": [json.loads(body) for kind, body in events if kind == "choice_reviewed"],
        "purchases": [{"stage": stage, "checkout_id": checkout} for stage, checkout in purchases],
        "grant": {"state": delegation["state"], "revoked_reason": delegation["revoked_reason"]},
        "model_requests": projected(rows),
        "model_request_rows": rows,
        "merchant_calls": calls(lane.proxy.calls[start:]),
        "psp_calls": calls(lane.psp_proxy.calls[psp_start:]),
        "effects_delta": {name: counts_after[name] - counts_before[name] for name in counts_after},
        "before": before,
        "after": after,
        "ledger_before": ledger_before,
        "ledger_after": ledger_after,
    }


def hnp_checks(oracle: dict[str, Any], seen: dict[str, Any]) -> dict[str, bool]:
    checks = {
        "trigger_accepted": seen["trigger_status"] == 202,
        "model_http_attempts": seen["model_http_attempts"] == oracle["model_http_attempts"],
        "model_requests": seen["model_requests"] == oracle["model_requests"],
        "task": seen["task"] == oracle["task"],
        "choice_reviewed": seen["choice_reviewed"] == oracle["choice_reviewed"],
    }
    if "task_finished_failure_kind" in oracle:
        checks["task_finished_failure_kind"] = seen["task_finished_failure_kinds"] == [
            oracle["task_finished_failure_kind"]
        ]
    if "task_outcome_never" in oracle:
        checks["task_outcome_never"] = seen["task"]["outcome_code"] != oracle["task_outcome_never"]
    if "no_confirmed_review" in oracle:
        checks["no_confirmed_review"] = not any(
            review.get("decision") == "confirmed" for review in seen["choice_reviewed"]
        )
    if "purchases" in oracle:
        checks["purchases"] = seen["purchases"] == oracle["purchases"]
    if "scripted_429" in oracle:
        checks["scripted_429"] = scripted_429(oracle, seen)
    return checks


def hnp_summary(seen: dict[str, Any]) -> dict[str, Any]:
    kept = (
        "model_http_attempts",
        "answers",
        "scripted_429",
        "trigger_status",
        "task",
        "task_finished_failure_kinds",
        "choice_reviewed",
        "purchases",
        "grant",
        "model_requests",
        "merchant_calls",
        "psp_calls",
        "effects_delta",
    )
    return {key: seen[key] for key in kept}


@pytest.mark.parametrize(
    ("cell", "repetition"),
    plan("INF27-HNP", "INF28-HNP", "INF29-HNP", "INF33-HNP-a", "INF33-HNP-b"),
)
def test_p0_hnp_model_failure(hnp, tmp_path, monkeypatch, p0_cases, cell, repetition):
    """INF27/28/29-HNP (selector) and INF33-HNP-a/b (reviewer) in B2-HNP; no effect."""
    case = CASES[cell]
    oracle = case["oracle"]
    model = ScriptedModel(case["script"])
    model.install(monkeypatch)
    episode = hnp.episode(f"p0-{cell.lower()}")
    inputs = {"run_id": episode.run_id, "actor_id": episode.actor_id, "lane": "hnp"}
    with p0_case(p0_cases, hnp, tmp_path, cell, repetition, inputs) as record:
        facts = record["observations"]
        agent = ModelHnpAgent(hnp, episode, case["condition"], reviewer=case["reviewer"])
        try:
            agent.start()
            seen = hnp_run(hnp, agent, model, f"p0-{cell.lower()}")
            record["stimulus_verified"] = seen["answers"][:1] == case["script"][:1]
            checks = hnp_checks(oracle, seen)
            effect_checks, effect_facts = zero_effects(
                seen["before"],
                seen["after"],
                seen["ledger_before"],
                seen["ledger_after"],
                seen["merchant_calls"],
                seen["psp_calls"],
            )
            checks |= effect_checks
            facts.update(run=seen, summary=hnp_summary(seen) | {"effects": effect_facts})
        finally:
            agent.close()
            record["cleanup_verified"] = not agent.local.thread.is_alive()
        verdict(record, checks)


# Controls ---------------------------------------------------------------------------------------
# Pytest runs the tests in the order they are defined; the protocol's execution_order puts every
# zero-effect cell (above) before the controls that prepare checkouts or buy (below).


@pytest.mark.parametrize(("cell", "repetition"), plan("INF27-HP-control", "CTRL-HP"))
def test_p0_hp_control(b0, tmp_path, monkeypatch, p0_cases, cell, repetition):
    """INF27-HP-control and CTRL-HP: each surface in its own Agent prepares one checkout."""
    case = CASES[cell]
    oracle = case["oracle"]
    episodes = {surface: b0.episode(f"p0-{cell.lower()}-{surface}") for surface in case["surfaces"]}
    inputs = {
        "episodes": {
            surface: {"run_id": item.run_id, "actor_id": item.actor_id}
            for surface, item in episodes.items()
        },
        "lane": "b0",
    }
    with p0_case(p0_cases, b0, tmp_path, cell, repetition, inputs) as record:
        facts = record["observations"]
        checks: dict[str, bool] = {}
        surfaces: dict[str, Any] = {}
        summary: dict[str, Any] = {}
        cleaned: list[bool] = []
        for surface, episode in episodes.items():
            model = ScriptedModel(case["script"])
            model.install(monkeypatch)
            agent = HpAgent(b0, episode, tmp_path / surface, monkeypatch)
            try:
                agent.start()
                before = outside(b0, episode)
                seen = agent.turn(surface, model, f"p0-{cell}-r{repetition}-{surface}")
                after = outside(b0, episode)
            finally:
                agent.close()
                cleaned.append(agent.stopped() and cancel_checkouts(b0, episode))
            checks |= hp_surface_checks(oracle, surface, seen)
            if surface == "rest":
                body = seen["body"]
                checks["rest.purchase"] = [
                    seen["status"],
                    body.get("action"),
                    (body.get("purchase") or {}).get("stage"),
                ] == [
                    oracle["rest"]["status"],
                    oracle["rest"]["action"],
                    oracle["rest"]["purchase_stage"],
                ]
            else:
                view = seen["view"]
                checks["chat.purchase"] = [view["status"], view["purchase_stage"]] == [
                    oracle["chat"]["status"],
                    oracle["chat"]["purchase_stage"],
                ]
            posts = [call for call in seen["merchant_calls"] if call[:2] == ["POST", CREATE_PATH]]
            checks[f"{surface}.checkout_posts"] = len(posts) == oracle[
                "checkout_posts_per_surface"
            ] and all(call[2] == oracle["checkout_post_status"] for call in posts)
            tables = ("orders", "checkout_completion_attempts")
            checks[f"{surface}.no_payment"] = (
                all(
                    before["merchant_tables"][name] == after["merchant_tables"][name]
                    for name in tables
                )
                and before["psp"] == after["psp"]
                and before["psp_tokens"] == after["psp_tokens"]
                and not any("complete" in call[1] for call in seen["merchant_calls"])
            )
            surfaces[surface] = seen | {"merchant_before": before, "merchant_after": after}
            summary[surface] = hp_summary(seen)
        record["stimulus_verified"] = True
        facts.update(surfaces=surfaces, summary={"surfaces": summary})
        record["cleanup_verified"] = all(cleaned)
        verdict(record, checks)


def cancel_checkouts(lane: PurchaseLane, episode: Episode) -> bool:
    """The control's checkout canceled at the Merchant, as exp02's cleanup does; True once every
    checkout of the episode is canceled."""
    for checkout_id, status, *_ in observe(lane, episode)["checkouts"]:
        if status != "canceled":
            lane.rpc(
                "cancel_fixture",
                bearer=episode.bearer.get_secret_value(),
                checkout_id=checkout_id,
                key="p0-cleanup-" + checkout_id,
            )
    return all(row[1] == "canceled" for row in observe(lane, episode)["checkouts"])


@pytest.mark.parametrize(("cell", "repetition"), plan("CTRL-HNP", "CTRL-HNP-REVIEW"))
def test_p0_hnp_control(hnp, tmp_path, monkeypatch, p0_cases, cell, repetition):
    """CTRL-HNP and CTRL-HNP-REVIEW: a valid answer prepares the checkout and the run buys."""
    case = CASES[cell]
    oracle = case["oracle"]
    model = ScriptedModel(case["script"])
    model.install(monkeypatch)
    episode = hnp.episode(f"p0-{cell.lower()}")
    inputs = {"run_id": episode.run_id, "actor_id": episode.actor_id, "lane": "hnp"}
    with p0_case(p0_cases, hnp, tmp_path, cell, repetition, inputs) as record:
        facts = record["observations"]
        agent = ModelHnpAgent(hnp, episode, case["condition"], reviewer=case["reviewer"])
        try:
            agent.start()
            seen = hnp_run(hnp, agent, model, f"p0-{cell.lower()}")
            record["stimulus_verified"] = seen["answers"][:1] == case["script"][:1]
            checks = hnp_checks(oracle, seen)
            posts = [call for call in seen["merchant_calls"] if call[:2] == ["POST", CREATE_PATH]]
            checks["checkout_posts"] = len(posts) == oracle["checkout_posts"] and all(
                call[2] == oracle["checkout_post_status"] for call in posts
            )
            checks["orders_created"] = seen["effects_delta"]["orders"] == oracle["orders_created"]
            checks["psp_tokens_created"] = (
                seen["effects_delta"]["tokens"] == oracle["psp_tokens_created"]
            )
            facts.update(run=seen, summary=hnp_summary(seen))
        finally:
            agent.close()
            record["cleanup_verified"] = not agent.local.thread.is_alive()
        verdict(record, checks)


# Summary ----------------------------------------------------------------------------------------


def summarize(cases: list[dict[str, Any]], stack: LiveStack) -> dict[str, Any]:
    """Per cell: repetitions passed, each oracle over the repetitions, whether the observations
    repeat exactly, and those of the first repetition. Counts and codes only."""
    payment = Path(os.environ[PAYMENT_REPO]).expanduser().resolve()
    checkouts = {
        "agent": git_state(AGENT),
        "merchant": git_state(stack.merchant),
        "payment": git_state(payment),
    }
    spec = ROOT / PLAN["spec"]["path"]
    cells: dict[str, Any] = {}
    for cell in PLAN["execution_order"]:
        runs = sorted((c for c in cases if c["cell"] == cell), key=lambda c: c["repetition"])
        names = list(dict.fromkeys(name for run in runs for name in run["checks"]))
        cells[cell] = {
            "matrix_id": CASES[cell]["matrix_id"],
            "mode": CASES[cell]["mode"],
            "condition": CASES[cell]["condition"],
            "repetitions_passed": (
                f"{sum(run['status'] == 'passed' for run in runs)}/{PLAN['repetitions']}"
            ),
            "oracles": {
                name: f"{sum(run['checks'].get(name) is True for run in runs)}/{len(runs)}"
                for name in names
            },
            "observed_identical_across_repetitions": (
                all(run["observed"] == runs[0]["observed"] for run in runs) if runs else None
            ),
            "runs": [
                {
                    "repetition": run["repetition"],
                    "status": run["status"],
                    "failed_oracles": [name for name, ok in run["checks"].items() if not ok],
                    "exception_type": run["exception_type"],
                }
                for run in runs
            ],
            "observed_first_repetition": runs[0]["observed"] if runs else None,
        }
    statuses = [case["status"] for case in cases]
    order = list(dict.fromkeys(case["cell"] for case in cases))
    return {
        "schema": "p0-model-failures-summary/1",
        "protocol": {"path": str(PROTOCOL.relative_to(ROOT)), "sha256": sha256(PROTOCOL)},
        "spec": {
            "path": PLAN["spec"]["path"],
            "sha256": sha256(spec),
            "matches_protocol": sha256(spec) == PLAN["spec"]["sha256"],
        },
        "execution_order": {
            "observed": order,
            "matches_protocol": order == PLAN["execution_order"],
        },
        "checkouts": checkouts,
        "checkouts_match_protocol": {
            "merchant": checkouts["merchant"]["head"] == PLAN["merchant_sha"],
            "payment": checkouts["payment"]["head"] == PLAN["payment_sha"],
        },
        "model": {
            "double": PLAN["harness"]["model_double"],
            "model": PLAN["harness"]["model"],
            "real_provider_calls": 0,
            "cost_usd": "0",
        },
        "totals": {
            "cases": len(cases),
            "passed": statuses.count("passed"),
            "failed": statuses.count("failed"),
            "error": statuses.count("error"),
        },
        "not_applicable": PLAN["not_applicable"],
        "cells": cells,
    }
