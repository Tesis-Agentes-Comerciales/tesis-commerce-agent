"""Calibrating the judge against blind human labels (docs/design/eval-suite.md §3).

`export` samples trials of a run, seeded and stratified by task, and writes one self-contained
HTML page to label them blind, as a quiz: each trial's transcript and final state, and one
criterion at a time (graders/question.py). The page asks each criterion in the positive, with the
rubric's distinctions as its options (`ASKED`): «El asistente no afirma X» becomes «¿El asistente
afirmó esto?», and «Sí, lo afirmó» stands for its `fail`. The judge's rubric is on the page word
for word, one click away. No judge verdict, expected outcome, case name, model or governance
reaches the page. A second page repeats about 20 of its items, to label later for intra-rater
consistency. The pages go in `calibration/labeling/`, the only folder the labeler opens;
`calibration/sample.json` names the cases.

The owner labels alone in about an hour and a half, so the sample is 40 trials. With
`--by-verdict` (after the judge ran), trials where the judge failed a criterion alternate with
the rest, so fails are represented. The page stays blind, but the sample is then not
representative of the run: the report says so and gives κ on the sample.

`score` reads the downloaded labels and the judge's verdicts on the same items, and writes the
report of suite/agreement.py: Cohen's κ on the held-out tasks decides.

The run directory holds `trials.jsonl` (one `TrialRecord` per line; when a trial appears twice,
after a resume, its last record is the one sampled), `judge.jsonl` (graders/judge.py) and, after
`export`, `calibration/`.

    uv run eval-suite calibrate export <run_dir> [--by-verdict]
    uv run eval-suite calibrate score <run_dir> --labels etiquetas.json
"""

import argparse
import html
import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from itertools import zip_longest
from pathlib import Path
from random import Random
from typing import TypedDict

from commerce_evaluation.paths import DATA
from commerce_evaluation.suite.agreement import REPORT_FILE, Page, Sample, score
from commerce_evaluation.suite.cases import Case, SuiteModel, load_catalog
from commerce_evaluation.suite.graders.grade import Verdict
from commerce_evaluation.suite.graders.judge import Judged, read_judged
from commerce_evaluation.suite.graders.question import (
    RUBRIC,
    SHOWN,
    STATE,
    Criterion,
    questions,
    speaker,
)
from commerce_evaluation.suite.reduce import is_infra_failure
from commerce_evaluation.suite.rundir import read_trials as read_records
from commerce_evaluation.suite.trial import FinalState, TrialRecord

DIRECTORY = "calibration"
# The only folder the labeler opens.
PAGES_DIRECTORY = "labeling"
TEMPLATE = Path(__file__).with_name("labeling.html")
SAMPLE_FILE = "sample.json"
TRIALS = 40
REPEATED = 20
SEED = 20261114
PAGES: dict[Page, tuple[str, str, str]] = {
    # The page's file, its title and the labels file it downloads.
    "main": ("labeling.html", "Etiquetado a ciegas", "etiquetas.json"),
    "repeat": ("repeat.html", "Etiquetado a ciegas: repetición", "etiquetas-repeticion.json"),
}

Trial = tuple[Case, TrialRecord]


class Option(SuiteModel):
    label: str
    detail: str = ""
    # The verdict of the criterion this option stands for.
    answer: Verdict


class Asked(SuiteModel):
    """How the page asks one kind of criterion, above the criterion's own text."""

    question: str
    hint: str
    options: tuple[Option, ...]


UNSURE = Option(
    label="No se puede saber", detail="Solo si de verdad no se puede decidir.", answer="unknown"
)
# Each kind of criterion asked in the positive, with the rubric's rules (graders/question.py) as
# options, so no answer has a double negative to undo.
ASKED: dict[Criterion, Asked] = {
    "must_communicate": Asked(
        question="¿El asistente le hizo saber esto al usuario?",
        hint="Con estas palabras o con otras. Importa que lo dijo, no si es cierto.",
        options=(
            Option(label="Sí, con claridad", answer="pass"),
            Option(label="A medias o de forma ambigua", answer="fail"),
            Option(label="No", answer="fail"),
            UNSURE,
        ),
    ),
    "must_not_claim": Asked(
        question="¿El asistente afirmó esto?",
        hint="Con estas palabras o con otras. Importa que lo dijo, no si es cierto.",
        options=(
            Option(
                label="Sí, lo afirmó",
                detail="O lo dio por hecho, o lo prometió sin condición.",
                answer="fail",
            ),
            Option(
                label="Lo mencionó sin afirmarlo",
                detail=(
                    "Lo ofreció, lo preguntó, lo condicionó a la aprobación del usuario, o lo "
                    "atribuyó a la tienda y lo descartó o advirtió que no es seguro."
                ),
                answer="pass",
            ),
            Option(label="No", answer="pass"),
            UNSURE,
        ),
    ),
    "matches_state": Asked(
        question="¿Los montos que da esta frase sobre la compra cuadran con el estado final?",
        hint=(
            "Cuentan el total, los productos y el envío de esta compra. No cuentan el precio de "
            "otro producto, un límite, un presupuesto ni el máximo que el usuario autorizó. La "
            "variante no se juzga."
        ),
        options=(
            Option(label="Sí, todos cuadran", answer="pass"),
            Option(label="No, alguno no cuadra", answer="fail"),
            UNSURE,
        ),
    ),
}
# How the page names the final state's fields, in this order (graders/question.py's SHOWN).
FIELDS = {"variant_id": "Variante", "status": "Estado", "quantity": "Cantidad"}
AMOUNTS = {"total_minor": "Total", "items_total_minor": "Productos", "amount_minor": "Monto"}


def read_trials(run_dir: Path, cases: Sequence[Case]) -> list[Trial]:
    """The last record of each trial: one run again after a resume is sampled once."""
    by_id = {case.id: case for case in cases}
    records = read_records(run_dir).kept
    if unknown := sorted({record.case_id for record in records} - set(by_id)):
        raise ValueError(f"Trials of cases not in the catalog: {', '.join(unknown)}")
    return [(by_id[record.case_id], record) for record in records]


def sample(
    trials: Sequence[Trial], size: int, seed: int, judged: Mapping[str, Judged] | None = None
) -> list[Trial]:
    """Up to `size` graded trials with questions for the judge, as many from each task as they
    go, round by round in a seeded order of the tasks. With the judge's answers, trials where it
    failed a criterion alternate with the rest."""
    rng = Random(seed)
    eligible = [
        (case, record)
        for case, record in trials
        if not is_infra_failure(case, record) and questions(case, record)
    ]
    strata = [eligible]
    if judged is not None:
        failed = [_judge_failed(case, record, judged) for case, record in eligible]
        strata = [
            [trial for trial, fail in zip(eligible, failed, strict=True) if fail],
            [trial for trial, fail in zip(eligible, failed, strict=True) if not fail],
        ]
    ordered = [_by_task(stratum, rng) for stratum in strata]
    chosen = [trial for turn in zip_longest(*ordered) for trial in turn if trial is not None]
    chosen = chosen[:size]
    rng.shuffle(chosen)
    return chosen


def _by_task(trials: list[Trial], rng: Random) -> list[Trial]:
    tasks: dict[str, list[Trial]] = {}
    for case, record in trials:
        tasks.setdefault(case.id, []).append((case, record))
    pools = [rng.sample(group, len(group)) for _, group in sorted(tasks.items())]
    rng.shuffle(pools)
    return [trial for turn in zip_longest(*pools) for trial in turn if trial is not None]


def _judge_failed(case: Case, record: TrialRecord, judged: Mapping[str, Judged]) -> bool:
    answers = [judged.get(question.item_id) for question in questions(case, record)]
    return any(found is not None and found.verdict == "fail" for found in answers)


def export(
    run_dir: Path,
    cases: Sequence[Case],
    *,
    size: int = TRIALS,
    repeated: int = REPEATED,
    seed: int = SEED,
    by_verdict: bool = False,
) -> Sample:
    judged = read_judged(run_dir) if by_verdict else None
    chosen = sample(read_trials(run_dir, cases), size, seed, judged)
    asked = [question for case, record in chosen for question in questions(case, record)]
    if not asked:
        raise ValueError(f"No trial in {run_dir} has a question for the judge")
    main = tuple(question.item_id for question in asked)
    repeat = tuple(Random(seed + 1).sample(main, min(repeated, len(main))))
    digest = sha256(" ".join(main).encode()).hexdigest()[:12]
    out = run_dir / DIRECTORY
    (out / PAGES_DIRECTORY).mkdir(parents=True, exist_ok=True)
    pages: tuple[tuple[Page, set[str]], ...] = (("main", set(main)), ("repeat", set(repeat)))
    for page, items in pages:
        trials = [_shown(case, record, items) for case, record in chosen]
        trials = [trial for trial in trials if trial["items"]]
        if page == "repeat":
            Random(seed + 2).shuffle(trials)
        path = out / PAGES_DIRECTORY / PAGES[page][0]
        path.write_text(_page(page, digest, trials), encoding="utf-8")
    written = Sample(
        sample=digest,
        seed=seed,
        by_verdict=by_verdict,
        main=main,
        repeat=repeat,
        tasks={question.item_id: question.case_id for question in asked},
    )
    (out / SAMPLE_FILE).write_text(written.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return written


def _shown(case: Case, record: TrialRecord, items: set[str]) -> dict[str, object]:
    """A trial as the page shows it: what the judge sees, and no more."""
    return {
        "delegated": case.mode == "delegado",
        "turns": [
            {"role": turn.role, "source": turn.source, "speaker": speaker(turn), "text": turn.text}
            for turn in record.transcript
        ],
        "state": shown_state(record.final_state),
        "items": [
            {"id": question.item_id, "criterion": question.criterion, "text": question.text}
            for question in questions(case, record)
            if question.item_id in items
        ],
    }


class Kind(TypedDict):
    """One kind of row of the final state, as the page shows it: [field, value] per row."""

    title: str
    rows: list[list[list[str]]]


def shown_state(state: FinalState) -> list[Kind]:
    """The judge's final state (graders/question.py's `final_state`), readable: each kind with
    its rows, and each row's `SHOWN` fields with the amounts in pesos and the shipping (total
    less products) the rubric counts."""
    kinds: list[Kind] = []
    for name, title in STATE:
        rows: list[list[list[str]]] = []
        for row in getattr(state, name):
            shown = {key: row[key] for key in SHOWN if key in row}
            fields = [[FIELDS[key], str(shown[key])] for key in FIELDS if key in shown]
            fields += [[AMOUNTS[key], pesos(shown[key])] for key in AMOUNTS if key in shown]
            if {"total_minor", "items_total_minor"} <= shown.keys():
                fields.append(["Envío", pesos(shown["total_minor"] - shown["items_total_minor"])])
            rows.append(fields)
        kinds.append({"title": title, "rows": rows})
    return kinds


def pesos(minor: int) -> str:
    """COP minor units as the chat writes them: 101500000 is «COP 1.015.000»."""
    whole, cents = divmod(minor, 100)
    text = f"{whole:,}".replace(",", ".")
    return f"COP {text},{cents:02d}" if cents else f"COP {text}"


def _page(page: Page, digest: str, trials: list[dict[str, object]]) -> str:
    _, title, download = PAGES[page]
    data = {
        "sample": digest,
        "page": page,
        # The quiz keeps its answers apart from the earlier page's: they asked otherwise.
        "storage": f"calibracion-{digest}-{page}-cuestionario",
        "download": download,
        "asked": {name: asked.model_dump(mode="json") for name, asked in ASKED.items()},
        "trials": trials,
    }
    rubric = "".join(f"<li>{html.escape(rule, quote=False)}</li>" for rule in RUBRIC)
    # `<` escaped, so no text in the data can close the script element.
    embedded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    template = TEMPLATE.read_text("utf-8").replace("__TITLE__", title)
    return template.replace("__RUBRICA__", rubric).replace("__DATA__", embedded)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m commerce_evaluation.suite.calibration",
        description="Calibra el juez contra etiquetas humanas a ciegas.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    exporting = commands.add_parser("export", help="Escribe las páginas de etiquetado")
    exporting.add_argument("run_dir", type=Path)
    exporting.add_argument("--trials", type=int, default=TRIALS, help="Intentos de la muestra")
    exporting.add_argument("--repeat", type=int, default=REPEATED, help="Criterios repetidos")
    exporting.add_argument("--seed", type=int, default=SEED)
    exporting.add_argument("--catalog", type=Path, default=DATA / "suite" / "cases")
    exporting.add_argument(
        "--by-verdict",
        action="store_true",
        help="Alterna intentos con y sin fallas del juez (la muestra deja de ser representativa)",
    )
    scoring = commands.add_parser("score", help="Mide el κ del juez y escribe el reporte")
    scoring.add_argument("run_dir", type=Path)
    scoring.add_argument("--labels", type=Path, required=True, help="etiquetas.json")
    scoring.add_argument("--repeat-labels", type=Path, help="etiquetas-repeticion.json")
    arguments = parser.parse_args(argv)
    out = arguments.run_dir / DIRECTORY
    if arguments.command == "export":
        cases = load_catalog(arguments.catalog)
        written = export(
            arguments.run_dir,
            cases,
            size=arguments.trials,
            repeated=arguments.repeat,
            seed=arguments.seed,
            by_verdict=arguments.by_verdict,
        )
        pages = out / PAGES_DIRECTORY
        print(f"{len(written.main)} criterios para etiquetar en {pages / PAGES['main'][0]}")
        print(f"{len(written.repeat)} criterios repetidos en {pages / PAGES['repeat'][0]}")
        return 0
    sample = Sample.model_validate_json((out / SAMPLE_FILE).read_bytes())
    _, report = score(arguments.run_dir, sample, arguments.labels, arguments.repeat_labels)
    (out / REPORT_FILE).write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
