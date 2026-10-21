"""`hnp_pilot`: manifest, plan, environment and startup (docs/design/hnp-pilot.md, HNP-E2).

Nothing here starts a service or calls a model.
"""

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation import hnp_pilot
from commerce_evaluation.governance_pilot import _ap2_private_key
from commerce_evaluation.hnp_pilot import (
    CONDITIONS,
    LEDGER_CONDITIONS,
    InvocationSecrets,
    PlannedEpisode,
    build_plan,
    condition_environment,
    condition_order,
    episode_counts,
    load_pilot,
    stack_variables,
    startup_check,
    validate_pilot,
)

MERCHANT_ORIGIN = "http://127.0.0.1:4120"
STACK = stack_variables(merchant_origin=MERCHANT_ORIGIN, auth_origin="http://127.0.0.1:4215")


@pytest.fixture(scope="module")
def pilot() -> tuple[dict[str, Any], dict[str, Any]]:
    return load_pilot()


@pytest.fixture(scope="module")
def invocation() -> InvocationSecrets:
    return InvocationSecrets.generate()


@pytest.fixture(scope="module")
def provider_key() -> str:
    return _ap2_private_key()


@pytest.fixture(autouse=True)
def nothing_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test of the runner may start a process but `git` (the commit it records)."""
    popen = subprocess.Popen

    def only_git(command: list[str], *args: Any, **kwargs: Any) -> Any:
        if command[0] != "git":
            raise AssertionError(f"hnp-pilot tried to start {command[0]}")
        return popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", only_git)


def scenario(matrix: dict[str, Any], scenario_id: str) -> dict[str, Any]:
    return next(item for item in matrix["scenarios"] if item["id"] == scenario_id)


def keys(plan: list[PlannedEpisode], scenario_id: str) -> list[tuple[int, str]]:
    return [(item.repetition, item.condition) for item in plan if item.scenario_id == scenario_id]


# Manifest and plan -------------------------------------------------------------------------------


def test_the_registered_manifest_and_matrix_agree(pilot: tuple[dict, dict]) -> None:
    manifest, matrix = pilot
    assert validate_pilot(manifest, matrix) == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda m: m["scenarios"][0]["runs"].update({"B2-HNP": 2}), "HN01: runs"),
        (lambda m: m["scenarios"][0]["delegation"].update({"details": "elsewhere"}), "details"),
        (lambda m: m["scenarios"][0].update({"selector": {"kind": "scripted_adversarial"}}), "2 "),
        (lambda m: m["scenarios"][3]["stimuli"][0].update({"action": "reboot"}), "stimulus"),
        (lambda m: m["scenarios"][6]["trigger"].update({"mode": "thrice"}), "trigger"),
        (lambda m: m["scenarios"][0]["oracle"]["P"].update({"run": True}), "P oracle"),
    ],
)
def test_validation_names_each_departure_from_the_manifest(
    pilot: tuple[dict, dict], change: Any, problem: str
) -> None:
    manifest, matrix = pilot
    changed = copy.deepcopy(matrix)
    change(changed)
    assert any(problem in item for item in validate_pilot(manifest, changed))


def test_the_plan_has_the_registered_episodes(pilot: tuple[dict, dict]) -> None:
    manifest, matrix = pilot
    plan = build_plan(manifest, matrix, p_available=True)
    counts = episode_counts(plan)
    registered = manifest["episodes"]
    assert counts["by_condition"] == registered["by_condition"]
    assert counts["by_layer"] == registered["by_layer"]
    assert counts["total"] == registered["total"] == 133


def test_without_e3_the_p_cells_keep_their_place_but_do_not_run(pilot: tuple[dict, dict]) -> None:
    manifest, matrix = pilot
    plan = build_plan(manifest, matrix, p_available=False)
    with_p = build_plan(manifest, matrix, p_available=True)
    assert [item.key() for item in plan] == [item.key() for item in with_p]
    assert {item.condition for item in plan if not item.runs} == {"P"}
    assert episode_counts([item for item in plan if item.runs])["total"] == 108


def test_layer_a_runs_before_layer_b_by_scenario_and_repetition(pilot: tuple[dict, dict]) -> None:
    manifest, matrix = pilot
    plan = build_plan(manifest, matrix, p_available=True)
    layers = [item.layer for item in plan]
    assert layers == sorted(layers)
    order = list(dict.fromkeys(item.scenario_id for item in plan))
    assert order == [*manifest["layers"]["A"]["scenarios"], *manifest["layers"]["B"]["scenarios"]]
    for scenario_id in order:
        repetitions = [repetition for repetition, _ in keys(plan, scenario_id)]
        assert repetitions == sorted(repetitions)


def test_the_first_condition_rotates_with_p_and_alternates_without_it(
    pilot: tuple[dict, dict],
) -> None:
    manifest, matrix = pilot
    plan = build_plan(manifest, matrix, p_available=True)
    assert keys(plan, "HN04") == [
        (1, "B0-HNP"),
        (1, "B2-HNP"),
        (1, "P"),
        (2, "B2-HNP"),
        (2, "P"),
        (2, "B0-HNP"),
        (3, "P"),
        (3, "B0-HNP"),
        (3, "B2-HNP"),
    ]
    assert keys(plan, "HN01") == [
        (1, "B0-HNP"),
        (1, "B2-HNP"),
        (2, "B2-HNP"),
        (2, "B0-HNP"),
        (3, "B0-HNP"),
        (3, "B2-HNP"),
    ]
    assert len(keys(plan, "HN07")) == 30 and len(keys(plan, "HN10")) == 20
    assert condition_order(4, CONDITIONS) == list(CONDITIONS)


def test_a_smoke_runs_one_repetition_per_cell(pilot: tuple[dict, dict]) -> None:
    manifest, matrix = pilot
    smoke = build_plan(manifest, matrix, p_available=False, smoke=True)
    assert {item.repetition for item in smoke} == {1}
    assert len(smoke) == 9 * 2 + 6 + 2 * 2  # layer A with P in six cells, layer B
    only = build_plan(manifest, matrix, p_available=False, scenario_ids=["HN05"])
    assert {item.scenario_id for item in only} == {"HN05"} and len(only) == 9


# Environment and startup -------------------------------------------------------------------------


def test_the_conditions_get_the_manifest_s_variables_resolved(
    pilot: tuple[dict, dict], invocation: InvocationSecrets, provider_key: str
) -> None:
    manifest, matrix = pilot

    def environment(condition: str, layer: str, scenario_id: str) -> dict[str, str]:
        return condition_environment(
            manifest,
            condition,
            layer=layer,
            scenario=scenario(matrix, scenario_id),
            invocation=invocation,
            provider_key=provider_key,
            merchant_origin=MERCHANT_ORIGIN,
        )

    b0 = environment("B0-HNP", "A", "HN08")
    assert b0["AGENT_GOVERNANCE_MODE"] == "off" and b0["AGENT_HNP_SELECTOR"] == "deterministic"
    assert b0["AGENT_HNP_ENABLED"] == "true" and b0["AGENT_AUTH_MODE"] == "jwt"
    assert "AGENT_D1_ALLOWED_MERCHANT_ORIGINS" not in b0 and "OPENAI_MODEL" not in b0
    assert b0["AGENT_HNP_SERVICE_TOKEN"] == invocation.service_token
    assert b0["AGENT_HNP_OPERATOR_TOKEN"] == invocation.operator_token
    assert b0["AGENT_HNP_AGENT_KEY_PEM"] == invocation.agent_key_pem != provider_key
    assert b0["AGENT_AP2_PRIVATE_KEY_PEM"] == provider_key
    # A startup addition: load_hnp_trust needs the vault key the manifest leaves out.
    assert b0["AGENT_D1_AES_KEY_HEX"] == invocation.d1_aes_hex

    b2 = environment("B2-HNP", "A", "HN08")
    assert (b2["AGENT_GOVERNANCE_MODE"], b2["AGENT_HP_FLOW"]) == ("enforce", "governed")
    assert b2["AGENT_D1_ALLOWED_MERCHANT_ORIGINS"] == "https://comercio-autorizado.example"
    assert environment("B2-HNP", "A", "HN01")["AGENT_D1_ALLOWED_MERCHANT_ORIGINS"] == (
        MERCHANT_ORIGIN
    )
    assert b2["AGENT_D1_KEY_ID"] == "hnp-pilot-v1-run"
    assert (b2["AGENT_D1_POLICY_SET_ID"], b2["AGENT_D1_POLICY_REVISION"]) == ("b2-hp-v2", "1")

    layer_b = environment("B2-HNP", "B", "HN10")
    assert layer_b["AGENT_HNP_SELECTOR"] == "llm"
    assert (layer_b["OPENAI_MODEL"], layer_b["OPENAI_TIMEOUT_SECONDS"]) == ("gpt-5-mini", "90")
    assert "OPENAI_API_KEY" not in layer_b


def test_tokens_are_distinct_long_and_generated_per_invocation() -> None:
    first, second = InvocationSecrets.generate(), InvocationSecrets.generate()
    assert len(first.service_token) >= 32 and len(first.operator_token) >= 32
    assert first.service_token != first.operator_token
    assert set(first.values()).isdisjoint(second.values())
    assert first.service_token not in repr(first)


def test_the_agent_starts_with_each_condition_and_selector(
    pilot: tuple[dict, dict], invocation: InvocationSecrets, provider_key: str
) -> None:
    manifest, matrix = pilot
    for condition in LEDGER_CONDITIONS:
        for layer in ("A", "B"):
            environment = condition_environment(
                manifest,
                condition,
                layer=layer,
                scenario=matrix["scenarios"][0],
                invocation=invocation,
                provider_key=provider_key,
                merchant_origin=MERCHANT_ORIGIN,
            )
            assert startup_check(environment, STACK) == {"ok": True}, (condition, layer)


@pytest.mark.parametrize(
    ("condition", "error"),
    [("B0-HNP", "AGENT_D1_AES_KEY_HEX"), ("B2-HNP", "Missing policy revision")],
)
def test_without_the_startup_additions_the_agent_would_not_start(
    pilot: tuple[dict, dict],
    invocation: InvocationSecrets,
    provider_key: str,
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
    error: str,
) -> None:
    manifest, matrix = pilot
    monkeypatch.setitem(hnp_pilot.STARTUP_ADDITIONS, condition, {})
    environment = condition_environment(
        manifest,
        condition,
        layer="A",
        scenario=matrix["scenarios"][0],
        invocation=invocation,
        provider_key=provider_key,
        merchant_origin=MERCHANT_ORIGIN,
    )
    result = startup_check(environment, STACK)
    assert result["ok"] is False and error in result["error"]


def test_a_refused_startup_never_echoes_a_value(
    pilot: tuple[dict, dict], invocation: InvocationSecrets, provider_key: str
) -> None:
    manifest, matrix = pilot
    environment = condition_environment(
        manifest,
        "B2-HNP",
        layer="A",
        scenario=matrix["scenarios"][0],
        invocation=invocation,
        provider_key=provider_key,
        merchant_origin=MERCHANT_ORIGIN,
    )
    stack = {name: value for name, value in STACK.items() if name != "AGENT_AUTH_ISSUER"}
    result = startup_check(environment, stack)
    assert result["ok"] is False and "AGENT_AUTH_ISSUER" in result["error"]
    for secret in (*invocation.values(), provider_key, "BEGIN PRIVATE KEY"):
        assert secret not in result["error"]


def test_paths_stay_in_the_scratch_area(tmp_path: Path) -> None:
    stack = stack_variables(merchant_origin="m", auth_origin="a", episode_dir=tmp_path)
    assert stack["AGENT_DATABASE_PATH"] == str(tmp_path / "agent.db")
    assert stack["AGENT_FEED_PATH"] == str(tmp_path / "feed")
