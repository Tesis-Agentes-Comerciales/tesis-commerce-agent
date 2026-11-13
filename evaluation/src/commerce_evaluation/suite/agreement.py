"""How far the judge agrees with blind human labels (docs/design/eval-suite.md §3).

The sample's tasks are split, seeded, into a tuning set and a held-out set (about 70 %); a task
never sits in both, so a rubric tuned on the first is measured on tasks it never saw. Cohen's κ
between judge and human on the held-out set decides: the judge is accepted at κ ≥ 0.7. Items a
human labeled «No sé» are left out and counted; the judge's own `unknown` stays in as a third
answer, so it counts as a disagreement. Repeated items give the labeler's own κ.
"""

from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from random import Random
from typing import Literal

from commerce_evaluation.suite.cases import SuiteModel
from commerce_evaluation.suite.graders.grade import Verdict
from commerce_evaluation.suite.graders.judge import Judged, read_judged

HELD_OUT = 0.7
THRESHOLD = 0.7
REPORT_FILE = "report.md"
HUMAN: dict[Verdict, str] = {"pass": "Sí cumple", "fail": "No cumple", "unknown": "No sé"}
Page = Literal["main", "repeat"]


class Sample(SuiteModel):
    """What `calibration.export` wrote: the items of each page and the task of each item."""

    sample: str
    seed: int
    main: tuple[str, ...]
    repeat: tuple[str, ...]
    tasks: dict[str, str]


class Label(SuiteModel):
    answer: Verdict | None = None
    note: str = ""


class Labels(SuiteModel):
    """The file a labeling page downloads."""

    sample: str
    page: Page
    labels: dict[str, Label]


def read_labels(path: Path, sample: Sample, page: Page) -> dict[str, Label]:
    """The answered labels of one page of the sample."""
    labels = Labels.model_validate_json(path.read_bytes())
    if (labels.sample, labels.page) != (sample.sample, page):
        raise ValueError(f"{path} is not the {page} page of sample {sample.sample}")
    items = set(sample.main if page == "main" else sample.repeat)
    if stray := sorted(set(labels.labels) - items):
        raise ValueError(f"{path} labels items outside its page: {', '.join(stray[:3])}")
    return {item: label for item, label in labels.labels.items() if label.answer is not None}


def split(tasks: Sequence[str], seed: int) -> tuple[set[str], set[str]]:
    """The tuning tasks and the held-out tasks: about `HELD_OUT` of them, and at least one in
    each set when there are two tasks or more."""
    ordered = sorted(set(tasks))
    Random(seed).shuffle(ordered)
    cut = round(len(ordered) * HELD_OUT)
    if len(ordered) > 1:
        cut = min(max(cut, 1), len(ordered) - 1)
    return set(ordered[cut:]), set(ordered[:cut])


def kappa(pairs: Sequence[tuple[Verdict, Verdict]]) -> float | None:
    """Cohen's κ of two raters over the same items; None when it is undefined: no items, or
    both raters gave one and the same answer to all of them."""
    n = len(pairs)
    agreed = sum(first == second for first, second in pairs)
    firsts, seconds = Counter(first for first, _ in pairs), Counter(second for _, second in pairs)
    chance = sum(firsts[answer] * seconds[answer] for answer in firsts)
    if n == 0 or chance == n * n:
        return None
    return (n * agreed - chance) / (n * n - chance)


class Agreement(SuiteModel):
    """Judge against human over one set of tasks."""

    tasks: tuple[str, ...]
    items: tuple[str, ...]
    kappa: float | None
    # "human/judge" -> items.
    matrix: dict[str, int]


class Calibration(SuiteModel):
    sample: str
    items: int
    unlabeled: int
    # Labeled «No sé»: out of κ.
    unsure: int
    # Labeled, but the judge has no verdict on them.
    unjudged: int
    tuning: Agreement
    held_out: Agreement
    accepted: bool
    # The labeler against itself on the repeated items both labels answered.
    repeated: int
    repeated_unsure: int
    intra_kappa: float | None


def score(
    run_dir: Path, sample: Sample, labels: Path, repeat_labels: Path | None = None
) -> tuple[Calibration, str]:
    """The calibration and its Markdown report."""
    judged = read_judged(run_dir)
    human = read_labels(labels, sample, "main")
    answered: dict[str, Verdict] = {
        item: label.answer for item, label in human.items() if label.answer
    }
    sure = [item for item in sample.main if answered.get(item) in ("pass", "fail")]
    compared = [item for item in sure if item in judged]
    tuning_tasks, held_out_tasks = split(list(sample.tasks.values()), sample.seed)

    def among(tasks: set[str]) -> Agreement:
        items = tuple(item for item in compared if sample.tasks[item] in tasks)
        pairs: list[tuple[Verdict, Verdict]] = [
            (answered[item], judged[item].verdict) for item in items
        ]
        return Agreement(
            tasks=tuple(sorted(tasks)),
            items=items,
            kappa=kappa(pairs),
            matrix=dict(Counter(f"{first}/{second}" for first, second in pairs)),
        )

    held_out = among(held_out_tasks)
    repeated, repeated_unsure, intra = 0, 0, None
    if repeat_labels is not None:
        again = read_labels(repeat_labels, sample, "repeat")
        both = [item for item in again if item in answered]
        pairs: list[tuple[Verdict, Verdict]] = [
            (answered[item], again[item].answer or "unknown")
            for item in both
            if "unknown" not in (answered[item], again[item].answer)
        ]
        repeated, repeated_unsure, intra = len(pairs), len(both) - len(pairs), kappa(pairs)
    calibration = Calibration(
        sample=sample.sample,
        items=len(sample.main),
        unlabeled=len(sample.main) - len(answered),
        unsure=sum(answer == "unknown" for answer in answered.values()),
        unjudged=len(sure) - len(compared),
        tuning=among(tuning_tasks),
        held_out=held_out,
        accepted=held_out.kappa is not None and held_out.kappa >= THRESHOLD,
        repeated=repeated,
        repeated_unsure=repeated_unsure,
        intra_kappa=intra,
    )
    return calibration, report(calibration, judged, human)


def report(calibration: Calibration, judged: dict[str, Judged], human: dict[str, Label]) -> str:
    held_out, tuning = calibration.held_out, calibration.tuning
    if held_out.kappa is None:
        verdict = "Todavía no se puede decidir: κ no está definido en el conjunto reservado."
    else:
        accepted = "se acepta" if calibration.accepted else "no se acepta"
        verdict = f"El juez {accepted}: κ = {_number(held_out.kappa)} en el conjunto reservado."
    lines = [
        "# Calibración del juez",
        "",
        f"Muestra `{calibration.sample}`. Umbral: κ ≥ {_number(THRESHOLD)} en el conjunto "
        "reservado.",
        "",
        f"**{verdict}**",
        "",
        "| Criterios | |",
        "| --- | ---: |",
        f"| En la muestra | {calibration.items} |",
        f"| Sin etiqueta | {calibration.unlabeled} |",
        f"| «No sé», fuera del κ | {calibration.unsure} |",
        f"| Sin veredicto del juez, fuera del κ | {calibration.unjudged} |",
        f"| Comparados | {len(held_out.items) + len(tuning.items)} |",
        "",
        "## Conjunto reservado",
        "",
        *_agreement(held_out),
        "## Conjunto de ajuste",
        "",
        *_agreement(tuning),
        "Sirve para ajustar la rúbrica y no decide la aceptación. Desacuerdos:",
        "",
        "| Criterio | Humano | Juez | Razón del juez | Nota |",
        "| --- | --- | --- | --- | --- |",
        *(
            f"| {_cell(judged[item].question.statement)} | {HUMAN[answer]} | "
            f"{judged[item].verdict} | {_cell(judged[item].reason)} | {_cell(human[item].note)} |"
            for item in tuning.items
            if (answer := human[item].answer) and answer != judged[item].verdict
        ),
        "",
        "## Consistencia del etiquetador",
        "",
        _intra(calibration),
        "",
    ]
    return "\n".join(lines)


def _agreement(agreement: Agreement) -> list[str]:
    named = ", ".join(f"`{task}`" for task in agreement.tasks) or "ninguna"
    measured = "no definido" if agreement.kappa is None else _number(agreement.kappa)
    rows = [
        f"| {HUMAN[human]} | "
        + " | ".join(str(agreement.matrix.get(f"{human}/{judge}", 0)) for judge in HUMAN)
        + " |"
        for human in ("pass", "fail")
    ]
    return [
        f"κ = {measured} en {len(agreement.items)} criterios de {len(agreement.tasks)} tareas: "
        f"{named}.",
        "",
        "| Humano \\ Juez | pass | fail | unknown |",
        "| --- | ---: | ---: | ---: |",
        *rows,
        "",
    ]


def _intra(calibration: Calibration) -> str:
    if not calibration.repeated and not calibration.repeated_unsure:
        return "Todavía no hay etiquetas de la repetición."
    measured = (
        "no definido" if calibration.intra_kappa is None else _number(calibration.intra_kappa)
    )
    return (
        f"κ = {measured} en {calibration.repeated} criterios repetidos; "
        f"{calibration.repeated_unsure} con «No sé» en alguna de las dos quedaron fuera."
    )


def _number(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")
