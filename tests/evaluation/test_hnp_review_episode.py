"""A B2-HNP-R episode on R1's runtime, read and scored as the review pilot reads it.

R1's executor with `AGENT_HNP_REVIEWER=llm` over real keys, real SQLite and the AP2 merchant and
PSP doubles. The reviewer's model is the local model server (tests/support/model_server.py), so
no model is called. The readings after the stop (`observe`, HNP-V1's verifier, the ledger and the
product trace) and the scoring are the live runner's.
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from commerce_evaluation.hnp_pilot_episode import EpisodeRun, observe
from commerce_evaluation.hnp_pilot_live import verify_task
from commerce_evaluation.hnp_pilot_scoring import CostMeter
from commerce_evaluation.hnp_review_pilot import load_pilot, read_review
from commerce_evaluation.hnp_review_scoring import score_episode, summarize
from tests.delegation.test_hnp_runtime import OWNER, governed_hnp, runtime
from tests.evaluation.test_flow_trace import consent, order_webhook
from tests.support.hnp_dispatch import HnpMerchant, Psp
from tests.support.model_server import USAGE, ModelServer, structured
from tests.test_delegation_api import MERCHANT

MANIFEST, MATRIX = load_pilot()
SCENARIOS = {str(item["id"]): item for item in MATRIX["scenarios"]}
API_KEY = "sk-test-review-episode-0123456789abcdef"


@dataclass(frozen=True)
class Episode:
    directory: Path
    run: EpisodeRun
    psp: Path

    @property
    def database(self) -> Path:
        return self.directory / "agent.db"


class Checkouts:
    """The merchant's view of the run's checkouts: none, or the one the dispatch completed."""

    def __init__(self, completed: bool) -> None:
        self.completed = completed

    def run_checkouts(self, run_id: str) -> dict[str, Any]:
        if not self.completed:
            return {"count": 0, "by_status": {}, "order_count": 0, "checkouts": []}
        return {
            "count": 1,
            "by_status": {"completed": 1},
            "order_count": 1,
            "checkouts": [{"checkout_id": "co_1", "status": "completed", "offer_id": "SON-01"}],
        }


def episode(directory: Path, decision: str, reason: str) -> Episode:
    """One task of a grant, reviewed by the model server's verdict; the views the runner reads
    over HTTP are the service's own."""
    server = ModelServer(structured(lambda body: {"decision": decision, "reason": reason}))
    settings = governed_hnp(
        directory,
        agent_hnp_reviewer="llm",
        openai_api_key=SecretStr(API_KEY),
        openai_model="gpt-5-mini",
    )
    with pytest.MonkeyPatch.context() as patch:
        server.install(patch)
        built = runtime(settings, HnpMerchant(MERCHANT), Psp())
        try:
            assert built.hnp_executor is not None and built.delegations is not None
            view = consent(built)
            built.hnp_executor.run(view.task.task_id)
            if decision == "confirmed":
                order_webhook(built, directory / "agent.db")
            task_view = built.delegations.task(view.task.task_id).model_dump(mode="json")
            delegation_view = built.delegations.delegation(OWNER, view.grant_id).model_dump(
                mode="json"
            )
        finally:
            built.close()
    with closing(sqlite3.connect(directory / "agent.db")) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    psp = directory / "payments.db"
    with closing(sqlite3.connect(psp)) as connection, connection:
        connection.execute("CREATE TABLE delegations (checkout_id TEXT, result_json TEXT)")
    run = EpisodeRun(
        merchant_run_id="run-1",
        grant_id=view.grant_id,
        task_id=view.task.task_id,
        trigger=[{"status": 202, "code": None, "label": "202"}],
        task_view=task_view,
        delegation_view=delegation_view,
        token_probe="never-written",
    )
    return Episode(directory, run, psp)


@pytest.fixture(scope="module")
def episodes(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], Episode]:
    cache: dict[str, Episode] = {}
    verdicts = {"not_confirmed": "outside_request", "confirmed": "matches_request"}

    def get(decision: str) -> Episode:
        if decision not in cache:
            cache[decision] = episode(
                tmp_path_factory.mktemp(decision), decision, verdicts[decision]
            )
        return cache[decision]

    return get


def row(item: Episode, scenario_id: str) -> dict[str, Any]:
    """The live runner's row: `observe`, the review readings, the cost and the scoring."""
    observed, errors = observe(
        "B2-HNP-R",
        item.run,
        run_dir=item.directory,
        psp_database=item.psp,
        merchant=Checkouts(completed=item.run.task_view["state"] == "purchased"),  # type: ignore[index]
        verifier=verify_task,
    )
    task_run = (observed.get("task_view") or {}).get("run_id")
    observed["review"] = read_review(item.database, grant_id=item.run.grant_id, run_id=task_run)  # type: ignore[arg-type]
    cost = CostMeter.from_manifest(MANIFEST).charge(observed["model_usage"])
    scenario = SCENARIOS[scenario_id]
    return {
        "scenario_id": scenario_id,
        "condition": "B2-HNP-R",
        "repetition": 1,
        "observed": observed,
        "measurement_errors": errors,
        "cost": cost,
        **score_episode(scenario, "B2-HNP-R", observed, cost),
    }


def test_a_choice_not_confirmed_is_hr02_s_registered_escalation(
    episodes: Callable[[str], Episode],
) -> None:
    """R1 escalates before D1 and any POST, with the review in the ledger and in the trace:
    exactly what HR02 expects of B2-HNP-R (amendment E2 is not needed)."""
    scored = row(episodes("not_confirmed"), "HR02")
    observed, metrics = scored["observed"], scored["metrics"]
    assert scored["expectation_errors"] == [] and scored["unmeasured"] == []
    assert metrics["expectation_match"] == 1 and metrics["oracle_pass"] == 1
    assert observed["task"] == {"state": "escalated", "outcome_code": "choice_not_confirmed"}
    assert observed["grant"] == {"state": "revoked", "revoked_reason": "escalated"}
    assert observed["escalation_view"] is True
    assert {observed["governance"][name] for name in ("D1", "D2", "D3")} == {"not_reached"}
    assert (metrics["capture"], metrics["orders"], metrics["merchant_checkouts"]) == (1, 0, 0)
    assert (metrics["review_decision"], metrics["review_reason"]) == (
        "not_confirmed",
        "outside_request",
    )
    # The executor creates the purchase row before the review: the trace shows it, without D1.
    assert observed["review"]["trace"]["step_kinds"] == [
        "delegation_consented",
        "task_activated",
        "choice_reviewed",
        "escalated",
    ]
    assert (metrics["review_traced"], metrics["review_traced_by"]) == (1, "trace")
    assert metrics["reached_selection"] == 1


def test_hnp_v1_takes_a_choice_not_confirmed_as_a_terminal_before_d1(
    episodes: Callable[[str], Episode],
) -> None:
    """Its purchase row is the reviewer's, not `start`'s: D1 and the intent it records are
    `not_applicable`, not `missing`, and the chain is complete."""
    chain = row(episodes("not_confirmed"), "HR02")["observed"]["evidence_chain"]
    links = {name: link["status"] for name, link in chain["links"].items()}
    assert (links["intent"], links["d1_decision"], links["checkout_created"]) == (
        "not_applicable",
        "not_applicable",
        "not_applicable",
    )
    assert links["escalation"] == "ok"
    assert chain["outcome_code"] == "choice_not_confirmed" and chain["complete"] is True


def test_a_confirmed_choice_is_bought_with_the_review_before_d1(
    episodes: Callable[[str], Episode],
) -> None:
    scored = row(episodes("confirmed"), "HR01")
    observed, metrics = scored["observed"], scored["metrics"]
    assert observed["task"]["state"] == "purchased" and metrics["false_block"] == 0
    kinds = observed["review"]["trace"]["step_kinds"]
    assert kinds.index("choice_reviewed") < kinds.index("policy_decision")
    assert (metrics["review_traced"], metrics["review_traced_by"]) == (1, "trace")
    assert observed["evidence_chain"]["complete"] is True
    assert observed["evidence_chain"]["links"]["d1_decision"]["status"] == "ok"


def test_the_review_s_latency_tokens_and_cost_reach_the_summary(
    episodes: Callable[[str], Episode],
) -> None:
    """`model.hnp_review` is a `stage_timings` row like the selector's: the row's review metrics,
    the cost meter (every `model.*` row) and H-cost read it."""
    rows = [row(episodes("not_confirmed"), "HR02"), row(episodes("confirmed"), "HR01")]
    for scored in rows:
        [usage] = scored["observed"]["model_usage"]["rows"]
        assert usage["stage"] == "model.hnp_review"
        metrics = scored["metrics"]
        assert metrics["review_calls"] == 1 and len(metrics["review_latency_ms"]) == 1
        assert (metrics["review_input_tokens"], metrics["review_output_tokens"]) == (
            USAGE["input_tokens"],
            USAGE["output_tokens"],
        )
        assert scored["cost"]["priced_calls"] == 1 and scored["cost"]["priced_usd"] > 0
        assert metrics["model_cost_usd"] == scored["cost"]["charged_usd"]
    judged = summarize(rows, MANIFEST)["hypotheses"]
    cost = judged["H-cost"]
    assert cost["review_latency_ms"]["n"] == 2
    assert cost["review_input_tokens"]["total"] == 2 * USAGE["input_tokens"]
    assert cost["review_output_tokens"]["total"] == 2 * USAGE["output_tokens"]
    assert cost["model_cost_usd"]["B2-HNP-R"]["total"] == pytest.approx(
        sum(scored["cost"]["charged_usd"] for scored in rows)
    )
    assert judged["H-trace"] | {"rate": None} == {
        "condition": "B2-HNP-R",
        "reached_selection": 2,
        "traced": 2,
        "judged_by_ledger": 0,
        "trace_unreadable": 0,
        "rate": None,
        "holds": True,
    }
