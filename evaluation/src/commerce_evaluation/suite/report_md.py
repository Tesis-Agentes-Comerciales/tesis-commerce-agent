"""The report of a run as Markdown, in Spanish, for a person to read (suite/report.py)."""

from operator import attrgetter, itemgetter
from typing import TYPE_CHECKING, Any

from commerce_evaluation.suite.cases import Grader
from commerce_evaluation.suite.graders.claims import FOR_THE_JUDGE
from commerce_evaluation.suite.mapped import Coverage, summary
from commerce_evaluation.suite.tables import attack, rate, share, table, usd

if TYPE_CHECKING:
    from commerce_evaluation.suite.report import Report

SUITES = {"regression": "regresión", "capability": "capacidad", "adversarial": "adversarial"}
STATUSES = {
    "running": "en curso",
    "complete": "completa",
    "budget_exhausted": "detenida al llegar al tope de gasto",
    "funds_exhausted": "detenida porque una llamada no encontró fondos",
    "interrupted": "interrumpida",
}
GRADERS: dict[Grader, str] = {
    "effects": "Efectos",
    "outcome": "Resultado",
    "evidence": "Evidencia",
    "claims": "Lo que dice",
}
ROLES = {"agent": "Agente", "simulator": "Usuario simulado", "judge": "Juez"}
CAUSES = {"simulator": "usuario simulado", "stack_not_started": "el stack no arrancó"}
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
        f"Tope de gasto: {usd(manifest['max_usd']) if manifest['max_usd'] else 'ninguno'}. "
        f"Gastado: {usd(report.usd)}.",
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
        (*counts(each), each.infra_failures, rate(each.pass_1), rate(each.pass_k))
        for each in report.reduction.governances
    ]
    text = "## Resultado por gobernanza\n\n" + table(header, rows)
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
                rate(task.pass_1),
                share(task.pass_k),
                *verdicts,
            )
        )
    legend = (
        "Intentos: los calificados de los registrados. En cada calificador: pasa/falla/sin decidir."
    )
    return f"## Por tarea\n\n{legend}\n\n{table(header, rows)}"


def _graders(report: "Report") -> str:
    header = ("Gobernanza", "Calificador", "Pasa", "Falla", "Sin decidir")
    rows = [
        (each.governance, GRADERS[name], tally["pass"], tally["fail"], tally["unknown"])
        for each in report.governances
        for name, tally in sorted(
            each.graders.items(), key=lambda item: list(GRADERS).index(item[0])
        )
    ]
    return "## Calificadores\n\n" + table(header, rows)


def _attacks(report: "Report") -> str:
    attacked = [task for task in report.reduction.tasks if task.attacks is not None]
    if not attacked:
        return ""
    rates = [
        (each.governance, *attack(each.attack_success), each.attack_unknowns)
        for each in report.reduction.governances
        if each.attack_success is not None
    ]
    per_task = [
        (task.case_id, task.governance, *attack(task.attacks), task.attack_unknowns)
        for task in attacked
    ]
    if report.manifest["suite"] == "adversarial":
        title = "## Ataques que logran su objetivo\n\nPor intento: un checkout o una orden de "
        title += (
            "una variante prohibida. El control, sin variantes prohibidas, no entra en la tasa."
        )
    else:
        title = "## Compras de una variante prohibida\n\nPor intento: un checkout o una orden de "
        title += "una variante que el caso prohíbe."
    return (
        title
        + "\n\n"
        + table(("Gobernanza", "Lograron", "Tasa", "Sin decidir"), rates)
        + "\n\n"
        + table(("Tarea", "Gobernanza", "Lograron", "Tasa", "Sin decidir"), per_task)
    )


def _cost(report: "Report") -> str:
    header = ("Rol", "Llamadas", "Entrada", "Salida", "Caché leída", "Caché escrita", "USD")
    tokens = attrgetter(
        "calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"
    )
    rows: list[tuple[object, ...]] = [
        (ROLES.get(role.role, role.role), *tokens(role), usd(role.usd)) for role in report.cost
    ]
    if not rows:
        return "## Costo\n\nNinguna llamada a un LLM: `llm-usage.jsonl` está vacío o no existe."
    rows.append(("Total", sum(role.calls for role in report.cost), "", "", "", "", usd(report.usd)))
    legend = (
        "De `llm-usage.jsonl` y del de `judge/`, en tokens. Una llamada que el tope rechazó "
        "cuenta como llamada, con USD 0. Una que falló sin informar su uso cuenta su peor caso, "
        "que queda reservado porque pudo cobrarse."
    )
    return f"## Costo\n\n{legend}\n\n" + table(header, rows)


def _infra(report: "Report") -> str:
    infra = sum(row["kind"] == "infra" for row in report.retries)
    text = (
        "## Infraestructura y exclusiones\n\n"
        f"Intentos repetidos por una falla de infraestructura: {infra}. Repetidos porque el "
        f"usuario simulado falló: {len(report.retries) - infra}. Excluidos porque el usuario "
        f"simulado falló dos veces: {len(report.excluded)}. Ninguno cuenta contra el agente."
    )
    if report.retries:
        rows = [
            (
                *itemgetter("case_id", "governance", "trial")(row),
                "antes de retomar" if row["attempt"] is None else row["attempt"],
                CAUSES.get(str(row["cause"]), row["cause"]),
                row["detail"],
            )
            for row in report.retries
        ]
        header = ("Tarea", "Gobernanza", "Intento", "Vez", "Causa", "Detalle")
        text += "\n\nRepetidos:\n\n" + table(header, rows)
    if report.excluded:
        cells = itemgetter("case_id", "governance", "trial", "reason")
        rows = [cells(row) for row in report.excluded]
        text += "\n\nExcluidos:\n\n" + table(("Tarea", "Gobernanza", "Intento", "Motivo"), rows)
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
    told = f"{len(report.review)} intentos quedaron sin decidir y no cuentan como acierto."
    if report.manifest["suite"] == "regression":
        told += (
            f" En regresión no hacen fallar la corrida (§8): {for_the_judge(report)} quedan "
            "pendientes del juez en lo que dice."
        )
    return "## Para revisión humana\n\n" + told + "\n\n" + table(header, rows)


def for_the_judge(report: "Report") -> int:
    """The undecided trials whose `claims` code left to the judge (graders/claims.py)."""
    return sum(each.unknown.get("claims", "").startswith(FOR_THE_JUDGE) for each in report.review)


def _mapped(report: "Report") -> str:
    if report.mapped is None:
        return ""
    counts = summary(task.coverage for task in report.mapped)
    told = ", ".join(f"{count} {PLURAL[name]}" for name, count in counts.items())
    rows = []
    for task in report.mapped:
        results = [
            OUTCOMES[test.outcome] + (f": {test.reason}" if test.reason else "")
            for test in task.tests
        ]
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
        + table(("Tarea", "Fuente", "Cobertura", "Pruebas", "Resultado"), rows)
    )
