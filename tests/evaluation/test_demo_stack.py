"""demo-stack builds each mode's environment and never shows a secret. No service starts here."""

import io
import json
import os
import signal
import socket
import sqlite3
import subprocess
import threading
import time
from argparse import Namespace
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, get_args

import pytest

from commerce_agent.config import Settings
from commerce_agent.delegation.bootstrap import load_hnp_trust
from commerce_agent.delegation.models import TaskState
from commerce_agent.governance.bootstrap import (
    build_enforce_gate,
    build_hnp_d1_gate,
    configured_d1,
)
from commerce_agent.governance.evaluator import verify_artifact
from commerce_agent.governance.provenance import merchant_pseudonym
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger, utc_text
from commerce_evaluation import demo_stack
from commerce_evaluation.demo_stack import (
    HNP_SCHEDULED,
    OFFER_ID,
    OFFER_PRICE_MINOR,
    OFFER_STOCK,
    OTHER_MERCHANT,
    PILOT_MODEL,
    S04_PRICE_MINOR,
    DemoError,
    DemoPlan,
    HnpKeys,
    Launcher,
    Ports,
    Service,
    StackSecrets,
    base_environment,
    build_plan,
    describe,
    hnp_problems,
    preflight,
    read_model_settings,
    s04,
)
from commerce_evaluation.paths import DATA, LOCAL

# Ctrl-C, SIGTERM and SIGHUP: each one shuts the stack down.
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

MERCHANT_TOKEN = "merchant-session-" + "t" * 32
MODEL_KEY = "sk-test-" + "k" * 40
# What a merchant step may print when the database fails: its password must never be shown.
MERCHANT_DSN = "postgresql+psycopg://commerce:hunter2-db-password@127.0.0.1:55432/commerce"
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
        secrets=StackSecrets.generate(governed=mode != "b0", hnp=demo_stack.MODES[mode].hnp),
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


def d1_rules(settings: Settings) -> tuple[str, int, tuple[str, ...]]:
    """The policy set, revision and D1 rules the agent assembles from this environment."""
    configured = configured_d1(settings, None)
    assert configured.policy_binding is not None
    policy = verify_artifact(configured.policy_bytes, configured.policy_binding)
    return policy.policy_set_id, policy.version, policy.policy_ids


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
    assert d1_rules(settings) == ("b2-hp-v2", 1, ("POL-01", "POL-02", "POL-11"))
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
    assert d1_rules(settings) == ("b2-hp-v2", 1, ("POL-01", "POL-02", "POL-11"))
    build_enforce_gate(settings, None)


def test_b0_turns_governance_off_without_d1_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_for("b0", tmp_path)

    settings = agent_settings(plan, monkeypatch)

    assert (settings.agent_governance_mode, settings.agent_hp_flow) == ("off", "blocked")
    assert plan.secrets.d1 is None
    assert not [name for name in plan.agent_env(MERCHANT_TOKEN) if name.startswith("AGENT_D1_")]


def test_b2_hnp_is_b2_with_hnp_enabled_and_credentials_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_for("b2-hnp", tmp_path)
    keys = plan.secrets.hnp
    assert keys is not None

    settings = agent_settings(plan, monkeypatch)

    # The same B2 (HP governed, b2-hp-v2) plus HNP, which the same agent process serves.
    assert (settings.agent_governance_mode, settings.agent_hp_flow) == ("enforce", "governed")
    assert d1_rules(settings) == ("b2-hp-v2", 1, ("POL-01", "POL-02", "POL-11"))
    assert allowed_refs(settings) == ("http://127.0.0.1:5120", True)
    assert settings.agent_hnp_enabled and settings.agent_hnp_selector == "llm"
    assert settings.agent_hnp_agent_key_pem is not None
    assert settings.agent_hnp_agent_key_pem.get_secret_value() != plan.secrets.agent_ap2_pem
    assert len({keys.service_token, keys.operator_token}) == 2
    assert min(len(keys.service_token), len(keys.operator_token)) >= 32
    # The agent's own startup accepts it: trust, the HP enforce gate and D1-HNP.
    load_hnp_trust(settings)
    build_enforce_gate(settings, None)
    build_hnp_d1_gate(settings, None)


def test_b2_hnp_takes_the_deterministic_selector_as_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = replace(plan_for("b2-hnp", tmp_path), hnp_selector="deterministic")

    settings = agent_settings(plan, monkeypatch)

    assert settings.agent_hnp_selector == "deterministic"
    load_hnp_trust(settings)


@pytest.mark.parametrize("mode", ["b2", "b0", "pol02-deny"])
def test_only_b2_hnp_turns_hnp_on(mode: str, tmp_path: Path) -> None:
    plan = plan_for(mode, tmp_path)

    assert plan.secrets.hnp is None
    assert not [name for name in plan.agent_env(MERCHANT_TOKEN) if "HNP" in name]


def test_each_launch_generates_new_hnp_credentials() -> None:
    first, second = HnpKeys.generate(), HnpKeys.generate()

    assert set(first.values()).isdisjoint(second.values())
    assert "BEGIN PRIVATE KEY" in first.agent_key_pem
    assert first.service_token not in repr(first) and first.agent_key_pem not in repr(first)


@pytest.mark.parametrize("mode", sorted(demo_stack.MODES))
def test_every_agent_variable_is_an_agent_setting(mode: str, tmp_path: Path) -> None:
    plan = plan_for(mode, tmp_path)
    delivered = set(plan.agent_env(MERCHANT_TOKEN)) - set(plan.base)

    assert sorted(name for name in delivered if name.lower() not in Settings.model_fields) == []


def test_each_secret_reaches_only_the_services_that_need_it(tmp_path: Path) -> None:
    # b2-hnp carries every secret a launch can have: b2's and the HNP ones.
    plan = plan_for("b2-hnp", tmp_path)
    keys = plan.secrets
    assert keys.d1 is not None and keys.hnp is not None
    environments = {
        service.name: set(service.env.values()) for service in plan.services(MERCHANT_TOKEN)
    }
    owners = {
        keys.hnp.agent_key_pem: {"agent"},
        keys.hnp.service_token: {"agent"},
        keys.hnp.operator_token: {"agent"},
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

    # A new secret of the launch cannot be left out of this table.
    assert set(keys.values()) <= set(owners)
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
        hnp_selector=None,
        hnp_trigger_delay=None,
    )

    first = build_plan(arguments, now=datetime(2026, 10, 23, 14, 0, 0, tzinfo=UTC))
    second = build_plan(arguments, now=datetime(2026, 10, 23, 14, 9, 0, tzinfo=UTC))

    assert first.run_dir == LOCAL / "demo" / "20261023T140000Z"
    assert first.run_dir != second.run_dir
    assert first.agent_env("x")["AGENT_DATABASE_PATH"] == str(first.run_dir / "agent.db")
    assert first.secrets.values() != second.secrets.values()
    assert [label for label, _ in first.setup_steps()] == ["Migraciones del comercio"]


def test_b2_hnp_options_default_to_the_model_selector_and_five_seconds(tmp_path: Path) -> None:
    parse = demo_stack._up_parser().parse_args
    common = ["--mode", "b2-hnp", "--env-file", str(tmp_path / ".env")]

    default = build_plan(parse(common))
    fallback = build_plan(
        parse([*common, "--hnp-selector", "deterministic", "--hnp-trigger-delay", "2.5"])
    )

    assert (default.hnp_selector, default.hnp_trigger_delay) == ("llm", 5.0)
    assert default.secrets.hnp is not None and default.secrets.d1 is not None
    assert (fallback.hnp_selector, fallback.hnp_trigger_delay) == ("deterministic", 2.5)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--hnp-selector", "deterministic"],
        ["--mode", "pol02-deny", "--hnp-trigger-delay", "2"],
        ["--mode", "b2-hnp", "--hnp-trigger-delay", "-1"],
    ],
    ids=["selector-without-hnp", "delay-without-hnp", "negative-delay"],
)
def test_hnp_options_are_refused_outside_b2_hnp_or_below_zero(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def never(arguments: Namespace) -> DemoPlan:
        raise AssertionError("no plan is built from refused options")

    monkeypatch.setattr(demo_stack, "build_plan", never)

    with pytest.raises(SystemExit) as exited:
        demo_stack.up(arguments)

    assert exited.value.code == 2
    assert "--hnp" in capsys.readouterr().err


def test_scripted_offer_is_the_pilot_target_and_the_s04_price_fits_the_budget() -> None:
    matrix = json.loads((DATA / "pilot-scenarios.json").read_text("utf-8"))
    scenarios = {item["id"]: item for item in matrix["scenarios"]}
    s01, s04_setup = scenarios["S01"]["catalog_setup"], scenarios["S04"]["catalog_setup"]
    (stimulus, *_) = scenarios["S04"]["stimuli"]
    shipping = s01["target_shipping_minor"]

    assert matrix["shared_controls"]["model"] == PILOT_MODEL
    assert s01["target_variant_id"] == s04_setup["target_variant_id"] == OFFER_ID
    assert s01["target_item_price_minor"] == s04_setup["initial_item_price_minor"]
    assert s01["target_item_price_minor"] == OFFER_PRICE_MINOR
    # The guion asks S04 with a 1.700.000 COP budget: the new price must still fit it, so asking
    # again after canceling reaches a new checkout and a new D1 decision. The pilot's stimulus
    # (1.800.000) does not, which is why the demo uses its own price (the guion says so).
    assert OFFER_PRICE_MINOR < S04_PRICE_MINOR
    assert S04_PRICE_MINOR + shipping <= 170_000_000
    assert stimulus["new_item_price_minor"] + shipping > 170_000_000


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

    def __init__(self, plan: DemoPlan, *, health: dict[str, Any], failing: str | None = None):
        self.calls: list[tuple[str, ...]] = []
        self.spawned: dict[str, FakeProcess] = {}
        self.fake_health = health
        self.failing = failing
        # What each `webhook-dispatch` answers, in order; then nothing is due.
        self.due: list[list[dict[str, Any]]] = []
        self.dispatched: list[tuple[Path, dict[str, str]]] = []
        # What the next dispatcher passes raise instead of answering: a hang, a launch error.
        self.dispatch_errors: list[Exception] = []
        super().__init__(plan, io.StringIO())

    def _run(
        self, command: Sequence[str], cwd: Path, env: Mapping[str, str]
    ) -> tuple[int, str, str]:
        self.calls.append(tuple(command))
        # Merchant steps that echo secrets or the DSN on stderr: the launcher must not repeat them.
        stderr = f"warning {self.plan.secrets.merchant_webhook} at {MERCHANT_DSN}"
        if "webhook-dispatch" in command:
            self.dispatched.append((cwd, dict(env)))
        if self.failing is not None and self.failing in command:
            return 1, "", stderr
        if "webhook-dispatch" in command:
            return 0, json.dumps(self.due.pop(0) if self.due else []), stderr
        if "import-current" in command:
            return 0, json.dumps({"status": "already_imported", "run_id": "run-1"}), stderr
        if "update-offer" in command:
            offer = {"offer_id": OFFER_ID, "revision": 7, "price_minor": 154_000_000, "stock": 13}
            return 0, json.dumps(offer), stderr
        if "issue-session" in command:
            return 0, json.dumps({"session_token": MERCHANT_TOKEN}), f"{stderr} {MERCHANT_TOKEN}"
        return 0, "", stderr

    def _dispatch(
        self,
        command: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout: float = demo_stack.WEBHOOK_DISPATCH_TIMEOUT_SECONDS,
    ) -> tuple[int, str, str]:
        if self.dispatch_errors:
            self.dispatched.append((cwd, dict(env)))
            raise self.dispatch_errors.pop(0)
        return self._run(command, cwd, env)

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

    def _group_alive(self, process: Any) -> bool:
        # A fake process has no children: its group ends with it.
        return False


def shown(launcher: FakeLauncher) -> str:
    """Everything the launcher printed or wrote itself."""
    run_dir = launcher.plan.run_dir
    assert isinstance(launcher.out, io.StringIO)
    return "\n".join(
        [launcher.out.getvalue()]
        + [(run_dir / name).read_text("utf-8") for name in ("launcher.log", "setup.log")]
    )


def assert_no_secret(text: str, plan: DemoPlan) -> None:
    assert "hunter2" not in text and "postgresql" not in text
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
    assert "Política D1: b2-hp-v2 · revisión 1" in output
    assert "UI:    http://127.0.0.1:5174" in output and "Auth:  http://127.0.0.1:5215" in output
    assert "HNP" not in output
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


def delivery(outcome: str = "delivered", error: str | None = None) -> dict[str, Any]:
    """One item of `webhook-dispatch`'s answer (commerce_lab.webhook_delivery)."""
    status = 200 if outcome == "delivered" else 401
    return {
        "delivery_id": "whd_" + "a" * 32,
        "attempt_no": 1,
        "outcome": outcome,
        "http_status": status,
        "error_code": error,
    }


def test_the_merchant_dispatcher_gets_only_the_agent_origin_and_the_shared_secret(
    tmp_path: Path,
) -> None:
    plan = plan_for("b2", tmp_path)

    dispatcher = plan.webhook_env()

    # The merchant signs with the secret the agent verifies, and posts to the agent's origin.
    assert dispatcher["WEBHOOK_RECEIVER_URL"] == "http://127.0.0.1:45125"
    assert dispatcher["WEBHOOK_RECEIVER_URL"] == plan.merchant_env()["WEBHOOK_RECEIVER_URL"]
    assert (
        dispatcher["MERCHANT_WEBHOOK_SECRET"]
        == plan.agent_env(MERCHANT_TOKEN)["MERCHANT_WEBHOOK_SECRET"]
    )
    others = set(plan.secret_values(MERCHANT_TOKEN)) - {plan.secrets.merchant_webhook}
    assert not others & set(dispatcher.values())
    assert set(dispatcher) - set(plan.base) == {"WEBHOOK_RECEIVER_URL", "MERCHANT_WEBHOOK_SECRET"}
    assert plan.dispatch_webhooks() == (
        *demo_stack.MERCHANT_CLI,
        "commerce_lab.db",
        "webhook-dispatch",
        "--limit",
        str(demo_stack.WEBHOOK_BATCH),
    )


def test_watch_relays_merchant_webhooks_to_the_agent_until_a_service_stops(
    tmp_path: Path,
) -> None:
    plan = plan_for("b2", tmp_path)
    launcher = FakeLauncher(plan, health={"governance_mode": "enforce", "hp_flow": "governed"})
    launcher.start()
    launcher.due = [[delivery()], [], [delivery(), delivery("terminal", "HTTP_401")]]
    agent = launcher.spawned["agent"]
    polls = 0

    def stops_after_three_rounds() -> int | None:
        # The agent stops on the fourth round of the watch, whether or not anything was relayed.
        nonlocal polls
        polls += 1
        return 1 if polls > 3 else None

    agent.poll = stops_after_three_rounds  # type: ignore[method-assign]

    with pytest.raises(DemoError, match="Agente se detuvo"):
        launcher.watch(interval=0)
    launcher.stop()
    launcher.close()

    assert [cwd for cwd, _ in launcher.dispatched] == [plan.merchant_repo] * 3
    assert all(env == plan.webhook_env() for _, env in launcher.dispatched)
    output = shown(launcher)
    log = (plan.run_dir / "webhooks.log").read_text("utf-8")
    assert_no_secret(output + log, plan)
    assert "Webhooks del comercio: 1 entregado al agente" in output
    assert "Webhooks del comercio: 1 entregado al agente, 1 sin entregar (HTTP_401)" in output
    assert log.count("delivered") == 2 and "terminal http=401 error=HTTP_401" in log
    assert "cada 2 s" in output


def test_a_failing_relay_is_announced_once_and_the_demo_goes_on(tmp_path: Path) -> None:
    plan = plan_for("b2", tmp_path)
    launcher = FakeLauncher(plan, health={"governance_mode": "enforce", "hp_flow": "governed"})
    launcher.start()
    launcher.failing = "webhook-dispatch"

    assert launcher.relay_webhooks() == []
    assert launcher.relay_webhooks() == []
    launcher.failing = None
    launcher.due = [[delivery()]]
    assert launcher.relay_webhooks() == [delivery()]
    # A relay failure never stops a service.
    assert all(process.code is None for process in launcher.spawned.values())
    launcher.stop()
    launcher.close()

    output = shown(launcher) + (plan.run_dir / "webhooks.log").read_text("utf-8")
    assert_no_secret(output, plan)
    printed = launcher.out.getvalue()  # type: ignore[attr-defined]
    assert printed.count("Aviso: el comercio no pudo entregar sus webhooks al agente") == 1
    assert "Webhooks del comercio: entrega restablecida" in printed


def stops_after(rounds: int) -> Any:
    """A service's poll that reports it stopped on round `rounds + 1` of the watch."""
    polls = 0

    def poll() -> int | None:
        nonlocal polls
        polls += 1
        return 1 if polls > rounds else None

    return poll


def test_a_dispatcher_that_hangs_or_cannot_start_never_stops_the_watch(tmp_path: Path) -> None:
    plan = plan_for("b2", tmp_path)
    launcher = FakeLauncher(plan, health={"governance_mode": "enforce", "hp_flow": "governed"})
    launcher.start()
    launcher.dispatch_errors = [
        subprocess.TimeoutExpired("webhook-dispatch", demo_stack.WEBHOOK_DISPATCH_TIMEOUT_SECONDS),
        FileNotFoundError(2, "No such file or directory", "uv"),
    ]
    launcher.due = [[delivery()]]
    launcher.spawned["agent"].poll = stops_after(3)  # type: ignore[method-assign]

    # The agent stopping is still noticed after a hung pass and one that could not start.
    with pytest.raises(DemoError, match="Agente se detuvo"):
        launcher.watch(interval=0)
    launcher.stop()
    launcher.close()

    assert len(launcher.dispatched) == 3
    printed = launcher.out.getvalue()  # type: ignore[attr-defined]
    assert printed.count("Aviso: el comercio no pudo entregar sus webhooks al agente") == 1
    assert "(no respondió en 10 s y se detuvo)" in printed
    assert "Webhooks del comercio: entrega restablecida" in printed
    log = (plan.run_dir / "webhooks.log").read_text("utf-8")
    assert "no respondió en 10 s" in log and "no se pudo lanzar: FileNotFoundError" in log
    assert_no_secret(shown(launcher) + log, plan)


def test_a_webhooks_log_that_cannot_be_written_never_stops_the_watch(tmp_path: Path) -> None:
    plan = plan_for("b2", tmp_path)
    launcher = FakeLauncher(plan, health={"governance_mode": "enforce", "hp_flow": "governed"})
    launcher.start()
    (plan.run_dir / "webhooks.log").mkdir()
    launcher.due = [[delivery()]]
    launcher.failing = None
    launcher.spawned["agent"].poll = stops_after(2)  # type: ignore[method-assign]
    launcher.dispatch_errors = [OSError("dispatcher failed")]

    with pytest.raises(DemoError, match="Agente se detuvo"):
        launcher.watch(interval=0)
    launcher.stop()
    launcher.close()

    printed = launcher.out.getvalue()  # type: ignore[attr-defined]
    assert "Aviso: el comercio no pudo entregar sus webhooks al agente" in printed
    assert "Webhooks del comercio: 1 entregado al agente" in printed


def hanging_dispatcher(tmp_path: Path) -> tuple[list[str], Path, Path]:
    """A shell that records its pid, starts a child that outlives it and waits for it."""
    leader, child = tmp_path / "leader.pid", tmp_path / "child.pid"
    script = 'echo $$ > "$0"; sleep 30 & echo $! > "$1"; wait'
    return ["sh", "-c", script, str(leader), str(child)], leader, child


def test_a_hanging_dispatcher_is_killed_with_its_whole_group(tmp_path: Path) -> None:
    plan = plan_for("b2", tmp_path)
    launcher = Launcher(plan, io.StringIO())
    command, leader, child = hanging_dispatcher(tmp_path)
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            launcher._dispatch(command, tmp_path, os.environ, timeout=0.5)

        assert time.monotonic() - started < demo_stack.STOP_TIMEOUT_SECONDS
        # No orphan: the child the leader started dies with it.
        assert gone(pid_from(leader)) and gone(pid_from(child))
    finally:
        launcher.close()


def test_stop_kills_a_dispatcher_pass_still_in_flight(tmp_path: Path) -> None:
    plan = plan_for("b2", tmp_path)
    launcher = Launcher(plan, io.StringIO())
    command, leader, child = hanging_dispatcher(tmp_path)
    # What _dispatch holds while it waits, as when Ctrl-C arrives in the middle of a pass.
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    launcher._dispatcher = process
    try:
        orphan = pid_from(child)

        launcher.stop()

        assert gone(pid_from(leader)) and gone(orphan)
        assert process.returncode is not None
    finally:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        launcher.close()


def test_dry_run_names_variables_but_shows_no_value(tmp_path: Path) -> None:
    plan = plan_for("b2", tmp_path)
    out = io.StringIO()

    describe(plan, ["el puerto 5120 (comercio) está ocupado"], out)

    text = out.getvalue()
    assert_no_secret(text, plan)
    assert "AGENT_D1_HMAC_KEY_HEX=[redactado]" in text
    assert "AGENT_GOVERNANCE_MODE=enforce" in text
    assert "✗ el puerto 5120 (comercio) está ocupado" in text
    assert "HNP" not in text


def test_dry_run_describes_b2_hnp_without_its_credentials(tmp_path: Path) -> None:
    plan = plan_for("b2-hnp", tmp_path)
    out = io.StringIO()

    describe(plan, [], out)

    text = out.getvalue()
    assert_no_secret(text, plan)
    for name in ("AGENT_HNP_AGENT_KEY_PEM", "AGENT_HNP_SERVICE_TOKEN", "AGENT_HNP_OPERATOR_TOKEN"):
        assert f"{name}=[redactado]" in text
    assert "AGENT_HNP_ENABLED=true" in text and "AGENT_HNP_SELECTOR=llm" in text
    assert "AGENT_D1_POLICY_SET_ID=b2-hp-v2" in text
    assert "HNP: selector llm · disparo automático a los 5 s (hnp.log)" in text


def test_the_preflight_runs_the_agents_hnp_startup_check_without_starting_it(
    tmp_path: Path,
) -> None:
    plan = plan_for("b2-hnp", tmp_path)
    keys = plan.secrets.hnp
    assert keys is not None
    # Two equal tokens: the agent's HNP startup refuses them.
    equal = replace(keys, operator_token=keys.service_token)
    broken = replace(plan, secrets=replace(plan.secrets, hnp=equal))

    assert hnp_problems(plan) == []
    assert hnp_problems(plan_for("b2", tmp_path)) == []
    [problem] = hnp_problems(broken)
    assert problem.startswith("HNP: el agente no arrancaría")
    assert "AGENT_HNP_OPERATOR_TOKEN must differ" in problem
    assert problem in preflight(broken)
    assert_no_secret(problem, broken)
    assert not plan.run_dir.exists()


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


@pytest.mark.parametrize(
    "content",
    [
        f'OPENAI_API_KEY="{MODEL_KEY}#not-a-comment"  # quoted\n'
        "OPENAI_MODEL=gpt-5-mini  # the pilot model\n"
        "OPENAI_TIMEOUT_SECONDS=45 # seconds\n"
        "OPENAI_REASONING_EFFORT='low' # single quotes\n",
        f"export OPENAI_API_KEY={MODEL_KEY}\nOPENAI_MODEL=gpt-5-mini#no-space-keeps-it\n",
    ],
    ids=["inline-comments", "no-comment"],
)
def test_model_settings_read_the_env_file_like_the_agent_settings(
    content: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")
    for name in list(os.environ):
        monkeypatch.delenv(name)

    read = read_model_settings(env_file)
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.openai_api_key is not None
    assert read["OPENAI_API_KEY"] == settings.openai_api_key.get_secret_value()
    assert read["OPENAI_MODEL"] == settings.openai_model
    assert float(read.get("OPENAI_TIMEOUT_SECONDS", "30")) == settings.openai_timeout_seconds
    assert read.get("OPENAI_REASONING_EFFORT") == settings.openai_reasoning_effort


def test_a_model_other_than_the_pilot_model_is_announced_but_does_not_stop_the_launch(
    tmp_path: Path,
) -> None:
    plan = replace(
        plan_for("b2", tmp_path), model={"OPENAI_API_KEY": MODEL_KEY, "OPENAI_MODEL": "gpt-4.1"}
    )
    notice = f"OPENAI_MODEL=gpt-4.1 no es el modelo del piloto ({PILOT_MODEL})"
    out = io.StringIO()

    describe(plan, [], out)
    launcher = FakeLauncher(plan, health={"governance_mode": "enforce", "hp_flow": "governed"})
    launcher.start()
    launcher.stop()
    launcher.close()

    assert notice in out.getvalue() and "Comprobaciones: todo listo" in out.getvalue()
    assert notice in shown(launcher) and "ui" in launcher.spawned
    assert plan_for("b2", tmp_path).warnings() == []


class StubbornProcess(FakeProcess):
    """Exits only with a signal it does not ignore; every signal and wait goes to `events`."""

    def __init__(self, events: list[tuple[str, int, int]], ignores: set[int]) -> None:
        super().__init__()
        self.events = events
        self.ignores = ignores

    def wait(self, timeout: float | None = None) -> int:
        self.events.append(("wait", self.pid, 0))
        if not self.signals or self.signals[-1] in self.ignores:
            raise subprocess.TimeoutExpired("service", timeout or 0)
        self.code = -self.signals[-1]
        return self.code


def test_stop_signals_every_group_before_waiting_even_if_the_output_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_for("b2", tmp_path)
    launcher = FakeLauncher(plan, health={})
    events: list[tuple[str, int, int]] = []
    handlers: set[Any] = set()

    def record(process: Any, signum: int) -> None:
        process.signals.append(signum)
        events.append(("signal", process.pid, signum))
        handlers.update(signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP))

    monkeypatch.setattr(launcher, "_signal", record)
    payment, merchant, *_ = plan.services(MERCHANT_TOKEN)
    ignores_term = StubbornProcess(events, ignores={signal.SIGTERM})
    ignores_kill = StubbornProcess(events, ignores={signal.SIGTERM, signal.SIGKILL})
    launcher.running = [(payment, ignores_term), (merchant, ignores_kill)]
    # The terminal closed (SIGHUP): every line the launcher prints now fails.
    launcher.out.close()

    launcher.stop()
    launcher.close()

    terms = [("signal", process.pid, signal.SIGTERM) for process in (ignores_kill, ignores_term)]
    assert events[:2] == terms
    assert ignores_term.signals == ignores_kill.signals == [signal.SIGTERM, signal.SIGKILL]
    assert launcher.running == []
    # Another Ctrl-C, SIGTERM or SIGHUP during the shutdown cannot interrupt it.
    assert handlers == {signal.SIG_IGN}
    log = (plan.run_dir / "launcher.log").read_text("utf-8")
    assert "· Proveedor de pagos: apagado" in log
    assert f"no se detuvo (pid {ignores_kill.pid})" in log


def test_up_turns_sighup_and_sigterm_into_a_clean_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_for("b2", tmp_path)
    seen: dict[int, Any] = {}
    stopped: list[bool] = []

    class Interrupted(FakeLauncher):
        def __init__(self, plan: DemoPlan) -> None:
            super().__init__(plan, health={})

        def start(self) -> dict[str, str]:
            seen.update({signum: signal.getsignal(signum) for signum in SIGNALS})
            raise KeyboardInterrupt

        def stop(self) -> None:
            stopped.append(True)
            super().stop()

    before = {signum: signal.getsignal(signum) for signum in SIGNALS}
    monkeypatch.setattr(demo_stack, "build_plan", lambda arguments: plan)
    monkeypatch.setattr(demo_stack, "preflight", lambda plan: [])
    monkeypatch.setattr(demo_stack, "Launcher", Interrupted)

    assert demo_stack.up([]) == 0

    assert seen == dict.fromkeys(SIGNALS, demo_stack._interrupt)
    assert stopped == [True]
    assert {signum: signal.getsignal(signum) for signum in before} == before


@pytest.mark.parametrize(
    "first", [signal.SIGINT, signal.SIGTERM], ids=["ctrl-c-twice", "sigterm-then-ctrl-c"]
)
def test_a_second_ctrl_c_before_stop_cannot_cut_the_shutdown_short(
    first: signal.Signals, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_for("b2", tmp_path)
    stopped: list[bool] = []

    class TwoSignals(FakeLauncher):
        def __init__(self, plan: DemoPlan) -> None:
            super().__init__(plan, health={})

        def start(self) -> dict[str, str]:
            os.kill(os.getpid(), first)
            raise AssertionError("the first signal must interrupt the launch")

        def say(self, message: str = "") -> None:
            super().say(message)
            if "Apagando" in message:
                # The presenter presses Ctrl-C again before stop() starts.
                os.kill(os.getpid(), signal.SIGINT)

        def stop(self) -> None:
            stopped.append(True)
            super().stop()

    before = {signum: signal.getsignal(signum) for signum in SIGNALS}
    monkeypatch.setattr(demo_stack, "build_plan", lambda arguments: plan)
    monkeypatch.setattr(demo_stack, "preflight", lambda plan: [])
    monkeypatch.setattr(demo_stack, "Launcher", TwoSignals)

    try:
        code = demo_stack.up([])
    except KeyboardInterrupt:
        pytest.fail("the second Ctrl-C escaped up() (traceback and exit code 130)")

    assert code == 0
    assert stopped == [True]
    assert {signum: signal.getsignal(signum) for signum in before} == before


# A service whose leader dies with SIGTERM while a child it started ignores it (`uv run`,
# `npm run`): the child writes its pid, then becomes `sleep` with SIGTERM still ignored.
IGNORES_TERM = """trap '' TERM
echo $$ > "$1"
exec sleep 30
"""


def real_service(template: Service, tmp_path: Path, script: str, *arguments: str) -> Service:
    return replace(
        template,
        command=("sh", "-c", script, *arguments),
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
    )


def pid_from(path: Path) -> int:
    for _ in range(200):
        text = path.read_text("utf-8").strip() if path.is_file() else ""
        if text:
            return int(text)
        time.sleep(0.025)
    raise AssertionError(f"{path.name} was never written")


def gone(pid: int) -> bool:
    """The process no longer runs (a zombie waiting for its new parent counts as gone)."""
    for _ in range(200):
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
        ).stdout.strip()
        if not state or state.startswith("Z"):
            return True
        time.sleep(0.025)
    return False


def test_stop_kills_every_group_at_the_deadline_even_if_its_leader_already_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(demo_stack, "STOP_TIMEOUT_SECONDS", 0.5)
    plan = plan_for("b2", tmp_path)
    launcher = Launcher(plan, io.StringIO())
    payment, merchant, *_ = plan.services(MERCHANT_TOKEN)
    script, pidfile = tmp_path / "ignores-term.sh", tmp_path / "child.pid"
    script.write_text(IGNORES_TERM, "utf-8")
    # The leader only waits, so SIGTERM ends it; its child keeps the group alive.
    leader_exits = real_service(payment, tmp_path, 'sh "$0" "$1" & wait', str(script), str(pidfile))
    # A plain service whose whole group is gone after SIGTERM: its SIGKILL must be skipped.
    exits = real_service(merchant, tmp_path, "exec sleep 30")
    launcher.running = [(s, launcher._spawn(s)) for s in (leader_exits, exits)]
    groups = [process.pid for _, process in launcher.running]
    try:
        child = pid_from(pidfile)

        launcher.stop()

        assert gone(child)
        assert all(gone(pid) for pid in groups)
        log = (plan.run_dir / "launcher.log").read_text("utf-8")
        assert log.count(": apagado") == 2 and "no se detuvo" not in log
    finally:
        for group in groups:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signal.SIGKILL)
        launcher.close()


def merchant_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, code: int, stdout: str, stderr: str
) -> Path:
    merchant = tmp_path / "merchant"
    (merchant / "src" / "commerce_lab").mkdir(parents=True)

    def run(command: list[str], **_: object) -> Any:
        return Namespace(returncode=code, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(demo_stack.subprocess, "run", run)
    return merchant


def test_s04_shows_the_merchant_error_without_its_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stderr = f"Traceback ...\nOperationalError: connection to {MERCHANT_DSN} failed\n"
    merchant = merchant_answers(monkeypatch, tmp_path, code=1, stdout="", stderr=stderr)

    assert s04(["--merchant-repo", str(merchant)], io.StringIO()) == 1

    shown_error = capsys.readouterr().err
    assert "No se pudo cambiar la oferta (código 1)" in shown_error
    assert "OperationalError: connection to [redactado] failed" in shown_error
    assert "hunter2" not in shown_error and "postgresql" not in shown_error


@pytest.mark.parametrize(
    "stdout",
    ["", "warning only\n", "[]\n", json.dumps({"offer_id": OFFER_ID}) + "\n"],
    ids=["empty", "not-json", "not-an-object", "missing-fields"],
)
def test_s04_reports_an_unexpected_answer_without_a_traceback(
    stdout: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stderr = f"note: using {MERCHANT_DSN}\n"
    merchant = merchant_answers(monkeypatch, tmp_path, code=0, stdout=stdout, stderr=stderr)
    out = io.StringIO()

    assert s04(["--merchant-repo", str(merchant)], out) == 1

    shown_error = capsys.readouterr().err
    assert "El comercio respondió con una oferta inesperada" in shown_error
    assert "Traceback" not in shown_error and "hunter2" not in shown_error
    assert out.getvalue() == ""


# b2-hnp: /health at startup and the automatic trigger of delegated tasks --------------------------

HNP_HEALTH = {
    "governance_mode": "enforce",
    "hp_flow": "governed",
    "hnp_enabled": True,
    "hnp_kill_switch": False,
}
NOW = datetime(2026, 10, 23, 15, 0, 0, tzinfo=UTC)
TASK = "ht_" + "1" * 32
RUNNING = {"task_id": TASK, "state": "running", "outcome_code": None, "run_id": "run_1"}


def test_a_b2_hnp_launch_checks_hnp_in_health_and_announces_the_trigger(tmp_path: Path) -> None:
    plan = plan_for("b2-hnp", tmp_path)
    launcher = FakeLauncher(plan, health=HNP_HEALTH)

    launcher.start()
    launcher.stop()
    launcher.close()

    output = shown(launcher)
    assert_no_secret(output, plan)
    assert "Demo local · modo b2-hnp" in output
    assert "governance_mode=enforce · hp_flow=governed" in output
    assert "Política D1: b2-hp-v2 · revisión 1" in output
    assert "HNP: activo · selector llm · disparo automático a los 5 s" in output
    assert "ui" in launcher.spawned


@pytest.mark.parametrize(
    "hnp",
    [{}, {"hnp_enabled": True, "hnp_kill_switch": True}, {"hnp_enabled": 1, "hnp_kill_switch": 0}],
    ids=["hnp-off", "kill-switch-on", "not-booleans"],
)
def test_a_b2_hnp_launch_stops_if_health_does_not_report_hnp_ready(
    hnp: dict[str, Any], tmp_path: Path
) -> None:
    plan = plan_for("b2-hnp", tmp_path)
    launcher = FakeLauncher(
        plan, health={"governance_mode": "enforce", "hp_flow": "governed", **hnp}
    )

    with pytest.raises(DemoError, match="hnp_enabled=True y hnp_kill_switch=False"):
        launcher.start()
    launcher.stop()
    launcher.close()

    assert "ui" not in launcher.spawned
    assert launcher.spawned["agent"].signals


def test_watch_runs_the_hnp_trigger_and_no_failure_of_it_stops_the_demo(tmp_path: Path) -> None:
    plan = plan_for("b2-hnp", tmp_path)
    launcher = FakeLauncher(plan, health=HNP_HEALTH)
    launcher.start()
    passes: list[datetime | None] = []

    def trigger(now: datetime | None = None) -> list[str]:
        passes.append(now)
        raise RuntimeError("unexpected")

    launcher.trigger_hnp_tasks = trigger  # type: ignore[method-assign]
    launcher.spawned["agent"].poll = stops_after(2)  # type: ignore[method-assign]

    with pytest.raises(DemoError, match="Agente se detuvo"):
        launcher.watch(interval=0)
    launcher.stop()
    launcher.close()

    assert len(passes) == 2
    assert "RuntimeError" in (plan.run_dir / "hnp.log").read_text("utf-8")


def test_watch_never_runs_the_hnp_trigger_outside_b2_hnp(tmp_path: Path) -> None:
    plan = plan_for("b2", tmp_path)
    launcher = FakeLauncher(plan, health={"governance_mode": "enforce", "hp_flow": "governed"})
    launcher.start()
    passes: list[datetime | None] = []
    launcher.trigger_hnp_tasks = lambda now=None: passes.append(now) or []  # type: ignore[method-assign]
    launcher.spawned["agent"].poll = stops_after(2)  # type: ignore[method-assign]

    with pytest.raises(DemoError, match="Agente se detuvo"):
        launcher.watch(interval=0)
    launcher.stop()
    launcher.close()

    assert passes == []
    assert not (plan.run_dir / "hnp.log").exists()


@dataclass
class AgentDouble:
    """A local HTTP server in place of the agent: records each request, answers in order."""

    port: int
    requests: list[dict[str, Any]]
    answers: list[tuple[int, dict[str, Any]]]


@contextmanager
def agent_double(*answers: tuple[int, dict[str, Any]]) -> Iterator[AgentDouble]:
    double = AgentDouble(0, [], list(answers))

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            double.requests.append(
                {
                    "path": self.path,
                    "headers": {name.lower(): value for name, value in self.headers.items()},
                    "body": self.rfile.read(length),
                }
            )
            status, body = double.answers.pop(0) if double.answers else (500, {})
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    double.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield double
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def hnp_plan(tmp_path: Path, agent_port: int, *, delay: float = 5.0) -> DemoPlan:
    plan = plan_for("b2-hnp", tmp_path)
    return replace(plan, ports=replace(plan.ports, agent=agent_port), hnp_trigger_delay=delay)


def hnp_database(plan: DemoPlan) -> Path:
    """The agent's database with the HNP ledger's own schema (persistence/delegation_sqlite.py)."""
    database = plan.run_dir / "agent.db"
    SqliteDelegationLedger(database).initialize()
    return database


def add_task(
    database: Path, task_id: str, *, created_at: datetime, state: str = "scheduled"
) -> None:
    """A task row as the ledger writes it at consent (`scheduled`) or after a claim (`running`)."""
    created = utc_text(created_at)
    lease = ("lease_1", utc_text(created_at + timedelta(minutes=5)))
    lease_id, lease_expires_at = lease if state == "running" else (None, None)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO hnp_tasks (task_id, grant_id, owner_id, run_id, state, outcome_code, "
            "claim_count, lease_id, lease_expires_at, created_at, updated_at, finished_at) "
            "VALUES (?, ?, 'owner_1', NULL, ?, NULL, 0, ?, ?, ?, ?, NULL)",
            (task_id, f"grant_{task_id}", state, lease_id, lease_expires_at, created, created),
        )


def hnp_shown(launcher: Launcher) -> tuple[str, str]:
    """What the launcher printed (and logged in launcher.log), and its hnp.log."""
    assert isinstance(launcher.out, io.StringIO)
    run_dir = launcher.plan.run_dir
    printed = launcher.out.getvalue() + (run_dir / "launcher.log").read_text("utf-8")
    log = run_dir / "hnp.log"
    return printed, log.read_text("utf-8") if log.is_file() else ""


def free_port() -> int:
    """A loopback port nobody listens on: connecting to it is refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_trigger_reads_a_state_the_agents_tasks_really_have() -> None:
    assert HNP_SCHEDULED in get_args(TaskState)


def test_the_hnp_trigger_runs_a_due_task_once_with_the_service_token_and_no_origin(
    tmp_path: Path,
) -> None:
    with agent_double((202, RUNNING)) as agent:
        plan = hnp_plan(tmp_path, agent.port)
        launcher = Launcher(plan, io.StringIO())
        database = hnp_database(plan)
        add_task(database, TASK, created_at=NOW - timedelta(seconds=6))
        # Only `scheduled` tasks are due: one already claimed or finished is not run from here.
        add_task(database, "ht_claimed", created_at=NOW - timedelta(minutes=1), state="running")
        add_task(database, "ht_done", created_at=NOW - timedelta(minutes=2), state="purchased")
        before = database.read_bytes()

        first = launcher.trigger_hnp_tasks(now=NOW)
        # The task is still `scheduled` in this database, yet the launcher never runs it again.
        later = launcher.trigger_hnp_tasks(now=NOW + timedelta(seconds=10))
        launcher.close()

    assert (first, later) == ([TASK], [])
    assert [request["path"] for request in agent.requests] == [f"/internal/hnp/tasks/{TASK}/run"]
    headers = agent.requests[0]["headers"]
    assert plan.secrets.hnp is not None
    assert headers["authorization"] == f"Bearer {plan.secrets.hnp.service_token}"
    assert "origin" not in headers and agent.requests[0]["body"] == b""
    # Read-only: the agent's database is left as it was.
    assert database.read_bytes() == before
    printed, log = hnp_shown(launcher)
    assert f"{TASK} http=202 state=running" in log
    assert f"· HNP: tarea {TASK} disparada (HTTP 202, running)" in printed
    assert_no_secret(printed + log, plan)


def test_the_hnp_trigger_waits_for_the_delay_after_the_consent(tmp_path: Path) -> None:
    with agent_double((202, RUNNING)) as agent:
        plan = hnp_plan(tmp_path, agent.port, delay=5)
        launcher = Launcher(plan, io.StringIO())
        add_task(hnp_database(plan), TASK, created_at=NOW)

        early = launcher.trigger_hnp_tasks(now=NOW + timedelta(seconds=4.9))
        sent_early = len(agent.requests)
        due = launcher.trigger_hnp_tasks(now=NOW + timedelta(seconds=5))
        launcher.close()

    assert (early, sent_early, due) == ([], 0, [TASK])
    assert len(agent.requests) == 1


def test_a_refused_trigger_is_logged_and_never_retried(tmp_path: Path) -> None:
    with agent_double() as agent:
        plan = hnp_plan(tmp_path, agent.port)
        assert plan.secrets.hnp is not None
        # Even an answer that echoed the credential would reach neither the log nor the terminal.
        echoed = f"no {plan.secrets.hnp.service_token}"
        agent.answers.append((409, {"code": "hnp_task_not_runnable", "message": echoed}))
        launcher = Launcher(plan, io.StringIO())
        add_task(hnp_database(plan), TASK, created_at=NOW - timedelta(seconds=30))

        rounds = [launcher.trigger_hnp_tasks(now=NOW + timedelta(seconds=s)) for s in (0, 2, 4)]
        launcher.close()

    assert rounds == [[TASK], [], []]
    assert len(agent.requests) == 1
    printed, log = hnp_shown(launcher)
    assert log.count(TASK) == 1 and "http=409" in log and "code=hnp_task_not_runnable" in log
    assert f"Aviso: la tarea HNP {TASK} no se pudo disparar (HTTP 409" in printed
    assert "no se reintenta" in printed
    assert_no_secret(printed + log, plan)


def test_an_agent_that_does_not_answer_is_logged_and_the_task_is_not_retried(
    tmp_path: Path,
) -> None:
    plan = hnp_plan(tmp_path, free_port())
    launcher = Launcher(plan, io.StringIO())
    add_task(hnp_database(plan), TASK, created_at=NOW - timedelta(seconds=30))

    rounds = [launcher.trigger_hnp_tasks(now=NOW + timedelta(seconds=s)) for s in (0, 2)]
    launcher.close()

    assert rounds == [[TASK], []]
    printed, log = hnp_shown(launcher)
    assert log.count(TASK) == 1 and "error=ConnectError" in log
    assert f"Aviso: la tarea HNP {TASK} no se pudo disparar (ConnectError" in printed
    assert_no_secret(printed + log, plan)


def test_an_unreadable_agent_database_is_announced_once_and_never_raises(tmp_path: Path) -> None:
    with agent_double((202, RUNNING)) as agent:
        plan = hnp_plan(tmp_path, agent.port)
        launcher = Launcher(plan, io.StringIO())
        database = plan.run_dir / "agent.db"

        # Before the agent creates its database there is nothing to read, nor anything to say.
        missing = launcher.trigger_hnp_tasks(now=NOW)
        quiet = hnp_shown(launcher)
        database.write_bytes(b"not a database, " * 64)
        rounds = [launcher.trigger_hnp_tasks(now=NOW) for _ in range(3)]
        database.unlink()
        add_task(hnp_database(plan), TASK, created_at=NOW - timedelta(minutes=1))
        recovered = launcher.trigger_hnp_tasks(now=NOW)
        launcher.close()

    assert (missing, quiet) == ([], ("", ""))
    assert rounds == [[], [], []] and recovered == [TASK] and len(agent.requests) == 1
    printed, log = hnp_shown(launcher)
    terminal = launcher.out.getvalue()  # type: ignore[attr-defined]
    assert terminal.count("Aviso: el lanzador no pudo leer las tareas HNP") == 1
    assert "· HNP: lectura de las tareas restablecida" in terminal
    assert_no_secret(printed + log, plan)
    assert log.count("DatabaseError") == 3
