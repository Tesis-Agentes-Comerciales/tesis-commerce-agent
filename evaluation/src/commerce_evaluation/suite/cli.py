"""`eval-suite`: run a suite of the evaluation, judge it and report it (eval-suite.md §8).

`run` plans the trials of the suite's cases, runs them through the live executor
(`suite/live_executor.py`, imported only when a run starts) and writes the report. `judge` asks the
judge what code left to it (suite/judging.py). A capability or adversarial run, and the judge,
call a real LLM: they refuse to start without `--max-usd` and `ANTHROPIC_API_KEY`, and only the
owner launches them (AGENTS.md). The key is read from the environment or from the repository's
`.env`, and only it is loaded from there; its value is never printed. `--dry-run` prints the plan
and its cost and touches nothing. Regression also runs the pytest ids of the regression map, and
fails when code fails a trial or leaves it undecided, or one of those tests does not pass; a
trial that only waits for the judge on what the agent says does not fail it (`console.failing`).
`report` rewrites the report of a run, and `calibrate` is `suite/calibration.py`.
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

from commerce_evaluation.llm.budget import LEDGER_FILE, RunBudget
from commerce_evaluation.llm.cost import CLAUDE_PRICES
from commerce_evaluation.llm.transport import Effort, FundsExhausted
from commerce_evaluation.paths import DATA, RESULTS
from commerce_evaluation.suite import calibration, console, judging, mapped
from commerce_evaluation.suite.cases import SUITES, Case, Suite, load_catalog
from commerce_evaluation.suite.console import KEY, PAID, money, shown
from commerce_evaluation.suite.executor import RunSettings, TrialExecutor
from commerce_evaluation.suite.graders.judge import MODEL as JUDGE
from commerce_evaluation.suite.plan import DEFAULT_SEED, K, Plan, select
from commerce_evaluation.suite.report import write
from commerce_evaluation.suite.run import RunMismatch, Status, execute, resume_problem
from commerce_evaluation.suite.rundir import MANIFEST_FILE, repair_jsonl

RUNS = RESULTS / "suite"
CATALOG = DATA / "suite" / "cases"
AGENT = "claude-haiku-5-5"
EFFORT = "low"
# §3: a weak simulator adds noise charged to the agent, and no model speaks on both sides.
SIMULATOR = "claude-sonnet-5-5"
SIMULATOR_FOR_SONNET = "claude-opus-5-5"
EXECUTOR_MODULE = "commerce_evaluation.suite.live_executor"
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
STOPPED: dict[Status, str] = {
    "budget_exhausted": "La corrida llegó a su tope de gasto.",
    "funds_exhausted": "Una llamada no encontró fondos, por el tope o por el saldo de Anthropic.",
    "misconfigured": (
        "La configuración del modelo del agente falla: revisa la clave, sus permisos, el nombre "
        "del modelo y la solicitud que rechaza el proveedor."
    ),
    "interrupted": "La infraestructura falló varias veces seguidas: revisa el stack.",
}

Factory = Callable[[RunSettings, RunBudget | None], TrialExecutor]


class Refused(Exception):
    """The command cannot start as asked; its message says why, in Spanish."""


def main(argv: Sequence[str] | None = None) -> int:
    given = list(sys.argv[1:] if argv is None else argv)
    if given[:1] == ["calibrate"]:
        return calibration.main(given[1:])
    arguments = parser().parse_args(given)
    try:
        return arguments.command(arguments)
    except Refused as refusal:
        print(f"eval-suite: {refusal}", file=sys.stderr)
        return 2


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="eval-suite", description="Corre la suite de evaluación, la juzga y la reporta."
    )
    commands = root.add_subparsers(required=True, metavar="{run,judge,report,calibrate}")
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
    judge = commands.add_parser(
        "judge",
        help="juzga lo que el código dejó al juez",
        description=f"Pregunta al juez ({JUDGE}, en un lote) lo que el código no pudo calificar "
        "y reescribe el reporte. Llama a un LLM real: lo lanza solo el dueño, con su propio "
        "--max-usd. Lo ya juzgado no se vuelve a preguntar.",
    )
    judge.set_defaults(command=judge_command)
    judge.add_argument("run", metavar="RUN", help="id de la corrida o su carpeta")
    judge.add_argument(
        "--max-usd", type=_usd, help="tope del juez en USD, sumando sus sesiones anteriores"
    )
    judge.add_argument("--dry-run", action="store_true", help="muestra las preguntas y su costo")
    report = commands.add_parser("report", help="reescribe el reporte de una corrida")
    report.set_defaults(command=report_command)
    report.add_argument("run", metavar="RUN", help="id de la corrida o su carpeta")
    # Listed for --help; main() hands its arguments to suite/calibration.py untouched.
    commands.add_parser("calibrate", help="calibra el juez: export o score (ver calibrate -h)")
    return root


def run_command(arguments: argparse.Namespace) -> int:
    plan = _plan(arguments)
    settings, paid = plan.settings, plan.settings.suite in PAID
    entries = _map(arguments) if settings.suite == "regression" else None
    if arguments.dry_run:
        console.run_preview(plan, entries)
        return 0
    if problem := resume_problem(plan):
        raise Refused(problem)
    if paid:
        _require_paid(plan.max_usd)
    factory, budget = load_executor(), None
    # The executor names the plan's events it does not apply, before it starts or spends.
    check: Callable[[Sequence[Case]], list[str]] = getattr(factory, "unsupported_events", _none)
    if unsupported := check(plan.cases):
        raise Refused("El ejecutor en vivo no aplica estos eventos: " + "; ".join(unsupported))
    if paid and plan.max_usd is not None:
        repair_jsonl(settings.run_dir / LEDGER_FILE)
        budget = RunBudget(max_usd=plan.max_usd, run_dir=settings.run_dir)
    print(f"Corrida {settings.run_id} en {shown(settings.run_dir)}")
    try:
        outcome = execute(plan, factory(settings, budget), budget=budget)
    except RunMismatch as error:
        raise Refused(str(error)) from error
    except KeyboardInterrupt:
        print(f"Interrumpida. Para seguir: {_resume(arguments, plan)}", file=sys.stderr)
        return 130
    if entries is not None and outcome.status == "complete":
        print(f"Pruebas del mapa de regresión: {len(mapped.mapped_ids(entries))}")
        mapped.run_mapped(entries, settings.run_dir)
    report = write(settings.run_dir)
    print(
        f"Intentos guardados: {outcome.kept} nuevos y {outcome.resumed} de antes. Repetidos: "
        f"{outcome.infra_retries} por infraestructura y {outcome.simulator_retries} por el "
        f"usuario simulado. Excluidos: {outcome.excluded}."
    )
    print(f"Reporte: {shown(settings.run_dir / 'report.md')}")
    if injected := console.delivery(report):
        print(injected)
    if outcome.status != "complete":
        spent = f" Ya van {money(budget.spent_usd)}." if budget else ""
        stopped = f"{STOPPED[outcome.status]}{spent} Para seguir: {_resume(arguments, plan)}"
        print(stopped, file=sys.stderr)
        return 1
    if settings.suite != "regression":
        return 0
    if undecided := console.undecided(report):
        print(f"{undecided}. Solo los que esperan al juez no hacen fallar la regresión.")
    if failing := console.failing(report):
        print(f"La regresión no pasa: {failing}.", file=sys.stderr)
        return 1
    print("La regresión pasa.")
    return 0


def judge_command(arguments: argparse.Namespace) -> int:
    run_dir = _run_dir(arguments.run)
    todo = judging.pending(run_dir)
    print(
        f"Juez {JUDGE} sobre {run_dir.name}: {todo.answered} intentos ya juzgados, "
        f"{len(todo.trials)} por juzgar con {todo.questions} preguntas."
    )
    if arguments.dry_run:
        console.judge_preview(todo, arguments.max_usd)
        return 0
    if todo.trials:
        _require_paid(arguments.max_usd)
        print("Se envía un lote: Anthropic puede tardar hasta 24 horas en devolverlo.")
    try:
        judged = judging.judge(run_dir, judging.batch_client, max_usd=arguments.max_usd)
    except FundsExhausted:
        write(run_dir)
        print(
            "El juez no encontró fondos, por su tope o por el saldo; lo pagado quedó en "
            f"judge.jsonl. Ya gastó {money(judging.pending(run_dir).spent_usd)}: para seguir, "
            "otra vez con un --max-usd que sume eso y el peor caso que da judge --dry-run.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print(
            f"Interrumpido. El lote {judging.last_batch(run_dir) or '(sin enviar)'} sigue en "
            "Anthropic y se cobra, pero sus respuestas no se recogen: volver a correr judge "
            "envía un lote nuevo y paga esas preguntas otra vez.",
            file=sys.stderr,
        )
        return 130
    write(run_dir)
    counts = judging.tally(judged)
    print(
        f"Respuestas nuevas: {counts['pass']} cumple, {counts['fail']} no cumple y "
        f"{counts['unknown']} sin decidir, de ellas {counts['failure']} sin respuesta usable."
    )
    print(f"Reporte: {shown(run_dir / 'report.md')}")
    return 0


def report_command(arguments: argparse.Namespace) -> int:
    run_dir = _run_dir(arguments.run)
    report = write(run_dir)
    print(f"Reporte: {shown(run_dir / 'report.md')} ({len(report.reduction.tasks)} tareas)")
    if injected := console.delivery(report):
        print(injected)
    return 0


def _none(cases: Sequence[Case]) -> list[str]:
    return []


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
    if max_usd is None:
        raise Refused("Llamar a un LLM real exige --max-usd.")
    if not os.environ.get(KEY):
        if not (value := console.env_file_key()):
            raise Refused(f"Falta {KEY}: ponla en el entorno o en {shown(console.ENV_FILE)}.")
        # Only the key is loaded: the rest of .env must not change the stack under test.
        os.environ[KEY] = value


def _map(arguments: argparse.Namespace) -> dict[str, dict[str, Any]] | None:
    if not mapped.MAP_FILE.exists():
        print(f"No está {shown(mapped.MAP_FILE)}: se omiten las pruebas del mapa de regresión.")
        return None
    return mapped.load_map(mapped.MAP_FILE, arguments.cases)


def _run_dir(given: str) -> Path:
    path = Path(given)
    run_dir = path if path.is_dir() else RUNS / given
    if not (run_dir / MANIFEST_FILE).exists():
        raise Refused(f"{given} no es una corrida: falta {MANIFEST_FILE}")
    return run_dir


def _resume(arguments: argparse.Namespace, plan: Plan) -> str:
    settings = plan.settings
    words = ["uv run eval-suite run", f"--suite {settings.suite}", f"--run-id {settings.run_id}"]
    if settings.suite in PAID:
        words += [f"--agent {settings.agent_model}", f"--effort {settings.agent_effort}"]
        words += [f"--simulator {settings.simulator_model}", "--max-usd <tope>"]
    words += [f"--k {plan.k}", f"--seed {plan.seed}"]
    if arguments.cases:
        words.append(f"--cases '{arguments.cases}'")
    return " ".join(words)


def _usd(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(f"{value!r} no es un monto") from error
    if not amount.is_finite() or amount <= 0:
        raise argparse.ArgumentTypeError(f"{value!r}: tiene que ser un monto finito mayor que 0")
    return amount
