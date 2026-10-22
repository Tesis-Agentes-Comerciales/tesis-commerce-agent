"""The live HNP intent reviewer pilot: the v1's stack, harness and results files.

Only `hnp-pilot --manifest <review manifest>` without `--dry-run` reaches this module, after every
guard of hnp_review_pilot.py has passed. The leader runs it by hand after the merge; tests and CI
never do (manifest `cost.who_runs_it`). It differs from hnp_pilot_live.run_live only where the
review pilot does: each scripted scenario starts its own factory, `/health` must report the
condition's reviewer, the row adds the review readings, and the scoring is the review pilot's.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from commerce_evaluation.governance_pilot import (
    INVOCATION_SECRETS,
    ConditionMismatchError,
    _tail,
    prepare_services,
    run_artifacts_root,
    running_services,
    services_environment,
    services_provenance,
)
from commerce_evaluation.hnp_pilot import (
    InvocationSecrets,
    PlannedEpisode,
    run_plan,
    stack_variables,
)
from commerce_evaluation.hnp_pilot_episode import (
    AgentApi,
    BuyerAccount,
    EpisodeRun,
    SeededEpisodeError,
    _measure,
    observe,
    run_episode,
)
from commerce_evaluation.hnp_pilot_live import (
    CliMerchant,
    JwksPace,
    UvicornAgent,
    finished_episode,
    verify_task,
)
from commerce_evaluation.hnp_pilot_scoring import CostMeter
from commerce_evaluation.hnp_review_pilot import (
    ADDITIONS,
    REVIEWERS,
    Preparation,
    agent_factory,
    episode_counts,
    episode_environment,
    read_review,
)
from commerce_evaluation.hnp_review_scoring import score_episode, summarize
from commerce_evaluation.paths import ROOT
from commerce_evaluation.redaction import secret_values
from commerce_evaluation.run_artifacts import RunArtifacts


def run_live(
    preparation: Preparation, *, arguments: argparse.Namespace, output: Path
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hnp-review-pilot-") as temporary:
        return _run_live(
            preparation,
            arguments=arguments,
            output=output,
            temp=Path(temporary),
            invocation_id=f"hnp-review-pilot-{uuid4().hex[:12]}",
            artifacts_root=run_artifacts_root(arguments.artifacts_dir),
        )


def _run_live(
    preparation: Preparation,
    *,
    arguments: argparse.Namespace,
    output: Path,
    temp: Path,
    invocation_id: str,
    artifacts_root: Path,
) -> dict[str, Any]:
    manifest, matrix = preparation.manifest, preparation.matrix
    assert preparation.cost.cap_usd is not None
    merchant_repo = arguments.merchant_repo.resolve()
    payment_repo = arguments.payment_repo.resolve()
    run_metadata: dict[str, Any] = {
        "manifest_id": manifest["manifest_id"],
        "matrix_id": matrix["matrix_id"],
        "invocation_id": invocation_id,
        "smoke": preparation.smoke,
        **preparation.provenance,
        **services_provenance(
            {"merchant": merchant_repo, "payment": payment_repo}, allow_dirty=False
        ),
    }
    environment = services_environment(
        merchant_port=arguments.merchant_port,
        payment_port=arguments.payment_port,
        agent_port=arguments.agent_port,
        auth_port=arguments.auth_port,
    )
    stack = stack_variables(
        merchant_origin=environment["MERCHANT_BASE_URL"],
        auth_origin=environment["BETTER_AUTH_URL"],
    )
    invocation = InvocationSecrets.generate()
    artifacts = RunArtifacts(
        root=artifacts_root,
        invocation_id=invocation_id,
        runner="hnp-pilot",
        provenance=run_metadata,
    )
    artifacts.hide(*invocation.values(), *secret_values(environment, INVOCATION_SECRETS))
    meter = CostMeter.from_manifest(manifest)
    started_at = datetime.now(UTC).isoformat()
    poll_timeout = float(manifest["execution"]["task_poll_timeout_seconds"])
    prepare_services(
        ROOT,
        merchant_repo,
        payment_repo,
        environment,
        skip_postgres_start=arguments.skip_postgres_start,
    )
    with running_services(
        agent=ROOT,
        merchant=merchant_repo,
        payment=payment_repo,
        environment=environment,
        temp=temp,
        merchant_port=arguments.merchant_port,
        payment_port=arguments.payment_port,
    ):
        buyer = BuyerAccount(environment["BETTER_AUTH_URL"])
        pace = JwksPace()
        merchant = CliMerchant(merchant_repo, environment, buyer.signup())
        psp_database = Path(environment["PAYMENT_DATABASE_PATH"])

        def execute(episode: PlannedEpisode, index: int) -> dict[str, Any]:
            run_dir = (
                temp / f"{index:03d}-{episode.condition.lower()}-{episode.scenario_id.lower()}"
            )
            return finished_episode(artifacts, run_dir, episode_row(episode, index, run_dir))

        def episode_row(episode: PlannedEpisode, index: int, run_dir: Path) -> dict[str, Any]:
            scenario, condition = episode.scenario, episode.condition
            run_dir.mkdir(parents=True, exist_ok=True)
            variables = {
                **environment,
                **stack,
                **episode_environment(
                    manifest,
                    condition,
                    scenario=scenario,
                    invocation=invocation,
                    provider_key=environment["AGENT_AP2_PRIVATE_KEY_PEM"],
                    merchant_origin=environment["MERCHANT_BASE_URL"],
                ),
            }
            row: dict[str, Any] = {
                **run_metadata,
                **episode.key(),
                "plan_index": index,
                "model": manifest["model"],
                "hnp_reviewer": REVIEWERS[condition],
                "agent_factory": agent_factory(scenario),
                "startup_additions": dict(ADDITIONS),
                "recorded_at": datetime.now(UTC).isoformat(),
            }
            with (run_dir / "agent.log").open("w", encoding="utf-8") as log:
                agent = UvicornAgent(
                    port=arguments.agent_port, log=log, factory=row["agent_factory"], pace=pace
                )
                api = AgentApi(
                    agent.origin,
                    service_token=invocation.service_token,
                    operator_token=invocation.operator_token,
                )
                run: EpisodeRun | None = None
                try:
                    run = run_episode(
                        scenario,
                        condition,
                        api=api,
                        buyer=buyer,
                        merchant=merchant,
                        agent=agent,
                        variables=variables,
                        run_dir=run_dir,
                        poll_timeout_seconds=poll_timeout,
                        expected_reviewer=REVIEWERS[condition],
                    )
                except ConditionMismatchError:
                    raise
                except SeededEpisodeError as error:
                    run = error.run
                    row["harness_error"] = str(error)
                except Exception as error:
                    row["harness_error"] = f"{type(error).__name__}: {error}"
            artifacts.hide(*agent.secrets, run.token_probe if run is not None else None)
            if "harness_error" in row:
                tail = _tail(run_dir / "agent.log", tuple(artifacts.secrets))
                if tail:
                    print(f"Agent log tail for {run_dir.name}:\n{tail}", file=sys.stderr)
            if run is None:
                return row
            row |= {
                "merchant_run_id": run.merchant_run_id,
                "health": run.health,
                "grant_id": run.grant_id,
                "task_id": run.task_id,
                "trigger": run.trigger,
                "merchant_webhooks": run.webhooks,
            }
            if run.grant_id is None:
                return row
            observed, errors = observe(
                condition,
                run,
                run_dir=run_dir,
                psp_database=psp_database,
                merchant=merchant,
                verifier=verify_task,
            )
            task_run = (observed.get("task_view") or {}).get("run_id")
            grant_id = run.grant_id
            observed["review"] = _measure(
                errors,
                "review",
                lambda: read_review(run_dir / "agent.db", grant_id=grant_id, run_id=task_run),
            )
            cost = meter.charge(observed.get("model_usage"))
            row |= {
                "observed": observed,
                "measurement_errors": errors,
                "cost": cost,
                **score_episode(scenario, condition, observed, cost),
            }
            return row

        with output.open("w", encoding="utf-8") as result_file:
            rows, stop = run_plan(
                preparation.plan,
                execute,
                cap_usd=preparation.cost.cap_usd,
                spent=meter.total_usd,
                result_file=result_file,
            )
    summary = {
        **run_metadata,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "results": str(output),
        "episodes_planned": episode_counts(preparation.plan),
        "episodes_run": len(rows),
        "cost": meter.describe() | {"cap_usd": preparation.cost.cap_usd},
        "stop": dict(stop),
        "summary": summarize(rows, manifest),
    }
    output.with_suffix(".summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary
