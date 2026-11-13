"""The report of a run, on runs whose numbers are computed by hand (eval-suite.md §2, §5)."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation.llm.budget import RunBudget
from commerce_evaluation.llm.cost import TokenUsage
from commerce_evaluation.suite import mapped, report
from commerce_evaluation.suite.executor import SimulatorFailure
from commerce_evaluation.suite.run import Plan, execute
from tests.evaluation.suite.runs import (
    STACK_DOWN,
    FakeExecutor,
    adversarial_case,
    make_case,
    plan,
    regression_case,
    write_catalog,
)
from tests.evaluation.suite.trials import B, bought

PASSES = make_case("pasa-siempre")
HALF = make_case("pasa-la-mitad")
UNSURE = make_case("para-el-juez", must_communicate=["que no hay stock"])
BROKEN = make_case("se-cae-la-primera")
EXCLUDED = make_case("simulador-falla")
CASES = [PASSES, HALF, UNSURE, BROKEN, EXCLUDED]


def usage(input_tokens: int, output_tokens: int) -> TokenUsage:
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
    )


def run(tmp_path: Path, cases: list[Any], script: dict[Any, list[Any]], **settings: Any) -> Plan:
    done = plan(tmp_path / "run", write_catalog(tmp_path / "cases", cases), cases, **settings)
    execute(done, FakeExecutor(done.settings.suite, script), say=lambda _: None)
    return done


@pytest.fixture
def capability(tmp_path: Path) -> Path:
    done = run(
        tmp_path,
        CASES,
        {
            (HALF.id, "B2", 2): [{"state": bought()}],
            (UNSURE.id, "B2", 1): [{"said": ("No hay stock.",)}],
            (UNSURE.id, "B2", 2): [{"said": ("No hay stock.",)}],
            (BROKEN.id, "B2", 1): [{"infra_failure": STACK_DOWN}],
            (EXCLUDED.id, "B2", 1): [SimulatorFailure("refusal")],
        },
        k=2,
    )
    budget = RunBudget(max_usd=Decimal(10), run_dir=done.settings.run_dir)
    for _ in range(2):  # USD 0.01 each: a hundred thousand Haiku input tokens
        budget.charge(role="agent", model="claude-haiku-5-5", usage=usage(100_000, 0))
    # USD 1.00: a hundred thousand Sonnet output tokens
    budget.charge(role="simulator", model="claude-sonnet-5-5", usage=usage(0, 100_000))
    return done.settings.run_dir


def test_pass_rates_by_task_and_governance(capability: Path) -> None:
    built = report.build(capability)

    tasks = {task.case_id: task for task in built.reduction.tasks}
    assert tasks[PASSES.id].pass_1.rate == 1.0 and tasks[PASSES.id].pass_k == 1.0
    assert tasks[HALF.id].pass_1.rate == 0.5 and tasks[HALF.id].pass_k == 0.0
    assert tasks[UNSURE.id].pass_1.rate == 0.0 and tasks[UNSURE.id].unknowns == 2
    # One trial left after the infrastructure broke the other, and one after the exclusion.
    assert (tasks[BROKEN.id].trials, tasks[BROKEN.id].infra_failures) == (2, 1)
    assert tasks[BROKEN.id].pass_k is None and tasks[EXCLUDED.id].trials == 1
    [b2] = built.reduction.governances
    assert b2.pass_1.rate == pytest.approx((1 + 0.5 + 0 + 1 + 1) / 5)
    assert b2.pass_k.rate == pytest.approx(1 / 3)
    assert b2.short == (BROKEN.id, EXCLUDED.id)


def test_each_grader_is_counted_apart_and_unknowns_go_to_review(capability: Path) -> None:
    built = report.build(capability)

    graders = {task.case_id: task.graders for task in built.tasks}
    assert graders[HALF.id]["effects"] == {"pass": 1, "fail": 1, "unknown": 0}
    assert graders[UNSURE.id]["claims"] == {"pass": 0, "fail": 0, "unknown": 2}
    assert graders[BROKEN.id]["effects"] == {"pass": 1, "fail": 0, "unknown": 0}
    [b2] = built.governances
    # Eight graded trials: the infrastructure broke one of the nine kept.
    assert b2.graders["outcome"] == {"pass": 7, "fail": 1, "unknown": 0}
    assert [(each.case_id, each.trial) for each in built.review] == [(UNSURE.id, 1), (UNSURE.id, 2)]
    assert built.review[0].unknown["claims"].startswith("Para el juez")
    assert not built.judged


def test_cost_by_role_infra_retries_and_exclusions(capability: Path) -> None:
    built = report.build(capability)

    cost = {role.role: role for role in built.cost}
    assert (cost["agent"].calls, cost["agent"].input_tokens) == (2, 200_000)
    assert cost["agent"].usd == pytest.approx(0.02)
    assert cost["simulator"].usd == pytest.approx(1.0)
    assert built.usd == pytest.approx(1.02)
    assert [(row["case_id"], row["attempt"]) for row in built.infra_retries] == [
        (BROKEN.id, 1),
        (BROKEN.id, 2),
    ]
    assert [(row["case_id"], row["trial"]) for row in built.excluded] == [(EXCLUDED.id, 1)]


def test_the_report_files_are_written_in_spanish(capability: Path) -> None:
    report.write(capability)

    data = json.loads((capability / "report.json").read_text())
    assert data["reduction"]["governances"][0]["pass_k"]["n"] == 3
    text = (capability / "report.md").read_text()
    assert chr(0xB7) not in text  # the middle dot is never a separator
    assert "Suite capacidad, con 2 intentos por caso y gobernanza" in text
    assert "| B2 | 5 | 9 | 5 | 1 | 2 | 1 | 70,0 % (" in text
    assert "| Total | 3 |  |  |  |  | USD 1,0200 |" in text
    assert "## Para revisión humana" in text and "Lo que dice: solo código, sin el juez." in text
    assert "Intentos excluidos porque el usuario simulado falló dos veces: 1." in text


def test_the_judge_hook_folds_its_verdicts_into_claims(
    capability: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (capability / report.JUDGE_FILE).write_text("{}\n")
    monkeypatch.setattr(report, "JUDGE_MODULE", "tests.evaluation.suite.fake_judge")

    built = report.build(capability)

    assert built.judged and built.review == ()
    tasks = {task.case_id: task for task in built.reduction.tasks}
    assert tasks[UNSURE.id].passes == 2


def test_a_judged_run_needs_the_judge(capability: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (capability / report.JUDGE_FILE).write_text("{}\n")
    monkeypatch.setattr(report, "JUDGE_MODULE", "commerce_evaluation.suite.no_such_judge")

    with pytest.raises(RuntimeError, match="no_such_judge"):
        report.build(capability)


def test_the_attack_success_rate_leaves_the_control_out(tmp_path: Path) -> None:
    attack = adversarial_case()
    control = make_case(
        "catalogo-sin-ordenes",
        group="manipulacion",
        suites=("regression", "adversarial"),
        governance=("B0", "B2"),
    )
    checkout_of_b = {"orders": [], "checkouts": [{"variant_id": B}], "psp_charges": []}
    done = run(
        tmp_path,
        [attack, control],
        {
            (attack.id, "B0", 1): [{"state": bought(B)}],
            (attack.id, "B0", 2): [{"state": bought(B)}],
            (attack.id, "B2", 1): [{"state": checkout_of_b}],
            (control.id, "B0", 1): [{"state": bought(B)}],
        },
        suite="adversarial",
        k=2,
    )

    built = report.write(done.settings.run_dir)

    rates = {each.governance: each.attack_success for each in built.reduction.governances}
    assert rates["B0"] is not None and (rates["B0"].successes, rates["B0"].n) == (2, 2)
    assert rates["B2"] is not None and (rates["B2"].successes, rates["B2"].n) == (1, 2)
    text = (done.settings.run_dir / "report.md").read_text()
    assert "## Ataques que logran su objetivo" in text
    assert "| B2 | 1 de 2 | 50,0 % (" in text


def test_regression_reports_the_coverage_of_the_mapped_tasks(tmp_path: Path) -> None:
    done = run(tmp_path, [regression_case()], {}, suite="regression")
    entries = {
        "a/presente": {"source": "CT1", "tests": ["t.py::ok", "t.py::param"], "why": "Cubre."},
        "b/delegado": {"source": "CT2", "tests": ["t.py::bad"], "why": "Parcial: solo un lado."},
        "c/presente": {"source": "CT3", "uncovered": "No hay cupones."},
    }
    outcomes = [
        '{"id": "t.py::ok", "outcome": "passed"}',
        '{"id": "t.py::bad", "outcome": "failed"}',
    ]
    outcomes.append('{"id": "t.py::param[x]", "outcome": "passed"}')

    def runner(command: list[str], **options: Any) -> None:
        Path(options["env"][mapped.OUTCOMES_ENV]).write_text("\n".join(outcomes) + "\n")

    mapped.run_mapped(entries, done.settings.run_dir, runner=runner)
    built = report.write(done.settings.run_dir)

    assert built.mapped is not None
    assert [task.coverage for task in built.mapped] == ["covered", "partial", "uncovered"]
    text = (done.settings.run_dir / "report.md").read_text()
    assert "1 cubiertas, 1 parciales, 1 sin cubrir" in text
    assert "| a/presente | CT1 | cubierta | 2 | 2 pasa |" in text
    assert "| b/delegado | CT2 | parcial | 1 | 1 falla |" in text
