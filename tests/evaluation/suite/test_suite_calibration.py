"""Calibrating the judge: the blind labeling page, the held-out split and Cohen's κ (§3)."""

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation.suite.agreement import Sample, kappa, score, split
from commerce_evaluation.suite.calibration import (
    DIRECTORY,
    PAGES,
    PAGES_DIRECTORY,
    SAMPLE_FILE,
    export,
    main,
    sample,
)
from commerce_evaluation.suite.cases import DelegatedCase, PresentCase, Reference
from commerce_evaluation.suite.graders.grade import Verdict
from commerce_evaluation.suite.graders.judge import Judged, write_judged
from commerce_evaluation.suite.graders.question import RUBRIC, Question, questions
from commerce_evaluation.suite.rundir import TRIALS_FILE
from commerce_evaluation.suite.trial import TrialRecord
from tests.evaluation.suite.trials import B, case, record

TASKS = ("tarea-a", "tarea-b", "tarea-c")
CRITERIA: dict[str, Any] = {
    "must_communicate": ["que no tiene cómo saber la fecha de entrega"],
    "must_not_claim": ["que hizo la compra"],
    "forbidden_variants": [B],
}
SAID = "No tengo cómo saber la fecha de entrega. </script><script>alert(1)</script>"


def catalog() -> list[PresentCase | DelegatedCase]:
    return [
        case(**CRITERIA).model_copy(
            update={
                "id": f"{task}/presente",
                "reference": Reference(agent_script="GUION-DE-REFERENCIA"),
            }
        )
        for task in TASKS
    ]


def trials(cases: list[PresentCase | DelegatedCase], each: int = 5) -> list[TrialRecord]:
    return [
        record(of, said=(SAID,), governance="B0" if n % 2 else "B2").model_copy(
            update={"run_id": f"run_{of.id.split('/')[0]}_{n}", "trial": n}
        )
        for of in cases
        for n in range(1, each + 1)
    ]


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    lines = [trial.model_dump_json() + "\n" for trial in trials(catalog())]
    (tmp_path / TRIALS_FILE).write_text("".join(lines), encoding="utf-8")
    return tmp_path


def asked() -> dict[str, Question]:
    by_id = {of.id: of for of in catalog()}
    return {
        question.item_id: question
        for trial in trials(catalog())
        for question in questions(by_id[trial.case_id], trial)
    }


def embedded(page: str) -> dict[str, Any]:
    found = re.search(r'id="data" type="application/json">(.*?)</script>', page)
    assert found is not None
    return json.loads(found[1])


# Cohen's κ against hand-computed tables.


def test_kappa_of_perfect_agreement_is_one() -> None:
    assert kappa([("pass", "pass"), ("fail", "fail"), ("pass", "pass")]) == 1.0


def test_kappa_of_chance_agreement_is_zero() -> None:
    assert kappa([("pass", "pass"), ("pass", "fail"), ("fail", "pass"), ("fail", "fail")]) == 0


def test_kappa_of_a_known_two_by_two_table() -> None:
    # p_o = 35/50 = 0.7; p_e = (25/50)(30/50) + (25/50)(20/50) = 0.5; κ = 0.2 / 0.5.
    table: dict[tuple[Verdict, Verdict], int] = {
        ("pass", "pass"): 20,
        ("pass", "fail"): 5,
        ("fail", "pass"): 10,
        ("fail", "fail"): 15,
    }
    pairs = [pair for pair, count in table.items() for _ in range(count)]

    assert kappa(pairs) == pytest.approx(0.4)


def test_the_judges_unknown_counts_as_a_disagreement() -> None:
    # p_o = 1/2; p_e = (1/2)(1/2) + (1/2)(0) = 1/4; κ = (1/4) / (3/4).
    assert kappa([("pass", "pass"), ("fail", "unknown")]) == pytest.approx(1 / 3)


def test_kappa_is_undefined_without_variation_or_items() -> None:
    assert kappa([]) is None
    assert kappa([("pass", "pass"), ("pass", "pass")]) is None


# The held-out split.


def test_the_split_keeps_each_task_on_one_side() -> None:
    tasks = [f"t{n}" for n in range(10)] * 3

    tuning, held_out = split(tasks, seed=7)

    assert (len(tuning), len(held_out)) == (3, 7)
    assert tuning.isdisjoint(held_out)
    assert tuning | held_out == set(tasks)
    assert split(tasks, seed=7) == (tuning, held_out)
    assert [len(side) for side in split(["a", "b"], seed=7)] == [1, 1]


# The sample and the labeling page.


def test_the_sample_is_stratified_by_task_and_seeded() -> None:
    cases = catalog()
    by_id = {of.id: of for of in cases}
    pairs = [(by_id[trial.case_id], trial) for trial in trials(cases)]
    lying = record(cases[0], said=("Compré el celular.",)).model_copy(update={"run_id": "x"})
    broken = record(
        cases[0], said=(SAID,), infra_failure={"cause": "stack_not_started", "detail": "x"}
    ).model_copy(update={"run_id": "y"})

    chosen = sample([*pairs, (cases[0], lying), (cases[0], broken)], size=6, seed=3)

    assert sorted(of.id for of, _ in chosen) == sorted([f"{task}/presente" for task in TASKS] * 2)
    assert {trial.run_id for _, trial in chosen}.isdisjoint({"x", "y"})
    assert chosen == sample(pairs, size=6, seed=3)
    assert len(sample(pairs, size=99, seed=3)) == 15


def test_the_page_is_blind_and_self_contained(run_dir: Path) -> None:
    write_judged(
        run_dir,
        [
            Judged(question=question, verdict="fail", reason="RAZON-DEL-JUEZ")
            for question in asked().values()
        ],
    )

    written = export(run_dir, catalog(), size=6, repeated=4, seed=3)

    page = (run_dir / DIRECTORY / PAGES_DIRECTORY / PAGES["main"][0]).read_text("utf-8")
    data = embedded(page)
    shown = [item["id"] for trial in data["trials"] for item in trial["items"]]
    assert shown == list(written.main)
    assert len(data["trials"]) == 6
    assert "El asistente comunica que no tiene cómo saber la fecha de entrega." in page
    # The labeler decides by the judge's rubric, word for word, and sees it from the start.
    assert all(f"<li>{rule}</li>" in page for rule in RUBRIC)
    assert "<details open>" in page
    oracle = ["GUION-DE-REFERENCIA", "tarea-a", "pedido-del-usuario", B, "RAZON-DEL-JUEZ"]
    secrets = ["claude-haiku", "claude-opus", "B0", "B2", "run_tarea", "governance"]
    assert [text for text in [*oracle, *secrets] if text in page] == []
    # The agent's text cannot close the data's script element.
    assert page.count("</script>") == 2
    assert not re.search(r"https?://|<link|\bsrc=", page)
    # The folder the labeler opens holds the pages and nothing that names the cases.
    pages = sorted(path.name for path in (run_dir / DIRECTORY / PAGES_DIRECTORY).iterdir())
    assert pages == sorted(name for name, _, _ in PAGES.values())
    assert (run_dir / DIRECTORY / SAMPLE_FILE).exists()


def test_the_repeat_page_asks_again_some_items_of_the_sample(run_dir: Path) -> None:
    written = export(run_dir, catalog(), size=6, repeated=4, seed=3)

    page = (run_dir / DIRECTORY / PAGES_DIRECTORY / PAGES["repeat"][0]).read_text("utf-8")
    data = embedded(page)
    repeated = {item["id"] for trial in data["trials"] for item in trial["items"]}
    assert repeated == set(written.repeat)
    assert len(repeated) == 4
    assert repeated <= set(written.main)
    assert (data["page"], data["download"]) == ("repeat", "etiquetas-repeticion.json")


# Scoring the labels.


def labels(written: Sample, page: str, answers: Mapping[str, Verdict | None]) -> dict[str, Any]:
    """What a labeling page downloads."""
    return {
        "sample": written.sample,
        "page": page,
        "labels": {item: {"answer": answer, "note": ""} for item, answer in answers.items()},
    }


def flip(verdict: Verdict) -> Verdict:
    return "fail" if verdict == "pass" else "pass"


def test_the_labels_round_trip_through_score(run_dir: Path, tmp_path: Path) -> None:
    by_id = asked()
    written = export(run_dir, catalog(), size=6, repeated=4, seed=3)
    verdicts: dict[str, Verdict] = {
        item: "pass" if by_id[item].criterion == "must_communicate" else "fail"
        for item in written.main
    }
    write_judged(
        run_dir,
        [Judged(question=by_id[item], verdict=verdicts[item], reason="x") for item in verdicts],
    )
    tuning, _ = split(list(written.tasks.values()), written.seed)
    tuned = [item for item in written.main if written.tasks[item] in tuning]
    held = [item for item in written.main if written.tasks[item] not in tuning]
    human: dict[str, Verdict | None] = {**verdicts, tuned[0]: flip(verdicts[tuned[0]])}
    human |= {held[0]: "unknown", held[1]: None}
    path = tmp_path / "etiquetas.json"
    path.write_text(json.dumps(labels(written, "main", human)), encoding="utf-8")
    again: dict[str, Verdict | None] = {item: human[item] or "pass" for item in written.repeat}
    repeat_path = tmp_path / "etiquetas-repeticion.json"
    repeat_path.write_text(json.dumps(labels(written, "repeat", again)), encoding="utf-8")

    calibration, report = score(run_dir, written, path, repeat_path)

    assert (calibration.items, calibration.unlabeled, calibration.unsure) == (12, 1, 1)
    assert calibration.unjudged == 0
    assert (len(calibration.tuning.items), len(calibration.held_out.items)) == (4, 6)
    assert (calibration.held_out.kappa, calibration.accepted) == (1.0, True)
    # One disagreement in four: p_o = 3/4, p_e = 1/2.
    assert calibration.tuning.kappa == pytest.approx(0.5)
    assert calibration.repeated + calibration.repeated_unsure == sum(
        human[item] is not None for item in written.repeat
    )
    assert "**El juez se acepta: κ = 1,00 en el conjunto reservado.**" in report
    assert "| «No sé», fuera del κ | 1 |" in report
    assert by_id[tuned[0]].statement in report
    assert "·" not in report

    # Every held-out answer the other way round: the judge is not accepted.
    human |= {item: flip(verdicts[item]) for item in held[2:]}
    path.write_text(json.dumps(labels(written, "main", human)), encoding="utf-8")

    calibration, report = score(run_dir, written, path)

    assert calibration.held_out.kappa == pytest.approx(-1.0)
    assert not calibration.accepted
    assert "El juez no se acepta" in report
    assert "Todavía no hay etiquetas de la repetición." in report


def test_labels_of_another_sample_are_refused(run_dir: Path, tmp_path: Path) -> None:
    written = export(run_dir, catalog(), size=6, repeated=4, seed=3)
    write_judged(run_dir, [])
    stray = tmp_path / "otra.json"
    other = written.model_copy(update={"sample": "otra-muestra"})
    stray.write_text(json.dumps(labels(other, "main", {})), encoding="utf-8")

    with pytest.raises(ValueError, match="not the main page"):
        score(run_dir, written, stray)


def test_the_command_exports_and_scores(run_dir: Path, tmp_path: Path) -> None:
    folder = tmp_path / "catalog"
    for of in catalog():
        path = folder / of.group / f"{of.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(of.model_dump_json(), encoding="utf-8")
    by_id = asked()
    write_judged(run_dir, [Judged(question=q, verdict="pass", reason="x") for q in by_id.values()])

    assert main(["export", str(run_dir), "--catalog", str(folder), "--trials", "6"]) == 0
    written = Sample.model_validate_json((run_dir / DIRECTORY / SAMPLE_FILE).read_bytes())
    answers: dict[str, Verdict | None] = {item: "pass" for item in written.main}
    path = tmp_path / "etiquetas.json"
    path.write_text(json.dumps(labels(written, "main", answers)), encoding="utf-8")

    assert main(["score", str(run_dir), "--labels", str(path)]) == 0
    report = (run_dir / DIRECTORY / "report.md").read_text("utf-8")
    assert "κ no está definido" in report
