"""The report of a run as Markdown, in Spanish, for a person to read (suite/report.py)."""

from collections.abc import Iterable, Sequence
from operator import attrgetter, itemgetter
from typing import TYPE_CHECKING, Any

from commerce_evaluation.suite.cases import Grader
from commerce_evaluation.suite.mapped import Coverage, summary
from commerce_evaluation.suite.reduce import Rate

if TYPE_CHECKING:
    from commerce_evaluation.suite.report import Report

SUITES = {"regression": "regresión", "capability": "capacidad", "adversarial": "adversarial"}
STATUSES = {
    "running": "en curso",
    "complete": "completa",
    "funds_exhausted": "detenida por falta de presupuesto",
    "interrupted": "interrumpida",
}
GRADERS: dict[Grader, str] = {
    "effects": "Efectos",
    "outcome": "Resultado",
    "evidence": "Evidencia",
    "claims": "Lo que dice",
}
ROLES = {"agent": "Agente", "simulator": "Usuario simulado", "judge": "Juez"}
COVERAGE: dict[Coverage, str] = {
    "covered": "cubierta",
    "partial": "parcial",
    "uncovered": "sin cubrir",
}
PLURAL: dict[Coverage, str] = {
    "covered": "cubiertas",
    "partial": "parciales",
    "uncovered": "sin cubrir",
}
OUTCOMES = {"passed": "pasa", "failed": "falla", "skipped": "se saltó", "not_run": "no corrió"}


def markdown(report: "Report") -> str:
    manifest = report.manifest
    sections = [
        f"# Reporte de la corrida {manifest['run_id']}",
        _settings(report),
        _governances(report),
        _tasks(report),
        _graders(report),
        _attacks(report),
        _cost(report),
        _infra(report),
        _review(report),
        _mapped(report),
    ]
    return "\n\n".join(section for section in sections if section) + "\n"


def _settings(report: "Report") -> str:
    manifest: dict[str, Any] = report.manifest
    agent, simulator = manifest["agent"], manifest["simulator"]
    git = manifest["sessions"][0]["git"]
    dirty = {True: "con cambios sin confirmar", False: "sin cambios locales", None: "sin datos"}
    lines = [
        f"Suite {SUITES[manifest['suite']]}, con {manifest['k']} intentos por caso y gobernanza, "
        f"y semilla {manifest['seed']}.",
        "Agente: "
        + (f"`{agent['model']}`, esfuerzo `{agent['effort']}`." if agent["model"] else "guionado."),
        "Usuario: "
        + (f"simulado por `{simulator['model']}`." if simulator["model"] else "guionado."),
        f"Commit: `{git['commit']}`, {dirty[git['dirty']]}.",
        f"Inicio: {manifest['started_at']}. Fin: {manifest['ended_at'] or 'sin terminar'}. "
        f"Estado: {STATUSES[manifest['status']]}.",
        f"Tope de gasto: {_usd(manifest['max_usd']) if manifest['max_usd'] else 'ninguno'}. "
        f"Gastado: {_usd(report.usd)}.",
        "Lo que dice: "
        + ("con el juez (`judge.jsonl`)." if report.judged else "solo código, sin el juez."),
    ]
    return "\n".join(f"- {line}" for line in lines)


def _governances(report: "Report") -> str:
    k = report.reduction.k
    header = (
        "Gobernanza",
        "Tareas",
        "Intentos",
        "Pasan",
        "Fallan",
        "Sin decidir",
        "Infraestructura",
        "pass^1",
        f"pass^{k}",
    )
    counts = attrgetter("governance", "tasks", "trials", "passes", "fails", "unknowns")
    rows = [
        (*counts(each), each.infra_failures, _rate(each.pass_1), _rate(each.pass_k))
        for each in report.reduction.governances
    ]
    text = "## Resultado por gobernanza\n\n" + _table(header, rows)
    text += "\n\npass^1 y pass^k son promedios por tarea, con su intervalo de Wilson al 95 %."
    if short := sorted({task for each in report.reduction.governances for task in each.short}):
        text += f" Sin {k} intentos calificados, pass^{k} no se calcula en: {', '.join(short)}."
    return text


def _tasks(report: "Report") -> str:
    k = report.reduction.k
    graders = {(each.case_id, each.governance): each.graders for each in report.tasks}
    header = ("Tarea", "Gobernanza", "Intentos", "pass^1", f"pass^{k}", *GRADERS.values())
    rows = []
    for task in report.reduction.tasks:
        tally = graders[(task.case_id, task.governance)]
        verdicts = [
            "/".join(str(tally[name][verdict]) for verdict in ("pass", "fail", "unknown"))
            if name in tally
            else "no cuenta"
            for name in GRADERS
        ]
        trials = f"{task.trials - task.infra_failures} de {task.trials}"
        rows.append(
            (
                task.case_id,
                task.governance,
                trials,
                _rate(task.pass_1),
                _share(task.pass_k),
                *verdicts,
            )
        )
    legend = (
        "Intentos: los calificados de los registrados. En cada calificador: pasa/falla/sin decidir."
    )
    return f"## Por tarea\n\n{legend}\n\n{_table(header, rows)}"


def _graders(report: "Report") -> str:
    header = ("Gobernanza", "Calificador", "Pasa", "Falla", "Sin decidir")
    rows = [
        (each.governance, GRADERS[name], tally["pass"], tally["fail"], tally["unknown"])
        for each in report.governances
        for name, tally in sorted(
            each.graders.items(), key=lambda item: list(GRADERS).index(item[0])
        )
    ]
    return "## Calificadores\n\n" + _table(header, rows)


def _attacks(report: "Report") -> str:
    attacked = [task for task in report.reduction.tasks if task.attacks is not None]
    if not attacked:
        return ""
    rates = [
        (each.governance, *_attack(each.attack_success), each.attack_unknowns)
        for each in report.reduction.governances
        if each.attack_success is not None
    ]
    per_task = [
        (task.case_id, task.governance, *_attack(task.attacks), task.attack_unknowns)
        for task in attacked
    ]
    return (
        "## Ataques que logran su objetivo\n\n"
        "Por intento: un checkout o una orden de una variante prohibida. El control, sin "
        "variantes prohibidas, no entra en la tasa.\n\n"
        + _table(("Gobernanza", "Lograron", "Tasa", "Sin decidir"), rates)
        + "\n\n"
        + _table(("Tarea", "Gobernanza", "Lograron", "Tasa", "Sin decidir"), per_task)
    )


def _cost(report: "Report") -> str:
    header = ("Rol", "Llamadas", "Entrada", "Salida", "Caché leída", "Caché escrita", "USD")
    tokens = attrgetter(
        "calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"
    )
    rows: list[tuple[object, ...]] = [
        (ROLES.get(role.role, role.role), *tokens(role), _usd(role.usd)) for role in report.cost
    ]
    if not rows:
        return "## Costo\n\nNinguna llamada a un LLM: `llm-usage.jsonl` está vacío o no existe."
    rows.append(
        ("Total", sum(role.calls for role in report.cost), "", "", "", "", _usd(report.usd))
    )
    return "## Costo\n\nDe `llm-usage.jsonl`, en tokens.\n\n" + _table(header, rows)


def _infra(report: "Report") -> str:
    text = (
        "## Infraestructura y exclusiones\n\n"
        f"Intentos repetidos por una falla de infraestructura: {len(report.infra_retries)}. "
        f"Intentos excluidos porque el usuario simulado falló dos veces: {len(report.excluded)}. "
        "Ninguno cuenta contra el agente."
    )
    if report.infra_retries:
        cells = itemgetter("case_id", "governance", "trial", "attempt", "cause", "detail")
        rows = [cells(row) for row in report.infra_retries]
        header = ("Tarea", "Gobernanza", "Intento", "Vez", "Causa", "Detalle")
        text += "\n\nRepetidos:\n\n" + _table(header, rows)
    if report.excluded:
        cells = itemgetter("case_id", "governance", "trial", "reason")
        rows = [cells(row) for row in report.excluded]
        text += "\n\nExcluidos:\n\n" + _table(("Tarea", "Gobernanza", "Intento", "Motivo"), rows)
    return text


def _review(report: "Report") -> str:
    if not report.review:
        return "## Para revisión humana\n\nNingún intento quedó sin decidir."
    rows = [
        (each.case_id, each.governance, each.trial, f"`{each.run_id}`", GRADERS[name], reason)
        for each in report.review
        for name, reason in each.unknown.items()
    ]
    header = ("Tarea", "Gobernanza", "Intento", "run_id", "Calificador", "Por qué")
    return (
        "## Para revisión humana\n\n"
        f"{len(report.review)} intentos quedaron sin decidir y no cuentan como acierto.\n\n"
        + _table(header, rows)
    )


def _mapped(report: "Report") -> str:
    if report.mapped is None:
        return ""
    counts = summary(task.coverage for task in report.mapped)
    told = ", ".join(f"{count} {PLURAL[name]}" for name, count in counts.items())
    rows = []
    for task in report.mapped:
        results = [OUTCOMES[test.outcome] for test in task.tests]
        tally = ", ".join(f"{results.count(word)} {word}" for word in dict.fromkeys(results))
        rows.append(
            (
                task.task,
                task.source,
                COVERAGE[task.coverage],
                len(task.tests),
                tally or "sin pruebas",
            )
        )
    return (
        "## Mapa de regresión\n\n"
        f"Las tareas que cubren las pruebas guionadas: {told}.\n\n"
        + _table(("Tarea", "Fuente", "Cobertura", "Pruebas", "Resultado"), rows)
    )


def _table(header: Sequence[object], rows: Iterable[Sequence[object]]) -> str:
    lines = [_line(header), "| " + " | ".join("---" for _ in header) + " |"]
    lines += [_line(row) for row in rows]
    return "\n".join(lines)


def _line(cells: Sequence[object]) -> str:
    return "| " + " | ".join(_cell(cell) for cell in cells) + " |"


def _cell(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}".replace(",", ".")
    return " ".join(str(value).split()).replace("|", "\\|")


def _rate(rate: Rate | None) -> str:
    if rate is None or rate.rate is None or rate.low is None or rate.high is None:
        return "sin datos"
    return f"{_pct(rate.rate)} ({_pct(rate.low)} a {_pct(rate.high)})"


def _share(value: float | None) -> str:
    return "sin datos" if value is None else _pct(value)


def _attack(rate: Rate | None) -> tuple[str, str]:
    """How many attacks reached their goal, of how many could be told, and the rate."""
    return (
        ("sin datos", "sin datos")
        if rate is None
        else (f"{rate.successes:g} de {rate.n}", _rate(rate))
    )


def _pct(value: float) -> str:
    return f"{value * 100:.1f}".replace(".", ",") + " %"


def _usd(value: object) -> str:
    return "USD " + f"{float(str(value)):.4f}".replace(".", ",")
