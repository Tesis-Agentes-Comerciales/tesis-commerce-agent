"""The Agent's settings for one trial: the lane, the governance condition and the model.

Every value is explicit (`ExplicitSettings`): no developer .env and no variable of this process
reaches the Agent, so its condition is the one the trial records. The governance conditions map
as the pilots ran them (docs/design/eval-suite.md §4):

- presente, on the `b0` lane: B2 is `agent_environment` of condition B2 with the b2-hp-v2 policy
  (enforce, governed flow, POL-02 allowing the lane's Merchant); B0 is B0' (governance off).
- delegado, on the `hnp` lane: `hnp_condition` of B2-HNP (enforce) or B0-HNP (off).

With a model under test, the Agent runs on Claude with the run's model and effort (PR 8): its key
comes from this process's ANTHROPIC_API_KEY and goes to the Agent's settings alone, never to a
log, a trace or the record. In delegado the selector is the model's, and under B2 the intent
reviewer too, on the same model. Without one, the reference agent stands at the model boundary
(suite/live/reference.py) and no model is configured.
"""

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from commerce_agent.config import Settings
from commerce_evaluation.agent_condition import CONDITIONS, D1RunKeys, agent_environment
from commerce_evaluation.pilot_policy import HP_V2_POLICY
from commerce_evaluation.stack.hnp_agent import hnp_condition
from commerce_evaluation.stack.purchase_lane import SANDBOX_CLOCK, Episode, PurchaseLane, ap2_key
from commerce_evaluation.suite.cases import Governance

API_KEY = "ANTHROPIC_API_KEY"
# The trial's Agent database, in the trial's directory: its `model_requests` is the agent's usage.
AGENT_DATABASE = "agent.db"
# What the Agent needs to run on Claude (PR 8, docs/design/eval-suite.md §6).
CLAUDE_SETTINGS = frozenset({"anthropic_api_key", "agent_model", "agent_model_effort"})
# The Agent authenticates no buyer itself (`EpisodeBuyer`); jwt only satisfies AP2 and HNP.
AUTH = {"agent_auth_mode": "jwt", "agent_auth_issuer": "http://127.0.0.1:9"}


class ExplicitSettings(Settings):
    """The Agent's settings from the values given and nothing else."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


@dataclass(frozen=True)
class Model:
    """The model under test and its effort."""

    name: str
    effort: str


@dataclass(frozen=True)
class TrialAgent:
    """One trial's Agent settings and the tokens its callers present."""

    settings: Settings
    buyer_token: str = field(repr=False)
    service_token: str = field(repr=False, default="")

    @property
    def database(self) -> Path:
        return self.settings.agent_database_path

    def hidden(self) -> list[str]:
        """Every secret the trial handled, for the flow trace's sweep."""
        values = [self.buyer_token, self.service_token]
        for name in type(self.settings).model_fields:
            value = getattr(self.settings, name)
            if isinstance(value, SecretStr):
                values.append(value.get_secret_value())
            elif isinstance(value, str) and name.endswith(("_token", "_pem", "_hex")):
                values.append(value)
        return [value for value in values if value]


def claude_missing() -> list[str]:
    """Why the Agent cannot run on Claude here; empty when it can. Never prints values."""
    missing = sorted(CLAUDE_SETTINGS - set(Settings.model_fields))
    problems = [f"the Agent on Claude (Settings lacks {', '.join(missing)})"] if missing else []
    if not os.environ.get(API_KEY):
        problems.append(API_KEY)
    return problems


def presente_agent(
    lane: PurchaseLane, episode: Episode, governance: Governance, root: Path, model: Model | None
) -> TrialAgent:
    condition = CONDITIONS["B2" if governance == "B2" else "B0prime"]
    variables = agent_environment(condition, lane.origin, D1RunKeys.generate(), HP_V2_POLICY)
    assert lane.agent_key is not None
    values = _lowered(variables) | _base(lane, episode, root, lane.payment.origin)
    values.update(
        agent_ap2_required=True, agent_ap2_private_key_pem=lane.agent_key.get_secret_value()
    )
    values.update(_model(model))
    return TrialAgent(ExplicitSettings(**values), secrets.token_urlsafe(32))


def delegado_agent(
    lane: PurchaseLane, episode: Episode, governance: Governance, root: Path, model: Model | None
) -> TrialAgent:
    agent_key, _ = ap2_key()
    service = secrets.token_urlsafe(32)
    variables = hnp_condition(
        lane,
        f"{governance}-HNP",
        agent_key=agent_key,
        service=service,
        operator=secrets.token_urlsafe(32),
    )
    values = _lowered(variables) | _base(lane, episode, root, lane.payment_origin)
    if model is not None:
        values.update(_model(model), agent_hnp_selector="llm")
        if governance == "B2":
            # The reviewer runs in enforce only, on the agent's model.
            values.update(agent_hnp_reviewer="llm")
    return TrialAgent(ExplicitSettings(**values), secrets.token_urlsafe(32), service)


def _base(lane: PurchaseLane, episode: Episode, root: Path, payment: str) -> dict[str, Any]:
    return {
        "merchant_base_url": lane.origin,
        "merchant_bearer_token": episode.bearer.get_secret_value(),
        "merchant_webhook_secret": lane.webhook_secret.get_secret_value(),
        "agent_approval_secret": lane.approval_secret.get_secret_value(),
        "payment_provider_url": payment,
        "payment_agent_bearer_token": lane.payment_token.get_secret_value(),
        "agent_database_path": root / AGENT_DATABASE,
        "agent_feed_source": "file",
        "agent_feed_path": episode.feed,
        # The episode's offers are observed on its synthetic clock.
        "agent_sandbox_clock_at": SANDBOX_CLOCK,
        **AUTH,
    }


def _model(model: Model | None) -> dict[str, Any]:
    if model is None:
        return {}
    return {
        "anthropic_api_key": os.environ[API_KEY],
        "agent_model": model.name,
        "agent_model_effort": model.effort,
    }


def _lowered(variables: dict[str, str]) -> dict[str, Any]:
    return {name.lower(): value for name, value in variables.items()}
