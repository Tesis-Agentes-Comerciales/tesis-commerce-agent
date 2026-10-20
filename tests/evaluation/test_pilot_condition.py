"""The runner delivers each pilot condition to the agent and verifies it before every repetition."""

import json
import os
import secrets
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from commerce_agent.api import create_app
from commerce_agent.config import Settings
from commerce_agent.governance.bootstrap import build_enforce_gate
from commerce_evaluation.governance_pilot import (
    CONDITIONS,
    ConditionMismatchError,
    D1RunKeys,
    PilotCondition,
    agent_environment,
    agent_log_tail,
    verify_agent_condition,
)
from commerce_evaluation.paths import DATA

MERCHANT = "http://127.0.0.1:4120"
FEED = Path(__file__).parents[1] / "fixtures" / "feed"
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


@pytest.mark.parametrize("condition", [B0_PRIME, B2], ids=["B0prime", "B2"])
def test_agent_started_with_the_condition_passes_the_health_check(
    condition: PilotCondition, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner_secrets = {
        name: secrets.token_urlsafe(32)
        for name in (
            "MERCHANT_BEARER_TOKEN",
            "MERCHANT_WEBHOOK_SECRET",
            "AGENT_APPROVAL_SECRET",
            "PAYMENT_AGENT_BEARER_TOKEN",
        )
    }
    runner_variables = {
        "MERCHANT_BASE_URL": MERCHANT,
        "AGENT_DATABASE_PATH": str(tmp_path / "agent.db"),
        "AGENT_FEED_SOURCE": "file",
        "AGENT_FEED_PATH": str(FEED),
    }
    start_with(
        agent_environment(condition, MERCHANT, KEYS) | runner_secrets | runner_variables,
        monkeypatch,
    )

    def no_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"/health must not call {request.url}")

    app = create_app(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        transport=httpx.MockTransport(no_network),
        payment_transport=httpx.MockTransport(no_network),
    )
    with TestClient(app) as client:
        health = client.get("/health").json()

    assert verify_agent_condition(condition, health) == {
        "governance_mode": condition.governance_mode,
        "hp_flow": condition.hp_flow,
    }


@pytest.mark.parametrize(
    "health",
    [
        {"status": "ok", "governance_mode": "off", "hp_flow": "blocked"},
        {"status": "ok", "governance_mode": "enforce", "hp_flow": "blocked"},
        {"status": "ok"},
    ],
    ids=["b0-prime-agent", "blocked-flow", "agent-without-condition"],
)
def test_runner_aborts_when_health_reports_another_condition(health: dict[str, str]) -> None:
    with pytest.raises(ConditionMismatchError, match="condition B2 requires"):
        verify_agent_condition(B2, health)


def test_agent_log_tail_never_shows_the_d1_keys(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    key_line = f"\nloaded {KEYS.aes_hex}\n"
    # The last 4000 characters start in the middle of the HMAC key.
    log.write_text(KEYS.hmac_hex + "." * (4000 - 32 - len(key_line)) + key_line, "utf-8")

    tail = agent_log_tail(log, agent_environment(B2, MERCHANT, KEYS))

    assert KEYS.hmac_hex[-32:] not in tail
    assert KEYS.aes_hex not in tail
    assert "[redacted]" in tail
    assert KEYS.hmac_hex not in repr(KEYS)
