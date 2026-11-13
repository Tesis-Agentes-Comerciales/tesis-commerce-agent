"""The calibration report on a small sample: judge failures out of κ, the observed agreement and
the matrix's totals, a warning when the human's rarer class is small, and a sample stratified by
the judge's verdict that says it is not representative (§3)."""

import json
from pathlib import Path

import pytest

from commerce_evaluation.suite.agreement import Sample, score, split
from commerce_evaluation.suite.calibration import (
    DIRECTORY,
    SAMPLE_FILE,
    export,
    main,
    sample,
)
from commerce_evaluation.suite.graders.grade import Verdict
from commerce_evaluation.suite.graders.judge import Failure, Judged, write_judged
from commerce_evaluation.suite.graders.question import Question
from commerce_evaluation.suite.rundir import TRIALS_FILE
from tests.evaluation.suite.test_suite_calibration import asked, catalog, labels, trials

FAILED_TASK = "tarea-a/presente"


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    lines = [trial.model_dump_json() + "\n" for trial in trials(catalog())]
    (tmp_path / TRIALS_FILE).write_text("".join(lines), encoding="utf-8")
    return tmp_path


def right(question: Question) -> Verdict:
    """The human's answer in these tests: the agent says what it must, and claims nothing."""
    return "pass" if question.criterion == "must_communicate" else "fail"


def judge(run_dir: Path, failures: dict[str, Failure] | None = None) -> None:
    """The judge agrees with `right`, except on the items of `failures`, where it failed."""
    failures = failures or {}
    write_judged(
        run_dir,
        [
            Judged(question=question, verdict=right(question), reason="x")
            if item not in failures
            else Judged(question=question, verdict="unknown", reason="x", failure=failures[item])
            for item, question in asked().items()
        ],
    )


def labeled(run_dir: Path, written: Sample) -> Path:
    by_id = asked()
    answers: dict[str, Verdict | None] = {item: right(by_id[item]) for item in written.main}
    path = run_dir / "etiquetas.json"
    path.write_text(json.dumps(labels(written, "main", answers)), encoding="utf-8")
    return path


def test_a_judge_failure_is_left_out_of_kappa_and_counted(run_dir: Path) -> None:
    written = export(run_dir, catalog(), size=6, repeated=4, seed=3)
    tuning, _ = split(list(written.tasks.values()), written.seed)
    held = [item for item in written.main if written.tasks[item] not in tuning]
    outside = [item for item in asked() if item not in written.main]
    judge(run_dir, {held[0]: "transport", held[1]: "incomplete", outside[0]: "incomplete"})

    calibration, report = score(run_dir, written, labeled(run_dir, written))

    assert calibration.judge_failed == 2
    assert not {held[0], held[1]} & set(calibration.held_out.items)
    # Left in, the two would have been disagreements; left out, the judge agrees on the rest.
    assert (calibration.held_out.kappa, calibration.accepted) == (1.0, True)
    assert calibration.run_questions == len(asked())
    assert calibration.run_failures["incomplete"] == 2
    assert calibration.run_failures["transport"] == 1
    assert "| Sin respuesta útil del juez, fuera del κ | 2 |" in report
    assert "| incomplete | 2 |" in report
    assert "| refusal | 0 |" in report


def test_the_report_shows_the_matrix_and_warns_on_a_rare_class(run_dir: Path) -> None:
    written = export(run_dir, catalog(), size=6, repeated=4, seed=3)
    judge(run_dir)
    tuning, _ = split(list(written.tasks.values()), written.seed)
    held = sum(written.tasks[item] not in tuning for item in written.main)

    _, report = score(run_dir, written, labeled(run_dir, written))

    assert "κ = 1,00, acuerdo observado 100 %, en" in report
    assert f"| Total | {held // 2} | {held // 2} | 0 | {held} |" in report
    assert f"Ojo: el humano dio «No cumple» a solo {held // 2} criterios" in report
    assert "si dos tareas tienen un criterio con el mismo texto, puede aparecer en ambos" in report
    assert "La muestra reparte los intentos de la corrida entre sus tareas." in report
    assert "·" not in report


def test_the_disagreement_table_names_the_item(run_dir: Path) -> None:
    written = export(run_dir, catalog(), size=6, repeated=4, seed=3)
    tuning, _ = split(list(written.tasks.values()), written.seed)
    first = next(item for item in written.main if written.tasks[item] in tuning)
    by_id = asked()
    other: Verdict = "fail" if right(by_id[first]) == "pass" else "pass"
    rows = [Judged(question=by_id[first], verdict=other, reason="RAZON-DEL-JUEZ")]
    rows += [
        Judged(question=q, verdict=right(q), reason="x") for i, q in by_id.items() if i != first
    ]
    write_judged(run_dir, rows)

    _, report = score(run_dir, written, labeled(run_dir, written))

    [line] = [line for line in report.splitlines() if "RAZON-DEL-JUEZ" in line]
    assert line.startswith(f"| `{first}` | {by_id[first].statement} |")


def test_a_sample_by_verdict_alternates_the_judges_fails() -> None:
    by_id = {of.id: of for of in catalog()}
    pairs = [(by_id[trial.case_id], trial) for trial in trials(catalog())]
    judged = {
        item: Judged(question=q, verdict="fail" if q.case_id == FAILED_TASK else "pass", reason="x")
        for item, q in asked().items()
    }

    plain = sample(pairs, size=6, seed=3)
    by_verdict = sample(pairs, size=6, seed=3, judged=judged)

    assert sum(of.id == FAILED_TASK for of, _ in plain) == 2
    assert sum(of.id == FAILED_TASK for of, _ in by_verdict) == 3
    assert by_verdict == sample(pairs, size=6, seed=3, judged=judged)


def test_the_report_says_a_sample_by_verdict_is_not_representative(
    run_dir: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "catalog"
    for of in catalog():
        path = folder / of.group / f"{of.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(of.model_dump_json(), encoding="utf-8")
    judge(run_dir)

    command = ["export", str(run_dir), "--catalog", str(folder), "--trials", "6", "--by-verdict"]
    assert main(command) == 0
    written = Sample.model_validate_json((run_dir / DIRECTORY / SAMPLE_FILE).read_bytes())
    _, report = score(run_dir, written, labeled(run_dir, written))

    assert written.by_verdict
    assert "no representa la corrida, y el κ vale sobre la muestra" in report
