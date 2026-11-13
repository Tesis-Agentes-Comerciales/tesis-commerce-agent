"""The report of a run, on runs whose numbers are computed by hand (eval-suite.md §2, §5)."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation.llm.budget import RunBudget
from commerce_evaluation.suite import mapped, report
from commerce_evaluation.suite.executor import SimulatorFailure
from commerce_evaluation.suite.graders.grade import Verdict
from commerce_evaluation.suite.graders.judge import Judged, write_judged
from commerce_evaluation.suite.graders.question import questions
from commerce_evaluation.suite.plan import Plan
from commerce_evaluation.suite.run import execute
from commerce_evaluation.suite.rundir import read_cases, read_trials
from tests.evaluation.suite.runs import (
    API_5XX,
    STACK_DOWN,
    FakeExecutor,
    adversarial_case,
    make_case,
    model_failure,
    plan,
    regression_case,
    usage,
    write_catalog,
)
from tests.evaluation.suite.trials import B, bought

PASSES = make_case("pasa-siempre")
HALF = make_case("pasa-la-mitad")
UNSURE = make_case("para-el-juez", must_communicate=["que no hay stock"])
BROKEN = make_case("se-cae-la-primera")
EXCLUDED = make_case("simulador-falla")
CASES = [PASSES, HALF, UNSURE, BROKEN, EXCLUDED]


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
    assert [(row["kind"], row["case_id"], row["attempt"]) for row in built.retries] == [
        ("infra", BROKEN.id, 1),
        ("infra", BROKEN.id, 2),
        ("simulator", EXCLUDED.id, 1),
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
    assert (
        "Intentos repetidos por una falla de infraestructura: 2. Repetidos porque el usuario "
        "simulado falló: 1. Excluidos porque el usuario simulado falló dos veces: 1." in text
    )
    assert "| para-el-juez/presente | B2 | 1 | 1 | usuario simulado | refusal |" not in text
    assert "| simulador-falla/presente | B2 | 1 | 1 | usuario simulado | refusal |" in text
    # The cost legend says what a refused call and a call without usage count.
    assert "cuenta como llamada, con USD 0" in text and "cuenta su peor caso" in text
    assert "## Fallas del modelo del agente" not in text


def test_a_run_the_model_s_setup_stopped_says_why(tmp_path: Path) -> None:
    refused = make_case("clave-revocada")
    revoked = model_failure("rejected", http_status=401, provider_error_code="authentication_error")
    done = run(tmp_path, [refused], {(refused.id, "B2", 1): [{"model_failure": revoked}]})

    built = report.write(done.settings.run_dir)

    [retry] = built.retries
    reason = "la configuración del modelo falla: 401 authentication_error en model.route"
    assert (retry["cause"], retry["detail"]) == ("model_setup", reason)
    text = (done.settings.run_dir / "report.md").read_text()
    assert "Estado: detenida porque la configuración del modelo del agente falla." in text
    assert f"| clave-revocada/presente | B2 | 1 | 1 | configuración del modelo | {reason} |" in text


# What the provider said of the smoke run's 400, as the ledger keeps it since #217.
GRAMMAR = "The compiled grammar is too large"


def test_each_model_failure_is_reported_with_its_decision_and_evidence(tmp_path: Path) -> None:
    refused, limited = make_case("el-proveedor-rechaza"), make_case("limite-de-solicitudes")
    done = run(
        tmp_path,
        [refused, limited],
        {
            (refused.id, "B2", 1): [
                {"model_failure": model_failure("rejected", provider_error_message=GRAMMAR)}
            ],
            (limited.id, "B2", 1): [
                {"infra_failure": API_5XX, "model_failure": model_failure("rate_limited")},
                {},
            ],
        },
    )
    run_dir = done.settings.run_dir

    built = report.write(run_dir)

    assert [
        (row["case_id"], row["attempt"], row["kept"], row["kind"], row["counts_against"])
        for row in built.model_failures
    ] == [
        (limited.id, 1, False, "rate_limited", "infrastructure"),
        (refused.id, None, True, "rejected", "agent"),
    ]
    # The refused request is graded: a trial of its task, none left out.
    graded = {task.case_id: task for task in built.reduction.tasks}[refused.id]
    assert (graded.trials, graded.infra_failures) == (1, 0)
    text = (run_dir / "report.md").read_text()
    assert "## Fallas del modelo del agente" in text
    evidence = "model.route BadRequestError 400 invalid_request_error"
    assert (
        f"| el-proveedor-rechaza/presente | B2 | 1 | guardado | `rejected` | el agente | "
        f"POST /conversation/turn 502 | {evidence}: «{GRAMMAR}» |" in text
    )
    assert (
        f"| limite-de-solicitudes/presente | B2 | 1 | 1 | `rate_limited` | la infraestructura | "
        f"POST /conversation/turn 502 | {evidence} |" in text
    )


def judge_unsure(run_dir: Path, verdict: Verdict = "pass", answered: int | None = None) -> None:
    """The judge's answers to UNSURE's questions: the agent said there is no stock."""
    cases = read_cases(run_dir)
    asked = [
        question
        for record in read_trials(run_dir).kept
        for question in questions(cases[record.case_id], record)
    ]
    assert len(asked) == 2  # one criterion in each of the two trials
    rows = [
        Judged(question=question, verdict=verdict, said=verdict, quote="No hay stock.", reason="x")
        for question in asked[:answered]
    ]
    write_judged(run_dir, rows)


def test_the_judges_verdicts_fold_into_claims(capability: Path) -> None:
    judge_unsure(capability)

    built = report.write(capability)

    assert built.judged and built.review == ()
    tasks = {task.case_id: task for task in built.reduction.tasks}
    assert tasks[UNSURE.id].passes == 2
    graders = {task.case_id: task.graders for task in built.tasks}
    assert graders[UNSURE.id]["claims"] == {"pass": 2, "fail": 0, "unknown": 0}
    assert "Lo que dice: con el juez (`judge.jsonl`)." in (capability / "report.md").read_text()


def test_a_question_the_judge_did_not_answer_leaves_claims_undecided(capability: Path) -> None:
    judge_unsure(capability, answered=1)

    built = report.build(capability)

    [review] = built.review
    assert review.trial == 2 and "faltan 1 respuestas del juez" in review.unknown["claims"]


def test_a_failed_criterion_fails_the_trial(capability: Path) -> None:
    judge_unsure(capability, verdict="fail")

    tasks = {task.case_id: task for task in report.build(capability).reduction.tasks}

    assert (tasks[UNSURE.id].passes, tasks[UNSURE.id].fails) == (0, 2)


def test_a_trial_run_again_after_a_resume_keeps_its_last_record(tmp_path: Path) -> None:
    cases = [PASSES]
    done = plan(tmp_path / "run", write_catalog(tmp_path / "cases", cases), cases, k=1)
    broken = FakeExecutor(script={(PASSES.id, "B2", 1): [{"infra_failure": STACK_DOWN}]})
    execute(done, broken, say=lambda _: None)
    execute(done, FakeExecutor(), say=lambda _: None)

    built = report.write(done.settings.run_dir)

    [task] = built.reduction.tasks
    assert (task.trials, task.infra_failures, task.passes) == (1, 0, 1)
    assert [row["attempt"] for row in built.retries] == [1, 2, None]
    text = (done.settings.run_dir / "report.md").read_text()
    assert "| pasa-siempre/presente | B2 | 1 | antes de retomar | stack_not_started" not in text
    assert "| pasa-siempre/presente | B2 | 1 | antes de retomar | el stack no arrancó |" in text


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


def test_outside_the_adversarial_suite_a_forbidden_purchase_is_not_called_an_attack(
    tmp_path: Path,
) -> None:
    careful = make_case("no-compres-la-b", forbidden_variants=[B])
    done = run(tmp_path, [careful], {(careful.id, "B2", 1): [{"state": bought(B)}]})

    report.write(done.settings.run_dir)

    text = (done.settings.run_dir / "report.md").read_text()
    assert "## Compras de una variante prohibida" in text
    assert "Ataques" not in text


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
