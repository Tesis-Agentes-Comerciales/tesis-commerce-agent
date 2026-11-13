"""The runner: which trials a suite runs, what it repeats or excludes, and how a run stops and
resumes (docs/design/eval-suite.md §2, §7)."""

import hashlib
import json
from pathlib import Path

import pytest

from commerce_evaluation.llm.transport import FundsExhausted
from commerce_evaluation.suite.cases import CASE
from commerce_evaluation.suite.executor import SimulatorFailure
from commerce_evaluation.suite.run import (
    CASES_FILE,
    EXCLUDED_FILE,
    INFRA_FILE,
    MANIFEST_FILE,
    TRIALS_FILE,
    RunMismatch,
    execute,
    pending,
    read_cases,
    select,
)
from tests.evaluation.suite.runs import (
    API_5XX,
    STACK_DOWN,
    FakeExecutor,
    adversarial_case,
    capability_case,
    lines,
    make_case,
    plan,
    regression_case,
    write_catalog,
)

CAPABILITY, ADVERSARIAL, REGRESSION = capability_case(), adversarial_case(), regression_case()
ALL = [CAPABILITY, ADVERSARIAL, REGRESSION]


def ran(run_dir: Path) -> list[tuple[str, str, int]]:
    rows = lines(run_dir / TRIALS_FILE)
    return [(row["case_id"], row["governance"], row["trial"]) for row in rows]


def test_each_suite_takes_the_cases_that_declare_it() -> None:
    assert select(ALL, "regression") == tuple(ALL)
    assert select(ALL, "capability") == (CAPABILITY,)
    assert select(ALL, "adversarial") == (ADVERSARIAL,)
    assert select(ALL, "regression", "llm-*") == (REGRESSION,)
    assert select(ALL, "capability", "llm-*") == ()


def test_each_governance_runs_k_times_with_a_seed_that_other_cases_do_not_move(
    tmp_path: Path,
) -> None:
    alone = plan(tmp_path / "a", tmp_path, [ADVERSARIAL], suite="adversarial", k=2).trials()
    together = plan(tmp_path / "b", tmp_path, ALL, suite="adversarial", k=2).trials()

    assert [(each.governance, each.trial) for each in alone] == [
        ("B0", 1),
        ("B0", 2),
        ("B2", 1),
        ("B2", 2),
    ]
    assert len({each.seed for each in alone}) == 4
    seeds = {each.key: each.seed for each in together}
    assert all(seeds[each.key] == each.seed for each in alone)
    other = plan(tmp_path / "c", tmp_path, [ADVERSARIAL], suite="adversarial", k=2, seed=2)
    assert {each.seed for each in other.trials()}.isdisjoint(each.seed for each in alone)


def test_a_run_keeps_every_trial_and_writes_its_manifest_first(tmp_path: Path) -> None:
    catalog = write_catalog(tmp_path / "cases", [CAPABILITY, ADVERSARIAL])
    run = plan(tmp_path / "run", catalog, [ADVERSARIAL], suite="adversarial", k=2)
    executor = FakeExecutor("adversarial")

    outcome = execute(run, executor, say=lambda _: None)

    assert outcome.status == "complete" and outcome.kept == 4
    assert ran(run.settings.run_dir) == [(each.case.id, *each.key[1:]) for each in run.trials()]
    assert [call[3] for call in executor.calls] == [each.seed for each in run.trials()]
    assert (executor.entered, executor.exited) == (1, 1)
    manifest = json.loads((run.settings.run_dir / MANIFEST_FILE).read_text())
    case_file = catalog / "manipulacion" / f"{ADVERSARIAL.id}.json"
    assert manifest["cases"] == {
        ADVERSARIAL.id: "sha256:" + hashlib.sha256(case_file.read_bytes()).hexdigest()
    }
    assert manifest["suite"] == "adversarial" and manifest["k"] == 2 and manifest["seed"] == 1
    assert manifest["agent"] == {"model": "claude-haiku-5-5", "effort": "low"}
    assert manifest["simulator"] == {"model": "claude-sonnet-5-5"}
    assert manifest["status"] == "complete" and manifest["max_usd"] is None
    assert manifest["started_at"] <= manifest["ended_at"]
    [session] = manifest["sessions"]
    assert isinstance(session["git"]["commit"], str) and isinstance(session["git"]["dirty"], bool)
    assert read_cases(run.settings.run_dir) == {ADVERSARIAL.id: ADVERSARIAL}
    assert (run.settings.run_dir / CASES_FILE).read_text().count("\n") == 1


def test_an_infrastructure_failure_is_repeated_twice_and_then_kept(tmp_path: Path) -> None:
    flaky = make_case("se-cae-una-vez")
    catalog = write_catalog(tmp_path / "cases", [CAPABILITY, flaky])
    run = plan(tmp_path / "run", catalog, [CAPABILITY, flaky])
    executor = FakeExecutor(
        script={
            (CAPABILITY.id, "B2", 1): [{"infra_failure": STACK_DOWN}],
            (flaky.id, "B2", 1): [{"infra_failure": API_5XX}, {}],
        }
    )

    outcome = execute(run, executor, say=lambda _: None)

    run_dir = run.settings.run_dir
    assert len(executor.calls) == 3 + 2
    assert outcome.infra_retries == 3
    discarded = lines(run_dir / INFRA_FILE)
    assert [(row["record"]["case_id"], row["attempt"]) for row in discarded] == [
        (CAPABILITY.id, 1),
        (CAPABILITY.id, 2),
        (flaky.id, 1),
    ]
    kept = lines(run_dir / TRIALS_FILE)
    assert [row["infra_failure"] for row in kept] == [STACK_DOWN, None]


def test_a_5xx_the_case_injects_is_the_agents_and_is_not_repeated(tmp_path: Path) -> None:
    down = make_case(
        "tienda-no-responde",
        group="fallas-tecnicas",
        events=({"at": "on_checkout_create", "kind": "merchant_unavailable"},),
    )
    run = plan(tmp_path / "run", write_catalog(tmp_path / "cases", [down]), [down])
    executor = FakeExecutor(script={(down.id, "B2", 1): [{"infra_failure": API_5XX}]})

    execute(run, executor, say=lambda _: None)

    assert len(executor.calls) == 1
    assert not (run.settings.run_dir / INFRA_FILE).exists()
    assert ran(run.settings.run_dir) == [(down.id, "B2", 1)]


def test_a_simulator_failure_is_repeated_once_and_then_excluded(tmp_path: Path) -> None:
    again = make_case("responde-a-la-segunda")
    cases = [CAPABILITY, again]
    run = plan(tmp_path / "run", write_catalog(tmp_path / "c", cases), cases)
    executor = FakeExecutor(
        script={
            (CAPABILITY.id, "B2", 1): [SimulatorFailure("schema_invalid")],
            (again.id, "B2", 1): [SimulatorFailure("refusal"), {}],
        }
    )

    outcome = execute(run, executor, say=lambda _: None)

    run_dir = run.settings.run_dir
    assert outcome.excluded == 1 and outcome.kept == 1
    [excluded] = lines(run_dir / EXCLUDED_FILE)
    assert excluded["case_id"] == CAPABILITY.id
    assert (excluded["attempts"], excluded["reason"]) == (2, "schema_invalid")
    assert ran(run_dir) == [(again.id, "B2", 1)]
    assert pending(run) == []


def test_a_spent_budget_stops_the_run_and_the_same_run_id_resumes_it(tmp_path: Path) -> None:
    run = plan(tmp_path / "run", write_catalog(tmp_path / "c", [CAPABILITY]), [CAPABILITY], k=3)
    spent = FakeExecutor(script={(CAPABILITY.id, "B2", 2): [FundsExhausted("BudgetExhausted")]})
    said: list[str] = []

    stopped = execute(run, spent, say=said.append)

    run_dir = run.settings.run_dir
    assert stopped.status == "funds_exhausted" and stopped.kept == 1
    assert spent.exited == 1 and any("presupuesto" in line for line in said)
    assert json.loads((run_dir / MANIFEST_FILE).read_text())["status"] == "funds_exhausted"
    assert ran(run_dir) == [(CAPABILITY.id, "B2", 1)]

    resumed = FakeExecutor()
    finished = execute(run, resumed, say=lambda _: None)

    assert [call[2] for call in resumed.calls] == [2, 3]
    assert (finished.status, finished.resumed, finished.kept) == ("complete", 1, 2)
    manifest = json.loads((run_dir / MANIFEST_FILE).read_text())
    assert [session["status"] for session in manifest["sessions"]] == [
        "funds_exhausted",
        "complete",
    ]
    assert manifest["status"] == "complete"


def test_a_trial_cut_by_a_crash_runs_again(tmp_path: Path) -> None:
    run = plan(tmp_path / "run", write_catalog(tmp_path / "c", [CAPABILITY]), [CAPABILITY], k=2)
    execute(run, FakeExecutor(), say=lambda _: None)
    trials = run.settings.run_dir / TRIALS_FILE
    first, second = trials.read_text().splitlines()
    trials.write_text(f"{first}\n{second[:40]}")

    resumed = FakeExecutor()
    execute(run, resumed, say=lambda _: None)

    assert [call[2] for call in resumed.calls] == [2]
    assert ran(run.settings.run_dir) == [(CAPABILITY.id, "B2", 1), (CAPABILITY.id, "B2", 2)]


def test_a_finished_run_resumes_without_starting_the_executor(tmp_path: Path) -> None:
    run = plan(tmp_path / "run", write_catalog(tmp_path / "c", [CAPABILITY]), [CAPABILITY])
    execute(run, FakeExecutor(), say=lambda _: None)
    idle = FakeExecutor()

    assert execute(run, idle, say=lambda _: None).status == "complete"
    assert (idle.entered, idle.calls) == (0, [])


def test_a_run_id_is_not_resumed_with_other_settings_or_cases(tmp_path: Path) -> None:
    catalog = write_catalog(tmp_path / "c", [CAPABILITY])
    run = plan(tmp_path / "run", catalog, [CAPABILITY])
    execute(run, FakeExecutor(), say=lambda _: None)

    with pytest.raises(RunMismatch, match="k"):
        execute(plan(tmp_path / "run", catalog, [CAPABILITY], k=2), FakeExecutor())
    path = catalog / CAPABILITY.group / f"{CAPABILITY.id}.json"
    path.write_bytes(CASE.dump_json(CAPABILITY, indent=4))
    with pytest.raises(RunMismatch, match="cases"):
        execute(run, FakeExecutor())


def test_a_record_of_another_trial_is_refused(tmp_path: Path) -> None:
    run = plan(tmp_path / "run", write_catalog(tmp_path / "c", [CAPABILITY]), [CAPABILITY])

    with pytest.raises(ValueError, match="returned the record"):
        execute(run, FakeExecutor(suite="adversarial"), say=lambda _: None)

    manifest = json.loads((run.settings.run_dir / MANIFEST_FILE).read_text())
    assert manifest["status"] == "interrupted"
