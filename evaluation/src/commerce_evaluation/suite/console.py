"""What `eval-suite` prints before it spends, and where it finds the key (suite/cli.py).

The dry runs: a run's plan and estimated cost (suite/estimate.py), and the judge's questions and
reservation (suite/judging.py). Neither calls a model nor writes. The key is read from the
environment or from the repository's `.env`; only whether it was found is ever printed.
"""

import os
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from commerce_evaluation.paths import ROOT
from commerce_evaluation.suite import mapped
from commerce_evaluation.suite.cases import Suite
from commerce_evaluation.suite.estimate import estimate
from commerce_evaluation.suite.judging import Pending
from commerce_evaluation.suite.plan import Plan, pending
from commerce_evaluation.suite.report import Report
from commerce_evaluation.suite.report_md import SUITES
from commerce_evaluation.suite.run import resume_problem

ENV_FILE = ROOT / ".env"
KEY = "ANTHROPIC_API_KEY"
PAID: frozenset[Suite] = frozenset({"capability", "adversarial"})
# A mapped test that did not pass. A skipped one is reported but does not fail the regression.
NOT_PASSED = ("failed", "not_run")

Say = Callable[[str], None]


def env_file_key() -> str | None:
    return dotenv_values(ENV_FILE).get(KEY) if ENV_FILE.is_file() else None


def key_found() -> str:
    found = "en el entorno" if os.environ.get(KEY) else "en .env" if env_file_key() else None
    return f"{KEY}: {found or 'falta, y la llamada la exige'}."


def run_preview(plan: Plan, entries: dict[str, dict[str, Any]] | None, say: Say = print) -> None:
    settings, trials, left = plan.settings, plan.trials(), pending(plan)
    agent, simulator = settings.agent_model, settings.simulator_model
    say(f"Plan de la corrida {settings.run_id}: no se corre nada.")
    say(f"Suite {SUITES[settings.suite]}, k = {plan.k}, semilla {plan.seed}.")
    say(f"Agente: {f'{agent}, esfuerzo {settings.agent_effort}' if agent else 'guionado'}.")
    say(f"Usuario: {f'simulado por {simulator}' if simulator else 'guionado'}.")
    say(f"Casos: {len(plan.cases)}. Intentos: {len(trials)}, de ellos {len(left)} por correr.")
    for case in plan.cases:
        governances = case.environment.governance
        each = " en cada una" if len(governances) > 1 else ""
        say(f"  {case.id}: {' y '.join(governances)}, {plan.k} intentos{each}")
    if settings.suite in PAID:
        cost = estimate(left, agent, simulator)
        say(
            "Costo estimado del agente y del usuario simulado, sin los reintentos (el juez va "
            f"aparte): típico {money(cost.typical_usd)}, peor caso {money(cost.worst_usd)}."
        )
        if (cap := plan.max_usd) is None:
            say("Tope: falta --max-usd, que la corrida exige.")
        else:
            fits = "cabe" if cost.worst_usd <= cap else "lo pasa: se detendría en el tope"
            say(f"Tope: {money(cap)}, sumando lo ya gastado; el peor caso {fits}.")
        say(key_found())
    state = "nueva"
    if settings.run_dir.exists():
        problem = resume_problem(plan)
        state = f"no se puede retomar. {problem}" if problem else "existe: se retoma"
    say(f"Carpeta: {shown(settings.run_dir)} ({state}).")
    if entries is not None:
        counts = mapped.summary(mapped.coverage(entry) for entry in entries.values())
        say(
            f"Mapa de regresión: {len(entries)} tareas ({counts['covered']} cubiertas, "
            f"{counts['partial']} parciales, {counts['uncovered']} sin cubrir) y "
            f"{len(mapped.mapped_ids(entries))} pruebas con RUN_LIVE_STACK=1."
        )


def judge_preview(todo: Pending, cap: Decimal | None, say: Say = print) -> None:
    say(
        f"El lote se reserva entero antes de enviarlo: peor caso {money(todo.worst_usd)} a la "
        f"tarifa de lotes. El juez ya gastó {money(todo.spent_usd)} en esta corrida."
    )
    if not todo.trials:
        say("No hay nada que preguntar.")
    elif cap is None:
        say("Tope: falta --max-usd, que el juez exige.")
    else:
        fits = todo.spent_usd + todo.worst_usd <= cap
        verdict = "cabe" if fits else "no cabe, y el lote entero se rechazaría sin enviar nada"
        say(f"Tope: {money(cap)}, sumando lo ya gastado; el peor caso {verdict}.")
    say(key_found())


def failing(report: Report) -> str | None:
    """Why a regression does not pass, or None: a trial or a mapped test that did not pass."""
    trials = [task for task in report.reduction.tasks if task.passes != task.trials]
    tests = [
        test for task in report.mapped or () for test in task.tests if test.outcome in NOT_PASSED
    ]
    if not trials and not tests:
        return None
    return (
        f"{len(trials)} tareas con intentos que no pasan y {len(tests)} pruebas del mapa que "
        "fallan o no corrieron"
    )


def money(amount: Decimal) -> str:
    return "USD " + f"{amount:.2f}".replace(".", ",")


def shown(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
