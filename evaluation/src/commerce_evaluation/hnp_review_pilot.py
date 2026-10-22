"""The HNP intent reviewer pilot (docs/design/hnp-review-pilot.md) on the `hnp-pilot` runner.

`hnp-pilot --manifest evaluation/data/hnp-review-pilot-v1.json --dry-run` validates the manifest
and its matrix, prints the plan, the environment of each condition and scenario (names only),
whether the agent would start, the readiness of the reviewer (R1) and of the v1 dependencies, and
every guard of a live run; it never starts a service or calls a model. A live run has the v1's
guards, accepts the plan approval registered in the manifest and reuses the v1 harness: the
episode flow, `BuyerAccount` and `JwksPace` (amendment H1), the merchant CLI and the cost meter.
`--rescore` scores a results file again from its rows alone.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

from commerce_agent import api
from commerce_agent.config import Settings
from commerce_agent.delegation.contract import EscalationCode
from commerce_agent.trace.assembler import TraceContext, assemble
from commerce_agent.trace.sources import read_records
from commerce_evaluation import hnp_pilot, hnp_pilot_agent
from commerce_evaluation.governance_pilot import PilotError, _ap2_private_key, repository_provenance
from commerce_evaluation.hnp_pilot import (
    GENERATED,
    SECRET_VARIABLES,
    STARTUP_ADDITIONS,
    CostCap,
    InvocationSecrets,
    PlannedEpisode,
    destination_accepted,
    manifest_registration,
    startup_check,
)
from commerce_evaluation.hnp_review_scoring import (
    CONDITIONS,
    REVIEW_STEP,
    score_episode,
    summarize,
)
from commerce_evaluation.paths import DATA, RESULTS, ROOT
from commerce_evaluation.pilot_measures import read_only
from commerce_evaluation.pilot_policy import ALLOWLIST, scenario_environment
from commerce_evaluation.redaction import error_text

MANIFEST = DATA / "hnp-review-pilot-v1.json"
PILOT = "hnp-review"
LAYER = "R"
REVIEWER_VARIABLE = "AGENT_HNP_REVIEWER"
REVIEWERS = {"B2-HNP": "off", "B2-HNP-R": "llm"}
# The plan Yair approved; the pre-registration is fixed by the merge (manifest `approval`).
APPROVED = frozenset({"plan_approved", "approved"})
AGENT_FACTORY = "commerce_agent.api:app_factory"
# The v1 dependencies a live run also needs (hnp_pilot.REQUIRED_READINESS without the v1 tag).
V1_READINESS = ("HNP-05", "HNP-06", "HNP-08", "HNP-09a", "HNP-09b", "HNP-V1", "HNP-E2")
REQUIRED_READINESS = (*V1_READINESS, "R1", "R2", "agent_base")
# Both conditions are B2-HNP of the v1: the same startup additions (v1 decision 12).
ADDITIONS = STARTUP_ADDITIONS["B2-HNP"]


def is_review_manifest(path: Path) -> bool:
    """Whether `hnp-pilot --manifest` names this pilot's manifest (`pilot: hnp-review`)."""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("pilot") == PILOT
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def load_pilot(manifest_path: Path = MANIFEST) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = hnp_pilot._read_json(manifest_path, "HNP review pilot manifest")
    if manifest.get("pilot") != PILOT or "scenario_manifest" not in manifest:
        raise PilotError(f"Not an HNP review pilot manifest: {manifest_path}")
    matrix = hnp_pilot._read_json(
        ROOT / str(manifest["scenario_manifest"]), "HNP review scenario matrix"
    )
    if not isinstance(matrix.get("scenarios"), list):
        raise PilotError("The HNP review scenario matrix has no scenarios array")
    return manifest, matrix


def scenarios_in_order(
    manifest: Mapping[str, Any], matrix: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    by_id = {str(item["id"]): item for item in matrix["scenarios"]}
    order = list(manifest["execution"]["scenario_order"])
    unknown = [scenario_id for scenario_id in order if scenario_id not in by_id]
    if unknown:
        raise PilotError(f"The scenario order lists scenarios the matrix lacks: {unknown}")
    return [by_id[scenario_id] for scenario_id in order]


def validate_pilot(
    manifest: Mapping[str, Any], matrix: Mapping[str, Any], manifest_path: Path = MANIFEST
) -> list[str]:
    """Every way the matrix departs from what the manifest registers; empty when they agree."""
    problems: list[str] = []
    if (ROOT / str(matrix.get("manifest"))).resolve() != manifest_path.resolve():
        problems.append(f"the matrix names another manifest: {matrix.get('manifest')}")
    if list(manifest["conditions"]) != list(CONDITIONS) or list(matrix["conditions"]) != list(
        CONDITIONS
    ):
        problems.append(f"conditions differ from {list(CONDITIONS)}")
    for condition, reviewer in REVIEWERS.items():
        spec = manifest["conditions"].get(condition) or {}
        environment = spec.get("agent_environment") or {}
        if (
            environment.get(REVIEWER_VARIABLE) != reviewer
            or environment.get("AGENT_GOVERNANCE_MODE") != "enforce"
            or spec.get("health") != {"governance_mode": "enforce", "hnp_reviewer": reviewer}
        ):
            problems.append(f"{condition}: enforce with the reviewer {reviewer} is not registered")
    try:
        scenarios = scenarios_in_order(manifest, matrix)
    except PilotError as error:
        return [*problems, str(error)]
    if sorted(item["id"] for item in scenarios) != sorted(
        str(item["id"]) for item in matrix["scenarios"]
    ):
        problems.append("the scenario order does not list each matrix scenario exactly once")
    repetitions = int(manifest["repetitions_per_condition"])
    catalog = matrix["catalog"]
    grants = {
        (tuple(item["delegation"]["variant_ids"]), item["delegation"]["total_max_minor"])
        for item in scenarios
    }
    if len(grants) != 1:
        problems.append("the scenarios do not share one grant")
    by_scenario: dict[str, int] = {}
    for scenario in scenarios:
        prefix = f"{scenario['id']}:"
        delegation, selector = scenario["delegation"], scenario["selector"]
        if dict(scenario["runs"]) != dict.fromkeys(CONDITIONS, repetitions):
            problems.append(f"{prefix} runs {scenario['runs']} instead of {repetitions}")
        by_scenario[str(scenario["id"])] = sum(int(value) for value in scenario["runs"].values())
        if set(scenario["expected"]) != set(CONDITIONS):
            problems.append(f"{prefix} an expectation per condition is missing")
        if delegation.get("details") != hnp_pilot.BUYER_DETAILS or not delegation.get(
            "preference_text"
        ):
            problems.append(f"{prefix} the buyer details or the preference are not registered")
        if scenario.get("stimuli") or scenario["trigger"].get("mode") != "once":
            problems.append(f"{prefix} the review pilot has no stimuli and one trigger")
        if selector["kind"] == "llm":
            if selector != {"kind": "llm", "model": manifest["model"]}:
                problems.append(f"{prefix} the llm selector does not run the registered model")
        elif selector["kind"] == "scripted":
            variant = selector.get("returns_variant_id")
            if variant not in delegation["variant_ids"] or variant not in catalog["variants"]:
                problems.append(f"{prefix} the scripted variant is not in the grant and the feed")
        else:
            problems.append(f"{prefix} unknown selector {selector}")
    deviated = catalog.get("deviated_variant")
    variants, total_max = next(iter(grants)) if grants else ((), 0)
    if (
        deviated not in variants
        or deviated == catalog.get("preferred_variant")
        or catalog["variants"].get(deviated, {}).get("total_minor", total_max + 1) > total_max
    ):
        problems.append("the deviated variant is not in the grant, within the total and another")
    forbidding = [
        item["id"]
        for item in scenarios
        if deviated in item["oracle"].get("forbidden_variant_ids", [])
    ]
    if forbidding != [manifest["hypotheses"]["H-capture"]["scenario"]]:
        problems.append(f"only the capture scenario forbids the deviated variant: {forbidding}")
    registered = manifest["episodes"]
    counts = {condition: repetitions * len(scenarios) for condition in CONDITIONS}
    if counts != registered["by_condition"] or by_scenario != registered["by_scenario"]:
        problems.append("the episodes differ from the manifest")
    if sum(counts.values()) != registered["total"]:
        problems.append("the episodes do not add up to the registered total")
    if destination_accepted() is not True:
        problems.append("the merchant does not deliver to the synthetic Bogota buyer")
    return problems


# Plan --------------------------------------------------------------------------------------------


def condition_order(repetition: int) -> list[str]:
    """Within a repetition both conditions; the first alternates: B2-HNP in odd repetitions,
    B2-HNP-R in even ones (manifest `execution.order`, as the v1)."""
    return list(CONDITIONS) if repetition % 2 == 1 else list(CONDITIONS[::-1])


def build_plan(
    manifest: Mapping[str, Any],
    matrix: Mapping[str, Any],
    *,
    scenario_ids: Sequence[str] | None = None,
    smoke: bool = False,
) -> list[PlannedEpisode]:
    """By scenario (`execution.scenario_order`) and repetition, the conditions interleaved."""
    wanted = None if scenario_ids is None else set(scenario_ids)
    repetitions = 1 if smoke else int(manifest["repetitions_per_condition"])
    return [
        PlannedEpisode(LAYER, scenario, repetition, condition)
        for scenario in scenarios_in_order(manifest, matrix)
        if wanted is None or scenario["id"] in wanted
        for repetition in range(1, repetitions + 1)
        for condition in condition_order(repetition)
    ]


def episode_counts(plan: Sequence[PlannedEpisode]) -> dict[str, Any]:
    by_condition: dict[str, int] = dict.fromkeys(CONDITIONS, 0)
    by_scenario: dict[str, int] = {}
    for episode in plan:
        by_condition[episode.condition] += 1
        by_scenario[episode.scenario_id] = by_scenario.get(episode.scenario_id, 0) + 1
    return {"by_condition": by_condition, "by_scenario": by_scenario, "total": len(plan)}


# Environment -------------------------------------------------------------------------------------


def agent_factory(scenario: Mapping[str, Any]) -> str:
    """The product's factory, or the scenario's scripted one (injected by code, never by a
    variable)."""
    if scenario["selector"]["kind"] == "scripted":
        return hnp_pilot_agent.scenario_factory(str(scenario["id"]))
    return AGENT_FACTORY


def episode_environment(
    manifest: Mapping[str, Any],
    condition: str,
    *,
    scenario: Mapping[str, Any],
    invocation: InvocationSecrets,
    provider_key: str,
    merchant_origin: str,
) -> dict[str, str]:
    """The agent variables of a condition in one scenario, all explicit over the agent's .env."""
    declared: Mapping[str, str] = manifest["conditions"][condition]["agent_environment"]
    environment: dict[str, str] = {}
    for name, value in [*declared.items(), *ADDITIONS.items()]:
        if value == GENERATED:
            environment[name] = getattr(invocation, SECRET_VARIABLES[name])
        elif name == "AGENT_HNP_SELECTOR":
            # HR02 and HR03's scripted selector replaces it by code (hnp_pilot_agent).
            environment[name] = "llm" if scenario["selector"]["kind"] == "llm" else "deterministic"
        elif name == "AGENT_AP2_PRIVATE_KEY_PEM":
            environment[name] = provider_key
        elif name == ALLOWLIST:
            environment[name] = merchant_origin
        elif hnp_pilot._LITERAL.fullmatch(value):
            environment[name] = value
        else:
            raise PilotError(f"{condition}: the manifest value of {name} cannot be resolved")
    environment["OPENAI_TIMEOUT_SECONDS"] = (
        f"{float(manifest['execution']['openai_timeout_seconds']):g}"
    )
    return scenario_environment(environment, scenario, merchant_origin)


def described_environment(manifest: Mapping[str, Any], condition: str) -> list[dict[str, str]]:
    """Names and where each value comes from; never a value."""
    declared: Mapping[str, str] = manifest["conditions"][condition]["agent_environment"]
    rows = [
        {
            "name": name,
            "source": "generated_per_invocation"
            if value == GENERATED
            else "derived"
            if name in hnp_pilot.DERIVED_VARIABLES
            else "manifest",
        }
        for name, value in declared.items()
    ]
    rows += [{"name": name, "source": "startup_addition"} for name in ADDITIONS]
    rows.append({"name": "OPENAI_TIMEOUT_SECONDS", "source": "manifest_execution"})
    return rows


def episode_startup(environment: Mapping[str, str], stack: Mapping[str, str]) -> dict[str, Any]:
    """Whether the agent's startup accepts the variables. A model selector or reviewer needs a
    key: the agent reads its own, so the check only lends a placeholder that never leaves it."""
    uses_model = "llm" in (
        environment.get("AGENT_HNP_SELECTOR"),
        environment.get(REVIEWER_VARIABLE),
    )
    lent = {"OPENAI_API_KEY": "dry-run-placeholder"} if uses_model else {}
    return startup_check({**environment, **lent}, stack)


# Readiness and guards ----------------------------------------------------------------------------


def reviewer_readiness() -> dict[str, Any]:
    """R1 on this checkout: the switch, its `/health` field and the new escalation."""
    checks = {
        "settings": "agent_hnp_reviewer" in Settings.model_fields,
        "health": "hnp_reviewer" in api.Health.model_fields,
        "escalation": "choice_not_confirmed" in get_args(EscalationCode),
    }
    return {"ready": all(checks.values()), **checks}


def scripted_readiness(matrix: Mapping[str, Any]) -> dict[str, Any]:
    """Each scripted scenario has its factory, with the variant its matrix registers."""
    injection = hnp_pilot_agent.injection_readiness()
    registered = hnp_pilot_agent.scripted_variants()
    factories = {
        str(item["id"]): {
            "factory": hnp_pilot_agent.scenario_factory(str(item["id"])),
            "variant_id": registered.get(str(item["id"])),
            "ready": registered.get(str(item["id"])) == item["selector"]["returns_variant_id"]
            and callable(getattr(hnp_pilot_agent.scenario, str(item["id"]), None)),
        }
        for item in matrix["scenarios"]
        if item["selector"]["kind"] == "scripted"
    }
    return {
        "ready": injection["ready"] and all(item["ready"] for item in factories.values()),
        "seam": injection["seam"],
        "factories": factories,
    }


def integration_readiness(
    manifest: Mapping[str, Any], matrix: Mapping[str, Any], *, agent_base: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """The v1 dependencies, checked as the v1 runner does, plus R1 and this pilot's factories."""
    v1_manifest, _ = hnp_pilot.load_pilot()
    v1 = hnp_pilot.integration_readiness(v1_manifest, agent_base=agent_base)
    return {
        **{name: v1[name] for name in V1_READINESS},
        "R1": reviewer_readiness(),
        "R2": scripted_readiness(matrix),
        "agent_base": dict(agent_base),
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
    """Each reason a live run would refuse to start, the v1's; none has an override."""
    approval = manifest["approval"]
    missing = [name for name in REQUIRED_READINESS if readiness[name]["ready"] is not True]
    failed = [name for name, check in startup.items() if not check["ok"]]
    return {
        "hnp_live": {
            "ok": environ.get(hnp_pilot.LIVE_VARIABLE) == "1",
            "variable": hnp_pilot.LIVE_VARIABLE,
        },
        "approval": {
            "ok": approval.get("status") in APPROVED,
            "status": approval.get("status"),
            "fixed_on": approval.get("fixed_on"),
        },
        "cost_cap": {"ok": cost.error is None, "error": cost.error},
        "manifest_and_matrix": {"ok": not problems, "problems": list(problems)},
        "registered_manifest": dict(registration),
        "integrations": {"ok": not missing, "missing": missing},
        "agent_startup": {"ok": not failed, "failed": failed},
        "agent_worktree": {"ok": not worktree_dirty, "dirty": worktree_dirty},
    }


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
    manifest, matrix = load_pilot(manifest_path)
    problems = validate_pilot(manifest, matrix, manifest_path)
    unknown = sorted(set(scenario_ids or ()) - {str(item["id"]) for item in matrix["scenarios"]})
    if unknown:
        raise PilotError(f"Unknown scenarios: {', '.join(unknown)}")
    provenance = repository_provenance(ROOT)
    readiness = integration_readiness(
        manifest, matrix, agent_base=hnp_pilot._agent_base(manifest, provenance)
    )
    plan = build_plan(manifest, matrix, scenario_ids=scenario_ids, smoke=smoke)
    invocation = InvocationSecrets.generate()
    provider_key = _ap2_private_key()
    startup = {
        f"{condition}/{item['id']}": episode_startup(
            episode_environment(
                manifest,
                condition,
                scenario=item,
                invocation=invocation,
                provider_key=provider_key,
                merchant_origin=stack["MERCHANT_BASE_URL"],
            ),
            stack,
        )
        for condition in CONDITIONS
        for item in matrix["scenarios"]
    }
    cost = CostCap.read(manifest, environ)
    guards = live_guards(
        manifest,
        problems=problems,
        readiness=readiness,
        cost=cost,
        startup=startup,
        registration=manifest_registration(
            manifest_path, ROOT / str(manifest["scenario_manifest"]), registered=MANIFEST
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


def dry_run_document(preparation: Preparation) -> dict[str, Any]:
    manifest, matrix, plan = preparation.manifest, preparation.matrix, preparation.plan
    execution = manifest["execution"]
    return {
        "manifest_id": manifest["manifest_id"],
        "matrix_id": matrix["matrix_id"],
        "pilot": manifest["pilot"],
        "approval": manifest["approval"]["status"],
        **preparation.provenance,
        "smoke": preparation.smoke,
        "manifest_and_matrix_valid": not preparation.problems,
        "problems": preparation.problems,
        "destination_accepted": destination_accepted(),
        "policy": {key: manifest["policy"][key] for key in ("policy_set_id", "revision", "key_id")},
        "conditions": {
            condition: {
                "governance_mode": "enforce",
                "hnp_reviewer": REVIEWERS[condition],
                "health_check": manifest["conditions"][condition]["health"],
                "environment": described_environment(manifest, condition),
            }
            for condition in CONDITIONS
        },
        "startup_additions": dict(ADDITIONS),
        "agent_startup": preparation.startup,
        "scenarios": {
            str(item["id"]): {
                "selector": item["selector"],
                "agent_factory": agent_factory(item),
                "preference_text": item["delegation"]["preference_text"],
                "grant_variant_ids": item["delegation"]["variant_ids"],
                "oracle": item["oracle"]["text"],
            }
            for item in scenarios_in_order(manifest, matrix)
        },
        "deviated_variant": matrix["catalog"]["deviated_variant"],
        "scripted_selectors": preparation.readiness["R2"],
        "execution": {
            "order": execution["order"],
            "scenario_order": execution["scenario_order"],
            "task_poll_timeout_seconds": execution["task_poll_timeout_seconds"],
            "openai_timeout_seconds": execution["openai_timeout_seconds"],
            "trigger_route": f"POST {hnp_pilot.RUN_ROUTE}",
        },
        "hypotheses": {
            name: spec.get("prediction") for name, spec in manifest["hypotheses"].items()
        },
        "episodes_registered": manifest["episodes"],
        "episodes_planned": episode_counts(plan),
        "cost": preparation.cost.describe()
        | {
            "price_usd_per_mtok": manifest["cost"]["price_usd_per_mtok"],
            "model_calls": manifest["cost"]["model_calls"],
            "estimate_usd": manifest["cost"]["estimate_usd"],
        },
        "integration_readiness": preparation.readiness,
        "live_guards": preparation.guards,
        "live_run_would_start": all(guard["ok"] for guard in preparation.guards.values()),
        "plan": [episode.key() for episode in plan],
    }


# Readings after the stop -------------------------------------------------------------------------


def read_review(database: Path, *, grant_id: str, run_id: str | None) -> dict[str, Any]:
    """The grant's `choice_reviewed` ledger events (only the contract's fields) and the HNP
    trace the product builds for the task's run, read only after the agent stopped."""
    with closing(read_only(database)) as connection:
        rows = connection.execute(
            "SELECT sequence, body FROM delegation_events "
            "WHERE grant_id = ? AND event_type = ? ORDER BY sequence",
            (grant_id, REVIEW_STEP),
        ).fetchall()
    events = []
    for sequence, body in rows:
        fields = json.loads(body)
        events.append(
            {"sequence": sequence}
            | {name: fields.get(name) for name in ("decision", "reason", "prompt_id", "model")}
        )
    return {"events": events, "trace": product_trace(database, run_id)}


def product_trace(database: Path, run_id: str | None) -> dict[str, Any]:
    """The step kinds of the run's HNP trace, as `/purchases/{run_id}/trace` builds it. A run
    without a purchase row (it ended before `start`) has no trace: that is not an error."""
    if run_id is None:
        return {"available": False, "reason": "no_run", "step_kinds": None}
    graph = database.with_name(f"{database.stem}.graph.db")
    try:
        records = read_records(database, graph, run_id)
        if records is None:
            return {"available": False, "reason": "no_purchase", "step_kinds": None}
        trace = assemble(records, TraceContext())
    except (sqlite3.Error, ValueError, LookupError, KeyError) as error:
        return {"available": False, "reason": error_text(error), "step_kinds": None}
    except Exception as error:
        return {"available": False, "reason": type(error).__name__, "step_kinds": None}
    return {"available": True, "reason": None, "step_kinds": [step.kind for step in trace.steps]}


# Rescore -----------------------------------------------------------------------------------------

SCORED = ("expectation_errors", "unmeasured", "metrics")


def rescore(path: Path, manifest: Mapping[str, Any], matrix: Mapping[str, Any]) -> dict[str, Any]:
    """Every row scored again from its `observed` and `cost`, and the summary of the file. The
    rows that differ from what they recorded are listed; `flow_trace` is never read."""
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError) as error:
        raise PilotError(f"Cannot read results to rescore: {path}") from error
    if not rows:
        raise PilotError(f"No rows to rescore in {path}")
    scenarios = {str(item["id"]): item for item in matrix["scenarios"]}
    rescored: list[dict[str, Any]] = []
    differences: list[dict[str, Any]] = []
    for line, row in enumerate(rows, start=1):
        if "observed" in row:
            scored = score_episode(
                scenarios[row["scenario_id"]], row["condition"], row["observed"], row.get("cost")
            )
            changed = [name for name in SCORED if row.get(name) != scored[name]]
            if changed:
                differences.append({"line": line, "fields": changed})
            row = row | scored
        rescored.append(row)
    return {
        "results": str(path),
        "manifest_id": manifest["manifest_id"],
        "rows": len(rows),
        "row_differences": differences,
        "summary": summarize(rescored, manifest),
    }


# Command -----------------------------------------------------------------------------------------


def run(arguments: argparse.Namespace) -> int:
    """`hnp-pilot --manifest <this pilot's manifest>`: dry run, rescore or the live run."""
    manifest_path = arguments.manifest.resolve()
    if arguments.rescore is not None:
        manifest, matrix = load_pilot(manifest_path)
        document = rescore(arguments.rescore.resolve(), manifest, matrix)
        print(json.dumps(document, ensure_ascii=False))
        return 0
    preparation = prepare(
        manifest_path,
        scenario_ids=arguments.scenarios,
        smoke=arguments.smoke,
        stack=hnp_pilot.stack_variables(
            merchant_origin=f"http://127.0.0.1:{arguments.merchant_port}",
            auth_origin=f"http://127.0.0.1:{arguments.auth_port}",
        ),
        environ=os.environ,
    )
    if arguments.dry_run:
        print(json.dumps(dry_run_document(preparation), ensure_ascii=False))
        return 0
    hnp_pilot.require_guards(preparation.guards)
    hnp_pilot.require_live_start(
        (
            arguments.merchant_port,
            arguments.payment_port,
            arguments.agent_port,
            arguments.auth_port,
        ),
        os.environ,
    )
    from commerce_evaluation.hnp_review_live import run_live

    output = arguments.output
    if output is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = RESULTS / f"hnp-review-pilot-{stamp}.jsonl"
    summary = run_live(preparation, arguments=arguments, output=output.resolve())
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(hnp_pilot.main(["--manifest", str(MANIFEST), *sys.argv[1:]]))
