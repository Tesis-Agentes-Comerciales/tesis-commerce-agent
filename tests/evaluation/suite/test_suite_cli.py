"""`eval-suite`: the guards of a paid run, the dry run and the commands (eval-suite.md §7, §8).

Every test runs a fake executor on a small catalog under tmp_path, with no `.env` of the
developer and no live executor: nothing here can call a model or start the stack.
"""

import json
import os
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation.llm.batch import FakeBatchTransport
from commerce_evaluation.llm.budget import RunBudget
from commerce_evaluation.llm.transport import FakeReply, FakeTransport, FundsExhausted
from commerce_evaluation.suite import cli, console, judging, mapped
from commerce_evaluation.suite import run as runner
from commerce_evaluation.suite.executor import RunSettings
from commerce_evaluation.suite.run import execute
from tests.evaluation.suite.runs import (
    STACK_DOWN,
    Charging,
    FakeExecutor,
    adversarial_case,
    capability_case,
    make_case,
    plan,
    regression_case,
    write_catalog,
)
from tests.evaluation.suite.trials import bought

SECRET = "sk-ant-api03-SOLO-PARA-PRUEBAS-no-es-una-llave-1234567890"
CASES = [capability_case(), adversarial_case(), regression_case()]


class Factory:
    """The live executor's stand-in: it records what it was built with."""

    def __init__(self, script: dict[Any, list[Any]] | None = None):
        self.script = script
        self.built: list[tuple[RunSettings, RunBudget | None]] = []
        self.key_seen: str | None = None

    def __call__(self, settings: RunSettings, budget: RunBudget | None) -> FakeExecutor:
        self.built.append((settings, budget))
        self.key_seen = os.environ.get(cli.KEY)
        return FakeExecutor(settings.suite, self.script)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cli, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(cli, "CATALOG", write_catalog(tmp_path / "cases", CASES))
    monkeypatch.setattr(console, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(mapped, "MAP_FILE", tmp_path / "regression-map.json")
    # Registered first so the key the CLI loads is removed after the test.
    monkeypatch.setenv(cli.KEY, "x")
    monkeypatch.delenv(cli.KEY)
    for switch in mapped.LIVE_MODEL_SWITCHES:
        monkeypatch.delenv(switch, raising=False)
    return tmp_path


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> Factory:
    built = Factory()
    monkeypatch.setattr(cli, "load_executor", lambda: built)
    return built


def never(*_: Any, **__: Any) -> Any:
    raise AssertionError("A dry run or a refused run must not get this far")


def code_of(argv: Sequence[str]) -> int:
    """The exit code, also when argparse refuses the arguments."""
    try:
        return cli.main(argv)
    except SystemExit as stop:
        return int(stop.code or 0)


def everything_written(root: Path) -> str:
    files = [path for path in root.rglob("*") if path.is_file() and path.name != ".env"]
    return "".join(path.read_text("utf-8") for path in files)


@pytest.mark.parametrize("suite", ["capability", "adversarial"])
@pytest.mark.parametrize("cap", [None, "0", "-5", "inf", "nan"])
def test_a_paid_run_refuses_to_start_without_a_finite_positive_cap(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    suite: str,
    cap: str | None,
) -> None:
    monkeypatch.setenv(cli.KEY, SECRET)
    monkeypatch.setattr(cli, "load_executor", never)

    code = code_of(["run", "--suite", suite, *(["--max-usd", cap] if cap else [])])

    assert code == 2
    assert "--max-usd" in capsys.readouterr().err
    assert not (home / "runs").exists()


def test_a_paid_run_refuses_to_start_without_the_key(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_executor", never)
    (home / ".env").write_text("OTRA_VARIABLE=1\n")

    code = cli.main(["run", "--suite", "capability", "--max-usd", "5"])

    assert code == 2
    assert f"Falta {cli.KEY}" in capsys.readouterr().err
    assert not (home / "runs").exists()


def test_a_paid_run_loads_only_the_key_from_env_and_never_shows_it(
    home: Path,
    factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (home / ".env").write_text(f"{cli.KEY}={SECRET}\nPAYMENT_AP2_REQUIRED=true\n")
    monkeypatch.delenv("PAYMENT_AP2_REQUIRED", raising=False)

    code = cli.main(["run", "--suite", "capability", "--max-usd", "5", "--run-id", "pagada"])

    out = capsys.readouterr()
    assert code == 0
    assert factory.key_seen == SECRET and "PAYMENT_AP2_REQUIRED" not in os.environ
    [(settings, budget)] = factory.built
    assert (settings.agent_model, settings.simulator_model) == (
        "claude-haiku-5-5",
        "claude-sonnet-5-5",
    )
    assert budget is not None and budget.max_usd == 5
    assert SECRET not in out.out + out.err
    assert SECRET not in everything_written(home)
    assert (home / "runs" / "pagada" / "report.md").exists()


def test_a_sonnet_agent_gets_opus_as_its_simulated_user(
    home: Path, factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cli.KEY, SECRET)

    cli.main(["run", "--suite", "adversarial", "--agent", "claude-sonnet-5-5", "--max-usd", "5"])

    [(settings, _)] = factory.built
    assert settings.simulator_model == "claude-opus-5-5"


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (["--suite", "capability", "--agent", "gpt-5-mini"], "Sin precio para gpt-5-mini"),
        (["--suite", "capability", "--simulator", "claude-haiku-5-5"], "mismo modelo"),
        (["--suite", "regression", "--agent", "claude-haiku-5-5"], "guionada"),
        (["--suite", "capability", "--cases", "nada-*"], "Ningún caso"),
        (["--suite", "capability", "--run-id", "../fuera"], "--run-id"),
    ],
    ids=["unpriced", "same-model", "scripted-regression", "no-cases", "run-id"],
)
def test_a_run_refuses_what_it_cannot_honour(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    error: str,
) -> None:
    monkeypatch.setattr(cli, "load_executor", never)

    assert cli.main(["run", *arguments, "--max-usd", "5", "--dry-run"]) == 2
    assert error in capsys.readouterr().err


def test_a_dry_run_prints_the_plan_and_calls_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_executor", never)
    monkeypatch.setattr(mapped, "run_mapped", never)
    monkeypatch.setattr(cli, "RunBudget", never)
    (home / ".env").write_text(f"{cli.KEY}={SECRET}\n")

    code = cli.main(["run", "--suite", "adversarial", "--dry-run", "--run-id", "plan"])

    out = capsys.readouterr().out
    assert code == 0
    assert "no se corre nada" in out
    assert "Casos: 1. Intentos: 10, de ellos 10 por correr." in out
    assert "B0 y B2, 5 intentos en cada una" in out
    # Ten presente trials. Typical: Haiku routes 3 times (USD 0.0000698 each) and selects once
    # (0.0002055); Sonnet answers twice (0.006 each). Worst: 6 uncached routes and selections
    # with 4 times their output (0.0059166 in all) and 5 Sonnet answers of 2,000 tokens (0.028).
    assert "típico USD 0,12, peor caso USD 1,46." in out
    assert "Tope: falta --max-usd" in out
    assert f"{cli.KEY}: en .env." in out
    assert SECRET not in out
    assert cli.KEY not in os.environ
    assert not (home / "runs").exists()


def test_a_spent_budget_says_how_to_resume(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(cli.KEY, SECRET)
    spent = Factory({(CASES[0].id, "B2", 2): [FundsExhausted("BudgetExhausted")]})
    monkeypatch.setattr(cli, "load_executor", lambda: spent)

    code = cli.main(["run", "--suite", "capability", "--max-usd", "1", "--run-id", "corta"])

    err = capsys.readouterr().err
    assert code == 1
    assert "no encontró fondos" in err
    assert "--run-id corta" in err and "--max-usd <tope>" in err
    assert (home / "runs" / "corta" / "report.md").exists()


def test_a_run_that_reaches_its_cap_stops_and_says_how_to_resume(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(cli.KEY, SECRET)
    built: list[Charging] = []

    def charging(settings: RunSettings, budget: RunBudget | None) -> Charging:
        assert budget is not None
        built.append(Charging(budget, settings.suite))
        return built[-1]

    monkeypatch.setattr(cli, "load_executor", lambda: charging)
    argv = ["run", "--suite", "capability", "--max-usd", "0.015", "--run-id", "tope"]

    assert cli.main(argv) == 1

    err = capsys.readouterr().err
    assert "llegó a su tope de gasto. Ya van USD 0,02." in err and "--run-id tope" in err
    assert len(built[0].calls) == 2
    manifest = json.loads((home / "runs" / "tope" / "manifest.json").read_text())
    assert manifest["status"] == "budget_exhausted"


def test_a_truncated_ledger_line_is_cut_before_the_budget_reads_it(
    home: Path, factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cli.KEY, SECRET)
    argv = ["run", "--suite", "capability", "--max-usd", "5", "--run-id", "cortada", "--k", "2"]
    run_dir = home / "runs" / "cortada"
    run_dir.mkdir(parents=True)
    (run_dir / "llm-usage.jsonl").write_text('{"at": "2026-11-13T00:00:00+00:00", "rol')

    assert cli.main(argv) == 0
    assert (run_dir / "llm-usage.jsonl").read_text() == ""


def test_without_the_live_executor_a_run_says_so(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "EXECUTOR_MODULE", "commerce_evaluation.suite.no_such_executor")

    assert cli.main(["run", "--suite", "regression"]) == 2
    assert "no_such_executor" in capsys.readouterr().err


def test_a_plan_with_events_the_executor_does_not_apply_is_refused_before_it_starts(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Executor:
        """Refuses the model fault: neither built nor entered."""

        @staticmethod
        def unsupported_events(cases: Any) -> list[str]:
            return [f"{case.id}: model_quota_exhausted@on_interpretation" for case in cases]

        def __init__(self, *_: Any) -> None:
            raise AssertionError("A refused plan must not build the executor")

    monkeypatch.setattr(cli, "load_executor", lambda: Executor)

    assert cli.main(["run", "--suite", "regression", "--run-id", "sin-eventos"]) == 2
    assert "llm-sin-saldo/presente: model_quota_exhausted" in capsys.readouterr().err


def test_regression_skips_the_map_when_it_is_missing(
    home: Path, factory: Factory, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["run", "--suite", "regression", "--run-id", "sin-mapa"])

    assert code == 0
    assert "se omiten las pruebas del mapa" in capsys.readouterr().out
    [(settings, budget)] = factory.built
    assert (settings.agent_model, settings.simulator_model, budget) == (None, None, None)
    assert "Mapa de regresión" not in (home / "runs" / "sin-mapa" / "report.md").read_text()


def test_regression_runs_the_mapped_tests_without_a_live_model(
    home: Path, factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapped.MAP_FILE.write_text(
        '{"llm-sin-saldo/delegado": {"source": "INF28", "tests": ["t.py::ok"], "why": "Cubre."}}'
    )
    monkeypatch.setenv("RUN_LIVE_LLM", "1")
    calls: list[dict[str, Any]] = []

    def runner(command: list[str], **options: Any) -> None:
        calls.append({"command": command, **options})
        Path(options["env"][mapped.OUTCOMES_ENV]).write_text(
            '{"id": "t.py::ok", "outcome": "passed"}\n'
        )

    original = mapped.run_mapped
    monkeypatch.setattr(
        mapped, "run_mapped", lambda entries, run_dir: original(entries, run_dir, runner=runner)
    )

    assert cli.main(["run", "--suite", "regression", "--run-id", "con-mapa"]) == 0

    [call] = calls
    assert (
        call["command"][-1] == "t.py::ok" and "commerce_evaluation.suite.mapped" in call["command"]
    )
    assert call["env"]["RUN_LIVE_STACK"] == "1"
    assert not set(mapped.LIVE_MODEL_SWITCHES) & set(call["env"])
    text = (home / "runs" / "con-mapa" / "report.md").read_text()
    assert "| llm-sin-saldo/delegado | INF28 | cubierta | 1 | 1 pasa |" in text


def test_report_rewrites_the_report_of_a_run_by_id_or_directory(
    home: Path, factory: Factory, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["run", "--suite", "regression", "--run-id", "hecha"])
    report = home / "runs" / "hecha" / "report.md"
    report.unlink()

    assert cli.main(["report", "hecha"]) == 0
    assert report.exists()
    assert cli.main(["report", str(home / "runs" / "hecha")]) == 0
    assert cli.main(["report", "no-existe"]) == 2
    assert "no es una corrida" in capsys.readouterr().err


def test_report_prints_how_many_attack_trials_the_injection_reached(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    attack = adversarial_case()
    script = {(attack.id, "B2", 1): [{"delivered": False}]}
    monkeypatch.setattr(cli, "load_executor", lambda: Factory(script))
    cli.main(["run", "--suite", "regression", "--run-id", "inyectada"])
    capsys.readouterr()

    assert cli.main(["report", "inyectada"]) == 0

    out = capsys.readouterr().out
    assert "Inyección entregada al modelo:" in out
    assert f"| {attack.id} | B0 | 1 de 1 | model.choose_candidate (entrada) |" in out
    assert f"| {attack.id} | B2 | 0 de 1 | ninguna |" in out
    assert "1 intentos de ataque no probaron nada" in out


def test_a_run_id_with_other_settings_is_refused(
    home: Path, factory: Factory, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["run", "--suite", "regression", "--run-id", "una"])

    assert cli.main(["run", "--suite", "regression", "--run-id", "una", "--k", "2"]) == 2
    assert "ya existe con otro valor de k" in capsys.readouterr().err


@pytest.mark.parametrize("command", [["run", "--suite", "capability"], ["judge", "hecha"]])
def test_a_cap_that_is_not_a_finite_positive_amount_is_refused_even_in_a_dry_run(
    home: Path, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    for cap in ("nan", "inf", "0", "-1", "diez"):
        assert code_of([*command, "--max-usd", cap, "--dry-run"]) == 2
        assert "--max-usd" in capsys.readouterr().err


def test_a_dry_run_says_whether_the_run_can_be_resumed(
    home: Path,
    factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(runner, "git_state", lambda: {"commit": "a" * 40, "dirty": False})
    cli.main(["run", "--suite", "regression", "--run-id", "vieja", "--k", "1"])
    capsys.readouterr()

    cli.main(["run", "--suite", "regression", "--run-id", "vieja", "--dry-run"])
    assert "(existe: se retoma)" in capsys.readouterr().out

    monkeypatch.setattr(runner, "git_state", lambda: {"commit": "b" * 40, "dirty": False})
    cli.main(["run", "--suite", "regression", "--run-id", "vieja", "--dry-run"])
    assert "no se puede retomar. La corrida vieja empezó en el commit" in capsys.readouterr().out
    assert cli.main(["run", "--suite", "regression", "--run-id", "vieja"]) == 2
    assert "no se retoma con otro código" in capsys.readouterr().err


def test_a_regression_trial_that_does_not_pass_fails_the_command(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = Factory({(CASES[2].id, "B2", 1): [{"state": bought()}]})
    monkeypatch.setattr(cli, "load_executor", lambda: broken)

    assert cli.main(["run", "--suite", "regression", "--run-id", "rota", "--cases", "llm-*"]) == 1
    assert "La regresión no pasa: 1 tareas con un intento que falla" in capsys.readouterr().err


def test_an_undecided_trial_does_not_fail_the_regression_and_waits_for_the_judge(
    home: Path,
    factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    told = make_case("entrega-no-verificable", must_communicate=["que no sabe la fecha"])
    monkeypatch.setattr(cli, "CATALOG", write_catalog(home / "con-criterio", [told]))

    assert cli.main(["run", "--suite", "regression", "--run-id", "sin-juez"]) == 0

    out = capsys.readouterr().out
    assert "1 intentos quedan sin decidir: 1 solo esperan al juez en lo que dice y 0 " in out
    assert "La regresión pasa." in out
    text = (home / "runs" / "sin-juez" / "report.md").read_text()
    assert "los 1 que solo esperan al juez en lo que dice no hacen fallar la corrida" in text
    assert "En regresión cuentan como no acierto los intentos que solo esperan al juez" in text


@pytest.mark.parametrize(
    ("mode", "final"),
    [
        # The chat answered a bare 500: the record has no action (outcome.py).
        ("presente", {"action": None}),
        # The task stopped short of an order: the outcome cannot be told.
        ("delegado", {"task_state": "purchased"}),
    ],
)
def test_a_trial_another_grader_left_undecided_fails_the_regression(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    final: dict[str, Any],
) -> None:
    told = make_case("tienda-no-responde", mode, must_communicate=["que la tienda no responde"])
    monkeypatch.setattr(cli, "CATALOG", write_catalog(home / "sin-accion", [told]))
    script = {(told.id, "B2", 1): [{"final": final}]}
    monkeypatch.setattr(cli, "load_executor", lambda: Factory(script))

    assert cli.main(["run", "--suite", "regression", "--run-id", f"sin-accion-{mode}"]) == 1

    captured = capsys.readouterr()
    assert "0 solo esperan al juez en lo que dice y 1 los dejó sin decidir otro" in captured.out
    assert "1 tareas con un intento que falla, que el código no pudo decidir" in captured.err


def test_a_trial_the_infrastructure_left_ungraded_fails_the_regression(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    down = Factory({(CASES[2].id, "B2", 1): [{"infra_failure": STACK_DOWN}]})
    monkeypatch.setattr(cli, "load_executor", lambda: down)

    assert cli.main(["run", "--suite", "regression", "--run-id", "caida", "--cases", "llm-*"]) == 1
    assert "que la infraestructura no dejó calificar" in capsys.readouterr().err


@pytest.mark.parametrize(("outcome", "code"), [("passed", 0), ("failed", 1), ("not_run", 1)])
def test_a_mapped_test_that_does_not_pass_fails_the_regression(
    home: Path, factory: Factory, monkeypatch: pytest.MonkeyPatch, outcome: str, code: int
) -> None:
    mapped.MAP_FILE.write_text(
        '{"llm-sin-saldo/delegado": {"source": "INF28", "tests": ["t.py::ok"], "why": "Cubre."}}'
    )

    def runner(command: list[str], **options: Any) -> None:
        if outcome != "not_run":
            line = json.dumps({"id": "t.py::ok", "outcome": outcome})
            Path(options["env"][mapped.OUTCOMES_ENV]).write_text(line + "\n")

    original = mapped.run_mapped
    monkeypatch.setattr(
        mapped, "run_mapped", lambda entries, run_dir: original(entries, run_dir, runner=runner)
    )

    assert cli.main(["run", "--suite", "regression", "--run-id", f"mapa-{outcome}"]) == code


def test_a_browser_test_without_npm_ci_fails_the_regression_and_says_why(
    home: Path,
    factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(mapped.shutil, "which", lambda name: f"/bin/{name}")
    browser = "tests/integration/test_live_browser_journeys.py::test_revoke"
    entry = {"source": "MA24", "tests": [browser], "why": "Cubre."}
    mapped.MAP_FILE.write_text(json.dumps({"revoca-antes-de-ejecutar/delegado": entry}))
    original = mapped.run_mapped
    monkeypatch.setattr(
        mapped,
        "run_mapped",
        lambda entries, run_dir: original(entries, run_dir, runner=never, cwd=home),
    )

    assert cli.main(["run", "--suite", "regression", "--run-id", "sin-npm"]) == 1

    assert "falta npm ci en auth/ y ui/" in capsys.readouterr().err
    text = (home / "runs" / "sin-npm" / "report.md").read_text()
    assert "| 1 | 1 no corrió: falta npm ci en auth/ y ui/ |" in text


UNSURE = make_case("sin-stock", must_communicate=["que no hay stock"])


def judgeable(home: Path, run_id: str = "juzgable") -> Path:
    """A finished run with one question for the judge."""
    catalog = write_catalog(home / "otros", [UNSURE])
    done = plan(home / "runs" / run_id, catalog, [UNSURE])
    script = {(UNSURE.id, "B2", 1): [{"said": ("No hay stock.",)}]}
    execute(done, FakeExecutor(script=script), say=lambda _: None)
    return done.settings.run_dir


def fake_judge(monkeypatch: pytest.MonkeyPatch, reply: FakeReply) -> FakeTransport:
    fake = FakeTransport(lambda _: reply)
    monkeypatch.setattr(judging, "batch_client", lambda: FakeBatchTransport(fake))
    return fake


PASS = FakeReply(output={"verdict": "pass", "quote": "No hay stock.", "reason": "Lo dice."})


def test_judge_dry_run_shows_the_questions_and_the_reservation_and_writes_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = judgeable(home)
    before = sorted(run_dir.rglob("*"))
    monkeypatch.setattr(judging, "batch_client", never)

    assert cli.main(["judge", "juzgable", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "0 intentos ya juzgados, 1 por juzgar con 1 preguntas." in out
    assert "peor caso USD 0,0" in out and "Tope: falta --max-usd" in out
    assert f"{cli.KEY}: falta" in out

    assert cli.main(["judge", str(run_dir), "--dry-run", "--max-usd", "0.01"]) == 0
    assert "no cabe, y el lote entero se rechazaría" in capsys.readouterr().out
    assert cli.main(["judge", "juzgable", "--dry-run", "--max-usd", "1"]) == 0
    assert "el peor caso cabe" in capsys.readouterr().out
    assert sorted(run_dir.rglob("*")) == before


def test_judge_refuses_to_call_without_its_cap_or_the_key(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    judgeable(home)
    monkeypatch.setattr(judging, "batch_client", never)

    assert cli.main(["judge", "juzgable"]) == 2
    assert "exige --max-usd" in capsys.readouterr().err
    assert cli.main(["judge", "juzgable", "--max-usd", "1"]) == 2
    assert f"Falta {cli.KEY}" in capsys.readouterr().err
    assert cli.main(["judge", "no-existe", "--max-usd", "1"]) == 2


def test_judge_asks_once_rewrites_the_report_and_never_shows_the_key(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = judgeable(home)
    (home / ".env").write_text(f"{cli.KEY}={SECRET}\n")
    fake = fake_judge(monkeypatch, PASS)

    assert cli.main(["judge", "juzgable", "--max-usd", "1"]) == 0

    out = capsys.readouterr()
    assert len(fake.requests) == 1 and os.environ[cli.KEY] == SECRET
    assert "Respuestas nuevas: 1 cumple, 0 no cumple y 0 sin decidir" in out.out
    report = json.loads((run_dir / "report.json").read_text())
    assert report["judged"] and report["review"] == []
    assert [role["role"] for role in report["cost"]] == ["judge"]
    assert SECRET not in out.out + out.err and SECRET not in everything_written(home)

    monkeypatch.setattr(judging, "batch_client", never)
    assert cli.main(["judge", "juzgable", "--max-usd", "1"]) == 0
    assert "1 intentos ya juzgados, 0 por juzgar" in capsys.readouterr().out


def test_judge_without_funds_keeps_what_was_paid_and_says_how_to_go_on(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = judgeable(home)
    monkeypatch.setenv(cli.KEY, SECRET)
    fake = fake_judge(monkeypatch, PASS)

    assert cli.main(["judge", "juzgable", "--max-usd", "0.01"]) == 1

    assert fake.requests == []
    assert "El juez no encontró fondos" in capsys.readouterr().err
    assert (run_dir / "report.md").exists() and not (run_dir / "judge.jsonl").exists()
    assert judging.pending(run_dir).spent_usd == Decimal(0)


def test_calibrate_hands_its_arguments_to_the_calibration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(cli.calibration, "main", lambda argv: seen.append(argv) or 0)

    assert cli.main(["calibrate", "export", "corrida", "--trials", "3"]) == 0
    assert cli.main(["calibrate", "--help"]) == 0
    assert seen == [["export", "corrida", "--trials", "3"], ["--help"]]
    assert code_of(["--help"]) == 0
    assert "calibra el juez" in capsys.readouterr().out
