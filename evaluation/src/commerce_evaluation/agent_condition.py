"""The condition an evaluation agent starts under (B0', B2) and the checks that it took.

`demo-stack` and the live suites start the agent with these pieces; they came from the October
pilot runners, which are reproduced from the `pilotos-octubre` tag.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from commerce_agent.config import Settings
from commerce_agent.delegation.bootstrap import load_hnp_trust
from commerce_agent.governance.bootstrap import configured_d1
from commerce_evaluation.pilot_policy import PILOT_V1_POLICY, D1Policy


class ConditionError(RuntimeError):
    """The agent cannot start under the requested condition."""


class ConditionMismatchError(ConditionError):
    """The agent runs another condition: the run stops instead of recording it."""


@dataclass(frozen=True)
class PilotCondition:
    name: str
    governance_mode: str
    hp_flow: str
    # The Merchant seed only labels runs B0, B1 or B2.
    merchant_variant: str


B0_PRIME = PilotCondition("B0prime", "off", "blocked", merchant_variant="B0")
# "B0" stays accepted: run on this commit it is B0', not the historical B0-2026-10-06.
CONDITIONS = {
    "B0prime": B0_PRIME,
    "B2": PilotCondition("B2", "enforce", "governed", merchant_variant="B2"),
    "B0": B0_PRIME,
}


@dataclass(frozen=True)
class D1RunKeys:
    """D1 keys of one run: kept in memory and given only to the agent."""

    hmac_hex: str = field(repr=False)
    aes_hex: str = field(repr=False)

    @classmethod
    def generate(cls) -> D1RunKeys:
        return cls(hmac_hex=secrets.token_hex(32), aes_hex=secrets.token_hex(32))


def ap2_private_key() -> str:
    """A fresh P-256 key in PKCS#8 PEM, for the agent's AP2 signatures."""
    return (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )


def agent_environment(
    condition: PilotCondition,
    merchant_origin: str,
    keys: D1RunKeys | None,
    policy: D1Policy = PILOT_V1_POLICY,
) -> dict[str, str]:
    """Agent variables that define the condition.

    All explicit, so the agent's local .env cannot change it: process variables take precedence.
    """
    environment = {
        "AGENT_GOVERNANCE_MODE": condition.governance_mode,
        "AGENT_HP_FLOW": condition.hp_flow,
    }
    if condition.governance_mode == "off":
        return environment
    if keys is None:
        raise ConditionError(f"Condition {condition.name} requires D1 run keys")
    return environment | {
        "AGENT_D1_ALLOWED_MERCHANT_ORIGINS": merchant_origin,
        "AGENT_D1_POLICY_SET_ID": policy.set_id,
        "AGENT_D1_POLICY_REVISION": policy.revision,
        "AGENT_D1_KEY_ID": policy.key_id,
        "AGENT_D1_HMAC_KEY_HEX": keys.hmac_hex,
        "AGENT_D1_AES_KEY_HEX": keys.aes_hex,
    }


def verify_agent_condition(condition: PilotCondition, health: dict[str, Any]) -> dict[str, str]:
    """Return the condition /health reports; stop the run if it is not the requested one."""
    expected = {"governance_mode": condition.governance_mode, "hp_flow": condition.hp_flow}
    reported = {name: health.get(name) for name in expected}
    if reported != expected:
        raise ConditionMismatchError(
            f"Agent /health reports {reported}, but condition {condition.name} requires {expected}"
        )
    return expected


class _DeclaredSettings(Settings):
    """Settings from the given values only: no process environment and no .env file."""

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


def startup_check(environment: Mapping[str, str], stack: Mapping[str, str]) -> dict[str, Any]:
    """Whether the agent's HNP startup accepts the condition's variables (`load_hnp_trust`, and
    `configured_d1` in enforce). The result never carries a setting's value."""
    values = {name.lower(): value for name, value in {**stack, **environment}.items()}
    if environment.get("AGENT_HNP_SELECTOR") == "llm":
        # The agent reads its own key at startup; the check only needs one to be present.
        values.setdefault("anthropic_api_key", "dry-run-placeholder")
    try:
        settings = _DeclaredSettings.model_validate(values)
        load_hnp_trust(settings)
        if settings.agent_governance_mode == "enforce":
            configured_d1(settings, None)
    except ValidationError as error:
        messages = [item["msg"] for item in error.errors(include_url=False, include_input=False)]
        return {"ok": False, "error": "; ".join(messages)}
    except Exception as error:
        # The startup's own errors never echo a setting (delegation/bootstrap.py).
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}
    return {"ok": True}
