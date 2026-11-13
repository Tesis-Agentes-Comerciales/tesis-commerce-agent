"""Each condition reaches the agent as explicit variables, and /health confirms it took."""

import json
import os
import secrets
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from commerce_agent.api import create_app
from commerce_agent.config import Settings
from commerce_agent.governance.bootstrap import build_enforce_gate, configured_d1
from commerce_agent.governance.evaluator import verify_artifact
from commerce_evaluation.agent_condition import (
    CONDITIONS,
    ConditionError,
    ConditionMismatchError,
    D1RunKeys,
    PilotCondition,
    agent_environment,
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


def test_the_v1_pilot_stays_pinned_to_its_published_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B2-PILOT-v1 was published with b2-pilot-v1; POL-11 (b2-hp-v2) would change its S02 path.
    environment = agent_environment(B2, MERCHANT, KEYS)
    start_with(environment | {"AGENT_DATABASE_PATH": str(tmp_path / "agent.db")}, monkeypatch)

    configured = configured_d1(Settings(_env_file=None), None)  # type: ignore[call-arg]

    assert environment["AGENT_D1_POLICY_SET_ID"] == "b2-pilot-v1"
    assert environment["AGENT_D1_POLICY_REVISION"] == "1"
    assert environment["AGENT_D1_KEY_ID"] == "b2-pilot-v1-run"
    assert configured.policy_binding is not None
    policy = verify_artifact(configured.policy_bytes, configured.policy_binding)
    assert policy.policy_ids == ("POL-01", "POL-02")


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
def test_a_health_check_of_another_condition_stops_the_run(health: dict[str, str]) -> None:
    with pytest.raises(ConditionMismatchError, match="condition B2 requires"):
        verify_agent_condition(B2, health)


def test_a_governed_condition_needs_its_d1_keys() -> None:
    with pytest.raises(ConditionError, match="Condition B2 requires D1 run keys"):
        agent_environment(B2, MERCHANT, None)


def test_the_d1_keys_never_show_in_their_repr() -> None:
    assert KEYS.hmac_hex not in repr(KEYS)
    assert KEYS.aes_hex not in repr(KEYS)
