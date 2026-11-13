"""A presente trial: the user talks to the Agent API, and the runner approves by rule (§2, §4).

The Agent is production `create_app` in this process, behind a TestClient, on the `b0` lane and
a new Merchant episode, with its own database in the trial's directory. Each turn posts the
whole conversation, as the chat does without a `conversation_id`. Up to six user turns, or until
the user ends the conversation.

A turn the API answers with an error ends the conversation where the chat shows that error. The
error's message is what the user reads, so it is the turn's assistant text, and the turn ends
in `respond`: the agent stopped without presenting a purchase. An error without a message ends
it with no action. A 502, 503 or 504 is also kept for the record's `infra_failure`, unless its
body names a model failure of the agent's kind (`model_failure`, suite/live/failures.py); a
bare 500 is an unhandled exception of the product. Both are graded as the agent's and kept in
the trace (observe.py).

Nobody else approves: when a turn presents a checkout, the runner rereads its terms, as the UI
does right before the click, and approves and completes it exactly when its one line is the
expected purchase's variant and its total the expected total; never otherwise, so a presented
purchase that the case does not expect stays pending.

After the click the chat tells the human what happened: the UI sends `purchase-updated`, and the
transport adds an assistant message with `purchase_update_text` over the purchase's stage and
order number (assistant_transport._purchase_updated). The runner adds the same notice to the
transcript, read through the API, marked `purchase_update`, also when the purchase was approved
but not completed. The user reads it; the agent's next turn does not carry it, because the UI
sends a `conversation_id` and the model's history then comes from the graph's checkpoint, where
the notice never goes (ConversationService._ingest).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from fastapi.testclient import TestClient

from commerce_agent.acp import load_product_feed
from commerce_agent.api import create_app
from commerce_agent.assistant_transport import purchase_update_text
from commerce_evaluation.stack.hnp_agent import EpisodeBuyer
from commerce_evaluation.stack.purchase_agent import buyer_details
from commerce_evaluation.stack.purchase_lane import PurchaseLane
from commerce_evaluation.suite.cases import Governance, PresentCase
from commerce_evaluation.suite.live import events, failures
from commerce_evaluation.suite.live.observe import (
    INFRA_STATUSES,
    Played,
    deliver_webhooks,
    flow_trace,
)
from commerce_evaluation.suite.live.reference import ReferenceRouter
from commerce_evaluation.suite.live.settings import Model, presente_agent
from commerce_evaluation.suite.trial import AGENT_FAILURES, Ending, Turn, TurnAction
from commerce_evaluation.suite.users import User

MAX_TURNS = 6
TURN = "/conversation/turn"
# How a transcript marks the chat's notice after the click (trial.Turn.source).
PURCHASE_UPDATE: Final = "purchase_update"


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
    model_failure: failures.Seen | None = None

    def call(self, response: Response, what: str) -> Any | None:
        """The answer's body, or None when the API answered an error, which is kept."""
        if response.status_code < 400:
            return response.json()
        body = _json(response)
        error = {"call": what, "status": response.status_code, "code": body.get("code")}
        error["message"] = body.get("message")
        kind = failures.kind_of(body.get("model_failure"))
        if kind is not None:
            error["model_failure"] = kind
        if response.status_code in INFRA_STATUSES:
            answer = f"{what} {response.status_code}"
            if kind is not None and self.model_failure is None:
                self.model_failure = failures.Seen(kind, answer)
            if kind not in AGENT_FAILURES:
                self.api_5xx.append(answer)
        self.errors.append(error)
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
        # What the agent's model reads, as in the chat: the runner's notices stay out.
        messages = [
            {"role": turn.role, "content": turn.text}
            for turn in talk.transcript
            if turn.source is None
        ]
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
            settled = settle(client, case, run_id, talk)
            talk.approvals.append(settled)
            if "update" in settled:
                text = settled["update"]["text"]
                talk.transcript.append(Turn(role="assistant", text=text, source=PURCHASE_UPDATE))
    return talk


def settle(client: Client, case: PresentCase, run_id: str, talk: Talk) -> dict[str, Any]:
    """The runner's rule on a presented checkout: approve and complete it, or leave it. Once
    approved, `update` is the notice the chat adds next (`notice`)."""
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
    if approved is None:
        return seen | {"approved": False, "completed": False}
    completed = talk.call(client.post(f"{path}/complete"), "POST /purchases/{run_id}/complete")
    settled = seen | {"approved": True, "completed": completed is not None}
    update = notice(client, run_id, completed, talk)
    return settled if update is None else settled | {"update": update}


def notice(
    client: Client, run_id: str, completed: dict[str, Any] | None, talk: Talk
) -> dict[str, Any] | None:
    """What the chat says after the click, as `AssistantTransport._purchase_updated` builds it:
    the purchase's view from the agent's database (`GET /purchases/{run_id}`, the row of
    `ConversationService.purchase_view`) and, once completed, the order number the receipt
    shows, from the completion's own answer (`ConversationService.order_number` reads the same
    answer stored). None when the view cannot be read."""
    status = talk.call(client.get(f"/purchases/{run_id}"), "GET /purchases/{run_id}")
    if status is None:
        return None
    view = {"run_id": run_id, "stage": status["stage"], "order_id": status["order_id"]}
    number = _order_number(completed) if view["stage"] == "completed" else None
    return {"view": view, "order_number": number, "text": purchase_update_text(view, number)}


def _order_number(completed: dict[str, Any] | None) -> str | None:
    order = completed.get("order") if completed is not None else None
    if not isinstance(order, dict):
        return None
    number = order.get("order_number") or order.get("id")
    return number if isinstance(number, str) else None


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
        model_failure=talk.model_failure,
    )


def _texts(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _json(response: Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
