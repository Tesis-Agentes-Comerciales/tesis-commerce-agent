"""The runner delivers each pilot condition to the agent and verifies it before every repetition."""

import json
import os
from pathlib import Path

import pytest

from commerce_agent.config import Settings
from commerce_agent.governance.bootstrap import build_enforce_gate
from commerce_evaluation.governance_pilot import (
    CONDITIONS,
    D1RunKeys,
    PilotCondition,
    agent_environment,
)
from commerce_evaluation.paths import DATA

MERCHANT = "http://127.0.0.1:4120"
KEYS = D1RunKeys.generate()
B0_PRIME, B2 = CONDITIONS["B0prime"], CONDITIONS["B2"]


def start_with(environment: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the runner's variables reach Settings, never the developer's shell."""
    for name in list(os.environ):
        if name.startswith("AGENT_"):
            monkeypatch.delenv(name)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("condition", [B0_PRIME, B2], ids=["B0prime", "B2"])
def test_every_condition_variable_is_an_agent_setting(condition: PilotCondition) -> None:
    unread = [
        name
        for name in agent_environment(condition, MERCHANT, KEYS)
        if name.lower() not in Settings.model_fields
    ]

    assert unread == []


def test_condition_variables_cover_the_pre_registered_manifest() -> None:
    manifest = json.loads((DATA / "b2-pilot-v1.json").read_text(encoding="utf-8"))

    for name, condition in manifest["conditions"].items():
        delivered = agent_environment(CONDITIONS[name], MERCHANT, KEYS)
        assert set(condition["agent_environment"]) <= set(delivered)


def test_b2_environment_starts_the_enforce_gate_with_the_governed_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = agent_environment(B2, MERCHANT, KEYS)
    start_with(environment | {"AGENT_DATABASE_PATH": str(tmp_path / "agent.db")}, monkeypatch)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.agent_governance_mode == "enforce"
    assert settings.agent_hp_flow == "governed"
    assert settings.agent_d1_allowed_merchant_origins == MERCHANT
    build_enforce_gate(settings, None)


@pytest.mark.parametrize("requested", ["B0prime", "B0"])
def test_b0_prime_stays_off_even_when_a_local_env_file_enables_governance(
    requested: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_env = tmp_path / ".env"
    local_env.write_text("AGENT_GOVERNANCE_MODE=enforce\nAGENT_HP_FLOW=governed\n", "utf-8")
    condition = CONDITIONS[requested]
    environment = agent_environment(condition, MERCHANT, KEYS)
    start_with(environment, monkeypatch)

    settings = Settings(_env_file=local_env)  # type: ignore[call-arg]

    assert condition.name == "B0prime"
    assert (settings.agent_governance_mode, settings.agent_hp_flow) == ("off", "blocked")
    assert not any(name.startswith("AGENT_D1_") for name in environment)
