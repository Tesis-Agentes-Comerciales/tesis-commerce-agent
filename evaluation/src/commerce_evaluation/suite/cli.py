"""`eval-suite`: run a suite of the evaluation and report it (docs/design/eval-suite.md §8).

`run` plans the trials of the suite's cases, runs them through the live executor
(`suite/live_executor.py`, imported only when a run starts) and writes the report. Capability and
adversarial call a real LLM: they refuse to start without `--max-usd` and `ANTHROPIC_API_KEY`, and
only the owner launches them (AGENTS.md). The key is read from the environment or from the
repository's `.env`, and only it is loaded from there; its value is never printed. `--dry-run`
prints the plan and its estimated cost and touches nothing. Regression also runs the pytest ids
of the regression map. `report` rewrites the report of a run directory.
"""

import argparse
import importlib
import os
import re
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, get_args

from dotenv import dotenv_values

from commerce_evaluation.llm.budget import RunBudget
from commerce_evaluation.llm.cost import CLAUDE_PRICES
from commerce_evaluation.llm.transport import Effort
from commerce_evaluation.paths import DATA, RESULTS, ROOT
from commerce_evaluation.suite import mapped
from commerce_evaluation.suite.cases import SUITES, Suite, load_catalog
from commerce_evaluation.suite.estimate import estimate
from commerce_evaluation.suite.executor import RunSettings, TrialExecutor
from commerce_evaluation.suite.report import write
from commerce_evaluation.suite.report_md import SUITES as SUITE_NAMES
from commerce_evaluation.suite.run import (
    DEFAULT_SEED,
    K,
    Plan,
    RunMismatch,
    execute,
    pending,
    select,
)

RUNS = RESULTS / "suite"
CATALOG = DATA / "suite" / "cases"
ENV_FILE = ROOT / ".env"
KEY = "ANTHROPIC_API_KEY"
PAID: frozenset[Suite] = frozenset({"capability", "adversarial"})
AGENT = "claude-haiku-5-5"
EFFORT = "low"
# §3: a weak simulator adds noise charged to the agent, and no model speaks on both sides.
SIMULATOR = "claude-sonnet-5-5"
SIMULATOR_FOR_SONNET = "claude-opus-5-5"
EXECUTOR_MODULE = "commerce_evaluation.suite.live_executor"
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

Factory = Callable[[RunSettings, RunBudget | None], TrialExecutor]


class Refused(Exception):
    """The command cannot start as asked; its message says why, in Spanish."""


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        return arguments.command(arguments)
    except Refused as refusal:
        print(f"eval-suite: {refusal}", file=sys.stderr)
        return 2


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="eval-suite", description="Corre la suite de evaluación y escribe su reporte."
    )
    commands = root.add_subparsers(required=True, metavar="{run,report}")
    run = commands.add_parser(
        "run",
        help="corre una suite",
        description="Corre una suite. Capacidad y adversarial llaman a un LLM real y las lanza "
        "solo el dueño, con --max-usd y ANTHROPIC_API_KEY.",
    )
    run.set_defaults(command=run_command)
    run.add_argument("--suite", required=True, choices=SUITES)
    run.add_argument("--agent", help=f"modelo del agente (por defecto {AGENT}; no en regresión)")
    run.add_argument(
        "--effort", default=EFFORT, choices=get_args(Effort), help="esfuerzo del agente"
    )
    run.add_argument(
        "--simulator",
        help=f"modelo del usuario simulado (por defecto {SIMULATOR}, o {SIMULATOR_FOR_SONNET} "
        "si el agente es Sonnet; no en regresión)",
    )
    run.add_argument("--k", type=int, help="intentos por caso y gobernanza (regresión 1, si no 5)")
    run.add_argument("--max-usd", type=_usd, help="tope de gasto en USD; obligatorio si es paga")
    run.add_argument("--cases", metavar="GLOB", help="solo los casos cuyo id coincide")
    run.add_argument("--run-id", help="id de la corrida; uno que ya existe se retoma")
    run.add_argument("--seed", type=int, default=DEFAULT_SEED, help="semilla base")
    run.add_argument("--dry-run", action="store_true", help="muestra el plan y no corre nada")
    report = commands.add_parser("report", help="reescribe el reporte de una corrida")
    report.set_defaults(command=report_command)
    report.add_argument("run", metavar="RUN", help="id de la corrida o su carpeta")
    return root


def run_command(arguments: argparse.Namespace) -> int:
    suite: Suite = arguments.suite
    plan = _plan(arguments)
    settings = plan.settings
    entries = _map(arguments) if suite == "regression" else None
    if arguments.dry_run:
        _dry_run(plan, entries)
        return 0
    budget = None
    if suite in PAID:
        _require_paid(plan.max_usd)
    factory = load_executor()
    if suite in PAID and plan.max_usd is not None:
        budget = RunBudget(max_usd=plan.max_usd, run_dir=settings.run_dir)
    print(f"Corrida {settings.run_id} en {_shown(settings.run_dir)}")
    try:
        outcome = execute(plan, factory(settings, budget))
    except RunMismatch as error:
        raise Refused(str(error)) from error
    except KeyboardInterrupt:
        print(f"Interrumpida. Para seguir: {_resume(arguments, plan)}", file=sys.stderr)
        return 130
    if entries is not None and outcome.status == "complete":
        print(f"Pruebas del mapa de regresión: {len(mapped.mapped_ids(entries))}")
        mapped.run_mapped(entries, settings.run_dir)
    write(settings.run_dir)
    print(
        f"Intentos guardados: {outcome.kept} nuevos y {outcome.resumed} de antes. Repetidos por "
        f"infraestructura: {outcome.infra_retries}. Excluidos: {outcome.excluded}."
    )
    print(f"Reporte: {_shown(settings.run_dir / 'report.md')}")
    if outcome.status == "funds_exhausted":
        print(f"Se acabó el presupuesto. Para seguir: {_resume(arguments, plan)}", file=sys.stderr)
        return 1
    return 0


def report_command(arguments: argparse.Namespace) -> int:
    given = Path(arguments.run)
    run_dir = given if given.is_dir() else RUNS / arguments.run
    if not (run_dir / "manifest.json").exists():
        raise Refused(f"{arguments.run} no es una corrida: falta manifest.json")
    report = write(run_dir)
    print(f"Reporte: {_shown(run_dir / 'report.md')} ({len(report.reduction.tasks)} tareas)")
    return 0


def load_executor() -> Factory:
    """The live executor, imported only when a run starts."""
    try:
        module = importlib.import_module(EXECUTOR_MODULE)
    except ModuleNotFoundError as error:
        if error.name != EXECUTOR_MODULE:
            raise
        raise Refused(f"Falta {EXECUTOR_MODULE}: el ejecutor en vivo todavía no está.") from error
    return module.LiveExecutor


def _plan(arguments: argparse.Namespace) -> Plan:
    suite: Suite = arguments.suite
    agent, simulator = arguments.agent, arguments.simulator
    if suite in PAID:
        agent = agent or AGENT
        simulator = simulator or (SIMULATOR_FOR_SONNET if "sonnet" in agent else SIMULATOR)
        if unpriced := [model for model in (agent, simulator) if model not in CLAUDE_PRICES]:
            raise Refused(f"Sin precio para {', '.join(unpriced)}: no se le puede poner tope.")
        if agent == simulator:
            raise Refused("El agente y el usuario simulado no pueden ser el mismo modelo (§3).")
    elif agent or simulator:
        raise Refused("La regresión es guionada: no recibe --agent ni --simulator.")
    k = arguments.k or K[suite]
    if k < 1:
        raise Refused("--k tiene que ser al menos 1.")
    cases = select(load_catalog(CATALOG), suite, arguments.cases)
    if not cases:
        raise Refused(f"Ningún caso de la suite {suite} coincide con {arguments.cases}.")
    run_id = arguments.run_id or f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{suite}"
    if not RUN_ID.match(run_id):
        raise Refused(f"--run-id {run_id!r}: solo letras, dígitos, punto, guion y guion bajo.")
    settings = RunSettings(run_id, RUNS / run_id, suite, agent, arguments.effort, simulator)
    max_usd = arguments.max_usd if suite in PAID else None
    return Plan(settings, cases, k, arguments.seed, max_usd, CATALOG)


def _require_paid(max_usd: Decimal | None) -> None:
    if max_usd is None or not max_usd.is_finite() or max_usd <= 0:
        raise Refused("Una corrida paga exige --max-usd, un monto finito mayor que cero.")
    if not os.environ.get(KEY):
        if not (value := _env_file_key()):
            raise Refused(f"Falta {KEY}: ponla en el entorno o en {_shown(ENV_FILE)}.")
        # Only the key is loaded: the rest of .env must not change the stack under test.
        os.environ[KEY] = value


def _env_file_key() -> str | None:
    return dotenv_values(ENV_FILE).get(KEY) if ENV_FILE.is_file() else None


def _map(arguments: argparse.Namespace) -> dict[str, dict[str, Any]] | None:
    if not mapped.MAP_FILE.exists():
        print(f"No está {_shown(mapped.MAP_FILE)}: se omiten las pruebas del mapa de regresión.")
        return None
    return mapped.load_map(mapped.MAP_FILE, arguments.cases)


def _dry_run(plan: Plan, entries: dict[str, dict[str, Any]] | None) -> None:
    settings, trials = plan.settings, plan.trials()
    left = pending(plan)
    agent = settings.agent_model
    print(f"Plan de la corrida {settings.run_id}: no se corre nada.")
    simulator = settings.simulator_model
    print(f"Suite {SUITE_NAMES[settings.suite]}, k = {plan.k}, semilla {plan.seed}.")
    print(f"Agente: {f'{agent}, esfuerzo {settings.agent_effort}' if agent else 'guionado'}.")
    print(f"Usuario: {f'simulado por {simulator}' if simulator else 'guionado'}.")
    print(f"Casos: {len(plan.cases)}. Intentos: {len(trials)}, de ellos {len(left)} por correr.")
    for case in plan.cases:
        governances = case.environment.governance
        each = " en cada una" if len(governances) > 1 else ""
        print(f"  {case.id}: {' y '.join(governances)}, {plan.k} intentos{each}")
    if settings.suite in PAID:
        cost = estimate(left, agent, simulator)
        print(
            "Costo estimado del agente y del usuario simulado (el juez va aparte): "
            f"típico {_money(cost.typical_usd)}, peor caso {_money(cost.worst_usd)}."
        )
        cap = plan.max_usd
        if cap is None:
            print("Tope: falta --max-usd, que la corrida exige.")
        else:
            fits = (
                "cabe" if cost.worst_usd <= cap else "lo pasa: la corrida se detendría en el tope"
            )
            print(f"Tope: {_money(cap)}; el peor caso {fits}.")
        found = "en el entorno" if os.environ.get(KEY) else "en .env" if _env_file_key() else None
        print(f"{KEY}: {found or 'falta, y la corrida la exige'}.")
    state = "existe: se retoma" if settings.run_dir.exists() else "nueva"
    print(f"Carpeta: {_shown(settings.run_dir)} ({state}).")
    if entries is not None:
        counts = mapped.summary(mapped.coverage(entry) for entry in entries.values())
        print(
            f"Mapa de regresión: {len(entries)} tareas ({counts['covered']} cubiertas, "
            f"{counts['partial']} parciales, {counts['uncovered']} sin cubrir) y "
            f"{len(mapped.mapped_ids(entries))} pruebas con RUN_LIVE_STACK=1."
        )


def _resume(arguments: argparse.Namespace, plan: Plan) -> str:
    settings = plan.settings
    words = ["uv run eval-suite run", f"--suite {settings.suite}", f"--run-id {settings.run_id}"]
    if settings.suite in PAID:
        words += [f"--agent {settings.agent_model}", f"--effort {settings.agent_effort}"]
        words += [f"--simulator {settings.simulator_model}", "--max-usd <tope mayor>"]
    words += [f"--k {plan.k}", f"--seed {plan.seed}"]
    if arguments.cases:
        words.append(f"--cases '{arguments.cases}'")
    return " ".join(words)


def _money(amount: Decimal) -> str:
    return "USD " + f"{amount:.2f}".replace(".", ",")


def _usd(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(f"{value!r} no es un monto") from error


def _shown(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
