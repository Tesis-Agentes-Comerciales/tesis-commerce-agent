"""The HNP pilot's plan (EC-HNP-03) on the governance pilot's stack: docs/design/hnp-pilot.md.

Loads the manifest and its scenario matrix, checks that they agree, builds the registered plan of
episodes and the agent environment of each condition, and checks that the agent's HNP startup
accepts that environment. Nothing here starts a service or calls a model.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from commerce_agent.acp.destination import merchant_fulfillment
from commerce_agent.application.purchase import CheckoutDetails
from commerce_agent.config import Settings
from commerce_agent.delegation.bootstrap import load_hnp_trust
from commerce_agent.governance.bootstrap import configured_d1
from commerce_evaluation.governance_pilot import (
    DETAILS,
    PilotError,
    _ap2_private_key,
)
from commerce_evaluation.paths import DATA, ROOT
from commerce_evaluation.pilot_policy import ALLOWLIST, HP_V2_POLICY, scenario_environment

MANIFEST = DATA / "hnp-pilot-v1.json"
CONDITIONS = ("B0-HNP", "B2-HNP", "P")
LEDGER_CONDITIONS = ("B0-HNP", "B2-HNP")
LAYERS = ("A", "B")
BUYER_DETAILS = "synthetic_bogota_buyer"
GENERATED = "generated_per_invocation_never_logged"
STIMULI = frozenset(
    {"merchant_update_offer", "wait_until_grant_expired", "owner_revoke", "operator_kill_switch"}
)
TRIGGER_MODES = frozenset({"once", "twice_concurrently"})

# Agent variables the manifest generates per invocation, and the secret each one takes.
SECRET_VARIABLES = {
    "AGENT_HNP_AGENT_KEY_PEM": "agent_key_pem",
    "AGENT_HNP_SERVICE_TOKEN": "service_token",
    "AGENT_HNP_OPERATOR_TOKEN": "operator_token",
    "AGENT_D1_HMAC_KEY_HEX": "d1_hmac_hex",
    "AGENT_D1_AES_KEY_HEX": "d1_aes_hex",
}
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
