"""One episode of the second P0 real-model batch on the live stack (docs/design/p0-llm-batch2.md).

`run_slot` runs one slot of the schedule: a fresh Merchant episode and its exported feed (CT21
mounted feed-side), a fresh Agent of the slot's condition in this process, the cell's turn or task,
and what it left at the Merchant, the PSP and the Agent's own ledger, then the cleanup of any open
checkout. The executor (commerce_evaluation/p0_llm_batch2_execution.py) runs it with the real model
behind batch 1's cost guard; the scripted controls (test_p0_llm_batch2_controls.py) run the same
code with the scripted transport of tests/support/model_server.py.

The model key goes only to the Agent's `ExplicitSettings`: never to an environment variable, a
file, a log or the evidence. Nothing here reads a developer .env.
"""

import json
import secrets
import shutil
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from commerce_agent.api import create_app
from commerce_agent.config import Settings
from commerce_agent.trace.assembler import PurchaseTraces
from commerce_agent.trace.models import PolicyDecisionStep
from commerce_evaluation.agent_condition import D1RunKeys
from commerce_evaluation.demo_stack import MODES, mode_environment
from commerce_evaluation.p0_llm_batch2 import (
    PreflightError,
    Slot,
    cells,
    clarify_source,
    exposure,
    faithful_intent,
    list_unchanged,
    mount_injection,
    preflight,
    protocol,
    review_of,
    routed_intent,
    selected_variant,
)
from tests.assistant_support import add_message, assistant_body, parse_sse, rebuild_state, sse_body
from tests.integration.live_browser import ExplicitSettings
from tests.integration.live_model import ModelHnpAgent
from tests.integration.live_purchase import (
    APPROVED_AT,
    Episode,
    EpisodeBuyer,
    HnpAgent,
    PurchaseLane,
    buyer_details,
    delegation_request,
)
from tests.integration.live_stack import CREATE_PATH
from tests.support.agent_server import AgentServer

PLAN = protocol()
CT21 = PLAN["ct21"]
CONTEXT = PLAN["context"]["model_visible_message"]
# The B0' budget guard cancels its checkout through the lane's proxy.
CANCEL_ROUTE = ("POST", "/checkout_sessions/*/cancel")
BUYER = "lote2"
# A turn can take every attempt of its cell: up to 6 requests of up to 30 s, plus the Merchant.
TURN_TIMEOUT = 420
HP_CONDITIONS = {"B2": ("enforce", "governed"), "B0prime": ("off", "blocked")}
# Kept out of the evidence: the vocabulary is the full catalog again, on every route row.
UNRECORDED = ("catalog_vocabulary",)


@dataclass(frozen=True)
class ModelAccess:
    """The model the Agent of every episode uses; the key is never shown."""

    api_key: SecretStr = field(repr=False)
    model: str


def model_settings(access: ModelAccess) -> dict[str, Any]:
    """conditions.model_settings: minimal effort, no reasoning summary (no stream), 30 s."""
    return {
        "openai_api_key": access.api_key,
        "openai_model": access.model,
        "openai_reasoning_effort": "minimal",
        "openai_reasoning_summary": False,
        "openai_timeout_seconds": 30,
    }


def history(cell: Mapping[str, Any]) -> list[str]:
    """The context message, then the cell's messages, all from the user (spec §3)."""
    return [CONTEXT, *cell["messages"]]


# The Agent's ledger


def last_sequence(database: Path) -> int:
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        return db.execute("SELECT coalesce(max(sequence), 0) FROM model_requests").fetchone()[0]


def model_requests(database: Path, after: int = 0) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM model_requests WHERE sequence > ? ORDER BY sequence", (after,)
        ).fetchall()
    return [
        {name: value for name, value in dict(row).items() if name not in UNRECORDED} for row in rows
    ]


def approvals(database: Path) -> int:
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return (
            db.execute("SELECT count(*) FROM approvals").fetchone()[0]
            if "approvals" in tables
            else 0
        )


def pol11_denied(settings: Settings, actor_id: str) -> bool:
    """D1 denied the run with POL-11 failed: the governed stop of CT21-HP in B2 (spec §6)."""
    traces = PurchaseTraces(settings)
    history_ = traces.history(actor_id, limit=5, before=None)
    for purchase in history_.purchases if history_ is not None else ():
        for step in traces.trace(purchase.run_id).steps:
            if (
                isinstance(step, PolicyDecisionStep)
                and step.decision.outcome == "deny"
                and ("POL-11", "fail") in [(rule.id, rule.result) for rule in step.decision.rules]
            ):
                return True
    return False


# The Merchant and the PSP


def merchant(lane: PurchaseLane, episode: Episode) -> dict[str, Any]:
    return lane.rpc("p0_llm_batch2_catalog", run_id=episode.run_id)


def new_effects(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """The checkouts and orders the window created, with the Merchant's total of their offer."""
    totals = after["totals"]

    def lines(kind: str, key: str) -> list[dict[str, Any]]:
        known = {item[key] for item in before[kind]}
        return [
            {
                "variant_id": item["offer_id"],
                "total_minor": totals[item["offer_id"]] * item["quantity"],
                "quantity": item["quantity"],
                **({"status": item["status"]} if "status" in item else {}),
                key: item[key],
            }
            for item in after[kind]
            if item[key] not in known
        ]

    return {"checkouts": lines("checkouts", "checkout_id"), "orders": lines("orders", "order_id")}


def checkout_posts(lane: PurchaseLane, start: int) -> int:
    return sum(
        (call.method, call.path) == ("POST", CREATE_PATH) for call in lane.proxy.calls[start:]
    )


def cancel_open(lane: PurchaseLane, episode: Episode) -> bool:
    """Every open checkout of the episode canceled at the Merchant after it was measured."""
    for item in merchant(lane, episode)["checkouts"]:
        if item["status"] not in {"canceled", "completed"}:
            lane.rpc(
                "cancel_fixture",
                bearer=episode.bearer.get_secret_value(),
                checkout_id=item["checkout_id"],
                key="p0-llm2-cleanup-" + item["checkout_id"],
            )
    return all(
        item["status"] in {"canceled", "completed"} for item in merchant(lane, episode)["checkouts"]
    )


# The feed


def prepare_feed(lane: PurchaseLane, episode: Episode, root: Path, *, ct21: bool) -> dict[str, Any]:
    """C-PREFLIGHT on this episode's export, and the CT21 mount. Before any Agent exists."""
    catalog = merchant(lane, episode)
    plain = root / "plain-feed"
    shutil.copytree(episode.feed, plain)
    facts: dict[str, Any] = {
        "plain_feed": plain,
        "unmounted": preflight(plain, catalog, mounted=False),
    }
    if ct21:
        facts["mount"] = mount_injection(episode.feed)
        facts["mounted"] = preflight(episode.feed, catalog, mounted=True)
    return facts


# The Agents


def hp_settings(
    lane: PurchaseLane, episode: Episode, root: Path, condition: str, access: ModelAccess
) -> Settings:
    """`settings_for` of test_live_b2_d2_authorization.py, without the process environment:
    B2 is `MODES["b2"]` (enforce, governed, b2-hp-v2) and B0' is `MODES["b0"]` (off, blocked)."""
    mode = MODES["b2"] if condition == "B2" else MODES["b0"]
    variables = mode_environment(mode, lane.origin, D1RunKeys.generate())
    values: dict[str, Any] = {name.lower(): value for name, value in variables.items()}
    values.update(
        merchant_base_url=lane.origin,
        merchant_bearer_token=episode.bearer,
        merchant_webhook_secret=lane.webhook_secret,
        agent_approval_secret=lane.approval_secret,
        payment_provider_url=lane.payment.origin,
        payment_agent_bearer_token=lane.payment_token,
        agent_database_path=root / "agent.sqlite",
        agent_feed_source="file",
        agent_feed_path=episode.feed,
        agent_sandbox_clock_at=APPROVED_AT,
        agent_ap2_required=True,
        agent_ap2_private_key_pem=lane.agent_key,
        # build_runtime never authenticates buyers; jwt only satisfies the AP2 settings rule.
        agent_auth_mode="jwt",
        agent_auth_issuer="http://127.0.0.1:9",
    )
    settings = ExplicitSettings(**(values | model_settings(access)))
    if (settings.agent_governance_mode, settings.agent_hp_flow) != HP_CONDITIONS[condition]:
        raise AssertionError(f"not the {condition} condition")
    if condition == "B2" and settings.agent_d1_policy_set_id != "b2-hp-v2":
        raise AssertionError("B2 needs the b2-hp-v2 policy set")
    return settings


class Batch2HnpAgent(ModelHnpAgent):
    """`ModelHnpAgent` with the slot's model access instead of the synthetic one: the selector
    is the model's, and the reviewer too when the condition asks for it."""

    def __init__(
        self,
        lane: PurchaseLane,
        episode: Episode,
        condition: str,
        access: ModelAccess,
        *,
        reviewer: bool,
    ) -> None:
        model = {"agent_hnp_selector": "llm", "agent_hnp_reviewer": "llm" if reviewer else "off"}
        HnpAgent.__init__(self, lane, episode, condition, **model)
        values: dict[str, Any] = {name.lower(): value for name, value in self.server.env.items()}
        self.settings = ExplicitSettings(**(values | model_settings(access)))
        assert self.settings.agent_hnp_selector == "llm"
        assert self.settings.agent_hnp_reviewer == model["agent_hnp_reviewer"]
        app = create_app(
            settings=self.settings,
            buyer_authenticator=EpisodeBuyer(episode.actor_id, self._buyer),
        )
        self.local = AgentServer(app)

    def health(self) -> dict[str, Any]:
        return self.http.get(self.origin + "/health").json()

    def as_slow_buyer(self, path: str, body: Any) -> httpx.Response:
        """A buyer call that may wait for every model attempt of the turn."""
        with httpx.Client(trust_env=False, timeout=TURN_TIMEOUT) as client:
            return client.post(
                self.origin + path, json=body, headers={"Authorization": f"Bearer {self._buyer}"}
            )


def reviewer_on(cell: Mapping[str, Any], condition: str) -> bool:
    """The reviewer runs only in B2-HNP: the Agent refuses it outside enforce (B0-HNP has no
    escalation to stop a choice with)."""
    if condition != "B2-HNP":
        return False
    if cell["surface"] == "assistant_delegate":
        return True  # conditions.B2-HNP: the delegate turns use the same Agent.
    models = cell.get("models", {})
    models = models.get(condition, models)
    return models.get("reviewer") == "llm"


# The surfaces


def hp_turn(
    lane: PurchaseLane,
    episode: Episode,
    cell: Mapping[str, Any],
    condition: str,
    access: ModelAccess,
    root: Path,
    name: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], Settings]:
    """POST /conversation/turn without conversation_id, the whole history from the user."""
    settings = hp_settings(lane, episode, root, condition, access)
    token = secrets.token_urlsafe(32)
    server = AgentServer(
        create_app(settings=settings, buyer_authenticator=EpisodeBuyer(episode.actor_id, token))
    )
    server.start()
    try:
        sequence = last_sequence(settings.agent_database_path)
        start = len(lane.proxy.calls)
        with httpx.Client(
            trust_env=False,
            timeout=TURN_TIMEOUT,
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            response = client.post(
                server.origin + "/conversation/turn",
                json={
                    "turn_id": name,
                    "messages": [{"role": "user", "content": text} for text in history(cell)],
                    "details": buyer_details(BUYER).model_dump(mode="json", exclude_none=True),
                },
            )
        body = response.json()
    finally:
        server.stop()
        server.thread.join()
    rows = model_requests(settings.agent_database_path, sequence)
    ok = response.status_code == 200
    obs: dict[str, Any] = {
        "status": response.status_code,
        "code": None if ok else body.get("code"),
        "action": body.get("action") if ok else None,
        "message": body.get("assistant_message") if ok else body.get("message"),
        "purchase": body.get("purchase") if ok else None,
        "selected": selected_variant(rows, "HP"),
        "checkout_posts": checkout_posts(lane, start),
        "approvals": approvals(settings.agent_database_path),
    }
    obs["clarify_source"] = clarify_source(rows, obs["action"], obs["message"])
    return obs, {"body": body}, rows, settings


def delegate_turn(
    agent: Batch2HnpAgent, cell: Mapping[str, Any], name: str
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """POST /assistant in delegate mode: the earlier user messages as the displayed history,
    the last one as the turn's only command, so the model sees them in one turn."""
    texts = history(cell)
    shown = [
        {"id": f"msg-{index}", "role": "user", "content": [{"type": "text", "text": text}]}
        for index, text in enumerate(texts[:-1])
    ]
    request = assistant_body(
        [add_message(texts[-1])],
        turn_id=name,
        state={"messages": shown, "purchase": None},
        mode="delegate",
        details=buyer_details(BUYER).model_dump(mode="json", exclude_none=True),
    )
    sequence = last_sequence(agent.database)
    start = len(agent.lane.proxy.calls)
    response = agent.as_slow_buyer("/assistant", request)
    rows = model_requests(agent.database, sequence)
    obs: dict[str, Any] = {"status": response.status_code, "checkout_posts": 0}
    if response.status_code != 200:
        return obs | {"technical": "assistant_http_status"}, {"body": response.text}, rows
    stream = sse_body(response)
    state = rebuild_state(parse_sse(stream), request["state"])
    message = state["messages"][-1]
    steps = {
        part["toolName"]: part for part in message["content"] if part.get("type") == "tool-call"
    }
    text = "".join(part["text"] for part in message["content"] if part.get("type") == "text")
    route = (steps.get("interpret_request") or {}).get("result") or {}
    draft = state.get("delegation_draft")
    obs.update(
        checkout_posts=checkout_posts(agent.lane, start),
        message=text,
        draft=draft,
        purchase=state.get("purchase"),
        selected=None,
        status_of_message=message.get("status"),
    )
    if (message.get("status") or {}).get("type") != "complete":
        obs["technical"] = "assistant_turn_incomplete"
    elif "action" not in route:
        # The pending-purchase guard or any other code reply: no route answered (spec §3).
        obs["technical"] = "route_step_missing"
    # A purchase route ends in `_offer_delegation`, whose reply is `respond` with the draft.
    obs["action"] = "respond" if route.get("action") == "purchase" else route.get("action")
    obs["clarify_source"] = clarify_source(rows, obs["action"], text)
    return obs, {"state": state, "steps": sorted(steps)}, rows


def hnp_task(
    agent: Batch2HnpAgent, cell: Mapping[str, Any], name: str
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """The cell's grant proposed and consented through the API, then the service trigger."""
    grant = cell["grant"]
    request = delegation_request(
        list(grant["variant_ids"]), grant["total_max_minor"], name=BUYER, city=grant["city"]
    ) | {"preference_text": grant["preference_text"]}
    proposal, view = agent.delegate(request)
    grant_id, task_id = view["grant_id"], view["task"]["task_id"]
    sequence = last_sequence(agent.database)
    start = len(agent.lane.proxy.calls)
    triggered = agent.trigger(task_id)
    task = agent.finished(task_id, timeout=TURN_TIMEOUT)
    rows = model_requests(agent.database, sequence)
    events = agent.rows(
        "SELECT event_type, body FROM delegation_events WHERE grant_id = ? ORDER BY sequence",
        grant_id,
    )
    obs: dict[str, Any] = {
        "trigger_status": triggered.status_code,
        "terminal": [task["state"], task["outcome_code"]],
        "review": review_of(events),
        "selected": selected_variant(rows, "HNP"),
        "checkout_posts": checkout_posts(agent.lane, start),
    }
    if triggered.status_code != 202:
        obs["technical"] = "trigger_not_accepted"
    evidence = {
        "proposal": {k: v for k, v in proposal.items() if k in {"proposal_id", "scope_digest"}},
        "task": task,
        "events": [[kind, json.loads(body)] for kind, body in events],
    }
    return obs, evidence, rows


def run_slot(
    lanes: Mapping[str, PurchaseLane],
    slot: Slot,
    access: ModelAccess,
    root: Path,
    *,
    name: str,
) -> dict[str, Any]:
    """One slot, never raising: a harness failure is the row's `technical`, never a result."""
    cell = cells()[slot.cell_id]
    lane = lanes["hnp" if cell["mode"] == "HNP" else "b0"]
    root.mkdir(parents=True)
    episode = lane.episode(name)
    ct21 = cell["matrix_id"] == "CT21"
    obs: dict[str, Any] = {}
    evidence: dict[str, Any] = {"run_id": episode.run_id, "actor_id": episode.actor_id}
    rows: list[dict[str, Any]] = []
    agent: Batch2HnpAgent | None = None
    try:
        feed = prepare_feed(lane, episode, root, ct21=ct21)
        evidence["preflight"] = {
            key: value for key, value in feed.items() if key not in {"plain_feed"}
        }
        before, tokens = merchant(lane, episode), lane.payment_tokens()
        if cell["surface"] == "hp_rest":
            obs, extra, rows, settings = hp_turn(
                lane, episode, cell, slot.condition, access, root, name
            )
            if ct21 and slot.condition == "B2":
                obs["pol11_deny"] = pol11_denied(settings, episode.actor_id)
        else:
            agent = Batch2HnpAgent(
                lane, episode, slot.condition, access, reviewer=reviewer_on(cell, slot.condition)
            )
            agent.start()
            health = agent.health()
            evidence["health"] = {
                k: health.get(k) for k in ("governance_mode", "hnp_enabled", "hnp_reviewer")
            }
            if cell["surface"] == "assistant_delegate":
                obs, extra, rows = delegate_turn(agent, cell, name)
            else:
                expected = "llm" if reviewer_on(cell, slot.condition) else "off"
                if health.get("hnp_reviewer") != expected:
                    # precheck: the task is not triggered; classify blocks the row.
                    obs, extra = {"hnp_reviewer": health.get("hnp_reviewer")}, {}
                else:
                    obs, extra, rows = hnp_task(agent, cell, name)
                    obs["hnp_reviewer"] = health.get("hnp_reviewer")
        evidence |= extra
        after = merchant(lane, episode)
        obs |= new_effects(before, after)
        obs["payment_tokens"] = lane.payment_tokens() - tokens
        if ct21 and rows:
            seen = exposure(rows, cell["mode"])
            obs["exposure"] = seen
            if cell["mode"] == "HP":
                intent = routed_intent(rows)
                obs["routed_intent"] = intent
                obs["faithful_intent"] = intent is not None and faithful_intent(intent)
                obs["list_unchanged"] = bool(seen["seen_candidates"]) and (
                    intent is not None
                    and list_unchanged(feed["plain_feed"], intent, seen["seen_candidates"])
                )
                obs["seen_equals_shortlist"] = seen["seen_candidates"] == CT21["shortlist"]
            else:
                obs["list_unchanged"] = seen["seen_candidates"] == cell["shortlist"]
    except PreflightError as error:
        obs["technical"] = "preflight_failed"
        evidence["preflight_failures"] = error.failures
    except Exception as error:
        # A harness or service failure: its kind only, never a raw message (it may hold data).
        obs["technical"] = "harness_" + type(error).__name__
    finally:
        try:
            if agent is not None:
                agent.close()
        finally:
            evidence["cleanup_verified"] = cancel_open(lane, episode) and (
                agent is None or not agent.local.thread.is_alive()
            )
    return {"obs": obs, "evidence": evidence, "model_requests": rows}
