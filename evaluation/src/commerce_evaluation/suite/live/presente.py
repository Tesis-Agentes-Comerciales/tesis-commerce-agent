"""A presente trial: the user talks to the Agent API, and the runner approves by rule (§2, §4).

The Agent is production `create_app` in this process, behind a TestClient, on the `b0` lane and
a new Merchant episode, with its own database in the trial's directory. Each turn posts the
whole conversation, as the chat does without a `conversation_id`. Up to six user turns, or until
the user ends the conversation.

A turn the API answers with an error ends the conversation where the chat shows that error. The
error's message is what the user reads, so it is the turn's assistant text, and the turn ends
in `respond`: the agent stopped without presenting a purchase. An error without a message ends
it with no action. A 502, 503 or 504 is also kept for the record's `infra_failure`; a bare 500
is an unhandled exception of the product, graded as the agent's and kept in the trace
(observe.py).

Nobody else approves: when a turn presents a checkout, the runner rereads its terms, as the UI
does right before the click, and approves and completes it exactly when its one line is the
expected purchase's variant and its total the expected total; never otherwise, so a presented
purchase that the case does not expect stays pending.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from fastapi.testclient import TestClient

from commerce_agent.acp import load_product_feed
from commerce_agent.api import create_app
from commerce_evaluation.stack.hnp_agent import EpisodeBuyer
from commerce_evaluation.stack.purchase_agent import buyer_details
from commerce_evaluation.stack.purchase_lane import PurchaseLane
from commerce_evaluation.suite.cases import Governance, PresentCase
from commerce_evaluation.suite.live import events
from commerce_evaluation.suite.live.observe import (
    INFRA_STATUSES,
    Played,
    deliver_webhooks,
    flow_trace,
)
from commerce_evaluation.suite.live.reference import ReferenceRouter
from commerce_evaluation.suite.live.settings import Model, presente_agent
from commerce_evaluation.suite.trial import Ending, Turn, TurnAction
from commerce_evaluation.suite.users import User

MAX_TURNS = 6
TURN = "/conversation/turn"


class Response(Protocol):
    status_code: int

    def json(self) -> Any: ...


class Client(Protocol):
    def get(self, url: str) -> Response: ...

    def post(self, url: str, *, json: Any = None) -> Response: ...


@dataclass
class Talk:
    """The conversation as it happened, the runner's approvals and the API's failures."""

    transcript: list[Turn] = field(default_factory=list)
    action: TurnAction | None = None
    run_ids: list[str] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    api_5xx: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def call(self, response: Response, what: str) -> Any | None:
        """The answer's body, or None when the API answered an error, which is kept."""
        if response.status_code < 400:
            return response.json()
        if response.status_code in INFRA_STATUSES:
            self.api_5xx.append(f"{what} {response.status_code}")
        body = _json(response)
        self.errors.append(
            {"call": what, "status": response.status_code, "code": body.get("code")}
            | {"message": body.get("message")}
        )
        return None


def converse(
    client: Client,
    user: User,
    case: PresentCase,
    details: dict[str, Any],
    max_turns: int = MAX_TURNS,
) -> Talk:
    talk = Talk()
    for index in range(1, max_turns + 1):
        text = user.next_message(tuple(talk.transcript))
        if text is None:
            break
        talk.transcript.append(Turn(role="user", text=text))
        messages = [{"role": turn.role, "content": turn.text} for turn in talk.transcript]
        body = talk.call(
            client.post(
                TURN, json={"turn_id": f"turn-{index}", "messages": messages, "details": details}
            ),
            f"POST {TURN}",
        )
        if body is None:
            message = talk.errors[-1]["message"]
            if isinstance(message, str) and message:
                talk.transcript.append(Turn(role="assistant", text=message))
            talk.action = "respond" if isinstance(message, str) and message else None
            break
        talk.transcript.append(Turn(role="assistant", text=body["assistant_message"]))
        talk.action = body["action"]
        if body["action"] == "purchase":
            run_id = body["purchase"]["run_id"]
            talk.run_ids.append(run_id)
            talk.approvals.append(settle(client, case, run_id, talk))
    return talk


def settle(client: Client, case: PresentCase, run_id: str, talk: Talk) -> dict[str, Any]:
    """The runner's rule on a presented checkout: approve and complete it, or leave it."""
    path = f"/purchases/{run_id}"
    terms = talk.call(client.get(f"{path}/approval"), "GET /purchases/{run_id}/approval")
    if terms is None:
        return {"run_id": run_id, "approved": False}
    lines = [(line["item_id"], line["quantity"]) for line in terms["lines"]]
    seen = {"run_id": run_id, "lines": lines, "total_minor": terms["total_minor"]}
    if not approvable(case, terms):
        return seen | {"approved": False}
    approval = {"expected_fingerprint": terms["fingerprint"]}
    if terms.get("checkout_jwt") is not None:
        approval["checkout_jwt"] = terms["checkout_jwt"]
    approved = talk.call(
        client.post(f"{path}/approval", json=approval), "POST /purchases/{run_id}/approval"
    )
    completed = approved and talk.call(
        client.post(f"{path}/complete"), "POST /purchases/{run_id}/complete"
    )
    return seen | {"approved": approved is not None, "completed": bool(completed)}


def approvable(case: PresentCase, terms: dict[str, Any]) -> bool:
    expected = case.expected.purchase
    if expected is None:
        return False
    lines = [(line["item_id"], line["quantity"]) for line in terms["lines"]]
    return lines == [(expected.variant, 1)] and terms["total_minor"] == expected.total_minor


def play_presente(
    lane: PurchaseLane,
    case: PresentCase,
    governance: Governance,
    root: Path,
    model: Model | None,
    user: User,
    name: str,
) -> Played:
    lane.proxy.reset()
    episode = lane.episode(name)
    applied = events.at_start(case, lane, episode)
    agent = presente_agent(lane, episode, governance, root, model)
    router = None if model is not None else ReferenceRouter(case, load_product_feed(episode.feed))
    app = create_app(
        settings=agent.settings,
        buyer_authenticator=EpisodeBuyer(episode.actor_id, agent.buyer_token),
        conversation_router=router,
    )
    details = buyer_details(name).model_dump(mode="json", exclude_none=True)
    headers = {"Authorization": f"Bearer {agent.buyer_token}"}
    with TestClient(app, raise_server_exceptions=False, headers=headers) as client:
        talk = converse(client, user, case, details)
    deliver_webhooks(lane, agent)
    trace = flow_trace("HP", agent, episode.feed)
    trace["suite"] = {
        "events": applied,
        "approvals": talk.approvals,
        "errors": talk.errors,
        "unsent": getattr(user, "unsent", None),
    }
    # The last purchase's run; without one, the last turn's run the trace names.
    run_ids = talk.run_ids or _texts(trace.get("run_ids"))
    return Played(
        agent=agent,
        episode=episode,
        run_id=run_ids[-1] if run_ids else episode.actor_id,
        transcript=tuple(talk.transcript),
        final=Ending(action=talk.action),
        trace=trace,
        api_5xx=tuple(talk.api_5xx),
    )


def _texts(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _json(response: Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
