"""Run the HNP pilot (EC-HNP-03) on the governance pilot's stack: docs/design/hnp-pilot.md.

`hnp-pilot --dry-run` validates the manifest and its matrix, prints the registered plan, the
environment of each condition (names only), the integration readiness and whether a live run would
start; it never starts a service or calls a model. A live run refuses without `HNP_LIVE=1`,
without the oracles' approval in the committed manifest, without `HNP_PILOT_MAX_COST_USD` (or
above the manifest's cap), off the registered agent commit, on a dirty worktree, with a missing
integration, under pytest or with a port of the stack in use; it stops cleanly once the
accumulated model cost reaches the cap.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from pydantic import ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from commerce_agent import api, delegation_api
from commerce_agent.acp.destination import merchant_fulfillment
from commerce_agent.application.purchase import CheckoutDetails
from commerce_agent.config import Settings
from commerce_agent.delegation.bootstrap import load_hnp_trust
from commerce_agent.governance.bootstrap import configured_d1
from commerce_agent.governance.hnp_policy import build_hnp_policy
from commerce_agent.persistence.stage_timings_sqlite import ModelUsage, SqliteStageTimings
from commerce_evaluation import hnp_pilot_agent
from commerce_evaluation.governance_pilot import (
    DETAILS,
    PilotError,
    _ap2_private_key,
    base_readiness,
    repository_provenance,
)
from commerce_evaluation.paths import DATA, RESULTS, ROOT
from commerce_evaluation.pilot_policy import ALLOWLIST, HP_V2_POLICY, scenario_environment

MANIFEST = DATA / "hnp-pilot-v1.json"
CONDITIONS = ("B0-HNP", "B2-HNP", "P")
LEDGER_CONDITIONS = ("B0-HNP", "B2-HNP")
GOVERNANCE_MODES = {"B0-HNP": "off", "B2-HNP": "enforce"}
LAYERS = ("A", "B")
BUYER_DETAILS = "synthetic_bogota_buyer"
GENERATED = "generated_per_invocation_never_logged"
COST_VARIABLE_DEFAULT = "HNP_PILOT_MAX_COST_USD"
# A live run also needs this, as every paid evaluation run (docs/testing.md).
LIVE_VARIABLE = "HNP_LIVE"
STIMULI = frozenset(
    {"merchant_update_offer", "wait_until_grant_expired", "owner_revoke", "operator_kill_switch"}
)
TRIGGER_MODES = frozenset({"once", "twice_concurrently"})
RUN_ROUTE = "/internal/hnp/tasks/{task_id}/run"
P_MODULE = "commerce_evaluation.hnp.protocol_only"
# HNP-V1's profile of verify-evidence: its link list, and verify_database by grant (#105, #106).
VERIFIER_MODULE = "commerce_evaluation.verify_evidence_hnp"
VERIFIER_LINKS = "HNP_LINKS"
# Live runs need these; HNP-M1/P1 are commits recorded per row and HNP-E3 is optional (E4).
REQUIRED_READINESS = (
    "HNP-05",
    "HNP-06",
    "HNP-08",
    "HNP-09a",
    "HNP-09b",
    "HNP-V1",
    "HNP-E2",
    "agent_base",
)

# Agent variables the manifest generates per invocation, and the secret each one takes.
SECRET_VARIABLES = {
    "AGENT_HNP_AGENT_KEY_PEM": "agent_key_pem",
    "AGENT_HNP_SERVICE_TOKEN": "service_token",
    "AGENT_HNP_OPERATOR_TOKEN": "operator_token",
    "AGENT_D1_HMAC_KEY_HEX": "d1_hmac_hex",
    "AGENT_D1_AES_KEY_HEX": "d1_aes_hex",
}
# Manifest values the runner resolves per layer, from the stack or per scenario.
DERIVED_VARIABLES = frozenset({"AGENT_HNP_SELECTOR", "AGENT_AP2_PRIVATE_KEY_PEM", ALLOWLIST})
# Variables the agent's startup requires that the manifest leaves out; neither changes an HNP
# decision. B0-HNP: the vault key of the owner and destination pseudonyms (`load_hnp_trust`).
# B2-HNP: the Human Present policy set of the enforce D1 gate; the HNP purchase is evaluated with
# `b2-hnp-v1`, built in code (spec §7), which does not read these.
STARTUP_ADDITIONS: dict[str, dict[str, str]] = {
    "B0-HNP": {"AGENT_D1_AES_KEY_HEX": GENERATED},
    "B2-HNP": {
        "AGENT_D1_POLICY_SET_ID": HP_V2_POLICY.set_id,
        "AGENT_D1_POLICY_REVISION": HP_V2_POLICY.revision,
    },
}
_LITERAL = re.compile(r"^[A-Za-z0-9._:/-]+$")


def startup_additions(condition: str) -> dict[str, str]:
    """The variables the runner adds to a condition, with their values unless generated."""
    return {
        name: "generated_per_invocation" if value == GENERATED else value
        for name, value in STARTUP_ADDITIONS[condition].items()
    }


@dataclass(frozen=True)
class PlannedEpisode:
    layer: str
    scenario: Mapping[str, Any] = field(repr=False)
    repetition: int
    condition: str
    # False for P without HNP-E3 (amendment E4): the cell keeps its prediction.
    runs: bool = True

    @property
    def scenario_id(self) -> str:
        return str(self.scenario["id"])

    def key(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "scenario_id": self.scenario_id,
            "repetition": self.repetition,
            "condition": self.condition,
        }


@dataclass(frozen=True)
class InvocationSecrets:
    """Generated once per invocation, kept in memory and given only to the agent."""

    agent_key_pem: str = field(repr=False)
    service_token: str = field(repr=False)
    operator_token: str = field(repr=False)
    d1_hmac_hex: str = field(repr=False)
    d1_aes_hex: str = field(repr=False)

    @classmethod
    def generate(cls) -> InvocationSecrets:
        return cls(
            agent_key_pem=_ap2_private_key(),
            service_token=secrets.token_urlsafe(32),
            operator_token=secrets.token_urlsafe(32),
            d1_hmac_hex=secrets.token_hex(32),
            d1_aes_hex=secrets.token_hex(32),
        )

    def values(self) -> tuple[str, ...]:
        return tuple(getattr(self, name) for name in SECRET_VARIABLES.values())


# Loading and validation --------------------------------------------------------------------------


def _read_json(path: Path, what: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PilotError(f"Cannot read the {what}: {path}") from error
    if not isinstance(value, dict):
        raise PilotError(f"The {what} is not a JSON object: {path}")
    return value


def load_pilot(manifest_path: Path = MANIFEST) -> tuple[dict[str, Any], dict[str, Any]]:
    """The manifest and the scenario matrix it registers."""
    manifest = _read_json(manifest_path, "HNP pilot manifest")
    if "scenario_manifest" not in manifest or "layers" not in manifest:
        raise PilotError(f"Not an HNP pilot manifest: {manifest_path}")
    matrix = _read_json(ROOT / str(manifest["scenario_manifest"]), "HNP scenario matrix")
    if not isinstance(matrix.get("scenarios"), list):
        raise PilotError("The HNP scenario matrix has no scenarios array")
    return manifest, matrix


def scenarios_by_layer(
    manifest: Mapping[str, Any], matrix: Mapping[str, Any]
) -> dict[str, list[Mapping[str, Any]]]:
    """The matrix's scenarios in the order each manifest layer lists them."""
    by_id = {str(scenario["id"]): scenario for scenario in matrix["scenarios"]}
    layers: dict[str, list[Mapping[str, Any]]] = {}
    for layer in LAYERS:
        ids = manifest["layers"][layer]["scenarios"]
        unknown = [scenario_id for scenario_id in ids if scenario_id not in by_id]
        if unknown:
            raise PilotError(f"Layer {layer} lists scenarios the matrix lacks: {unknown}")
        layers[layer] = [by_id[scenario_id] for scenario_id in ids]
    return layers


def registered_runs(manifest: Mapping[str, Any], layer: str, scenario_id: str) -> dict[str, int]:
    """Repetitions per condition that the manifest's layers and the P arm imply for a scenario."""
    spec = manifest["layers"][layer]
    repetitions = int(spec.get("repetition_exceptions", {}).get(scenario_id, 0)) or int(
        spec["repetitions_per_condition"]
    )
    conditions = spec.get("conditions", list(CONDITIONS))
    p_cells = manifest["conditions"]["P"]["runs_only_in"]
    return {
        condition: (
            0
            if condition not in conditions or (condition == "P" and scenario_id not in p_cells)
            else repetitions
        )
        for condition in CONDITIONS
    }


def validate_pilot(
    manifest: Mapping[str, Any], matrix: Mapping[str, Any], manifest_path: Path = MANIFEST
) -> list[str]:
    """Every way the matrix departs from what the manifest registers; empty when they agree."""
    problems: list[str] = []
    if (ROOT / str(matrix.get("manifest"))).resolve() != manifest_path.resolve():
        problems.append(f"the matrix names another manifest: {matrix.get('manifest')}")
    if list(matrix.get("conditions", [])) != list(manifest["conditions"]) or list(
        manifest["conditions"]
    ) != list(CONDITIONS):
        problems.append(f"conditions differ from {list(CONDITIONS)}")
    try:
        layers = scenarios_by_layer(manifest, matrix)
    except PilotError as error:
        return [*problems, str(error)]
    listed = [str(scenario["id"]) for layer in LAYERS for scenario in layers[layer]]
    if sorted(listed) != sorted(str(scenario["id"]) for scenario in matrix["scenarios"]):
        problems.append("the layers do not list each matrix scenario exactly once")
    counts = {condition: {layer: 0 for layer in LAYERS} for condition in CONDITIONS}
    scripted = 0
    links = list(manifest["evidence_chain"]["links"])
    for layer in LAYERS:
        for scenario in layers[layer]:
            scenario_id = str(scenario["id"])
            prefix = f"{scenario_id}:"
            if scenario.get("layer") != layer:
                problems.append(f"{prefix} layer {scenario.get('layer')} instead of {layer}")
            expected = registered_runs(manifest, layer, scenario_id)
            if dict(scenario["runs"]) != expected:
                problems.append(f"{prefix} runs {scenario['runs']} instead of {expected}")
            for condition, runs in expected.items():
                counts[condition][layer] += runs
                oracle = scenario["oracle"].get(condition)
                if oracle is None or oracle.get("run") is not (runs > 0):
                    problems.append(f"{prefix} the {condition} oracle does not match its runs")
                chain = (oracle or {}).get("evidence_chain")
                if chain is not None and sorted(
                    link for statuses in chain["links"].values() for link in statuses
                ) != sorted(links):
                    problems.append(f"{prefix} the {condition} vector does not cover the links")
            selector = scenario["selector"]
            if layer == "B":
                if selector != {"kind": "llm", "model": manifest["layers"]["B"]["model"]}:
                    problems.append(f"{prefix} layer B runs llm with the registered model")
            elif selector["kind"] == "scripted_adversarial":
                scripted += 1
            elif selector != {"kind": "deterministic"}:
                problems.append(f"{prefix} unknown layer A selector {selector}")
            delegation = scenario["delegation"]
            if delegation.get("details") != BUYER_DETAILS:
                problems.append(f"{prefix} the buyer details are not {BUYER_DETAILS}")
            for stimulus in scenario.get("stimuli", []):
                if stimulus.get("action") not in STIMULI or stimulus.get("after") != "consented":
                    problems.append(f"{prefix} unsupported stimulus {stimulus}")
            if scenario["trigger"].get("mode") not in TRIGGER_MODES:
                problems.append(f"{prefix} unsupported trigger {scenario['trigger']}")
    if scripted != 1:
        problems.append(f"{scripted} scripted selectors instead of one (HN11)")
    registered = manifest["episodes"]
    for condition in CONDITIONS:
        total = sum(counts[condition].values())
        if total != registered["by_condition"][condition]:
            problems.append(f"{condition}: {total} episodes instead of the registered ones")
        for layer in LAYERS:
            if counts[condition][layer] != registered["by_layer"][layer][condition]:
                problems.append(f"{condition} layer {layer}: episodes differ from the manifest")
    if sum(sum(item.values()) for item in counts.values()) != registered["total"]:
        problems.append("the episodes do not add up to the registered total")
    if destination_accepted() is not True:
        problems.append("the merchant does not deliver to the synthetic Bogota buyer")
    return problems


def destination_accepted() -> bool:
    """The synthetic buyer's destination is one the merchant serves (Bogota, DC, CO, 110111)."""
    details = CheckoutDetails.model_validate(DETAILS)
    return merchant_fulfillment(details.fulfillment) is not None


def episode_counts(plan: Sequence[PlannedEpisode]) -> dict[str, Any]:
    by_condition = {condition: 0 for condition in CONDITIONS}
    by_layer = {layer: {condition: 0 for condition in CONDITIONS} for layer in LAYERS}
    for episode in plan:
        by_condition[episode.condition] += 1
        by_layer[episode.layer][episode.condition] += 1
    return {"by_condition": by_condition, "by_layer": by_layer, "total": len(plan)}


# Plan --------------------------------------------------------------------------------------------


def condition_order(repetition: int, active: Sequence[str]) -> list[str]:
    """Within a repetition the first condition rotates (B0, B2, P; B2, P, B0; P, B0, B2); a
    cell without P alternates B0-HNP and B2-HNP (manifest `execution.order`)."""
    if "P" in active:
        shift = (repetition - 1) % len(CONDITIONS)
        rotated = [*CONDITIONS[shift:], *CONDITIONS[:shift]]
        return [condition for condition in rotated if condition in active]
    ordered = [condition for condition in LEDGER_CONDITIONS if condition in active]
    return ordered if repetition % 2 == 1 else ordered[::-1]


def build_plan(
    manifest: Mapping[str, Any],
    matrix: Mapping[str, Any],
    *,
    p_available: bool,
    scenario_ids: Iterable[str] | None = None,
    smoke: bool = False,
) -> list[PlannedEpisode]:
    """Layer A, then layer B; by scenario and repetition; the registered rotation of conditions.

    Without HNP-E3, P keeps its place in the order but does not run (E4). A smoke runs one
    repetition per cell; `scenario_ids` keeps only those scenarios.
    """
    wanted = None if scenario_ids is None else set(scenario_ids)
    plan: list[PlannedEpisode] = []
    for layer, scenarios in scenarios_by_layer(manifest, matrix).items():
        for scenario in scenarios:
            if wanted is not None and scenario["id"] not in wanted:
                continue
            runs = {condition: int(scenario["runs"][condition]) for condition in CONDITIONS}
            repetitions = 1 if smoke else max(runs.values())
            for repetition in range(1, repetitions + 1):
                active = [condition for condition in CONDITIONS if runs[condition] >= repetition]
                plan.extend(
                    PlannedEpisode(
                        layer,
                        scenario,
                        repetition,
                        condition,
                        runs=condition != "P" or p_available,
                    )
                    for condition in condition_order(repetition, active)
                )
    return plan


# Environment -------------------------------------------------------------------------------------


def condition_environment(
    manifest: Mapping[str, Any],
    condition: str,
    *,
    layer: str,
    scenario: Mapping[str, Any],
    invocation: InvocationSecrets,
    provider_key: str,
    merchant_origin: str,
) -> dict[str, str]:
    """The agent variables of a condition in one episode, all explicit over the agent's .env."""
    declared: Mapping[str, str] = manifest["conditions"][condition]["agent_environment"]
    environment: dict[str, str] = {}
    for name, value in [*declared.items(), *STARTUP_ADDITIONS[condition].items()]:
        if value == GENERATED:
            environment[name] = getattr(invocation, SECRET_VARIABLES[name])
        elif name == "AGENT_HNP_SELECTOR":
            # HN11's scripted selector replaces it by code (hnp_pilot_agent), never by a variable.
            environment[name] = "llm" if layer == "B" else "deterministic"
        elif name == "AGENT_AP2_PRIVATE_KEY_PEM":
            environment[name] = provider_key
        elif name == ALLOWLIST:
            environment[name] = merchant_origin
        elif _LITERAL.fullmatch(value):
            environment[name] = value
        else:
            raise PilotError(f"{condition}: the manifest value of {name} cannot be resolved")
    environment["OPENAI_TIMEOUT_SECONDS"] = (
        f"{float(manifest['execution']['openai_timeout_seconds']):g}"
    )
    if layer == "B":
        environment["OPENAI_MODEL"] = str(manifest["layers"]["B"]["model"])
    return scenario_environment(environment, scenario, merchant_origin)


def described_environment(manifest: Mapping[str, Any], condition: str) -> list[dict[str, str]]:
    """Names and where each value comes from; never a value."""
    declared: Mapping[str, str] = manifest["conditions"][condition]["agent_environment"]
    rows = [
        {
            "name": name,
            "source": (
                "generated_per_invocation"
                if value == GENERATED
                else "derived"
                if name in DERIVED_VARIABLES
                else "manifest"
            ),
        }
        for name, value in declared.items()
    ]
    rows += [
        {
            "name": name,
            "source": "startup_addition_generated" if value == GENERATED else "startup_addition",
        }
        for name, value in STARTUP_ADDITIONS[condition].items()
    ]
    rows.append({"name": "OPENAI_TIMEOUT_SECONDS", "source": "manifest_execution"})
    rows.append({"name": "OPENAI_MODEL", "source": "manifest_layer_B (layer B only)"})
    return rows


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


def stack_variables(
    *,
    merchant_origin: str,
    auth_origin: str,
    episode_dir: Path = Path("episode"),
) -> dict[str, str]:
    """What an episode gives the agent besides its condition: the loopback stack, and its own
    feed and agent.db (a dry run names a directory it never creates)."""
    return {
        "MERCHANT_BASE_URL": merchant_origin,
        "AGENT_AUTH_ISSUER": auth_origin,
        "AGENT_UI_ORIGIN": auth_origin,
        "AGENT_AUTH_MODE": "jwt",
        "AGENT_FEED_SOURCE": "file",
        "AGENT_FEED_PATH": str(episode_dir / "feed"),
        "AGENT_DATABASE_PATH": str(episode_dir / "agent.db"),
    }


def startup_check(environment: Mapping[str, str], stack: Mapping[str, str]) -> dict[str, Any]:
    """Whether the agent's HNP startup accepts the condition's variables (`load_hnp_trust`, and
    `configured_d1` in enforce). The result never carries a setting's value."""
    values = {name.lower(): value for name, value in {**stack, **environment}.items()}
    if environment.get("AGENT_HNP_SELECTOR") == "llm":
        # The agent reads its own key at startup; the check only needs one to be present.
        values.setdefault("openai_api_key", "dry-run-placeholder")
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


# Readiness and guards ----------------------------------------------------------------------------


def _artifact() -> dict[str, Any]:
    artifact, _ = build_hnp_policy(merchant_ref="merchant:readiness-probe")
    return json.loads(artifact)


def _route_source() -> str:
    return inspect.getsource(api) + inspect.getsource(delegation_api)


def integration_readiness(
    manifest: Mapping[str, Any], *, agent_base: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """The manifest's `integration_dependencies`, checked on this checkout."""
    policy = manifest["policy"]
    rules = policy["rules"]
    readiness: dict[str, dict[str, Any]] = {}
    try:
        artifact = _artifact()
        binding = {
            "policy_set_id": artifact.get("policy_set_id"),
            "version": str(artifact.get("version")),
        }
        expected = {"policy_set_id": policy["policy_set_id"], "version": str(policy["revision"])}
        d2 = (artifact.get("authorization") or {}).get("policy_ids")
        d3 = (artifact.get("dispatch") or {}).get("policy_ids")
        readiness["HNP-05"] = {
            "ready": binding == expected and artifact.get("policy_ids") == rules["D1"],
            "policy": binding,
            "d1_rules": artifact.get("policy_ids"),
        }
        readiness["HNP-06"] = {
            "ready": d2 == rules["D2"] and d3 == rules["D3"],
            "d2_rules": d2,
            "d3_rules": d3,
        }
    except Exception as error:
        detail = {"ready": False, "error": f"{type(error).__name__}: {error}"}
        readiness["HNP-05"] = readiness["HNP-06"] = detail
    readiness["HNP-08"] = {
        "ready": {"hnp_enabled", "hnp_kill_switch"} <= set(api.Health.model_fields),
        "check": "/health reports hnp_enabled and hnp_kill_switch",
    }
    readiness["HNP-09a"] = {
        "ready": "agent_hnp_selector" in Settings.model_fields
        and {"input_tokens", "output_tokens"} <= set(ModelUsage.__dataclass_fields__)
        and hasattr(SqliteStageTimings, "initialize_model_tokens"),
        "check": "AGENT_HNP_SELECTOR and the token columns of stage_timings",
    }
    readiness["HNP-09b"] = {
        "ready": "/tasks/{task_id}/run" in _route_source(),
        "route": f"POST {RUN_ROUTE}",
    }
    readiness["HNP-V1"] = verifier_readiness(manifest["evidence_chain"]["links"])
    readiness["HNP-M1"] = {"ready": None, "check": "merchant commit recorded per row"}
    readiness["HNP-P1"] = {"ready": None, "check": "PSP commit recorded per row"}
    readiness["HNP-E2"] = hnp_pilot_agent.injection_readiness()
    readiness["HNP-E3"] = {
        "ready": importlib.util.find_spec(P_MODULE) is not None,
        "optional": True,
        "module": P_MODULE,
    }
    readiness["agent_base"] = dict(agent_base)
    return readiness


def verifier_readiness(links: Sequence[str]) -> dict[str, Any]:
    """HNP-V1 verifies the 17 HNP links, also for tasks without a purchase row, by grant."""
    from commerce_evaluation import verify_evidence

    expects = (
        f"{VERIFIER_MODULE}.{VERIFIER_LINKS} and "
        "verify_evidence.verify_database(database, *, psp_database, grant_id)"
    )
    try:
        module = importlib.import_module(VERIFIER_MODULE)
    except ImportError as error:
        return {"ready": False, "expects": expects, "error": f"{type(error).__name__}: {error}"}
    declared = getattr(module, VERIFIER_LINKS, None)
    by_grant = "grant_id" in inspect.signature(verify_evidence.verify_database).parameters
    return {
        "ready": declared is not None and list(declared) == list(links) and by_grant,
        "expects": expects,
    }


@dataclass(frozen=True)
class CostCap:
    variable: str
    cap_usd: float | None
    registered_usd: float
    error: str | None

    @classmethod
    def read(cls, manifest: Mapping[str, Any], environ: Mapping[str, str]) -> CostCap:
        cost = manifest["cost"]
        variable = str(cost.get("cap_variable", COST_VARIABLE_DEFAULT))
        registered = float(cost["cap_usd"])
        raw = environ.get(variable)
        if raw is None:
            return cls(variable, None, registered, f"{variable} is not set")
        try:
            value = float(raw)
        except ValueError:
            return cls(variable, None, registered, f"{variable} is not a number")
        if not 0 < value <= registered:
            return cls(variable, None, registered, f"{variable} must be in (0, {registered:g}]")
        return cls(variable, value, registered, None)

    def describe(self) -> dict[str, Any]:
        return {
            "variable": self.variable,
            "registered_cap_usd": self.registered_usd,
            "cap_usd": self.cap_usd,
            "ok": self.error is None,
            "error": self.error,
        }


def live_guards(
    manifest: Mapping[str, Any],
    *,
    problems: Sequence[str],
    readiness: Mapping[str, Mapping[str, Any]],
    cost: CostCap,
    startup: Mapping[str, Mapping[str, Any]],
    registration: Mapping[str, Any],
    worktree_dirty: bool,
    environ: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """Each reason a live run would refuse to start; all `ok` means it may start. None of them
    has an override: the approval counts only as committed, on a clean checkout."""
    approval = manifest["approval"]
    missing = [name for name in REQUIRED_READINESS if readiness[name]["ready"] is not True]
    failed_startups = [name for name, check in startup.items() if not check["ok"]]
    return {
        "hnp_live": {"ok": environ.get(LIVE_VARIABLE) == "1", "variable": LIVE_VARIABLE},
        "approval": {
            "ok": approval.get("status") == "approved",
            "status": approval.get("status"),
            "required_from": approval.get("required_from"),
        },
        "cost_cap": {"ok": cost.error is None, "error": cost.error},
        "manifest_and_matrix": {"ok": not problems, "problems": list(problems)},
        "registered_manifest": dict(registration),
        "integrations": {"ok": not missing, "missing": missing},
        "agent_startup": {"ok": not failed_startups, "failed": failed_startups},
        "agent_worktree": {"ok": not worktree_dirty, "dirty": worktree_dirty},
    }


def manifest_registration(
    manifest_path: Path,
    matrix_path: Path,
    *,
    root: Path = ROOT,
    registered: Path = MANIFEST,
) -> dict[str, Any]:
    """The run uses the registered manifest, and it and its matrix are tracked and equal to HEAD.

    The approval is recorded "in the commit the run uses": an uncommitted edit of the manifest,
    or an untracked copy passed with `--manifest`, must not pass for it.
    """
    problems: list[str] = []
    if manifest_path.resolve() != registered.resolve():
        problems.append(f"{manifest_path} is not the registered manifest")
    problems += [
        problem for path in (manifest_path, matrix_path) if (problem := _committed(root, path))
    ]
    return {"ok": not problems, "problems": problems}


def _committed(root: Path, path: Path) -> str | None:
    """Why `path` is not as committed in `root`'s HEAD; None when it is."""
    try:
        relative = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return f"{path} is outside the agent checkout"
    try:
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative],
            capture_output=True,
            check=False,
        )
        changed = subprocess.run(
            ["git", "-C", str(root), "diff", "--quiet", "HEAD", "--", relative],
            capture_output=True,
            check=False,
        )
    except OSError as error:
        return f"cannot read {relative} from git: {error}"
    if tracked.returncode != 0:
        return f"{relative} is not tracked"
    if changed.returncode == 1:
        return f"{relative} differs from HEAD"
    if changed.returncode != 0:
        return f"cannot compare {relative} with HEAD"
    return None


def require_guards(guards: Mapping[str, Mapping[str, Any]]) -> None:
    refused = {name: guard for name, guard in guards.items() if not guard["ok"]}
    if refused:
        raise PilotError(
            "A live HNP pilot run refuses to start: " + json.dumps(refused, ensure_ascii=False)
        )


# Dry run -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Preparation:
    manifest: dict[str, Any]
    matrix: dict[str, Any]
    problems: list[str]
    plan: list[PlannedEpisode]
    readiness: dict[str, dict[str, Any]]
    startup: dict[str, dict[str, Any]]
    cost: CostCap
    guards: dict[str, dict[str, Any]]
    provenance: dict[str, Any]
    smoke: bool


def prepare(
    manifest_path: Path,
    *,
    scenario_ids: Sequence[str] | None,
    smoke: bool,
    stack: Mapping[str, str],
    environ: Mapping[str, str],
) -> Preparation:
    """Everything a dry run reports and a live run checks, without starting anything."""
    merchant_origin = stack["MERCHANT_BASE_URL"]
    manifest, matrix = load_pilot(manifest_path)
    problems = validate_pilot(manifest, matrix, manifest_path)
    known = {str(scenario["id"]) for scenario in matrix["scenarios"]}
    unknown = sorted(set(scenario_ids or ()) - known)
    if unknown:
        raise PilotError(f"Unknown scenarios: {', '.join(unknown)}")
    provenance = repository_provenance(ROOT)
    readiness = integration_readiness(manifest, agent_base=_agent_base(manifest, provenance))
    plan = build_plan(
        manifest,
        matrix,
        p_available=False,
        scenario_ids=scenario_ids,
        smoke=smoke,
    )
    invocation = InvocationSecrets.generate()
    provider_key = _ap2_private_key()
    probe = next(iter(matrix["scenarios"]))
    startup = {
        f"{condition}/{layer}": startup_check(
            condition_environment(
                manifest,
                condition,
                layer=layer,
                scenario=probe,
                invocation=invocation,
                provider_key=provider_key,
                merchant_origin=merchant_origin,
            ),
            stack,
        )
        for condition in LEDGER_CONDITIONS
        for layer in LAYERS
    }
    cost = CostCap.read(manifest, environ)
    guards = live_guards(
        manifest,
        problems=problems,
        readiness=readiness,
        cost=cost,
        startup=startup,
        registration=manifest_registration(
            manifest_path, ROOT / str(manifest["scenario_manifest"])
        ),
        worktree_dirty=provenance.worktree_dirty,
        environ=environ,
    )
    return Preparation(
        manifest=manifest,
        matrix=matrix,
        problems=problems,
        plan=plan,
        readiness=readiness,
        startup=startup,
        cost=cost,
        guards=guards,
        provenance={
            "agent_commit": provenance.commit,
            "agent_worktree_dirty": provenance.worktree_dirty,
        },
        smoke=smoke or scenario_ids is not None,
    )


def _agent_base(manifest: Mapping[str, Any], provenance: Any) -> dict[str, Any]:
    return base_readiness(manifest, provenance).get("agent_base", {"ready": False})


def p_not_run_reason(readiness: Mapping[str, Mapping[str, Any]]) -> str:
    if readiness["HNP-E3"]["ready"]:
        return "HNP-E3 is present but hnp-pilot does not wire it yet; P stays a prediction"
    return "HNP-E3 is not merged: P does not run and its cells stay predictions (amendment E4)"


def dry_run_document(preparation: Preparation) -> dict[str, Any]:
    manifest, plan = preparation.manifest, preparation.plan
    execution = manifest["execution"]
    runnable = [episode for episode in plan if episode.runs]
    layer_b = manifest["layers"]["B"]
    return {
        "manifest_id": manifest["manifest_id"],
        "matrix_id": preparation.matrix["matrix_id"],
        "experiment": manifest["experiment"],
        "approval": manifest["approval"]["status"],
        **preparation.provenance,
        "smoke": preparation.smoke,
        "manifest_and_matrix_valid": not preparation.problems,
        "problems": preparation.problems,
        "destination_accepted": destination_accepted(),
        "policy": {key: manifest["policy"][key] for key in ("policy_set_id", "revision", "key_id")}
        | {"rules": manifest["policy"]["rules"]},
        "conditions": {
            condition: {
                "governance_mode": GOVERNANCE_MODES.get(condition),
                "runs": condition != "P",
                "environment": described_environment(manifest, condition),
            }
            for condition in LEDGER_CONDITIONS
        }
        | {"P": {"runs": False, "reason": p_not_run_reason(preparation.readiness)}},
        "startup_additions": {
            condition: startup_additions(condition) for condition in LEDGER_CONDITIONS
        },
        "agent_startup": preparation.startup,
        "layers": {
            "A": {"selector": "deterministic; HN11 scripted_adversarial injected by code"},
            "B": {"selector": "llm", "model": layer_b["model"]},
        },
        "scripted_selector": {
            **hnp_pilot_agent.injection_readiness(),
            "variant_id": hnp_pilot_agent.scripted_variant(preparation.matrix),
        },
        "execution": {
            "order": execution["order"],
            "task_poll_timeout_seconds": execution["task_poll_timeout_seconds"],
            "openai_timeout_seconds": execution["openai_timeout_seconds"],
            "trigger_route": f"POST {RUN_ROUTE}",
        },
        "episodes_registered": manifest["episodes"],
        "episodes_planned": episode_counts(plan),
        "episodes_runnable": episode_counts(runnable),
        "cost": preparation.cost.describe()
        | {"price_usd_per_mtok": manifest["cost"]["price_usd_per_mtok"]},
        "integration_readiness": preparation.readiness,
        "live_guards": preparation.guards,
        "live_run_would_start": all(guard["ok"] for guard in preparation.guards.values()),
        "plan": [episode.key() | {"runs": episode.runs} for episode in plan],
    }


# Live run ----------------------------------------------------------------------------------------


Execute = Callable[[PlannedEpisode, int], dict[str, Any]]


def run_plan(
    plan: Sequence[PlannedEpisode],
    execute: Execute,
    *,
    cap_usd: float,
    spent: Callable[[], float],
    result_file: TextIO,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the plan in order and write each row as it ends. After every episode `spent` gives the
    invocation's model cost, every row re-valued (`CostMeter.total_usd`); reaching the cap stops
    the invocation with the rows so far. Before that episode the total was under the cap, so the
    cap is passed by at most what that episode added."""
    rows: list[dict[str, Any]] = []
    stop: dict[str, Any] = {"stopped_by": None, "accumulated_cost_usd": 0.0, "cap_usd": cap_usd}
    for index, episode in enumerate(plan):
        if not episode.runs:
            continue
        row = execute(episode, index)
        accumulated = spent()
        row["accumulated_cost_usd"] = stop["accumulated_cost_usd"] = round(accumulated, 6)
        rows.append(row)
        result_file.write(json.dumps(row, ensure_ascii=False) + "\n")
        result_file.flush()
        if accumulated >= cap_usd:
            stop |= {
                "stopped_by": "cost_cap",
                "after_episode": episode.key(),
                "not_run": [later.key() for later in plan[index + 1 :] if later.runs],
            }
            break
    return rows, stop


def busy_ports(ports: Iterable[int]) -> list[int]:
    """Ports of the loopback stack that something already holds. The runner waits for a 200 on
    each port, so another stack there (a demo-stack) would stand in for the pilot's merchant and
    PSP, with other AP2 keys."""
    busy: list[int] = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            listening = probe.connect_ex(("127.0.0.1", port)) == 0
        if not listening:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    listening = True
        if listening:
            busy.append(port)
    return busy


def require_live_start(ports: Iterable[int], environ: Mapping[str, str]) -> None:
    """Past the guards: never under pytest, and only on free ports."""
    if "PYTEST_CURRENT_TEST" in environ:
        raise PilotError("A live HNP pilot run never starts under pytest")
    busy = busy_ports(ports)
    if busy:
        raise PilotError(
            f"Ports already in use: {busy}; stop the other stack before a live HNP pilot run"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the plan and the readiness; starts nothing and calls no model",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        help="only these scenarios (a smoke: its rows are not the pilot)",
    )
    parser.add_argument(
        "--smoke", action="store_true", help="one repetition per cell; validates the harness"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--merchant-repo", type=Path, default=ROOT.parent / "tesis-commerce-merchant"
    )
    parser.add_argument(
        "--payment-repo", type=Path, default=ROOT.parent / "tesis-payment-provider-sandbox"
    )
    parser.add_argument("--merchant-port", type=int, default=4120)
    parser.add_argument("--payment-port", type=int, default=4130)
    parser.add_argument("--agent-port", type=int, default=43125)
    parser.add_argument("--auth-port", type=int, default=4215)
    parser.add_argument("--skip-postgres-start", action="store_true")
    return parser


def _run(arguments: argparse.Namespace) -> int:
    preparation = prepare(
        arguments.manifest.resolve(),
        scenario_ids=arguments.scenarios,
        smoke=arguments.smoke,
        stack=stack_variables(
            merchant_origin=f"http://127.0.0.1:{arguments.merchant_port}",
            auth_origin=f"http://127.0.0.1:{arguments.auth_port}",
        ),
        environ=os.environ,
    )
    if arguments.dry_run:
        print(json.dumps(dry_run_document(preparation), ensure_ascii=False))
        return 0
    require_guards(preparation.guards)
    require_live_start(
        (
            arguments.merchant_port,
            arguments.payment_port,
            arguments.agent_port,
            arguments.auth_port,
        ),
        os.environ,
    )
    assert preparation.cost.cap_usd is not None
    from commerce_evaluation.hnp_pilot_live import run_live

    output = arguments.output
    if output is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = RESULTS / f"hnp-pilot-{stamp}.jsonl"
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hnp-pilot-") as temporary:
        summary = run_live(
            preparation,
            arguments=arguments,
            output=output,
            temp=Path(temporary),
            invocation_id=f"hnp-pilot-{uuid4().hex[:12]}",
        )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _run(_parser().parse_args(argv))
    except PilotError as error:
        print(f"hnp pilot failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
