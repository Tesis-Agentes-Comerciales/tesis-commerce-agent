"""The model boundary of the live stack: the real OpenAI SDK over `ModelServer`, never a provider.

`ModelServer` (tests/support/model_server.py) hands its scripted transport to the HTTP clients of
the test process. The Human Present Agent already runs there (`AgentServer` with
`ExplicitSettings`, as in live_browser.py and test_exp06_hp_infrastructure.py). `HnpAgent` does
not: it runs in a process of its own, refuses every OPENAI_* variable and picks with the
deterministic selector, so the scripted transport never reaches it (p0-model-failures.md §5.3).

`ModelHnpAgent` is that same HNP Agent served in the test process: the variables `HnpAgent`
would give its process (episode, condition, tokens, keys), read by `ExplicitSettings` with no
environment and no .env, plus `AGENT_HNP_SELECTOR=llm`, optionally `AGENT_HNP_REVIEWER=llm`, and
a synthetic key and model. The key and the model go only to those settings, never to an
environment variable. The runtime builds its model clients when the app is created, so a test
installs its `ModelServer` before building the agent; no request leaves the process.
"""

from typing import NoReturn

from commerce_agent.api import create_app
from tests.integration.live_browser import AgentServer, agent_settings
from tests.integration.live_purchase import Episode, EpisodeBuyer, HnpAgent, PurchaseLane

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
