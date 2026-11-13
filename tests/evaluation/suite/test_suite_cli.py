"""`eval-suite`: the guards of a paid run, the dry run and the commands (eval-suite.md §7, §8).

Every test runs a fake executor on a small catalog under tmp_path, with no `.env` of the
developer and no live executor: nothing here can call a model or start the stack.
"""

import os
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation.llm.budget import RunBudget
from commerce_evaluation.llm.transport import FundsExhausted
from commerce_evaluation.suite import cli, mapped
from commerce_evaluation.suite.executor import RunSettings
from tests.evaluation.suite.runs import (
    FakeExecutor,
    adversarial_case,
    capability_case,
    regression_case,
    write_catalog,
)

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
    monkeypatch.setattr(cli, "ENV_FILE", tmp_path / ".env")
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

    code = cli.main(["run", "--suite", suite, *(["--max-usd", cap] if cap else [])])

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
    assert "--run-id corta" in err and "--max-usd <tope mayor>" in err
    assert (home / "runs" / "corta" / "report.md").exists()


def test_without_the_live_executor_a_run_says_so(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "EXECUTOR_MODULE", "commerce_evaluation.suite.no_such_executor")

    assert cli.main(["run", "--suite", "regression"]) == 2
    assert "no_such_executor" in capsys.readouterr().err


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


def test_a_run_id_with_other_settings_is_refused(
    home: Path, factory: Factory, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["run", "--suite", "regression", "--run-id", "una"])

    assert cli.main(["run", "--suite", "regression", "--run-id", "una", "--k", "2"]) == 2
    assert "ya existe con otro valor de k" in capsys.readouterr().err
