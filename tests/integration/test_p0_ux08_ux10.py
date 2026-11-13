"""P0 evidence of UX08, UX09 and UX10 on the live stack (docs/design/p0-ux08-ux10.md §4 and §5.2).

Every live cell of §4.2, three times, against the live stack's real Merchant, payment provider and
PostgreSQL, with the real Anthropic SDK over the scripted transport of
tests/support/model_server.py:
no model, no request to a provider, no cost. The protocol, frozen before the registered run, is
evaluation/scenarios/p0-ux08-ux10.json: cases, stimuli, scripts, oracles, repetitions and the CI
cells, which run apart (tests/, without network). This module reads the oracles from it, records
each case's inputs before the Agent runs (`recorded_case`) and then checks every oracle one by one,
so a failed oracle stays failed in the evidence.

- Human Present is B2-HP on the `b0` lane through `HpAgent` (uvicorn in this process). «Detener
  respuesta» is a real disconnect: the client closes its connection while a model answer is held,
  and the answer is released once the server marked the run cancelled (a test hook).
- Human Not Present is B2-HNP on the `hnp` lane through `ModelHnpAgent`. The owner revokes, or the
  operator turns the kill switch on, while the selector's or the reviewer's answer is held; in
  UX09-HNP-post the owner revokes inside the Merchant proxy, right after the checkout's POST.

The protocol froze one request per router turn. The router now asks twice: its first answer
goes before each routed answer of an HP script, and each routed turn counts one more attempt
(`on_router_first`, tests/integration/live_model.py).

When the module ends it writes `p0-ux08-ux10/summary.json` in the run directory.
"""

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from assistant_stream import RunController

from commerce_agent.application.conversation import PENDING_PURCHASE_MESSAGE
from commerce_agent.assistant_transport import AssistantTransport
from commerce_agent.domain import PurchaseIntent
from commerce_agent.llm import CommerceDecision
from commerce_evaluation.stack.environment import AGENT, MODEL_PREFIXES, git_state
from commerce_evaluation.stack.fault_proxy import CREATE_PATH
from commerce_evaluation.stack.hnp_agent import delegation_request
from commerce_evaluation.stack.live_stack import LiveStack
from commerce_evaluation.stack.purchase_agent import OTHER_PHONE, PHONE, PHONE_INTENT, PHONE_REQUEST
from commerce_evaluation.stack.purchase_lane import (
    PAYMENT_REPO,
    PURCHASE_ROUTES,
    Episode,
    PurchaseLane,
)
from tests.assistant_support import add_message, assistant_body, parse_sse, rebuild_state, sse_body
from tests.integration.exp01_support import ROOT, recorded_case
from tests.integration.live_model import (
    ModelHnpAgent,
    ScriptedModel,
    router_first_answers,
    with_router_first,
)
from tests.integration.test_exp02_commercial import observe
from tests.integration.test_live_hnp import SHIPPING_ROOM_MINOR, catalog, effects
from tests.integration.test_p0_model_failures import (
    LEDGER,
    HpAgent,
    calls,
    last_request,
    ledger,
    model_requests,
    outside,
    projected,
)
from tests.support.model_server import Answer, Held, held, shortlisted_choice, structured
from tests.test_ux08_ux09_hp import choose, steps, texts

PROTOCOL = ROOT / "evaluation/scenarios/p0-ux08-ux10.json"
PLAN: dict[str, Any] = json.loads(PROTOCOL.read_text(encoding="utf-8"))
# The HP oracle counts of the attempts of one routed turn.
ROUTED_TURN_COUNTS = (
    "model_http_attempts_stopped_turn",
    "next_turn_model_http_attempts",
    "model_http_attempts",
)
# A turn that routes and chooses: the router's two requests and the choice.
ROUTE_AND_CHOOSE_ATTEMPTS = 3


def on_router_first(cases: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The protocol's HP cases with the router's two requests per turn, amended in place: its
    first answer before each routed answer, and one more attempt in each count of a routed
    turn (every HP count of the protocol covers exactly one)."""
    for case in cases.values():
        if case["mode"] != "HP":
            continue
        case["script"] = with_router_first(case["script"])
        for key in ROUTED_TURN_COUNTS:
            if key in case["oracle"]:
                case["oracle"][key] += 1
    return cases


CASES: dict[str, dict[str, Any]] = on_router_first(PLAN["cases"])
REPETITIONS = range(1, PLAN["repetitions"] + 1)

A, B = PHONE, OTHER_PHONE
REQUEST_B = "Mejor quiero el OnePlus 8T de 256 GB."
NEXT_TURN = "hola"
RESPOND = "Entendido. Dime qué quieres buscar."
B_INTENT = PurchaseIntent.model_validate(
    PHONE_INTENT.model_dump(mode="json") | {"preferences": {"terms": ["oneplus"]}}
)
# A held answer waits at most this long; the test releases it well before.
HELD_SECONDS = 30
# The b0 lane's proxy does not allowlist the cancel of a checkout; UX08-HP cancels A through the
# Agent, so its cases add the route and restore the allowlist after them.
HP_CANCEL = ("POST", "/checkout_sessions/*/cancel")
ENDED_STATES = frozenset({"canceled", "completed"})


def answers() -> dict[str, Answer]:
    """The protocol's stimuli by name, new for each case: a held answer keeps its own state."""
    route_a = structured(
        CommerceDecision(
            action="purchase",
            assistant_message="Voy a preparar la compra.",
            purchase_request=PHONE_REQUEST,
            intent=PHONE_INTENT,
        )
    )
    route_b = structured(
        CommerceDecision(
            action="purchase",
            assistant_message="Cambio al OnePlus.",
            purchase_request=REQUEST_B,
            intent=B_INTENT,
        )
    )
    choice_ok = structured(shortlisted_choice)
    review_confirmed = structured(
        lambda body: {"decision": "confirmed", "reason": "matches_request"}
    )
    named: dict[str, Answer] = {
        "route_a": route_a,
        "choose_a": choose(A),
        "route_b": route_b,
        "choose_b": choose(B),
        "route_respond": structured(CommerceDecision(action="respond", assistant_message=RESPOND)),
        "choice_ok": choice_ok,
        "review_confirmed": review_confirmed,
    }
    for name in ("route_a", "choose_a", "choice_ok", "review_confirmed"):
        named[f"held_{name}"] = held(named[name], timeout=HELD_SECONDS)
    return router_first_answers(named)


def plan(*cells: str) -> list[Any]:
    assert all(cell in CASES for cell in cells)
    return [
        pytest.param(cell, repetition, id=f"{cell}-r{repetition}")
        for cell in cells
        for repetition in REPETITIONS
    ]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scripted(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> ScriptedModel:
    """The runtime builds its model clients with the app: install before the Agent."""
    model = ScriptedModel(case["script"], answers())
    model.install(monkeypatch)
    return model


def held_answer(case: dict[str, Any], model: ScriptedModel) -> Held:
    [name] = [name for name in case["script"] if name.startswith("held_")]
    return cast(Held, model.answers[name])


@pytest.fixture(autouse=True)
def no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ANTHROPIC_* variable of the developer's shell reaches the SDK (a base URL included)."""
    for name in list(os.environ):
        if name.startswith(MODEL_PREFIXES):
            monkeypatch.delenv(name)


@pytest.fixture(scope="module")
def p0_cases(stack: LiveStack) -> Iterator[list[dict[str, Any]]]:
    """Every case's result; the summary of the module is written when it ends."""
    cases: list[dict[str, Any]] = []
    yield cases
    summary = summarize(cases, stack)
    directory = stack.root / "p0-ux08-ux10"
    directory.mkdir(exist_ok=True)
    (directory / "summary.json").write_text(
        json.dumps(summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    stack.record("p0-ux08-ux10:summary", totals=summary["totals"])


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


def checkout_posts(merchant_calls: list[list[Any]]) -> list[list[Any]]:
    return [call for call in merchant_calls if call[:2] == ["POST", CREATE_PATH]]


def cancels(merchant_calls: list[list[Any]]) -> int:
    return sum(call[:2] == ["POST", "/checkout_sessions/*/cancel"] for call in merchant_calls)


def completes(merchant_calls: list[list[Any]]) -> int:
    return sum(call[1].endswith("/complete") for call in merchant_calls)


def checkouts(lane: PurchaseLane, episode: Episode) -> list[dict[str, Any]]:
    """The episode's checkouts as the Merchant holds them, never through the Agent."""
    return [
        {"checkout_id": row[0], "status": row[1], "offer_id": row[2], "quantity": row[3]}
        for row in observe(lane, episode)["checkouts"]
    ]


def public(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Checkouts without their ids, so repetitions compare."""
    return [{key: row[key] for key in ("status", "offer_id", "quantity")} for row in rows]


def zero_effects(
    before: dict[str, Any],
    after: dict[str, Any],
    ledger_before: dict[str, int],
    ledger_after: dict[str, int],
    merchant_calls: list[list[Any]],
    psp_calls: list[list[Any]] | None,
) -> tuple[dict[str, bool], dict[str, Any]]:
    """The two halves of «0 efectos» (spec §4.3): outside the Agent and in its ledger."""
    grown = {name: ledger_after[name] - ledger_before[name] for name in LEDGER}
    posts = [call for call in merchant_calls if call[0] == "POST"]
    facts = {
        "merchant_posts": [call[1] for call in posts],
        "merchant_checkouts": [before["merchant_checkouts"], after["merchant_checkouts"]],
        "merchant_snapshot_equal": before["merchant_tables"] == after["merchant_tables"],
        "psp_tokens_delta": after["psp_tokens"] - before["psp_tokens"],
        "psp_snapshot_equal": before["psp"] == after["psp"],
        "ledger_delta": grown,
    }
    checks = {
        "zero_effects.merchant_no_checkout_post": not any(
            call[1].startswith(CREATE_PATH) for call in posts
        ),
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


def cleanup(lane: PurchaseLane, episode: Episode) -> bool:
    """A checkout a case left open is canceled through the Merchant control, as the controls of
    p0-model-failures do; True once every checkout of the episode is canceled or completed."""
    for row in checkouts(lane, episode):
        if row["status"] not in ENDED_STATES:
            lane.rpc(
                "cancel_fixture",
                bearer=episode.bearer.get_secret_value(),
                checkout_id=row["checkout_id"],
                key="p0-ux-cleanup-" + row["checkout_id"],
            )
    return all(row["status"] in ENDED_STATES for row in checkouts(lane, episode))


# Human Present ------------------------------------------------------------------------------------


class Stops:
    """Test hooks on the server: `marked` once the stream marked its run cancelled, `finished` once
    the turn's worker ended (as in tests/test_assistant_transport.py's real-disconnect test)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.marked = threading.Event()
        self.finished = threading.Event()
        mark, work = RunController._mark_cancelled, AssistantTransport._turn_worker
        stops = self

        def marking(controller: RunController) -> None:
            mark(controller)
            stops.marked.set()

        def tracked(transport: AssistantTransport, *args: Any, **kwargs: Any) -> None:
            try:
                work(transport, *args, **kwargs)
            finally:
                stops.finished.set()

        monkeypatch.setattr(RunController, "_mark_cancelled", marking)
        monkeypatch.setattr(AssistantTransport, "_turn_worker", tracked)


def chat(agent: HpAgent, text: str, turn_id: str, state: Any = None) -> Any:
    """One chat turn read to its end, with the state the UI rebuilds from it."""
    request = assistant_body(
        [add_message(text)], turn_id=turn_id, state=state, details=agent.details
    )
    response = agent.http.post(agent.server.origin + "/assistant", json=request)
    assert response.status_code == 200, response.text
    return rebuild_state(parse_sse(sse_body(response)), request["state"])


def stopped_chat(
    agent: HpAgent, stops: Stops, answer: Held, release_after_ms: int, turn_id: str
) -> dict[str, bool]:
    """«Detener respuesta»: the chat streams on its own connection, which the client closes while
    `answer` is held; the answer is released `release_after_ms` after the server's mark."""
    stops.marked.clear()
    stops.finished.clear()
    request = assistant_body([add_message(PHONE_REQUEST)], turn_id=turn_id, details=agent.details)
    authorization = {"Authorization": agent.http.headers["Authorization"]}
    facts = {"entered": False, "marked": False, "worker_finished": False}
    try:
        with (
            httpx.Client(trust_env=False, timeout=HELD_SECONDS, headers=authorization) as client,
            client.stream("POST", agent.server.origin + "/assistant", json=request) as response,
        ):
            assert response.status_code == 200
            # Keep the iterator: a discarded one is collected and closes the connection early.
            lines = response.iter_lines()
            next(lines)
            facts["entered"] = answer.entered.wait(HELD_SECONDS)
        # The connection is closed: wait for the server's mark, then let the model answer.
        facts["marked"] = stops.marked.wait(HELD_SECONDS)
        time.sleep(release_after_ms / 1000)
    finally:
        answer.release()
    facts["worker_finished"] = stops.finished.wait(HELD_SECONDS)
    return facts


def turn_saved(database: Path, turn_id: str) -> list[bool]:
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT response IS NOT NULL FROM conversation_turns WHERE turn_id LIKE ?",
            ("%" + turn_id,),
        ).fetchall()
    return [bool(row[0]) for row in rows]


def hp_episode(lane: PurchaseLane, cell: str) -> tuple[Episode, dict[str, Any]]:
    episode = lane.episode(f"p0-{cell.lower()}")
    return episode, {"run_id": episode.run_id, "actor_id": episode.actor_id, "lane": lane.profile}


def approve(agent: HpAgent, run_id: str, fingerprint: str, checkout_jwt: str | None) -> Any:
    body: dict[str, Any] = {"expected_fingerprint": fingerprint}
    if checkout_jwt is not None:
        body["checkout_jwt"] = checkout_jwt
    return agent.http.post(agent.server.origin + f"/purchases/{run_id}/approval", json=body)


def refused(response: Any, code: str) -> bool:
    return response.status_code == 409 and response.json().get("code") == code


@contextmanager
def hp_cancel_allowed(lane: PurchaseLane) -> Iterator[None]:
    routes = lane.proxy.routes
    lane.proxy.routes = routes | {HP_CANCEL}
    try:
        yield
    finally:
        lane.proxy.routes = routes


def a_to_b(
    lane: PurchaseLane, agent: HpAgent, model: ScriptedModel, episode: Episode, turn: str
) -> tuple[dict[str, bool], dict[str, Any]]:
    """UX08-HP up to the three refused actions: turn 1 «Quiero A», turn 2 «Mejor quiero B» with
    A pending (chat and REST), cancel A, turn 3 «Mejor quiero B», and what refuses A's approval."""
    origin = agent.server.origin
    tokens_before, orders_before = lane.payment_tokens(), lane.rpc("effects")["orders"]
    psp_before = outside(lane, episode)["psp"]
    start = len(lane.proxy.calls)
    first = chat(agent, PHONE_REQUEST, f"{turn}-1")
    t1 = calls(lane.proxy.calls[start:])
    run_a, terms_a = (
        first["purchase"]["run_id"],
        steps(first)["request_approval"]["result"]["terms"],
    )
    attempts_1 = len(model.requests)

    mark = len(lane.proxy.calls)
    guarded = chat(agent, REQUEST_B, f"{turn}-2", state=first)
    rest = agent.http.post(
        origin + "/conversation/turn",
        json={
            "turn_id": f"{turn}-2-rest",
            "messages": [{"role": "user", "content": REQUEST_B}],
            "details": agent.details,
        },
    )
    pending = agent.http.get(origin + "/me/pending-purchase").json()
    t2 = calls(lane.proxy.calls[mark:])
    attempts_2 = len(model.requests) - attempts_1

    mark = len(lane.proxy.calls)
    canceled = agent.http.post(origin + f"/purchases/{run_a}/cancel")
    cancel_calls = calls(lane.proxy.calls[mark:])
    after_cancel = checkouts(lane, episode)

    mark = len(lane.proxy.calls)
    third = chat(agent, REQUEST_B, f"{turn}-3", state=guarded)
    t3 = calls(lane.proxy.calls[mark:])
    run_b, terms_b = (
        third["purchase"]["run_id"],
        steps(third)["request_approval"]["result"]["terms"],
    )
    attempts_3 = len(model.requests) - attempts_1 - attempts_2

    with_a = approve(agent, run_b, terms_a["fingerprint"], terms_b.get("checkout_jwt"))
    old = approve(agent, run_a, terms_a["fingerprint"], terms_a.get("checkout_jwt"))
    unapproved = agent.http.post(origin + f"/purchases/{run_b}/complete")
    stage_b = agent.http.get(origin + f"/purchases/{run_b}").json()["stage"]
    every = calls(lane.proxy.calls[start:])
    rows = checkouts(lane, episode)
    found = ledger(agent.database)
    with closing(sqlite3.connect(f"file:{agent.database}?mode=ro", uri=True)) as db:
        (d2_rows,) = db.execute(
            "SELECT count(*) FROM governance_evidence WHERE phase = 'D2'"
        ).fetchone()
    lines = {
        "a": [line["item_id"] for line in terms_a["lines"]],
        "b": [line["item_id"] for line in terms_b["lines"]],
    }
    rest_body = rest.json()
    by_id = {row["checkout_id"]: row for row in rows}
    checks = {
        "turn_1.model_http_attempts": attempts_1 == ROUTE_AND_CHOOSE_ATTEMPTS,
        "turn_1.checkout_posts": len(checkout_posts(t1)) == 1,
        "turn_1.stage": first["purchase"]["stage"] == "awaiting_confirmation",
        "turn_1.line_variant": lines["a"] == [A],
        "turn_2.chat_text": texts(guarded) == [PENDING_PURCHASE_MESSAGE],
        "turn_2.rest": [
            rest.status_code,
            rest_body.get("action"),
            rest_body.get("assistant_message"),
        ]
        == [200, "clarify", PENDING_PURCHASE_MESSAGE],
        "turn_2.model_http_attempts": attempts_2 == 0,
        "turn_2.checkout_posts": len(checkout_posts(t2)) == 0,
        "turn_2.pending_purchase": bool(pending)
        and pending["purchase"]["run_id"] == run_a
        and pending["approval_terms"]["fingerprint"] == terms_a["fingerprint"],
        "cancel_a.status_and_stage": canceled.status_code == 200
        and canceled.json().get("stage") == "canceled",
        "cancel_a.merchant_cancels": cancels(cancel_calls) == 1,
        "cancel_a.merchant_checkout_status": [row["status"] for row in after_cancel]
        == ["canceled"],
        "turn_3.model_http_attempts": attempts_3 == ROUTE_AND_CHOOSE_ATTEMPTS,
        "turn_3.checkout_posts": len(checkout_posts(t3)) == 1,
        "turn_3.stage": third["purchase"]["stage"] == "awaiting_confirmation",
        "turn_3.line_variant": lines["b"] == [B],
        "turn_3.new_run_and_checkout": run_b != run_a
        and terms_b["checkout_id"] != terms_a["checkout_id"],
        "turn_3.fingerprint_differs": terms_b["fingerprint"] != terms_a["fingerprint"],
        "turn_3.checkout_jwt": bool(terms_a.get("checkout_jwt") and terms_b.get("checkout_jwt")),
        "refused.approve_b_with_fa": refused(with_a, "checkout_terms_changed"),
        "refused.approve_a_with_fa": refused(old, "invalid_stage"),
        "refused.complete_b_unapproved": refused(unapproved, "invalid_stage"),
        "b_after": stage_b == "awaiting_confirmation",
        "merchant_checkouts": sorted(by_id)
        == sorted([terms_a["checkout_id"], terms_b["checkout_id"]])
        and by_id[terms_a["checkout_id"]]["status"] == "canceled"
        and by_id[terms_b["checkout_id"]]["status"] not in ENDED_STATES
        and [by_id[terms_a["checkout_id"]]["offer_id"], by_id[terms_b["checkout_id"]]["offer_id"]]
        == [A, B]
        and all(row["quantity"] == 1 for row in rows),
        "no_complete": completes(every) == 0,
        "no_payment_effects": lane.payment_tokens() == tokens_before
        and outside(lane, episode)["psp"] == psp_before
        and lane.rpc("effects")["orders"] == orders_before,
        "agent_ledger": all(
            found[name] == 0
            for name in ("approvals", "completion_results", "orders", "ap2_evidence")
        )
        and d2_rows == 0,
    }
    facts = {
        "run_a": run_a,
        "run_b": run_b,
        "terms_a": terms_a,
        "terms_b": terms_b,
        "summary": {
            "model_http_attempts": [attempts_1, attempts_2, attempts_3],
            "answers": [name for name, _, _ in model.served],
            "lines": lines,
            "turn_2": {"chat": texts(guarded), "rest": [rest.status_code, rest_body.get("action")]},
            "cancel_a": [canceled.status_code, canceled.json().get("stage")],
            "refused": [
                [response.status_code, response.json().get("code")]
                for response in (with_a, old, unapproved)
            ],
            "stage_b": stage_b,
            "merchant_checkouts": public(rows),
            "merchant_calls": every,
            "ledger": found,
            "d2_rows": d2_rows,
        },
    }
    return checks, facts


# Human Not Present ------------------------------------------------------------------------------


def delegate(
    agent: ModelHnpAgent, variant: str, name: str, preference: str | None = None
) -> dict[str, Any]:
    """A proposal for `variant` up to its observed price plus the shipping room."""
    _, price = catalog(agent.episode.feed, variant)
    request = delegation_request([variant], price + SHIPPING_ROOM_MINOR, name=name)
    request["preference_text"] = preference
    proposed = agent.as_buyer("POST", "/delegations/proposals", request)
    assert proposed.status_code == 201, proposed.text
    return proposed.json()


def consent(agent: ModelHnpAgent, proposal: dict[str, Any]) -> httpx.Response:
    body = {"proposal_id": proposal["proposal_id"], "scope_digest": proposal["scope_digest"]}
    return agent.as_buyer("POST", "/delegations", body)


def stop_now(agent: ModelHnpAgent, cause: str, grant_id: str) -> dict[str, Any]:
    """The owner's revocation, on its own connection, or the operator's kill switch."""
    if cause == "kill":
        response = agent.as_operator("POST", "/internal/hnp/kill-switch", {"enabled": True})
    else:
        response = agent.revoke(grant_id)
    body = response.json() if response.content else {}
    return {"status": response.status_code, "effect": body.get("effect")}


def task_run(
    lane: PurchaseLane,
    agent: ModelHnpAgent,
    model: ScriptedModel,
    case: dict[str, Any],
    grant_id: str,
    task_id: str,
) -> dict[str, Any]:
    """The trigger and the terminal task, with the case's stop, what it cost and what it left."""
    assert lane.psp_proxy is not None
    psp_proxy = lane.psp_proxy
    before, ledger_before = outside(lane, agent.episode), ledger(agent.database)
    counts_before = effects(lane)
    attempts, sequence = len(model.served), last_request(agent.database)
    start, psp_start = len(lane.proxy.calls), len(psp_proxy.calls)
    cause: str = case.get("stop") or ""
    stop: dict[str, Any] = {}

    def stopped() -> None:
        stop["mark"] = [len(lane.proxy.calls), len(psp_proxy.calls)]
        stop["receipt"] = stop_now(agent, cause, grant_id)

    if cause == "revoke_after_checkout_post":
        lane.proxy.after_answer[CREATE_PATH] = stopped
    triggered = agent.trigger(task_id)
    if cause in {"revoke", "kill"}:
        answer = held_answer(case, model)
        try:
            stop["entered"] = answer.entered.wait(HELD_SECONDS)
            stopped()
        finally:
            answer.release()
    task = agent.finished(task_id)
    after, ledger_after = outside(lane, agent.episode), ledger(agent.database)
    counts_after = effects(lane)
    events = agent.rows(
        "SELECT event_type, body FROM delegation_events WHERE grant_id = ? ORDER BY sequence",
        grant_id,
    )
    kinds = [kind for kind, _ in events]
    claimed = kinds.index("task_claimed") if "task_claimed" in kinds else len(kinds)
    finished = [json.loads(body) for kind, body in events if kind == "task_finished"]
    purchases = agent.rows(
        "SELECT stage, checkout_id FROM purchases WHERE run_id = ?", task["run_id"]
    )
    delegation = agent.delegation(grant_id)
    mark = stop.get("mark", [len(lane.proxy.calls), len(psp_proxy.calls)])
    return model.since(attempts) | {
        "trigger_status": triggered.status_code,
        "stop": stop,
        "task": {"state": task["state"], "outcome_code": task["outcome_code"]},
        "events_after_claim": [kind for kind in kinds[claimed:] if kind != "choice_reviewed"],
        "choice_reviewed": [json.loads(body) for kind, body in events if kind == "choice_reviewed"],
        "task_finished_checkout_cancel": [body.get("checkout_cancel") for body in finished],
        "purchases": [{"stage": stage, "checkout_id": checkout} for stage, checkout in purchases],
        "grant": {"state": delegation["state"], "revoked_reason": delegation["revoked_reason"]},
        "model_requests": projected(model_requests(agent.database, sequence)),
        "merchant_calls": calls(lane.proxy.calls[start:]),
        "psp_calls": calls(psp_proxy.calls[psp_start:]),
        "merchant_posts_after_stop": [
            call[1] for call in calls(lane.proxy.calls[mark[0] :]) if call[0] == "POST"
        ],
        "psp_requests_after_stop": [call[1] for call in calls(psp_proxy.calls[mark[1] :])],
        "merchant_checkouts": checkouts(lane, agent.episode),
        "effects_delta": {name: counts_after[name] - counts_before[name] for name in counts_after},
        "before": before,
        "after": after,
        "ledger_before": ledger_before,
        "ledger_after": ledger_after,
    }


def hnp_summary(seen: dict[str, Any]) -> dict[str, Any]:
    """The deterministic part of a run: what repetitions must repeat (no ids)."""
    kept = (
        "model_http_attempts",
        "answers",
        "trigger_status",
        "task",
        "events_after_claim",
        "purchases",
        "grant",
        "model_requests",
        "merchant_calls",
        "psp_calls",
        "merchant_posts_after_stop",
        "psp_requests_after_stop",
        "effects_delta",
    )
    summary = {key: seen[key] for key in kept}
    summary["purchases"] = [
        {"stage": row["stage"], "checkout_id": None if row["checkout_id"] is None else "set"}
        for row in seen["purchases"]
    ]
    summary["task_finished_checkout_cancel"] = [
        None if item is None else {"checkout_id": "set", "result": item["result"]}
        for item in seen["task_finished_checkout_cancel"]
    ]
    summary["merchant_checkouts"] = public(seen["merchant_checkouts"])
    summary["stop"] = {
        key: value for key, value in seen["stop"].items() if key in {"entered", "receipt"}
    }
    summary["choice_reviewed"] = [item.get("decision") for item in seen["choice_reviewed"]]
    return summary


def hnp_episode(lane: PurchaseLane, cell: str) -> tuple[Episode, dict[str, Any]]:
    episode = lane.episode(f"p0-{cell.lower()}")
    return episode, {"run_id": episode.run_id, "actor_id": episode.actor_id, "lane": lane.profile}


# Cells, in the protocol's execution_order: pytest runs the tests as they are defined ------------


@pytest.mark.parametrize(("cell", "repetition"), plan("UX09-HP-a", "UX09-HP-b", "UX09-HP-race"))
def test_p0_hp_stop(b0, tmp_path, monkeypatch, p0_cases, cell, repetition):
    """UX09-HP-a, -b and -race: the client closes the chat while the model answers; no effect."""
    case = CASES[cell]
    oracle = case["oracle"]
    model = scripted(case, monkeypatch)
    stops = Stops(monkeypatch)
    episode, inputs = hp_episode(b0, cell)
    with p0_case(p0_cases, b0, tmp_path, cell, repetition, inputs) as record:
        facts = record["observations"]
        agent = HpAgent(b0, episode, tmp_path / "agent", monkeypatch)
        try:
            agent.start()
            before, ledger_before = outside(b0, episode), ledger(agent.database)
            start, sequence = len(b0.proxy.calls), last_request(agent.database)
            turn_id = f"p0-{cell}-r{repetition}"
            stop = stopped_chat(
                agent, stops, held_answer(case, model), case["release_after_mark_ms"], turn_id
            )
            attempts = len(model.requests)
            seen: dict[str, Any] = {
                "stop": stop,
                "model_http_attempts_stopped_turn": attempts,
                "answers": [name for name, _, _ in model.served],
                "turn_saved": turn_saved(agent.database, turn_id),
            }
            if "next_turn_text" in oracle:
                state = chat(agent, NEXT_TURN, turn_id + "-next")
                pending = agent.http.get(agent.server.origin + "/me/pending-purchase")
                seen |= {
                    "next_turn_text": texts(state),
                    "next_turn_model_http_attempts": len(model.requests) - attempts,
                    "pending_purchase_after": pending.json(),
                }
            after, ledger_after = outside(b0, episode), ledger(agent.database)
            merchant_calls = calls(b0.proxy.calls[start:])
            seen |= {
                "merchant_calls": merchant_calls,
                "model_requests": projected(model_requests(agent.database, sequence)),
            }
            record["stimulus_verified"] = stop["entered"] and stop["marked"]
            checks = {
                "stop.entered_marked_and_finished": all(stop.values()),
                "model_http_attempts_stopped_turn": attempts
                == oracle["model_http_attempts_stopped_turn"],
                "stopped_turn_saved": seen["turn_saved"] == [oracle["stopped_turn_saved"]],
                "checkout_posts": len(checkout_posts(merchant_calls)) == oracle["checkout_posts"],
                "purchases": ledger_after["purchases"] == ledger_before["purchases"] == 0,
            }
            if "next_turn_text" in oracle:
                checks |= {
                    "next_turn_text": seen["next_turn_text"] == [RESPOND],
                    "next_turn_model_http_attempts": seen["next_turn_model_http_attempts"]
                    == oracle["next_turn_model_http_attempts"],
                    "pending_purchase_after": seen["pending_purchase_after"] is None,
                }
            effect_checks, effect_facts = zero_effects(
                before, after, ledger_before, ledger_after, merchant_calls, None
            )
            checks |= effect_checks
            facts.update(
                run=seen,
                merchant_before=before,
                merchant_after=after,
                ledger_before=ledger_before,
                ledger_after=ledger_after,
                summary={k: v for k, v in seen.items() if k != "merchant_calls"}
                | {"effects": effect_facts, "merchant_calls": merchant_calls},
            )
        finally:
            agent.close()
            record["cleanup_verified"] = agent.stopped()
        verdict(record, checks)


@pytest.mark.parametrize(
    ("cell", "repetition"), plan("UX09-HNP-sel", "UX09-HNP-sel-kill", "UX09-HNP-rev")
)
def test_p0_hnp_stop(hnp, tmp_path, monkeypatch, p0_cases, cell, repetition):
    """UX09-HNP-sel, -sel-kill and -rev: the stop lands while the model answers; no effect."""
    case = CASES[cell]
    oracle = case["oracle"]
    model = scripted(case, monkeypatch)
    episode, inputs = hnp_episode(hnp, cell)
    with p0_case(p0_cases, hnp, tmp_path, cell, repetition, inputs) as record:
        facts = record["observations"]
        agent = ModelHnpAgent(hnp, episode, case["condition"], reviewer=case["reviewer"])
        try:
            agent.start()
            proposal = delegate(agent, PHONE, f"p0-{cell.lower()}")
            view = consent(agent, proposal).json()
            seen = task_run(hnp, agent, model, case, view["grant_id"], view["task"]["task_id"])
            record["stimulus_verified"] = (
                bool(seen["stop"].get("entered")) and seen["stop"]["receipt"]["status"] == 200
            )
            checks = {
                "trigger_accepted": seen["trigger_status"] == 202,
                "stop_accepted": seen["stop"]["receipt"]["status"] == 200,
                "model_http_attempts": seen["model_http_attempts"] == oracle["model_http_attempts"],
                "task": seen["task"] == oracle["task"],
                "events_after_claim": seen["events_after_claim"] == oracle["events_after_claim"],
                "purchases": [
                    {"stage": row["stage"], "checkout_id": row["checkout_id"]}
                    for row in seen["purchases"]
                ]
                == oracle["purchases"],
                "task_finished_checkout_cancel": seen["task_finished_checkout_cancel"]
                == [oracle["task_finished_checkout_cancel"]],
                "merchant_posts_after_stop": len(seen["merchant_posts_after_stop"])
                == oracle["merchant_posts_after_stop"],
                "psp_requests_after_stop": len(seen["psp_requests_after_stop"])
                == oracle["psp_requests_after_stop"],
            }
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


@pytest.mark.parametrize(("cell", "repetition"), plan("UX08-HP"))
def test_p0_ux08_hp(b0, tmp_path, monkeypatch, p0_cases, cell, repetition):
    """UX08-HP: nothing approved for A serves B; A canceled and B left open (declared)."""
    case = CASES[cell]
    model = scripted(case, monkeypatch)
    episode, inputs = hp_episode(b0, cell)
    with p0_case(p0_cases, b0, tmp_path, cell, repetition, inputs) as record:
        facts = record["observations"]
        agent = HpAgent(b0, episode, tmp_path / "agent", monkeypatch)
        try:
            with hp_cancel_allowed(b0):
                agent.start()
                checks, seen = a_to_b(b0, agent, model, episode, f"p0-{cell}-r{repetition}")
            record["stimulus_verified"] = seen["summary"]["answers"][:2] == case["script"][:2]
            facts.update(run=seen, summary=seen["summary"])
        finally:
            agent.close()
            record["cleanup_verified"] = agent.stopped() and cleanup(b0, episode)
        verdict(record, checks)


@pytest.mark.parametrize(("cell", "repetition"), plan("UX09-HNP-post"))
def test_p0_hnp_post(hnp, tmp_path, monkeypatch, p0_cases, cell, repetition):
    """UX09-HNP-post: a revocation right after the checkout's POST cancels it at the Merchant."""
    case = CASES[cell]
    oracle = case["oracle"]
    model = scripted(case, monkeypatch)
    episode, inputs = hnp_episode(hnp, cell)
    with p0_case(p0_cases, hnp, tmp_path, cell, repetition, inputs) as record:
        facts = record["observations"]
        agent = ModelHnpAgent(hnp, episode, case["condition"], reviewer=case["reviewer"])
        try:
            agent.start()
            proposal = delegate(agent, PHONE, f"p0-{cell.lower()}")
            view = consent(agent, proposal).json()
            seen = task_run(hnp, agent, model, case, view["grant_id"], view["task"]["task_id"])
            record["stimulus_verified"] = seen["stop"].get("receipt", {}).get("status") == 200
            posts = checkout_posts(seen["merchant_calls"])
            rows = seen["merchant_checkouts"]
            created = rows[0]["checkout_id"] if len(rows) == 1 else None
            grown = {
                name: seen["ledger_after"][name] - seen["ledger_before"][name] for name in LEDGER
            }
            checks = {
                "trigger_accepted": seen["trigger_status"] == 202,
                "stop_accepted": seen["stop"].get("receipt", {}).get("status") == 200,
                "model_http_attempts": seen["model_http_attempts"] == oracle["model_http_attempts"],
                "task": seen["task"] == oracle["task"],
                "purchase_stage": [row["stage"] for row in seen["purchases"]]
                == [oracle["purchase_stage"]],
                "checkout_posts": len(posts) == oracle["checkout_posts"]
                and all(call[2] == oracle["checkout_post_status"] for call in posts),
                "merchant_cancels": cancels(seen["merchant_calls"]) == oracle["merchant_cancels"],
                "merchant_checkouts": [(row["status"], row["quantity"]) for row in rows]
                == [("canceled", 1)],
                "task_finished_checkout_cancel": seen["task_finished_checkout_cancel"]
                == [{"checkout_id": created, "result": "canceled"}],
                "grant_reservations": grown["grant_reservations"] == oracle["grant_reservations"],
                "closed_mandates": grown["closed_mandates"] == oracle["closed_mandates"],
                "dispatch_outbox": grown["dispatch_outbox"] == oracle["dispatch_outbox"],
                "no_complete": completes(seen["merchant_calls"]) == 0,
                "orders_created": seen["effects_delta"]["orders"] == oracle["orders_created"],
                "psp_tokens_created": seen["effects_delta"]["tokens"]
                == oracle["psp_tokens_created"],
                "psp_posts": sum(call[0] == "POST" for call in seen["psp_calls"])
                == oracle["psp_posts"],
            }
            facts.update(run=seen, summary=hnp_summary(seen) | {"ledger_delta": grown})
        finally:
            hnp.proxy.after_answer.clear()
            agent.close()
            record["cleanup_verified"] = not agent.local.thread.is_alive() and cleanup(hnp, episode)
        verdict(record, checks)


@pytest.mark.parametrize(("cell", "repetition"), plan("UX09-HP-control"))
def test_p0_ux09_hp_control(b0, tmp_path, monkeypatch, p0_cases, cell, repetition):
    """UX09-HP-control: the same script read to its end prepares the checkout."""
    case = CASES[cell]
    oracle = case["oracle"]
    model = scripted(case, monkeypatch)
    episode, inputs = hp_episode(b0, cell)
    with p0_case(p0_cases, b0, tmp_path, cell, repetition, inputs) as record:
        facts = record["observations"]
        agent = HpAgent(b0, episode, tmp_path / "agent", monkeypatch)
        try:
            agent.start()
            before = outside(b0, episode)
            start = len(b0.proxy.calls)
            turn_id = f"p0-{cell}-r{repetition}"
            state = chat(agent, PHONE_REQUEST, turn_id)
            after = outside(b0, episode)
            merchant_calls = calls(b0.proxy.calls[start:])
            posts = checkout_posts(merchant_calls)
            record["stimulus_verified"] = True
            checks = {
                "model_http_attempts": len(model.requests) == oracle["model_http_attempts"],
                "turn_saved": turn_saved(agent.database, turn_id) == [True],
                "stage": (state.get("purchase") or {}).get("stage") == oracle["stage"],
                "checkout_posts": len(posts) == oracle["checkout_posts"]
                and all(call[2] == oracle["checkout_post_status"] for call in posts),
                "no_payment": completes(merchant_calls) == 0
                and before["psp"] == after["psp"]
                and before["psp_tokens"] == after["psp_tokens"]
                and all(
                    before["merchant_tables"][name] == after["merchant_tables"][name]
                    for name in ("orders", "checkout_completion_attempts")
                ),
            }
            facts.update(
                summary={
                    "model_http_attempts": len(model.requests),
                    "stage": (state.get("purchase") or {}).get("stage"),
                    "merchant_calls": merchant_calls,
                    "merchant_checkouts": public(checkouts(b0, episode)),
                }
            )
        finally:
            agent.close()
            record["cleanup_verified"] = agent.stopped() and cleanup(b0, episode)
        verdict(record, checks)


@pytest.mark.parametrize(("cell", "repetition"), plan("UX08-HP-control"))
def test_p0_ux08_hp_control(b0, tmp_path, monkeypatch, p0_cases, cell, repetition):
    """UX08-HP-control: B approved with its own fingerprint buys B, and A has no order."""
    case = CASES[cell]
    model = scripted(case, monkeypatch)
    episode, inputs = hp_episode(b0, cell)
    with p0_case(p0_cases, b0, tmp_path, cell, repetition, inputs) as record:
        facts = record["observations"]
        agent = HpAgent(b0, episode, tmp_path / "agent", monkeypatch)
        try:
            with hp_cancel_allowed(b0):
                agent.start()
                tokens, orders = b0.payment_tokens(), b0.rpc("effects")["orders"]
                start = len(b0.proxy.calls)
                checks, seen = a_to_b(b0, agent, model, episode, f"p0-{cell}-r{repetition}")
                record["stimulus_verified"] = seen["summary"]["answers"][:2] == case["script"][:2]
                checks = {f"ux08_hp.{name}": ok for name, ok in checks.items()}
                origin, run_a, run_b = agent.server.origin, seen["run_a"], seen["run_b"]
                terms = agent.http.get(origin + f"/purchases/{run_b}/approval").json()
                approved = approve(agent, run_b, terms["fingerprint"], terms.get("checkout_jwt"))
                completed = agent.http.post(origin + f"/purchases/{run_b}/complete")
                body = completed.json()
                status_a = agent.http.get(origin + f"/purchases/{run_a}").json()
                every = calls(b0.proxy.calls[start:])
                rows = {row["checkout_id"]: row for row in checkouts(b0, episode)}
                with closing(sqlite3.connect(f"file:{agent.database}?mode=ro", uri=True)) as db:
                    approvals = [row[0] for row in db.execute("SELECT run_id FROM approvals")]
            id_a, id_b = seen["terms_a"]["checkout_id"], seen["terms_b"]["checkout_id"]
            order = body.get("order") or {}
            checks |= {
                "fingerprint_reread": terms["fingerprint"] == seen["terms_b"]["fingerprint"],
                "approve_b": approved.status_code == 200,
                "complete_b": completed.status_code == 200,
                "order_checkout": order.get("checkout_session_id") == id_b,
                "order_lines": [line["item"]["id"] for line in body.get("line_items", [])] == [B],
                "a_after": [status_a["stage"], status_a["order_id"]] == ["canceled", None],
                "merchant_checkouts": sorted(rows) == sorted([id_a, id_b])
                and [rows[id_a]["status"], rows[id_b]["status"]] == ["canceled", "completed"]
                and rows[id_b]["offer_id"] == B
                and rows[id_b]["quantity"] == 1,
                "complete_calls": completes(every) == 1,
                "orders_created": b0.rpc("effects")["orders"] - orders == 1,
                "psp_tokens_created": b0.payment_tokens() - tokens == 1,
                "approvals": approvals == [run_b],
            }
            facts.update(
                run=seen,
                summary=seen["summary"]
                | {
                    "approve_b": approved.status_code,
                    "complete_b": completed.status_code,
                    "a_after": [status_a["stage"], status_a["order_id"]],
                    "merchant_checkouts_after": public(list(rows.values())),
                    "merchant_calls_after": every,
                },
            )
        finally:
            agent.close()
            record["cleanup_verified"] = agent.stopped() and cleanup(b0, episode)
        verdict(record, checks)


@pytest.mark.parametrize(
    ("cell", "repetition"),
    plan(
        "UX08-HNP-control",
        "UX09-HNP-control",
        "UX09-HNP-rev-control",
        "UX10-HNP-control",
        "UX10-HNP-b",
    ),
)
def test_p0_hnp_buys(hnp, tmp_path, monkeypatch, p0_cases, cell, repetition):
    """The HNP controls and UX10-HNP-b: a valid run buys one unit of the consented variant."""
    case = CASES[cell]
    oracle = case["oracle"]
    model = scripted(case, monkeypatch)
    episode, inputs = hnp_episode(hnp, cell)
    with p0_case(p0_cases, hnp, tmp_path, cell, repetition, inputs) as record:
        facts = record["observations"]
        agent = ModelHnpAgent(hnp, episode, case["condition"], reviewer=case["reviewer"])
        try:
            agent.start()
            name = f"p0-{cell.lower()}"
            checks: dict[str, bool] = {}
            extra: dict[str, Any] = {}
            variant = PHONE
            if cell == "UX08-HNP-control":
                proposal_a = delegate(agent, A, name)
                proposal = delegate(agent, B, name)
                variant = B
                granted = consent(agent, proposal)
                checks["proposal_digests_differ"] = (
                    proposal_a["scope_digest"] != proposal["scope_digest"]
                )
            elif cell == "UX10-HNP-b":
                proposal = delegate(agent, PHONE, name, "Quiero dos unidades, una para mi hermana.")
                granted = consent(agent, proposal)
                again = consent(agent, proposal)
                extra["consents"] = [granted.status_code, again.status_code]
                checks["consents"] = extra["consents"] == oracle["consents"]
                checks["same_grant"] = again.json().get("grant_id") == granted.json()["grant_id"]
                checks["scope_uses"] = granted.json()["scope"].get("uses") == oracle["scope_uses"]
            else:
                proposal = delegate(agent, PHONE, name)
                granted = consent(agent, proposal)
            assert granted.status_code == 201, granted.text
            view = granted.json()
            if cell == "UX08-HNP-control":
                checks["consent_b"] = granted.status_code == oracle["consent_b"]
                checks["grant_scope"] = view["scope"] == proposal["scope"]
            seen = task_run(hnp, agent, model, case, view["grant_id"], view["task"]["task_id"])
            record["stimulus_verified"] = seen["answers"][:1] == case["script"][:1]
            rows = seen["merchant_checkouts"]
            checks |= {
                "trigger_accepted": seen["trigger_status"] == 202,
                "model_http_attempts": seen["model_http_attempts"] == oracle["model_http_attempts"],
                "task": seen["task"] == oracle["task"],
                "merchant_checkouts": [
                    (row["status"], row["offer_id"], row["quantity"]) for row in rows
                ]
                == [("completed", variant, 1)],
                "orders_created": seen["effects_delta"]["orders"] == oracle["orders_created"],
                "psp_tokens_created": seen["effects_delta"]["tokens"]
                == oracle["psp_tokens_created"],
            }
            if "model_requests" in oracle:
                checks["model_requests"] = seen["model_requests"] == oracle["model_requests"]
            if "checkout_posts" in oracle:
                posts = checkout_posts(seen["merchant_calls"])
                checks["checkout_posts"] = len(posts) == oracle["checkout_posts"] and all(
                    call[2] == oracle["checkout_post_status"] for call in posts
                )
            if "grant_state" in oracle:
                checks["grant_state"] = seen["grant"]["state"] == oracle["grant_state"]
            facts.update(run=seen, summary=hnp_summary(seen) | extra)
        finally:
            agent.close()
            record["cleanup_verified"] = not agent.local.thread.is_alive()
        verdict(record, checks)


# Summary ----------------------------------------------------------------------------------------


def summarize(cases: list[dict[str, Any]], stack: LiveStack) -> dict[str, Any]:
    """Per cell: repetitions passed, each oracle over the repetitions, whether the observations
    repeat exactly, and those of the first repetition. Counts and codes only."""
    payment = Path(os.environ[PAYMENT_REPO]).expanduser().resolve()
    sources = {
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
        "schema": "p0-ux08-ux10-summary/1",
        "protocol": {"path": str(PROTOCOL.relative_to(ROOT)), "sha256": sha256(PROTOCOL)},
        # What this run sent and checked apart from the frozen protocol: the router's first
        # answer in each HP script, and the attempt counts of a routed turn.
        "oracle_amendment": {
            "router_first": {
                cell: {"script": case["script"]}
                | {key: case["oracle"][key] for key in ROUTED_TURN_COUNTS if key in case["oracle"]}
                for cell, case in CASES.items()
                if case["mode"] == "HP"
            },
            "ux08_route_and_choose_attempts": ROUTE_AND_CHOOSE_ATTEMPTS,
        },
        "spec": {
            "path": PLAN["spec"]["path"],
            "sha256": sha256(spec),
            "matches_protocol": sha256(spec) == PLAN["spec"]["sha256"],
        },
        "execution_order": {
            "observed": order,
            "matches_protocol": order == PLAN["execution_order"],
        },
        "checkouts": sources,
        "checkouts_match_protocol": {
            "merchant": sources["merchant"]["head"] == PLAN["merchant_sha"],
            "payment": sources["payment"]["head"] == PLAN["payment_sha"],
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


# The live suite adds the cancel route only inside UX08-HP's cases.
assert HP_CANCEL not in PURCHASE_ROUTES
