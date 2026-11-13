"""A delegado trial: the owner delegates, the scheduler triggers the task, nobody talks (§2, §4).

The Agent is production `create_app` with HNP enabled, in this process behind a TestClient, on
the `hnp` lane and a new Merchant episode, with its own database in the trial's directory. The
runner proposes the case's scope for the synthetic Bogotá buyer, consents to its exact digest,
applies the `after_consent` events and triggers the task as the scheduler does; the TestClient
returns once the background run ended. The task's final message (its `HNP_MESSAGES` text, or
the escalation's) is the trial's one assistant turn.

The selector is the reference's or the model's, behind `AtSelection`, which applies the case's
`on_selection` events before the first choice: the owner revokes while the agent selects.
"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from commerce_agent.api import create_app
from commerce_agent.application.hnp_selection import HnpSelector
from commerce_agent.domain import PurchaseIntent, RankedCandidate
from commerce_evaluation.stack.hnp_agent import (
    HNP_TERMINAL_STATES,
    EpisodeBuyer,
    delegation_request,
)
from commerce_evaluation.stack.purchase_lane import PurchaseLane
from commerce_evaluation.suite.cases import DelegatedCase, Delegation, Governance
from commerce_evaluation.suite.live import events
from commerce_evaluation.suite.live.observe import (
    INFRA_STATUSES,
    Played,
    deliver_webhooks,
    flow_trace,
)
from commerce_evaluation.suite.live.reference import ReferenceSelector
from commerce_evaluation.suite.live.settings import Model, TrialAgent, delegado_agent
from commerce_evaluation.suite.trial import Ending, Turn

# The task runs within the trigger's request; this only bounds a run left in the background.
TASK_SECONDS = 120


class Stopped(Exception):
    """The Agent API answered 5xx: the trial stops there. A 502, 503 or 504 is infrastructure;
    a bare 500 is the product's unhandled exception, graded as the agent's (observe.py)."""


class DelegationRefused(ValueError):
    """The Agent refused the case's scope: the case or the harness is wrong, not the agent."""


class Owner:
    """The owner's and the scheduler's calls on the Agent API, and what they answered."""

    def __init__(self, agent: TrialAgent, case_id: str):
        self.agent = agent
        self.case_id = case_id
        self.client: TestClient | None = None
        self.grant_id: str | None = None
        self.task_id: str | None = None
        self.task: dict[str, Any] = {}
        self.grants: list[str] = []
        self.revocations: list[dict[str, Any]] = []
        self.api_5xx: list[str] = []
        # Every other error answer, for the trace.
        self.errors: list[dict[str, Any]] = []
        # Why the task was still running when the wait ended, if it was.
        self.unfinished: str | None = None

    def _call(self, route: str, path: str, token: str, body: Any = None) -> Any:
        """`route` is "METHOD template", the call's name in a failure's detail."""
        assert self.client is not None
        method = route.split(" ", 1)[0]
        response = self.client.request(
            method, path, json=body, headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code in INFRA_STATUSES:
            self.api_5xx.append(f"{route} {response.status_code}")
            raise Stopped
        if response.status_code >= 500:
            self._error(route, response)
            raise Stopped
        return response

    def _error(self, route: str, response: Any) -> None:
        try:
            body = response.json()
        except ValueError:
            body = {}
        code = body.get("code") if isinstance(body, dict) else None
        self.errors.append({"call": route, "status": response.status_code, "code": code})

    def delegate(self, scope: Delegation, name: str) -> None:
        """Proposal and consent, as the owner's two clicks."""
        token = self.agent.buyer_token
        proposed = self._call(
            "POST /delegations/proposals", "/delegations/proposals", token, request(scope, name)
        )
        if proposed.status_code != 201:
            raise DelegationRefused(f"{self.case_id}: the proposal answered {proposed.status_code}")
        proposal = proposed.json()
        if differences := scope_differences(scope, proposal["scope"]):
            raise DelegationRefused(
                f"{self.case_id}: the proposal's scope differs from the case's in "
                + ", ".join(differences)
            )
        consent = {"proposal_id": proposal["proposal_id"], "scope_digest": proposal["scope_digest"]}
        consented = self._call("POST /delegations", "/delegations", token, consent)
        if consented.status_code != 201:
            raise DelegationRefused(f"{self.case_id}: the consent answered {consented.status_code}")
        view = consented.json()
        self.grant_id, self.task_id = view["grant_id"], view["task"]["task_id"]
        self.task = view["task"]
        self.grants.append(view["grant_id"])

    def revoke(self) -> None:
        response = self._call(
            "POST /delegations/{grant_id}/revoke",
            f"/delegations/{self.grant_id}/revoke",
            self.agent.buyer_token,
        )
        self.revocations.append(
            {"status": response.status_code, "effect": response.json().get("effect")}
        )

    def run(self) -> None:
        """The scheduler's trigger, then the task once terminal."""
        token = self.agent.service_token
        task = f"/internal/hnp/tasks/{self.task_id}"
        route = "POST /internal/hnp/tasks/{task_id}/run"
        triggered = self._call(route, f"{task}/run", token)
        if triggered.status_code not in (200, 202):
            # Not run (a task not runnable, for one): the trace says why, the task its state.
            self._error(route, triggered)
            self.task = self._call("GET /internal/hnp/tasks/{task_id}", task, token).json()
            return
        deadline = time.monotonic() + TASK_SECONDS
        while True:
            self.task = self._call("GET /internal/hnp/tasks/{task_id}", task, token).json()
            if self.task["state"] in HNP_TERMINAL_STATES:
                return
            if time.monotonic() > deadline:
                state = self.task["state"]
                self.unfinished = f"la tarea HNP seguía en {state} tras {TASK_SECONDS} s"
                return
            time.sleep(0.2)

    def message(self) -> str | None:
        """What the owner reads at the end: the escalation's text, or the task's."""
        path = f"/delegations/{self.grant_id}"
        view = self._call("GET /delegations/{grant_id}", path, self.agent.buyer_token).json()
        return (view.get("escalation") or {}).get("message") or view["task"].get("message")


class AtSelection:
    """The Agent's selector, with `before` applied once, right before its first choice."""

    def __init__(self, inner: HnpSelector, before: Callable[[], None]):
        self._inner, self._before, self._done = inner, before, False

    def choose(
        self,
        shortlist: tuple[RankedCandidate, ...],
        intent: PurchaseIntent,
        preference_text: str | None,
    ) -> str:
        if not self._done:
            self._done = True
            self._before()
        return self._inner.choose(shortlist, intent, preference_text)


def request(scope: Delegation, name: str) -> dict[str, Any]:
    """`POST /delegations/proposals` with the case's scope, for the lane's synthetic buyer."""
    body = delegation_request(list(scope.variant_ids), scope.total_max_minor, name=name)
    body.update(
        currency=scope.currency,
        valid_for_seconds=scope.valid_for_seconds,
        preference_text=scope.preference_text,
    )
    if scope.priority is not None:
        body["priority"] = list(scope.priority)
    return body


def scope_differences(scope: Delegation, proposed: dict[str, Any]) -> list[str]:
    """What of the case's scope the proposal's card does not show as asked, before the owner
    consents to it: its variants, total, currency, preference text and priority."""
    total = proposed["total_max"]
    shown = {
        "variant_ids": sorted(variant["variant_id"] for variant in proposed["variants"]),
        "total_max_minor": total["minor"],
        "currency": total["currency"].upper(),
        "preference_text": proposed.get("preference_text"),
        "priority": tuple(proposed["priority"]) if proposed.get("priority") else None,
    }
    asked = {
        "variant_ids": sorted(scope.variant_ids),
        "total_max_minor": scope.total_max_minor,
        "currency": scope.currency.upper(),
        "preference_text": scope.preference_text,
        "priority": tuple(scope.priority) if scope.priority else None,
    }
    return [name for name in asked if asked[name] != shown[name]]


def play_delegado(
    lane: PurchaseLane,
    case: DelegatedCase,
    governance: Governance,
    root: Path,
    model: Model | None,
    name: str,
) -> Played:
    assert lane.psp_proxy is not None
    lane.proxy.reset()
    lane.psp_proxy.reset()
    episode = lane.episode(name)
    applied = events.at_start(case, lane, episode)
    agent = delegado_agent(lane, episode, governance, root, model)
    owner = Owner(agent, case.id)

    def at_selection() -> None:
        for event in events.at(case, "on_selection"):
            owner.revoke()
            applied.append(events.described(event))

    def wrap(built: HnpSelector) -> HnpSelector:
        return AtSelection(built if model is not None else ReferenceSelector(case), at_selection)

    app = create_app(
        settings=agent.settings,
        buyer_authenticator=EpisodeBuyer(episode.actor_id, agent.buyer_token),
        wrap_hnp_selector=wrap,
    )
    message = None
    with TestClient(app, raise_server_exceptions=False) as client:
        owner.client = client
        try:
            owner.delegate(case.delegation, name)
            for event in events.at(case, "after_consent"):
                if event.kind == "delegation_replaced":
                    assert event.replacement is not None
                    owner.revoke()
                    owner.delegate(event.replacement, name)
                else:
                    events.offer(case, lane, episode, event, feed=False)
                applied.append(events.described(event))
            owner.run()
            message = owner.message()
        except Stopped:
            pass
    deliver_webhooks(lane, agent)
    trace = flow_trace("HNP", agent, episode.feed)
    trace["suite"] = {
        "events": applied,
        "grants": owner.grants,
        "revocations": owner.revocations,
        "errors": owner.errors,
    }
    task = owner.task
    return Played(
        agent=agent,
        episode=episode,
        run_id=task.get("run_id") or f"task-{owner.task_id or episode.actor_id}",
        transcript=(Turn(role="assistant", text=message),) if message else (),
        final=Ending(task_state=task.get("state"), outcome_code=task.get("outcome_code")),
        trace=trace,
        api_5xx=tuple(owner.api_5xx),
        unfinished=owner.unfinished,
    )
