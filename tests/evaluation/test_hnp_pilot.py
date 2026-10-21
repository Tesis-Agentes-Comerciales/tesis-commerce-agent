"""`hnp-pilot`: plan, environment, guards and dry run (docs/design/hnp-pilot.md, HNP-E2).

Nothing here starts a service or calls a model: the live run is refused before it could.
"""

import copy
import io
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation import hnp_pilot, hnp_pilot_agent, verify_evidence
from commerce_evaluation.governance_pilot import _ap2_private_key
from commerce_evaluation.hnp_pilot import (
    CONDITIONS,
    LEDGER_CONDITIONS,
    CostCap,
    InvocationSecrets,
    PlannedEpisode,
    build_plan,
    condition_environment,
    condition_order,
    episode_counts,
    live_guards,
    load_pilot,
    run_plan,
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


# Guards ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "cap"),
    [
        (None, None),
        ("abc", None),
        ("0", None),
        ("-1", None),
        ("2.01", None),
        ("2", 2.0),
        ("0.5", 0.5),
    ],
)
def test_the_cost_cap_comes_from_its_variable_and_never_exceeds_the_manifest(
    pilot: tuple[dict, dict], value: str | None, cap: float | None
) -> None:
    manifest, _ = pilot
    environ = {} if value is None else {"HNP_PILOT_MAX_COST_USD": value}
    read = CostCap.read(manifest, environ)
    assert read.cap_usd == cap
    assert (read.error is None) == (cap is not None)


def guards(manifest: dict[str, Any], **changes: Any) -> dict[str, dict[str, Any]]:
    ready = {name: {"ready": True} for name in hnp_pilot.REQUIRED_READINESS}
    options: dict[str, Any] = {
        "problems": [],
        "readiness": ready,
        "cost": CostCap.read(manifest, {"HNP_PILOT_MAX_COST_USD": "2"}),
        "startup": {"B0-HNP/A": {"ok": True}},
        "worktree_dirty": False,
        "allow_dirty": False,
    }
    return live_guards(manifest, **(options | changes))


def test_a_live_run_needs_every_guard(pilot: tuple[dict, dict]) -> None:
    manifest, _ = pilot
    approved = copy.deepcopy(manifest)
    approved["approval"]["status"] = "approved"
    assert all(guard["ok"] for guard in guards(approved).values())
    # The registered manifest is still pending Yair's approval: a live run refuses.
    assert manifest["approval"]["status"] == "pending"
    assert guards(manifest)["approval"]["ok"] is False
    assert guards(approved, cost=CostCap.read(manifest, {}))["cost_cap"]["ok"] is False
    missing = {name: {"ready": name != "HNP-09b"} for name in hnp_pilot.REQUIRED_READINESS}
    assert guards(approved, readiness=missing)["integrations"]["missing"] == ["HNP-09b"]
    assert guards(approved, worktree_dirty=True)["agent_worktree"]["ok"] is False
    assert guards(approved, worktree_dirty=True, allow_dirty=True)["agent_worktree"]["ok"]
    assert guards(approved, startup={"B2-HNP/B": {"ok": False}})["agent_startup"]["ok"] is False
    assert guards(approved, problems=["x"])["manifest_and_matrix"]["ok"] is False


def test_the_live_run_refuses_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HNP_PILOT_MAX_COST_USD", "2")
    assert hnp_pilot.main([]) == 1
    error = capsys.readouterr().err
    assert "refuses to start" in error and '"approval"' in error


def test_the_readiness_follows_the_run_route_and_the_verifier(
    pilot: tuple[dict, dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _ = pilot
    links = manifest["evidence_chain"]["links"]
    monkeypatch.setattr(
        hnp_pilot, "_route_source", lambda: '@router.post("/internal/hnp/tasks/{task_id}/run")'
    )
    profile = types.ModuleType(hnp_pilot.VERIFIER_MODULE)
    profile.HNP_LINKS = tuple(links)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, hnp_pilot.VERIFIER_MODULE, profile)

    def verify_database(database: Path, *, psp_database: Path, grant_id: str) -> list[Any]:
        return []

    monkeypatch.setattr(verify_evidence, "verify_database", verify_database)
    readiness = hnp_pilot.integration_readiness(manifest, agent_base={"ready": True})
    for name in ("HNP-05", "HNP-06", "HNP-08", "HNP-09a", "HNP-09b", "HNP-V1", "HNP-E2"):
        assert readiness[name]["ready"] is True, name
    assert readiness["HNP-E3"]["optional"] is True

    profile.HNP_LINKS = tuple(links[:-1])  # type: ignore[attr-defined]
    assert hnp_pilot.verifier_readiness(links)["ready"] is False


def test_without_the_hnp_profile_the_verifier_is_not_ready(
    pilot: tuple[dict, dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _ = pilot
    monkeypatch.setitem(sys.modules, hnp_pilot.VERIFIER_MODULE, None)
    readiness = hnp_pilot.verifier_readiness(manifest["evidence_chain"]["links"])
    assert readiness["ready"] is False and readiness["error"].startswith("ModuleNotFoundError")


# Dry run -----------------------------------------------------------------------------------------


def test_the_dry_run_prints_the_plan_and_the_readiness_without_secrets(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HNP_PILOT_MAX_COST_USD", raising=False)
    assert hnp_pilot.main(["--dry-run"]) == 0
    output = capsys.readouterr().out
    document = json.loads(output)
    assert document["manifest_id"] == "HNP-PILOT-v1"
    assert document["manifest_and_matrix_valid"] is True
    assert document["destination_accepted"] is True
    assert document["episodes_planned"]["total"] == 133
    assert document["episodes_runnable"]["by_condition"]["P"] == 0
    assert len(document["plan"]) == 133
    assert document["scripted_selector"]["variant_id"] == "BBOnePlus11-256GB"
    assert document["scripted_selector"]["ready"] is True
    assert set(document["agent_startup"]) == {"B0-HNP/A", "B0-HNP/B", "B2-HNP/A", "B2-HNP/B"}
    assert document["live_guards"]["cost_cap"]["ok"] is False
    assert document["live_run_would_start"] is False
    assert set(document["integration_readiness"]) >= {"HNP-09b", "HNP-V1", "HNP-E2", "HNP-E3"}
    for name in ("BEGIN PRIVATE KEY", "sk-", "dry-run-placeholder"):
        assert name not in output
    names = {row["name"] for row in document["conditions"]["B2-HNP"]["environment"]}
    assert {"AGENT_HNP_SERVICE_TOKEN", "AGENT_D1_POLICY_SET_ID"} <= names


def test_the_dry_run_of_one_scenario_is_a_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    assert hnp_pilot.main(["--dry-run", "--scenario", "HN11"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["smoke"] is True
    assert {row["scenario_id"] for row in document["plan"]} == {"HN11"}


def test_an_unknown_scenario_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert hnp_pilot.main(["--dry-run", "--scenario", "HN99"]) == 1
    assert "Unknown scenarios: HN99" in capsys.readouterr().err


# Live loop and cost cap --------------------------------------------------------------------------


def test_the_cost_cap_stops_the_invocation_after_the_episode_that_reaches_it(
    pilot: tuple[dict, dict],
) -> None:
    manifest, matrix = pilot
    plan = build_plan(manifest, matrix, p_available=False, scenario_ids=["HN09", "HN10"])
    executed: list[PlannedEpisode] = []

    def execute(episode: PlannedEpisode, index: int) -> dict[str, Any]:
        executed.append(episode)
        return episode.key() | {"cost": {"charged_usd": 0.6}}

    file = io.StringIO()
    rows, stop = run_plan(plan, execute, cap_usd=2.0, result_file=file)
    assert len(rows) == len(executed) == 4
    assert [json.loads(line) for line in file.getvalue().splitlines()] == rows
    assert stop["stopped_by"] == "cost_cap" and stop["accumulated_cost_usd"] == 2.4
    assert stop["after_episode"] == executed[-1].key()
    assert len(stop["not_run"]) == len(plan) - 4


def test_the_plan_runs_to_the_end_under_the_cap_and_skips_p(pilot: tuple[dict, dict]) -> None:
    manifest, matrix = pilot
    plan = build_plan(manifest, matrix, p_available=False, scenario_ids=["HN05"])
    rows, stop = run_plan(
        plan,
        lambda episode, index: episode.key() | {"cost": {"charged_usd": 0.0}},
        cap_usd=2.0,
        result_file=io.StringIO(),
    )
    assert len(rows) == 6 and {row["condition"] for row in rows} == set(LEDGER_CONDITIONS)
    assert stop["stopped_by"] is None


# HN11's scripted selector ------------------------------------------------------------------------


def test_hn11_registers_the_scripted_variant(pilot: tuple[dict, dict]) -> None:
    _, matrix = pilot
    assert hnp_pilot_agent.scripted_variant(matrix) == "BBOnePlus11-256GB"
    assert hnp_pilot_agent.scripted_variant() == "BBOnePlus11-256GB"


def test_the_scripted_selector_can_be_injected_on_this_runtime() -> None:
    readiness = hnp_pilot_agent.injection_readiness()
    assert readiness == {
        "ready": True,
        "factory": "commerce_evaluation.hnp_pilot_agent:app_factory",
        "seam": "commerce_agent.delegation.bootstrap.build_hnp_selector",
    }


def test_paths_stay_in_the_scratch_area(tmp_path: Path) -> None:
    stack = stack_variables(merchant_origin="m", auth_origin="a", episode_dir=tmp_path)
    assert stack["AGENT_DATABASE_PATH"] == str(tmp_path / "agent.db")
    assert stack["AGENT_FEED_PATH"] == str(tmp_path / "feed")
