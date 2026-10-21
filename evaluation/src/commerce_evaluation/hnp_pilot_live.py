"""The live HNP pilot on the governance pilot's stack: services, episodes and the results files.

Only `hnp-pilot` without `--dry-run` reaches this module, after every guard of hnp_pilot.py has
passed (approval, cost cap, integrations, commit). The leader runs it by hand; tests and CI never
do (manifest `cost.who_runs_it`).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from commerce_evaluation import hnp_pilot_agent, verify_evidence
from commerce_evaluation.governance_pilot import (
    AGENT_SECRETS,
    MUTATE_OFFER,
    SERVICE_SECRETS,
    ConditionMismatchError,
    _merchant_db,
    _request_json,
    _run_json,
    _tail,
    _terminate,
    _wait_http,
    dispatch_merchant_webhooks,
    merchant_checkouts,
    prepare_services,
    running_services,
    services_environment,
    services_provenance,
)
from commerce_evaluation.hnp_pilot import (
    LEDGER_CONDITIONS,
    InvocationSecrets,
    PlannedEpisode,
    Preparation,
    condition_environment,
    episode_counts,
    p_not_run_reason,
    run_plan,
    stack_variables,
    startup_additions,
)
from commerce_evaluation.hnp_pilot_episode import (
    AgentApi,
    BuyerAccount,
    EpisodeRun,
    SeededEpisodeError,
    observe,
    run_episode,
)
from commerce_evaluation.hnp_pilot_scoring import CostMeter, score_episode, summarize
from commerce_evaluation.paths import ROOT

AGENT_FACTORY = "commerce_agent.api:app_factory"


class CliMerchant:
    """The merchant's lab CLI, as the governance pilot drives it."""

    def __init__(self, repository: Path, environment: dict[str, str], actor_id: str) -> None:
        self.repository = repository
        self.environment = environment
        self.actor_id = actor_id

    def _json(self, command: list[str]) -> dict[str, Any]:
        return _run_json(command, cwd=self.repository, env=self.environment)

    def seed(self, label: str) -> str:
        run_id = self._json(_merchant_db("seed", "--actor-id", self.actor_id, "--variant", label))
        if not isinstance(run_id.get("run_id"), str):
            raise ValueError("Merchant seed did not return a run_id")
        return run_id["run_id"]

    def export_feed(self, run_id: str, directory: Path) -> None:
        self._json(
            [
                *("uv", "run", "python", "-m", "commerce_lab.feed", "export"),
                *("--output-dir", str(directory), "--run-id", run_id),
            ]
        )

    def issue_session(self, run_id: str) -> str:
        session = self._json(
            _merchant_db("issue-session", run_id, self.actor_id, "--ttl-seconds", "3600")
        )
        token = session.get("session_token")
        if not isinstance(token, str):
            raise ValueError("Merchant session command did not return a token")
        return token

    def update_offer(self, run_id: str, variant_id: str, price_minor: int) -> dict[str, Any]:
        return self._json(
            [
                *("uv", "run", "python", str(MUTATE_OFFER)),
                *("--run-id", run_id, "--offer-id", variant_id, "--price-minor", str(price_minor)),
            ]
        )

    def run_checkouts(self, run_id: str) -> dict[str, Any]:
        return merchant_checkouts(self.repository, self.environment, run_id)

    def dispatch_webhooks(self) -> dict[str, Any]:
        return dispatch_merchant_webhooks(self.repository, self.environment)


class UvicornAgent:
    """One agent process per start, on the episode's own agent.db; its log stays in the run."""

    def __init__(self, *, port: int, log: TextIO, factory: str) -> None:
        self.origin = f"http://127.0.0.1:{port}"
        self.port = port
        self.log = log
        self.factory = factory
        self.process: subprocess.Popen[str] | None = None
        # What the agent was started with that its log tail must never show (HP's AGENT_SECRETS,
        # the merchant session among them).
        self.secrets: set[str] = set()

    def start(self, variables: Mapping[str, str]) -> dict[str, Any]:
        self.secrets |= {variables[name] for name in AGENT_SECRETS if variables.get(name)}
        self.process = subprocess.Popen(
            [
                str(ROOT / ".venv" / "bin" / "python"),
                *("-m", "uvicorn", self.factory, "--factory"),
                *("--host", "127.0.0.1", "--port", str(self.port)),
            ],
            cwd=ROOT,
            env=dict(variables),
            text=True,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        _wait_http(f"{self.origin}/health", self.process, 45, require_ok_status=True)
        return _request_json(f"{self.origin}/health", timeout_seconds=5).body

    def stop(self) -> None:
        if self.process is not None:
            _terminate(self.process)
            self.process = None


def verify_task(
    database: Path, *, grant_id: str, task_id: str | None, psp_database: Path
) -> dict[str, Any] | None:
    """HNP-V1's document of the episode's task: verify-evidence by grant, HNP profile."""
    documents = verify_evidence.verify_database(
        database, psp_database=psp_database, grant_id=grant_id
    )
    return next(
        (
            document
            for document in documents
            if document.get("purchase_mode") == "HNP" and document.get("task_id") == task_id
        ),
        None,
    )


def run_live(
    preparation: Preparation,
    *,
    arguments: argparse.Namespace,
    output: Path,
    temp: Path,
    invocation_id: str,
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
            {"merchant": merchant_repo, "payment": payment_repo},
            allow_dirty=False,
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
    hidden = (*invocation.values(), *(environment[name] for name in SERVICE_SECRETS))
    meter = CostMeter.from_manifest(manifest)
    started_at = datetime.now(UTC).isoformat()
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
        merchant = CliMerchant(merchant_repo, environment, buyer.signup())
        psp_database = Path(environment["PAYMENT_DATABASE_PATH"])

        def execute(episode: PlannedEpisode, index: int) -> dict[str, Any]:
            scenario, condition = episode.scenario, episode.condition
            run_dir = temp / f"{index:03d}-{condition.lower()}-{episode.scenario_id.lower()}"
            run_dir.mkdir(parents=True, exist_ok=True)
            scripted = scenario["selector"]["kind"] == "scripted_adversarial"
            variables = {
                **environment,
                **stack,
                **condition_environment(
                    manifest,
                    condition,
                    layer=episode.layer,
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
                "model": manifest["layers"]["B"]["model"] if episode.layer == "B" else None,
                "agent_factory": hnp_pilot_agent.FACTORY if scripted else AGENT_FACTORY,
                "startup_additions": startup_additions(condition),
                "recorded_at": datetime.now(UTC).isoformat(),
            }
            with (run_dir / "agent.log").open("w", encoding="utf-8") as log:
                agent = UvicornAgent(
                    port=arguments.agent_port, log=log, factory=row["agent_factory"]
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
                        poll_timeout_seconds=float(
                            manifest["execution"]["task_poll_timeout_seconds"]
                        ),
                    )
                except ConditionMismatchError:
                    raise
                except SeededEpisodeError as error:
                    run = error.run
                    row["harness_error"] = str(error)
                except Exception as error:
                    row["harness_error"] = f"{type(error).__name__}: {error}"
            if "harness_error" in row:
                tail = _tail(run_dir / "agent.log", (*hidden, *agent.secrets))
                if tail:
                    print(f"Agent log tail for {run_dir.name}:\n{tail}", file=sys.stderr)
            if run is None:
                return row
            row |= {
                "merchant_run_id": run.merchant_run_id,
                "health": run.health,
                "grant_id": run.grant_id,
                "task_id": run.task_id,
                "stimuli": run.stimuli,
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
            cost = meter.charge(observed.get("model_usage"))
            row |= {
                "observed": observed,
                "measurement_errors": errors,
                "cost": cost,
                **score_episode(scenario, condition, observed, cost),
            }
            if episode.layer == "A" and cost["calls"] > 0 and "harness_error" not in row:
                row["harness_error"] = "a layer A row with a model call (manifest layers.A)"
            return row

        with output.open("w", encoding="utf-8") as result_file:
            rows, stop = run_plan(
                preparation.plan,
                execute,
                cap_usd=preparation.cost.cap_usd,
                spent=meter.total_usd,
                result_file=result_file,
            )
    summary = summary_document(
        rows,
        stop=stop,
        preparation=preparation,
        run_metadata=run_metadata | {"started_at": started_at},
        output=output,
        meter=meter,
    )
    output.with_suffix(".summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def summary_document(
    rows: list[dict[str, Any]],
    *,
    stop: Mapping[str, Any],
    preparation: Preparation,
    run_metadata: Mapping[str, Any],
    output: Path,
    meter: CostMeter,
) -> dict[str, Any]:
    costs = [row["cost"] for row in rows if "cost" in row]
    return {
        **run_metadata,
        "finished_at": datetime.now(UTC).isoformat(),
        "results": str(output),
        "episodes_planned": episode_counts(preparation.plan),
        "episodes_run": len(rows),
        "p_arm": p_not_run_reason(preparation.readiness),
        # Re-valued at the invocation's last unit; each row's `charged_usd` is at the unit of its
        # own time.
        "cost": meter.describe()
        | {
            "cap_usd": preparation.cost.cap_usd,
            "model_calls": sum(item["calls"] or 0 for item in costs),
            "unpriced_calls": sum(item["unpriced_calls"] for item in costs),
            "timed_out_calls": sum(item["timed_out_calls"] for item in costs),
            "usage_unmeasured": sum(item["usage_unmeasured"] for item in costs),
            "rows_without_cost": sum("cost" not in row for row in rows),
        },
        "stop": dict(stop),
        "summary": summarize(rows, LEDGER_CONDITIONS),
    }
