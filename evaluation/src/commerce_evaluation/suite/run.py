"""The run: every trial of a suite, each kept as it ends (docs/design/eval-suite.md §2, §7, §8).

The runner executes the trials of a plan (suite/plan.py) and writes the run directory
(suite/rundir.py) as it goes, so a crash loses at most the trial in flight, and the same run id
resumes it: what is kept or excluded is skipped, and a trial whose last record the
infrastructure broke runs again.

It stops cleanly, keeping everything written, when:
- the run's budget is spent (`budget_exhausted`), checked after keeping each trial: the agent's
  usage is charged without raising (llm/budget.py), so a paid trial is never lost;
- a call finds no funds mid-trial (`FundsExhausted`, `funds_exhausted`): that trial runs again.
  So does a trial the agent's model could not pay for (`model_failure.kind` `quota_exhausted`,
  not injected by the case): no retry pays for it, so it goes to `infra.jsonl` and the run stops;
- the infrastructure broke `INFRA_STREAK` trials in a row, each after its retries
  (`interrupted`): the stack is down, and going on would only spend the plan.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from commerce_evaluation.llm.budget import RunBudget
from commerce_evaluation.llm.transport import FundsExhausted
from commerce_evaluation.suite.executor import RunSettings, SimulatorFailure, TrialExecutor
from commerce_evaluation.suite.plan import Plan, Planned, done, pending
from commerce_evaluation.suite.reduce import is_infra_failure
from commerce_evaluation.suite.rundir import (
    EXCLUDED_FILE,
    INFRA_FILE,
    MANIFEST_FILE,
    TRIALS_FILE,
    append_jsonl,
    case_hash,
    git_state,
    read_manifest,
    repair_jsonl,
    write_cases,
    write_manifest,
)
from commerce_evaluation.suite.trial import TrialRecord

# §2: a trial the infrastructure broke is repeated up to twice more.
INFRA_RETRIES = 2
# A simulated user that answered off-schema or refused gets one more chance, then the trial is
# excluded: the simulator's noise must not be charged to the agent.
SIMULATOR_RETRIES = 1
# Trials in a row the infrastructure broke, retries included, before the run stops.
INFRA_STREAK = 3

Status = Literal["running", "complete", "budget_exhausted", "funds_exhausted", "interrupted"]
Ending = Literal["kept", "infra", "excluded"]
Say = Callable[[str], None]


class RunMismatch(ValueError):
    """The run id names a run made with other settings, cases or code: it cannot be resumed."""


@dataclass
class Outcome:
    status: Status = "running"
    # Trials kept or excluded by earlier sessions of the run, and kept by this one.
    resumed: int = 0
    kept: int = 0
    infra_retries: int = 0
    simulator_retries: int = 0
    excluded: int = 0


def execute(
    plan: Plan,
    executor: TrialExecutor,
    *,
    budget: RunBudget | None = None,
    say: Say = print,
) -> Outcome:
    """Runs every planned trial not yet done in the run directory, keeping each as it ends."""
    outcome = Outcome(resumed=_start(plan))
    left = pending(plan)
    if outcome.resumed:
        say(f"Se retoma la corrida: {outcome.resumed} intentos ya hechos, faltan {len(left)}.")
    outcome.status = "interrupted"
    try:
        outcome.status = _run(plan, left, executor, budget, outcome, say)
    except FundsExhausted:
        outcome.status = "funds_exhausted"
        say("Una llamada no encontró fondos: la corrida se detuvo y lo hecho quedó guardado.")
    finally:
        _finish(plan.settings.run_dir, outcome.status)
    return outcome


def _run(
    plan: Plan,
    left: list[Planned],
    executor: TrialExecutor,
    budget: RunBudget | None,
    outcome: Outcome,
    say: Say,
) -> Status:
    if _spent(budget, say):
        return "budget_exhausted"
    if not left:
        return "complete"
    streak = 0
    with executor:
        for index, planned in enumerate(left, start=1):
            say(f"[{index}/{len(left)}] {planned}")
            ending = _trial(plan.settings, planned, executor, outcome, say)
            streak = streak + 1 if ending == "infra" else 0
            if streak == INFRA_STREAK:
                say(
                    f"La infraestructura falló en {INFRA_STREAK} intentos seguidos, cada uno con "
                    "sus reintentos: la corrida se detuvo. Revisa el stack y retómala con el "
                    "mismo --run-id; esos intentos se repiten."
                )
                return "interrupted"
            if _spent(budget, say):
                return "budget_exhausted"
    return "complete"


def _spent(budget: RunBudget | None, say: Say) -> bool:
    if budget is None or not budget.exhausted:
        return False
    say(f"Se llegó al tope de USD {budget.max_usd}: la corrida se detuvo con lo hecho guardado.")
    return True


def _trial(
    settings: RunSettings,
    planned: Planned,
    executor: TrialExecutor,
    outcome: Outcome,
    say: Say,
) -> Ending:
    """One trial, repeated while the infrastructure or the simulated user is to blame."""
    run_dir, case = settings.run_dir, planned.case
    infra_left, simulator_left, attempt = INFRA_RETRIES, SIMULATOR_RETRIES, 0
    while True:
        attempt += 1
        try:
            record = executor.run(case, planned.governance, planned.trial, planned.seed)
        except SimulatorFailure as failure:
            case_id, governance, trial = planned.key
            row = {"case_id": case_id, "governance": governance, "trial": trial}
            row |= {"seed": planned.seed, "attempt": attempt, "reason": str(failure) or "?"}
            if simulator_left:
                simulator_left -= 1
                append_jsonl(run_dir / INFRA_FILE, _json({"kind": "simulator", **row}))
                outcome.simulator_retries += 1
                say(f"  El usuario simulado falló ({failure}); se repite una vez.")
                continue
            append_jsonl(run_dir / EXCLUDED_FILE, _json(row))
            outcome.excluded += 1
            say("  Excluido: el usuario simulado volvió a fallar. No cuenta contra el agente.")
            return "excluded"
        _check(settings, planned, record)
        broken = record.infra_failure is not None and is_infra_failure(case, record)
        unpaid = broken and _unpaid(record)
        if unpaid or (broken and infra_left):
            discarded = {
                "kind": "infra",
                "attempt": attempt,
                "record": record.model_dump(mode="json"),
            }
            append_jsonl(run_dir / INFRA_FILE, _json(discarded))
            outcome.infra_retries += 1
            if unpaid:
                say("  El modelo del agente se quedó sin saldo (quota_exhausted).")
                raise FundsExhausted("CreditBalanceExhausted", attempts=0)
            infra_left -= 1
            say("  Falla de infraestructura; se repite.")
            continue
        append_jsonl(run_dir / TRIALS_FILE, record.model_dump_json())
        outcome.kept += 1
        return "infra" if broken else "kept"


def _unpaid(record: TrialRecord) -> bool:
    """Whether the agent's model found its balance or spend cap exhausted."""
    return record.model_failure is not None and record.model_failure.kind == "quota_exhausted"


def _check(settings: RunSettings, planned: Planned, record: TrialRecord) -> None:
    got = (record.case_id, record.governance, record.trial, record.seed, record.suite)
    case_id, governance, trial = planned.key
    wanted = (case_id, governance, trial, planned.seed, settings.suite)
    if got != wanted:
        raise ValueError(f"The executor returned the record of {got} for {wanted}")


# The manifest.


def resume_problem(plan: Plan, git: dict[str, Any] | None = None) -> str | None:
    """Why the run directory cannot be resumed with this plan, or None when it can (or is new)."""
    settings = plan.settings
    if not (settings.run_dir / MANIFEST_FILE).exists():
        return None
    manifest, fixed = read_manifest(settings.run_dir), _fixed(plan)
    if changed := sorted(name for name in fixed if manifest.get(name) != fixed[name]):
        return (
            f"La corrida {settings.run_id} ya existe con otro valor de {', '.join(changed)}: "
            "usa otro --run-id."
        )
    first, now = manifest["sessions"][0]["git"], git or git_state()
    if (first["commit"], first["dirty"]) != (now["commit"], now["dirty"]):
        return (
            f"La corrida {settings.run_id} empezó en el commit {first['commit']} "
            f"({_dirty(first)}) y el árbol está en {now['commit']} ({_dirty(now)}): no se "
            "retoma con otro código. Vuelve a ese estado o usa otro --run-id."
        )
    return None


def _dirty(git: dict[str, Any]) -> str:
    return {True: "con cambios sin confirmar", False: "limpio"}.get(git["dirty"], "sin datos")


def _start(plan: Plan) -> int:
    """Writes the manifest, or checks a resumed run is the same run; returns how many trials
    the run already holds."""
    settings, git = plan.settings, git_state()
    if problem := resume_problem(plan, git):
        raise RunMismatch(problem)
    session = {"started_at": _now(), "ended_at": None, "status": "running", "git": git}
    session["max_usd"] = None if plan.max_usd is None else str(plan.max_usd)
    if (settings.run_dir / MANIFEST_FILE).exists():
        manifest = read_manifest(settings.run_dir)
        for name in (TRIALS_FILE, INFRA_FILE, EXCLUDED_FILE):
            repair_jsonl(settings.run_dir / name)
        manifest["sessions"].append(session)
    else:
        settings.run_dir.mkdir(parents=True, exist_ok=True)
        write_cases(settings.run_dir, plan.cases)
        manifest = {**_fixed(plan), "started_at": session["started_at"], "sessions": [session]}
    manifest |= {"ended_at": None, "status": "running", "max_usd": session["max_usd"]}
    write_manifest(settings.run_dir, manifest)
    return len(done(plan))


def _fixed(plan: Plan) -> dict[str, Any]:
    """What a resumed run must share with the run it resumes."""
    settings = plan.settings
    return {
        "run_id": settings.run_id,
        "suite": settings.suite,
        "k": plan.k,
        "seed": plan.seed,
        "agent": {"model": settings.agent_model, "effort": settings.agent_effort},
        "simulator": {"model": settings.simulator_model},
        "cases": {case.id: case_hash(plan.catalog, case) for case in plan.cases},
    }


def _finish(run_dir: Path, status: Status) -> None:
    manifest, ended = read_manifest(run_dir), _now()
    manifest["sessions"][-1] |= {"ended_at": ended, "status": status}
    manifest |= {"ended_at": ended, "status": status}
    write_manifest(run_dir, manifest)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False)
