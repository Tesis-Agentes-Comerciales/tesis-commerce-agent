"""The Human Not Present Agent of the `hnp` lane (docs/design/hnp-governed.md).

`HnpAgent` runs the Agent API with AGENT_HNP_ENABLED in a process of its own (`hnp_app` under
uvicorn), for one episode and one condition: B0-HNP (`off`) or B2-HNP (`enforce`).
"""

import hmac
import os
import secrets
import signal
import sqlite3
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from commerce_agent.api import create_app
from commerce_agent.buyer_auth import BuyerPrincipal
from commerce_agent.config import Settings
from commerce_evaluation.stack.environment import (
    AGENT,
    MODEL_PREFIXES,
    clean_environment,
)
from commerce_evaluation.stack.purchase_agent import buyer_details
from commerce_evaluation.stack.purchase_lane import SANDBOX_CLOCK, Episode, PurchaseLane, ap2_key
from commerce_evaluation.stack.service_server import ServiceServer

# The conditions of hnp-governed.md §11 the product runs, and their governance mode.
HNP_MODES = {"B0-HNP": "off", "B2-HNP": "enforce"}
HNP_TERMINAL_STATES = frozenset({"purchased", "escalated", "failed", "canceled", "uncertain"})
# The two seams of the HNP Agent process (`hnp_app`); harness variables, never product settings.
HNP_BUYER_ID = "LIVE_HNP_BUYER_ID"
HNP_BUYER_TOKEN = "LIVE_HNP_BUYER_TOKEN"
HNP_CLOCK_OFFSET = "LIVE_HNP_CLOCK_OFFSET_SECONDS"


class EpisodeBuyer:
    """The episode's buyer: a bearer token in this run's memory stands for the JWT that `auth/`
    would issue. The delegation routes still take the buyer from `current_buyer`."""

    def __init__(self, principal_id: str, token: str) -> None:
        self._principal = BuyerPrincipal(principal_id)
        self._token = token.encode()

    def authenticate(self, authorization: str | None) -> BuyerPrincipal | None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(token.encode(), self._token):
            return self._principal
        return None


def hnp_app() -> FastAPI:
    """The HNP Agent API of `HnpAgent`: production `create_app` over the variables the process
    was started with, never a developer .env. Two seams are the harness's: the episode's buyer
    (`EpisodeBuyer`) and an authority clock that may run ahead after a crash, so a re-claim finds
    the lease the killed run left lapsed (hnp-governed.md §4.5) without waiting its 120 s."""
    offset = timedelta(seconds=float(os.environ[HNP_CLOCK_OFFSET]))

    def authority_clock() -> datetime:
        return datetime.now(UTC) + offset

    return create_app(
        settings=Settings(_env_file=None),  # pyright: ignore[reportCallIssue]
        buyer_authenticator=EpisodeBuyer(os.environ[HNP_BUYER_ID], os.environ[HNP_BUYER_TOKEN]),
        authority_clock=authority_clock,
    )


def hnp_condition(
    lane: PurchaseLane, condition: str, *, agent_key: str, service: str, operator: str
) -> dict[str, str]:
    """The variables that make an Agent of the `hnp` lane the HNP Agent of `condition`: AP2,
    the Agent Key, the service and operator tokens, the deterministic selector and the D1 keys
    (hnp-governed.md §6 and §11). Shared by `HnpAgent` and the browser's Agent."""
    assert lane.profile == "hnp" and lane.agent_key is not None
    variables = {
        "AGENT_AP2_REQUIRED": "true",
        # The provider key the Merchant and the provider trust signs the open mandates.
        "AGENT_AP2_PRIVATE_KEY_PEM": lane.agent_key.get_secret_value(),
        "AGENT_HNP_ENABLED": "true",
        "AGENT_HNP_AGENT_KEY_PEM": agent_key,
        "AGENT_HNP_SERVICE_TOKEN": service,
        "AGENT_HNP_OPERATOR_TOKEN": operator,
        "AGENT_HNP_SELECTOR": "deterministic",
        "AGENT_D1_KEY_ID": "live-hnp",
        "AGENT_D1_HMAC_KEY_HEX": secrets.token_hex(32),
        "AGENT_D1_AES_KEY_HEX": secrets.token_hex(32),
        "AGENT_GOVERNANCE_MODE": HNP_MODES[condition],
    }
    if condition == "B2-HNP":
        # The HP policy set the enforce gate needs to start; HNP is evaluated with
        # b2-hnp-v1, built in code (§7), and POL-02 allows this lane's Merchant (K11).
        variables.update(
            AGENT_HP_FLOW="governed",
            AGENT_D1_POLICY_SET_ID="live-hnp-hp",
            AGENT_D1_POLICY_REVISION="1",
            AGENT_D1_ALLOWED_MERCHANT_ORIGINS=lane.origin,
        )
    return variables


def delegation_request(
    variant_ids: list[str], total_max_minor: int, *, name: str, city: str = "Bogotá"
) -> dict[str, Any]:
    """`POST /delegations/proposals` for the lane's synthetic buyer, delivered to `city`."""
    details = buyer_details(name)
    address = details.fulfillment.address.model_copy(update={"city": city})
    fulfillment = details.fulfillment.model_copy(update={"address": address})
    return {
        "variant_ids": variant_ids,
        "total_max_minor": total_max_minor,
        "currency": "COP",
        "valid_for_seconds": 3_600,
        "details": details.model_copy(update={"fulfillment": fulfillment}).model_dump(
            mode="json", exclude_none=True
        ),
        "preference_text": None,
    }


class HnpAgent:
    """The Agent API with AGENT_HNP_ENABLED for one episode of the `hnp` lane and one condition.

    It runs in a process of its own (`hnp_app` under uvicorn, from this checkout), so a test can
    kill it mid-run, and keeps its SQLite in the run directory. The buyer proposes and consents
    with its token; the service token triggers the task; the selector is the deterministic one
    and no model is configured (`clean_environment` drops every ANTHROPIC_* variable). With a model
    selector or reviewer the same Agent runs in the test process instead (`ModelHnpAgent` of
    tests/integration/live_model.py), where the scripted model transport reaches it.
    """

    def __init__(
        self, lane: PurchaseLane, episode: Episode, condition: str, **settings: str
    ) -> None:
        assert lane.profile == "hnp" and lane.agent_key is not None
        self.lane = lane
        self.episode = episode
        self.condition = condition
        self.root = lane.root / f"agent-{episode.actor_id}"
        self.root.mkdir()
        self.database = self.root / "agent.sqlite"
        self._buyer = secrets.token_urlsafe(32)
        self._service = secrets.token_urlsafe(32)
        self._operator = secrets.token_urlsafe(32)
        agent_key, _ = ap2_key()
        # The Agent Key that closes this episode's mandates (`cnf` of its open mandates).
        self.agent_key = SecretStr(agent_key)
        env = clean_environment()
        env.update(
            MERCHANT_BASE_URL=lane.origin,
            MERCHANT_BEARER_TOKEN=episode.bearer.get_secret_value(),
            MERCHANT_WEBHOOK_SECRET=lane.webhook_secret.get_secret_value(),
            AGENT_APPROVAL_SECRET=lane.approval_secret.get_secret_value(),
            PAYMENT_PROVIDER_URL=lane.payment_origin,
            PAYMENT_AGENT_BEARER_TOKEN=lane.payment_token.get_secret_value(),
            AGENT_DATABASE_PATH=str(self.database),
            AGENT_FEED_SOURCE="file",
            AGENT_FEED_PATH=str(episode.feed),
            # The offers of a B0 episode are observed on its synthetic clock; HNP authority
            # (grant, lease, mandates) runs on the real one (hnp-governed.md §3.4).
            AGENT_SANDBOX_CLOCK_AT=SANDBOX_CLOCK,
            # HNP requires jwt; `hnp_app` authenticates the buyer, so the issuer is never called.
            AGENT_AUTH_MODE="jwt",
            AGENT_AUTH_ISSUER="http://127.0.0.1:9",
            **{HNP_BUYER_ID: episode.actor_id, HNP_BUYER_TOKEN: self._buyer, HNP_CLOCK_OFFSET: "0"},
        )
        env.update(
            hnp_condition(
                lane, condition, agent_key=agent_key, service=self._service, operator=self._operator
            )
        )
        env.update({name.upper(): value for name, value in settings.items()})
        assert not any(name.startswith(MODEL_PREFIXES) for name in env)
        self.server = ServiceServer(
            self.root,
            "agent",
            AGENT,
            ("commerce_evaluation.stack.hnp_agent:hnp_app", "--factory"),
            env,
            "/health",
        )
        self.http = httpx.Client(trust_env=False, timeout=30)

    @property
    def origin(self) -> str:
        return self.server.origin

    def start(self) -> None:
        self.server.start()
        self.check_health()

    def check_health(self) -> None:
        health = self.http.get(self.origin + "/health").json()
        assert health["hnp_enabled"] is True and health["hnp_kill_switch"] is False
        assert health["governance_mode"] == HNP_MODES[self.condition]

    def crash(self) -> None:
        """SIGKILL to the whole process group, as a crash: no shutdown runs."""
        process = self.server.process
        assert process is not None
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=15)
        self.server.process = None

    def restart(self, *, clock_ahead_seconds: int = 0) -> None:
        self.server.env[HNP_CLOCK_OFFSET] = str(clock_ahead_seconds)
        self.start()

    def close(self) -> None:
        try:
            self.server.stop()
        finally:
            self.http.close()

    # The routes of hnp-governed.md §5.1 and §5.2.

    def _call(self, method: str, path: str, token: str, body: Any = None) -> httpx.Response:
        return self.http.request(
            method, self.origin + path, json=body, headers={"Authorization": f"Bearer {token}"}
        )

    def as_buyer(self, method: str, path: str, body: Any = None) -> httpx.Response:
        return self._call(method, path, self._buyer, body)

    def as_service(self, method: str, path: str) -> httpx.Response:
        return self._call(method, path, self._service)

    def as_operator(self, method: str, path: str, body: Any = None) -> httpx.Response:
        return self._call(method, path, self._operator, body)

    def delegate(self, request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Proposal and consent, as the buyer's two clicks: the proposal and the grant's view."""
        proposed = self.as_buyer("POST", "/delegations/proposals", request)
        assert proposed.status_code == 201, proposed.text
        proposal = proposed.json()
        consent = {"proposal_id": proposal["proposal_id"], "scope_digest": proposal["scope_digest"]}
        consented = self.as_buyer("POST", "/delegations", consent)
        assert consented.status_code == 201, consented.text
        return proposal, consented.json()

    def trigger(self, task_id: str) -> httpx.Response:
        return self.as_service("POST", f"/internal/hnp/tasks/{task_id}/run")

    def trigger_at_once(self, task_id: str, count: int = 2) -> list[httpx.Response]:
        """`count` triggers of the scheduler released together, each on its own connection."""
        barrier = threading.Barrier(count)

        def send() -> httpx.Response:
            with httpx.Client(trust_env=False, timeout=30) as client:
                barrier.wait(timeout=10)
                return client.post(
                    f"{self.origin}/internal/hnp/tasks/{task_id}/run",
                    headers={"Authorization": f"Bearer {self._service}"},
                )

        with ThreadPoolExecutor(count) as pool:
            return [future.result() for future in [pool.submit(send) for _ in range(count)]]

    def revoke(self, grant_id: str) -> httpx.Response:
        """The buyer's revocation on its own connection, so a proxy hook can send it mid-run."""
        with httpx.Client(trust_env=False, timeout=30) as client:
            return client.post(
                f"{self.origin}/delegations/{grant_id}/revoke",
                headers={"Authorization": f"Bearer {self._buyer}"},
            )

    def task(self, task_id: str) -> dict[str, Any]:
        response = self.as_service("GET", f"/internal/hnp/tasks/{task_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def finished(self, task_id: str, timeout: float = 90) -> dict[str, Any]:
        """The task once terminal; the run goes on in the Agent's background after the 202."""
        deadline = time.monotonic() + timeout
        while (task := self.task(task_id))["state"] not in HNP_TERMINAL_STATES:
            assert time.monotonic() < deadline, f"task still {task['state']}"
            time.sleep(0.2)
        return task

    def delegation(self, grant_id: str) -> dict[str, Any]:
        response = self.as_buyer("GET", f"/delegations/{grant_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def rows(self, query: str, *parameters: Any) -> list[tuple[Any, ...]]:
        """A read of the Agent's SQLite while it runs; WAL readers never block its writers."""
        with closing(sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)) as db:
            return db.execute(query, parameters).fetchall()


@contextmanager
def hnp_agent(lane: PurchaseLane, name: str, condition: str, **settings: str) -> Iterator[HnpAgent]:
    """A new episode of the lane and its HNP Agent, stopped and closed on the way out."""
    agent = HnpAgent(lane, lane.episode(name), condition, **settings)
    try:
        agent.start()
        yield agent
    finally:
        agent.close()
