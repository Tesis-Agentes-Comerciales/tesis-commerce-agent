"""The model boundary of the live stack: the real OpenAI SDK over `ModelServer`, never a provider.

`ModelServer` (tests/support/model_server.py) hands its scripted transport to the HTTP clients of
the test process. The Human Present Agent already runs there (`AgentServer` with
`ExplicitSettings`, as in live_browser.py and test_exp05_authenticated_ux.py). `HnpAgent` does
not: it runs in a process of its own, refuses every OPENAI_* variable and picks with the
deterministic selector, so the scripted transport never reaches it (p0-model-failures.md §5.3).

`ModelHnpAgent` is that same HNP Agent served in the test process: the variables `HnpAgent`
would give its process (episode, condition, tokens, keys), read by `ExplicitSettings` with no
environment and no .env, plus `AGENT_HNP_SELECTOR=llm`, optionally `AGENT_HNP_REVIEWER=llm`, and
a synthetic key and model. The key and the model go only to those settings, never to an
environment variable. The runtime builds its model clients when the app is created, so a test
installs its `ModelServer` before building the agent; no request leaves the process.

`ScriptedModel` is the `ModelServer` of the P0 protocols: it serves a script of answer names
from the answers its module defines (p0-model-failures.md §5.3, p0-ux08-ux10.md §5.3).
"""

import json
from collections.abc import Mapping
from typing import Any, NoReturn

import httpx

from commerce_agent.api import create_app
from commerce_agent.llm.model_failures import NO_RETRY_HEADER
from commerce_evaluation.stack.hnp_agent import EpisodeBuyer, HnpAgent
from commerce_evaluation.stack.purchase_lane import Episode, PurchaseLane
from tests.integration.live_browser import agent_settings
from tests.support.agent_server import AgentServer
from tests.support.model_server import Answer, ModelServer

# Synthetic: only the scripted transport ever sees it.
MODEL = "p0-scripted-model"
MODEL_SETTINGS = {
    "OPENAI_API_KEY": "sk-p0-scripted-transport-only-0123456789",
    "OPENAI_MODEL": MODEL,
}


class ModelHnpAgent(HnpAgent):
    """`HnpAgent` with the model selector (and reviewer), in this process, behind `AgentServer`.

    The routes, the tokens, the SQLite reads and the health check are `HnpAgent`'s. It cannot be
    killed or restarted: the crash cases keep the process of `HnpAgent`.
    """

    def __init__(
        self,
        lane: PurchaseLane,
        episode: Episode,
        condition: str,
        *,
        reviewer: bool = False,
        **settings: str,
    ) -> None:
        model = {"agent_hnp_selector": "llm", "agent_hnp_reviewer": "llm" if reviewer else "off"}
        super().__init__(lane, episode, condition, **(model | settings))
        self.settings = agent_settings(self.server.env | MODEL_SETTINGS)
        assert self.settings.agent_hnp_selector == "llm"
        assert self.settings.agent_hnp_reviewer == ("llm" if reviewer else "off")
        app = create_app(
            settings=self.settings,
            buyer_authenticator=EpisodeBuyer(episode.actor_id, self._buyer),
        )
        self.local = AgentServer(app)

    @property
    def origin(self) -> str:
        return self.local.origin

    def start(self) -> None:
        self.local.start()
        self.check_health()

    def crash(self) -> NoReturn:
        raise NotImplementedError("An in-process Agent cannot be killed; use HnpAgent")

    def restart(self, *, clock_ahead_seconds: int = 0) -> NoReturn:
        raise NotImplementedError("An in-process Agent cannot be restarted; use HnpAgent")

    def close(self) -> None:
        try:
            self.local.stop()
        finally:
            self.http.close()


class ScriptedModel(ModelServer):
    """`ModelServer` serving a named script of the protocol whose last answer persists: an attempt
    beyond the oracle gets the same stimulus and is counted, instead of breaking the double. Each
    served response keeps its `x-should-retry` as scripted, before the Agent's hooks see it.
    `restart` starts the script again: the protocol runs it per surface (HP) or per task run.
    `answers` are the protocol's stimuli by name; a held one (`held`) keeps its own state."""

    def __init__(self, script: list[str], answers: Mapping[str, Answer]) -> None:
        super().__init__()
        assert set(script) <= set(answers), set(script) - set(answers)
        self.script = script
        self.answers = answers
        self.served: list[tuple[str, httpx.Response | None, str | None]] = []
        self.origin = 0

    def restart(self) -> None:
        self.origin = len(self.requests)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        name = self.script[min(len(self.requests) - self.origin, len(self.script)) - 1]
        try:
            response = self.answers[name](json.loads(request.content))
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
