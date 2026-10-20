"""demo-stack builds each mode's environment and never shows a secret. No service starts here."""

import io
import json
import os
from argparse import Namespace
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from commerce_agent.config import Settings
from commerce_agent.governance.bootstrap import build_enforce_gate, configured_d1
from commerce_agent.governance.evaluator import verify_artifact
from commerce_agent.governance.provenance import merchant_pseudonym
from commerce_evaluation import demo_stack
from commerce_evaluation.demo_stack import (
    OFFER_ID,
    OFFER_PRICE_MINOR,
    OFFER_STOCK,
    OTHER_MERCHANT,
    S04_PRICE_MINOR,
    DemoError,
    DemoPlan,
    Launcher,
    Ports,
    Service,
    StackSecrets,
    base_environment,
    build_plan,
    describe,
    read_model_settings,
    s04,
)
from commerce_evaluation.paths import DATA, LOCAL

MERCHANT_TOKEN = "merchant-session-" + "t" * 32
MODEL_KEY = "sk-test-" + "k" * 40
# Shell variables a presenter may have exported; none of them may reach a service.
SHELL = {
    "PATH": "/usr/bin",
    "OPENAI_API_KEY": "sk-shell-key-that-must-not-leak",
    "AGENT_GOVERNANCE_MODE": "off",
    "MERCHANT_BEARER_TOKEN": "shell-token",
    "DATABASE_URL": "postgresql://shell",
    "VIRTUAL_ENV": "/somewhere/.venv",
}


def plan_for(mode: str, tmp_path: Path) -> DemoPlan:
    return DemoPlan(
        mode=demo_stack.MODES[mode],
        ports=Ports(merchant=5120, payment=5130, agent=45125, auth=5215, ui=5174),
        run_dir=tmp_path / "run",
        merchant_repo=tmp_path / "merchant",
        payment_repo=tmp_path / "payment",
        env_file=tmp_path / ".env",
        secrets=StackSecrets.generate(governed=mode != "b0"),
        model={"OPENAI_API_KEY": MODEL_KEY, "OPENAI_MODEL": "gpt-5-mini"},
        base=base_environment(SHELL),
    )


def agent_settings(plan: DemoPlan, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """The Settings the agent process would build from exactly this environment."""
    for name in list(os.environ):
        monkeypatch.delenv(name)
    for name, value in plan.agent_env(MERCHANT_TOKEN).items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def allowed_refs(settings: Settings) -> tuple[str, bool]:
    """POL-02's allowlist as the agent builds it, and whether it contains the demo merchant."""
    configured = configured_d1(settings, None)
    assert configured.policy_binding is not None
    policy = verify_artifact(configured.policy_bytes, configured.policy_binding)
    merchant = merchant_pseudonym(configured.keys, settings.merchant_base_url)
    return settings.agent_d1_allowed_merchant_origins or "", merchant in (
        policy.allowed_merchant_refs
    )


def test_b2_runs_enforce_and_governed_with_d1_keys_and_the_merchant_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_for("b2", tmp_path)

    settings = agent_settings(plan, monkeypatch)

    assert settings.agent_governance_mode == "enforce"
    assert settings.agent_hp_flow == "governed"
    assert settings.agent_d1_hmac_key_hex is not None
    assert settings.agent_d1_aes_key_hex is not None
    assert allowed_refs(settings) == ("http://127.0.0.1:5120", True)
    assert settings.agent_ap2_required and settings.agent_auth_mode == "jwt"
    assert settings.agent_auth_issuer == settings.agent_ui_origin == "http://127.0.0.1:5174"
    assert settings.agent_database_path == tmp_path / "run" / "agent.db"
    build_enforce_gate(settings, None)


def test_pol02_deny_keeps_b2_but_its_allowlist_excludes_the_demo_merchant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_for("pol02-deny", tmp_path)

    settings = agent_settings(plan, monkeypatch)

    assert (settings.agent_governance_mode, settings.agent_hp_flow) == ("enforce", "governed")
    assert settings.merchant_base_url == "http://127.0.0.1:5120"
    assert allowed_refs(settings) == (OTHER_MERCHANT, False)
    build_enforce_gate(settings, None)


def test_b0_turns_governance_off_without_d1_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_for("b0", tmp_path)

    settings = agent_settings(plan, monkeypatch)

    assert (settings.agent_governance_mode, settings.agent_hp_flow) == ("off", "blocked")
    assert plan.secrets.d1 is None
    assert not [name for name in plan.agent_env(MERCHANT_TOKEN) if name.startswith("AGENT_D1_")]


@pytest.mark.parametrize("mode", sorted(demo_stack.MODES))
def test_every_agent_variable_is_an_agent_setting(mode: str, tmp_path: Path) -> None:
    plan = plan_for(mode, tmp_path)
    delivered = set(plan.agent_env(MERCHANT_TOKEN)) - set(plan.base)

    assert sorted(name for name in delivered if name.lower() not in Settings.model_fields) == []


def test_each_secret_reaches_only_the_services_that_need_it(tmp_path: Path) -> None:
    plan = plan_for("b2", tmp_path)
    keys = plan.secrets
    assert keys.d1 is not None
    environments = {
        service.name: set(service.env.values()) for service in plan.services(MERCHANT_TOKEN)
    }
    owners = {
        keys.d1.hmac_hex: {"agent"},
        keys.d1.aes_hex: {"agent"},
        MODEL_KEY: {"agent"},
        MERCHANT_TOKEN: {"agent"},
        keys.approval: {"agent"},
        keys.agent_ap2_pem: {"agent"},
        keys.merchant_webhook: {"agent", "merchant"},
        keys.payment_agent_token: {"agent", "payment"},
        keys.payment_merchant_token: {"merchant", "payment"},
        keys.payment_token_secret: {"payment"},
        keys.payment_ap2_pem: {"payment"},
        keys.merchant_ap2_pem: {"merchant"},
        keys.auth_secret: {"auth"},
    }

    for secret, expected in owners.items():
        assert {name for name, values in environments.items() if secret in values} == expected
    # The presenter's shell never reaches a service: no stray key, token, DSN or virtualenv.
    for values in environments.values():
        assert not {"sk-shell-key-that-must-not-leak", "shell-token", "postgresql://shell"} & values
    assert all("VIRTUAL_ENV" not in service.env for service in plan.services(MERCHANT_TOKEN))


def test_only_model_settings_are_read_from_the_agent_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# local\n"
        f'OPENAI_API_KEY="{MODEL_KEY}"\n'
        "OPENAI_MODEL=gpt-5-mini\n"
        "OPENAI_REASONING_EFFORT=\n"
        "AGENT_GOVERNANCE_MODE=off\n"
        "MERCHANT_BEARER_TOKEN=stale\n"
        "AGENT_D1_POLICY_PATH=/tmp/policy.json\n",
        encoding="utf-8",
    )

    assert read_model_settings(env_file) == {
        "OPENAI_API_KEY": MODEL_KEY,
        "OPENAI_MODEL": "gpt-5-mini",
    }
    assert read_model_settings(tmp_path / "missing.env") == {}


def test_each_launch_gets_its_own_directory_and_agent_database(tmp_path: Path) -> None:
    arguments = Namespace(
        mode="b2",
        merchant_port=4120,
        payment_port=4130,
        agent_port=43120,
        auth_port=4210,
        ui_port=5173,
        merchant_repo=tmp_path,
        payment_repo=tmp_path,
        env_file=tmp_path / ".env",
        skip_postgres_start=True,
        keep_offer=False,
    )

    first = build_plan(arguments, now=datetime(2026, 10, 23, 14, 0, 0, tzinfo=UTC))
    second = build_plan(arguments, now=datetime(2026, 10, 23, 14, 9, 0, tzinfo=UTC))

    assert first.run_dir == LOCAL / "demo" / "20261023T140000Z"
    assert first.run_dir != second.run_dir
    assert first.agent_env("x")["AGENT_DATABASE_PATH"] == str(first.run_dir / "agent.db")
    assert first.secrets.values() != second.secrets.values()
    assert [label for label, _ in first.setup_steps()] == ["Migraciones del comercio"]


def test_scripted_offer_and_s04_price_match_the_pilot_scenarios() -> None:
    scenarios = {
        item["id"]: item
        for item in json.loads((DATA / "pilot-scenarios.json").read_text("utf-8"))["scenarios"]
    }
    s01, s04_setup = scenarios["S01"]["catalog_setup"], scenarios["S04"]["catalog_setup"]
    shipping = s01["target_shipping_minor"]

    assert s01["target_variant_id"] == s04_setup["target_variant_id"] == OFFER_ID
    assert s01["target_item_price_minor"] == s04_setup["initial_item_price_minor"]
    assert s01["target_item_price_minor"] == OFFER_PRICE_MINOR
    # The guion asks S04 with a 1.700.000 COP budget: the new price must still fit it, so asking
    # again after canceling reaches a new checkout and a new D1 decision.
    assert OFFER_PRICE_MINOR < S04_PRICE_MINOR
    assert S04_PRICE_MINOR + shipping <= 170_000_000


class FakeProcess:
    _next = 9000

    def __init__(self, exit_code: int | None = None) -> None:
        FakeProcess._next += 1
        self.pid = FakeProcess._next
        self.code = exit_code
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.code

    def wait(self, timeout: float | None = None) -> int:
        self.code = -15 if self.code is None else self.code
        return self.code


class FakeLauncher(Launcher):
    """Merchant CLI, services and HTTP replaced; everything else is the real launcher."""

    def __init__(self, plan: DemoPlan, *, health: dict[str, str], failing: str | None = None):
        self.calls: list[tuple[str, ...]] = []
        self.spawned: dict[str, FakeProcess] = {}
        self.fake_health = health
        self.failing = failing
        super().__init__(plan, io.StringIO())

    def _run(
        self, command: Sequence[str], cwd: Path, env: Mapping[str, str]
    ) -> tuple[int, str, str]:
        self.calls.append(tuple(command))
        # Merchant steps that echo secrets on stderr: the launcher must not repeat them.
        stderr = f"warning {self.plan.secrets.merchant_webhook}"
        if self.failing is not None and self.failing in command:
            return 1, "", stderr
        if "import-current" in command:
            return 0, json.dumps({"status": "already_imported", "run_id": "run-1"}), stderr
        if "update-offer" in command:
            offer = {"offer_id": OFFER_ID, "revision": 7, "price_minor": 154_000_000, "stock": 13}
            return 0, json.dumps(offer), stderr
        if "issue-session" in command:
            return 0, json.dumps({"session_token": MERCHANT_TOKEN}), f"{stderr} {MERCHANT_TOKEN}"
        return 0, "", stderr

    def _spawn(self, service: Service) -> FakeProcess:
        process = FakeProcess()
        self.spawned[service.name] = process
        return process

    def _probe(self, url: str) -> tuple[int, Any]:
        if url.endswith(":45125/health"):
            return 200, {"status": "ok", **self.fake_health}
        return 200, {"status": "ok"}

    def _signal(self, process: Any, signum: int) -> None:
        process.signals.append(signum)


def shown(launcher: FakeLauncher) -> str:
    """Everything the launcher printed or wrote itself."""
    run_dir = launcher.plan.run_dir
    assert isinstance(launcher.out, io.StringIO)
    return "\n".join(
        [launcher.out.getvalue()]
        + [(run_dir / name).read_text("utf-8") for name in ("launcher.log", "setup.log")]
    )


def assert_no_secret(text: str, plan: DemoPlan) -> None:
    for secret in plan.secret_values(MERCHANT_TOKEN):
        assert secret not in text
        for line in secret.splitlines():
            if len(line) >= 16 and not line.startswith("-----"):
                assert line not in text


def test_a_full_launch_prints_urls_and_mode_but_never_a_secret(tmp_path: Path) -> None:
    plan = plan_for("b2", tmp_path)
    launcher = FakeLauncher(plan, health={"governance_mode": "enforce", "hp_flow": "governed"})

    launcher.start()
    launcher.stop()
    launcher.close()

    output = shown(launcher)
    assert_no_secret(output, plan)
    assert "governance_mode=enforce · hp_flow=governed" in output
    assert "Comercios permitidos por POL-02: http://127.0.0.1:5120" in output
    assert "UI:    http://127.0.0.1:5174" in output and "Auth:  http://127.0.0.1:5215" in output
    assert "[redactado]" in (plan.run_dir / "setup.log").read_text("utf-8")
    assert list(launcher.spawned) == ["payment", "merchant", "auth", "agent", "ui"]
    assert all(process.signals for process in launcher.spawned.values())
    assert not any(call[:2] == ("docker", "compose") and "down" in call for call in launcher.calls)
    assert launcher.calls[0] == ("docker", "compose", "up", "-d", "--wait", "postgres")
    assert (*demo_stack.update_offer_command(OFFER_PRICE_MINOR, stock=OFFER_STOCK),) in (
        launcher.calls
    )


def test_a_failing_step_is_reported_without_its_secrets(tmp_path: Path) -> None:
    plan = plan_for("pol02-deny", tmp_path)
    launcher = FakeLauncher(plan, health={}, failing="migrate")

    with pytest.raises(DemoError) as raised:
        launcher.start()
    launcher.say(f"La demo se detuvo: {raised.value}")
    launcher.close()

    assert "Migraciones del comercio falló" in str(raised.value)
    assert_no_secret(shown(launcher), plan)
    assert launcher.spawned == {}


def test_the_launch_stops_if_the_agent_reports_another_condition(tmp_path: Path) -> None:
    plan = plan_for("b2", tmp_path)
    launcher = FakeLauncher(plan, health={"governance_mode": "off", "hp_flow": "blocked"})

    with pytest.raises(DemoError, match="requires"):
        launcher.start()
    launcher.stop()
    launcher.close()

    assert "ui" not in launcher.spawned
    assert launcher.spawned["agent"].signals


def test_dry_run_names_variables_but_shows_no_value(tmp_path: Path) -> None:
    plan = plan_for("b2", tmp_path)
    out = io.StringIO()

    describe(plan, ["el puerto 5120 (comercio) está ocupado"], out)

    text = out.getvalue()
    assert_no_secret(text, plan)
    assert "AGENT_D1_HMAC_KEY_HEX=[redactado]" in text
    assert "AGENT_GOVERNANCE_MODE=enforce" in text
    assert "✗ el puerto 5120 (comercio) está ocupado" in text


@pytest.mark.parametrize(
    ("arguments", "price", "stock", "shown_price"),
    [
        ([], S04_PRICE_MINOR, None, "COP 1.620.000"),
        (["--restore"], OFFER_PRICE_MINOR, OFFER_STOCK, "COP 1.540.000"),
    ],
    ids=["change", "restore"],
)
def test_s04_edits_only_the_scripted_offer_with_the_merchant_cli(
    arguments: list[str],
    price: int,
    stock: int | None,
    shown_price: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merchant = tmp_path / "merchant"
    (merchant / "src" / "commerce_lab").mkdir(parents=True)
    calls: list[tuple[list[str], Path]] = []

    def run(command: list[str], *, cwd: Path, **_: object) -> Any:
        calls.append((command, cwd))
        offer = {"offer_id": OFFER_ID, "revision": 8, "price_minor": price, "stock": 13}
        return Namespace(returncode=0, stdout=json.dumps(offer) + "\n", stderr="")

    monkeypatch.setattr(demo_stack.subprocess, "run", run)
    out = io.StringIO()

    assert s04([*arguments, "--merchant-repo", str(merchant)], out) == 0

    command = [*demo_stack.MERCHANT_CLI, "commerce_lab.managed_catalog", "update-offer", OFFER_ID]
    command += ["--price-minor", str(price)] + (["--stock", str(stock)] if stock else [])
    assert calls == [(command, merchant)]
    assert shown_price in out.getvalue()
