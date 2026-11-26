"""Local demo stack in one command: merchant, payment provider, auth, agent and UI.

`uv run demo-stack` starts the merchant's PostgreSQL (docker compose, never `down`), migrates,
imports the managed catalog, resets the scripted offer, issues the agent's merchant session and
starts every service with secrets and AP2/D1 keys generated for this launch. The agent runs one of
the pilot's conditions (agent_condition.CONDITIONS); unlike the pilot, it buys from the managed
catalog with the real clock. While the stack runs, the merchant's CLI delivers its signed order
webhooks to the agent every few seconds: the merchant has no worker of its own. `uv run demo-stack
s04` changes the scripted offer's price during the demo.

`--mode b2-hnp` is B2 plus Human Not Present (docs/design/hnp-governed.md): the same agent serves
the governed chat purchase and delegations. HNP has no scheduler, so the launcher, the only holder
of the service token, runs each scheduled task once a few seconds after the buyer's consent.

No service reads a developer .env: the merchant and the payment provider run from the launch
directory under their own interpreter and lock (`uv run --project`), the agent from the same
directory, and each one gets every setting it needs explicitly. From the agent's .env only the
model settings are read, and only for the agent.

Secrets live only in this process and in the children that need them: they are never printed and
never written. Each launch gets its own directory under .local/demo/ with a new agent database,
the auth and payment databases and every log. How to run it: «Probar la demo» in README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, Protocol, TextIO
from urllib.parse import quote

import httpx
from jwcrypto.jwk import JWK

from commerce_agent.config import DEFAULT_MODEL
from commerce_agent.llm.prices import format_minor_amount
from commerce_agent.persistence.delegation_sqlite import utc_text
from commerce_evaluation.agent_condition import (
    CONDITIONS,
    ConditionMismatchError,
    D1RunKeys,
    PilotCondition,
    agent_environment,
    ap2_private_key,
    startup_check,
    verify_agent_condition,
)
from commerce_evaluation.paths import LOCAL, ROOT
from commerce_evaluation.pilot_policy import HP_V2_POLICY
from commerce_evaluation.redaction import redact as redact_secrets

DEMO_RUNS = LOCAL / "demo"
AUTH = ROOT / "auth"
UI = ROOT / "ui"
AGENT_APP = "commerce_agent.api:app_factory"
# A fictitious approved merchant: an allowlist that is valid but excludes the demo merchant.
OTHER_MERCHANT = "https://comercio-autorizado.example"
# The managed catalog's actor (merchant README) and the scripted offer of S01, S02 and S04 with
# its curated price and stock; every launch restores them unless --keep-offer.
MANAGED_ACTOR = "merchant-agent"
OFFER_ID = "BBGooglePixel8-128GB"
OFFER_TITLE = "Google Pixel 8 128GB"
OFFER_PRICE_MINOR = 154_000_000
OFFER_STOCK = 13
# S04: still within the 1.700.000 COP budget, so asking again reaches a new checkout.
S04_PRICE_MINOR = 162_000_000
SESSION_TTL_SECONDS = 12 * 3600
# The merchant's local PostgreSQL as its compose.yaml publishes it and its Settings default names
# it (synthetic, loopback-only credentials). Both are passed explicitly, so neither the merchant
# nor docker compose takes them from a .env in the merchant's checkout.
MERCHANT_PG_PORT = "55433"
MERCHANT_DATABASE_URL = (
    f"postgresql://tesis_dev:tesis_local_only@127.0.0.1:{MERCHANT_PG_PORT}/tesis_lab"
)
# The merchant's id at the payment provider: the same value for both, whatever their .env says.
PAYMENT_MERCHANT_ID = "tesis_merchant"
# Only these model settings leave the agent's .env, and only for the agent process.
MODEL_SETTINGS = (
    "ANTHROPIC_API_KEY",
    "AGENT_MODEL",
    "AGENT_MODEL_EFFORT",
    "AGENT_MODEL_TIMEOUT_SECONDS",
    "AGENT_MODEL_REASONING_SUMMARY",
    "AGENT_HNP_REVIEWER_MODEL",
)
REQUIRED_MODEL_SETTINGS = ("ANTHROPIC_API_KEY",)
# The agent's model in the evaluation (eval-suite.md §6): another one is announced, not refused.
SUITE_MODEL = DEFAULT_MODEL
# Variables a child never inherits from the presenter's shell: each service gets explicit ones.
INHERITED_PREFIXES = (
    "ANTHROPIC_",
    "OPENAI_",
    "MERCHANT_",
    "PAYMENT_",
    "AGENT_",
    "BETTER_AUTH_",
    "AUTH_",
    "VITE_",
    "HNP_",
    "LANGSMITH_",
    "LANGCHAIN_",
    "PG",
    "UV_PROJECT",
)
INHERITED_NAMES = frozenset(
    {
        "DATABASE_URL",
        "WEBHOOK_RECEIVER_URL",
        "STOREFRONT_ORIGIN",
        "ACP_API_BASE_URL",
        "PORT",
        "VIRTUAL_ENV",
    }
)
REDACTED = "[redactado]"
# What python-dotenv (and so Settings) drops from an unquoted value: whitespace, then a comment.
INLINE_COMMENT = re.compile(r"\s+#.*")
READY_TIMEOUT_SECONDS = 90.0
# The merchant's outbox (order_create, order_update) has no worker: `webhook-dispatch` delivers
# what is due, signed with MERCHANT_WEBHOOK_SECRET, to WEBHOOK_RECEIVER_URL (merchant README).
WEBHOOK_RELAY_SECONDS = 2.0
WEBHOOK_BATCH = 20
# One dispatcher pass delivers at most WEBHOOK_BATCH events; past this it is stuck and is killed.
WEBHOOK_DISPATCH_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 10.0
# `docker info` answers in well under a second when the daemon runs; past this it does not.
DOCKER_INFO_TIMEOUT_SECONDS = 15.0
GROUP_POLL_SECONDS = 0.05
# Closing the terminal (SIGHUP) or `kill` (SIGTERM) shut the stack down like Ctrl-C.
SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
STOP_SIGNALS = (signal.SIGINT, *SHUTDOWN_SIGNALS)
# b2-hnp. `llm` picks the delegated variant with the model, as layer B of the HNP pilot;
# `deterministic` is the fallback if the model fails during the demo.
HNP_SELECTORS = ("llm", "deterministic")
# b2-hnp. `llm` adds the intent reviewer (docs/design/hnp-intent-review.md); off by default.
HNP_REVIEWERS = ("off", "llm")
HNP_TRIGGER_DELAY_SECONDS = 5.0
HNP_TRIGGER_TIMEOUT_SECONDS = 5.0
HNP_RUN_ROUTE = "/internal/hnp/tasks/{task_id}/run"
# The state a task has from the buyer's consent until a claim (persistence/delegation_sqlite.py).
HNP_SCHEDULED = "scheduled"
# `created_at` is `utc_text` (milliseconds and Z), so text order is time order.
HNP_DUE_TASKS = (
    "SELECT task_id FROM hnp_tasks WHERE state = ? AND created_at <= ? ORDER BY created_at, task_id"
)


class DemoError(RuntimeError):
    """A step failed; the message is ours and never carries a secret."""


@dataclass(frozen=True)
class DemoMode:
    name: str
    condition: PilotCondition
    # Whether the POL-02 allowlist contains the demo merchant (governed modes only).
    allows_merchant: bool
    description: str
    # Human Not Present on top of the condition (b2-hnp-v1, built by the agent in code).
    hnp: bool = False


MODES = {
    "b2": DemoMode(
        "b2",
        CONDITIONS["B2"],
        allows_merchant=True,
        description="B2: política activa y flujo gobernado; el comercio está permitido",
    ),
    "b0": DemoMode(
        "b0",
        CONDITIONS["B0prime"],
        allows_merchant=True,
        description="B0': gobernanza apagada, mismo agente",
    ),
    "pol02-deny": DemoMode(
        "pol02-deny",
        CONDITIONS["B2"],
        allows_merchant=False,
        description="B2 con una lista de comercios que excluye al comercio de la demo (POL-02)",
    ),
    "b2-hnp": DemoMode(
        "b2-hnp",
        CONDITIONS["B2"],
        allows_merchant=True,
        description="B2 y además Human Not Present: compras delegadas con la política b2-hnp-v1",
        hnp=True,
    ),
}


@dataclass(frozen=True)
class Ports:
    # README defaults: merchant, payment provider, agent API, auth service and Vite.
    merchant: int = 4120
    payment: int = 4130
    agent: int = 43120
    auth: int = 4210
    ui: int = 5173

    def origin(self, service: str) -> str:
        return f"http://127.0.0.1:{getattr(self, service)}"

    def named(self) -> dict[str, int]:
        return {
            "comercio": self.merchant,
            "proveedor de pagos": self.payment,
            "agente": self.agent,
            "auth": self.auth,
            "UI": self.ui,
        }


def ap2_key_pair() -> tuple[str, str]:
    """(PKCS8 PEM, public JWK JSON) of a new ES256 key for this launch."""
    pem = ap2_private_key()
    return pem, JWK.from_pem(pem.encode()).export_public()


@dataclass(frozen=True)
class HnpKeys:
    """HNP credentials of one launch (hnp-governed.md §6): the Agent Key that closes the open
    mandates, never the AP2 provider key, and the service and operator tokens."""

    agent_key_pem: str = field(repr=False)
    service_token: str = field(repr=False)
    operator_token: str = field(repr=False)

    @classmethod
    def generate(cls) -> HnpKeys:
        # 43 characters each; the agent requires at least 32 and two different tokens.
        return cls(ap2_private_key(), secrets.token_urlsafe(32), secrets.token_urlsafe(32))

    def values(self) -> tuple[str, ...]:
        return (self.agent_key_pem, self.service_token, self.operator_token)


@dataclass(frozen=True)
class StackSecrets:
    """Every credential and key of one launch. Values never appear in repr, output or files."""

    merchant_webhook: str = field(repr=False)
    approval: str = field(repr=False)
    payment_agent_token: str = field(repr=False)
    payment_merchant_token: str = field(repr=False)
    payment_token_secret: str = field(repr=False)
    auth_secret: str = field(repr=False)
    agent_ap2_pem: str = field(repr=False)
    merchant_ap2_pem: str = field(repr=False)
    payment_ap2_pem: str = field(repr=False)
    agent_jwk: str
    merchant_jwk: str
    d1: D1RunKeys | None = field(repr=False)
    hnp: HnpKeys | None = field(default=None, repr=False)

    @classmethod
    def generate(cls, *, governed: bool, hnp: bool = False) -> StackSecrets:
        agent_pem, agent_jwk = ap2_key_pair()
        merchant_pem, merchant_jwk = ap2_key_pair()
        payment_pem, _ = ap2_key_pair()
        return cls(
            merchant_webhook=secrets.token_urlsafe(32),
            approval=secrets.token_urlsafe(32),
            payment_agent_token=secrets.token_urlsafe(32),
            payment_merchant_token=secrets.token_urlsafe(32),
            payment_token_secret=secrets.token_urlsafe(32),
            auth_secret=secrets.token_urlsafe(32),
            agent_ap2_pem=agent_pem,
            merchant_ap2_pem=merchant_pem,
            payment_ap2_pem=payment_pem,
            agent_jwk=agent_jwk,
            merchant_jwk=merchant_jwk,
            d1=D1RunKeys.generate() if governed else None,
            hnp=HnpKeys.generate() if hnp else None,
        )

    def values(self) -> tuple[str, ...]:
        d1 = (self.d1.hmac_hex, self.d1.aes_hex) if self.d1 is not None else ()
        hnp = self.hnp.values() if self.hnp is not None else ()
        return (
            self.merchant_webhook,
            self.approval,
            self.payment_agent_token,
            self.payment_merchant_token,
            self.payment_token_secret,
            self.auth_secret,
            self.agent_ap2_pem,
            self.merchant_ap2_pem,
            self.payment_ap2_pem,
            *d1,
            *hnp,
        )


def env_value(raw: str) -> str:
    """A .env value as the agent's Settings reads it: a quoted one up to its closing quote,
    an unquoted one without its inline ` # comment`."""
    value = raw.strip()
    if value[:1] in {'"', "'"}:
        end = value.find(value[0], 1)
        if end > 0:
            return value[1:end]
    return INLINE_COMMENT.sub("", value)


def read_model_settings(path: Path) -> dict[str, str]:
    """The model settings of the agent's .env (KEY=VALUE lines); nothing else is read."""
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.removeprefix("export ").split("=", 1)
        name, value = name.strip(), env_value(value)
        if name in MODEL_SETTINGS and value:
            found[name] = value
    return found


def base_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The presenter's environment without any service setting, credential or virtualenv."""
    environment = {
        name: value
        for name, value in (os.environ if source is None else source).items()
        if not name.startswith(INHERITED_PREFIXES) and name not in INHERITED_NAMES
    }
    environment.update(
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
        LANGSMITH_TRACING="false",
    )
    return environment


def mode_environment(
    mode: DemoMode, merchant_origin: str, keys: D1RunKeys | None
) -> dict[str, str]:
    """The pilot's condition variables with the b2-hp-v2 policy (POL-11 included); pol02-deny
    only swaps the POL-02 allowlist."""
    allowed = merchant_origin if mode.allows_merchant else OTHER_MERCHANT
    return agent_environment(mode.condition, allowed, keys, HP_V2_POLICY)


def hnp_environment(keys: HnpKeys, selector: str, reviewer: str = "off") -> dict[str, str]:
    """HNP on the agent (config.py, delegation/bootstrap.py); the rest is B2's environment."""
    return {
        "AGENT_HNP_ENABLED": "true",
        "AGENT_HNP_AGENT_KEY_PEM": keys.agent_key_pem,
        "AGENT_HNP_SERVICE_TOKEN": keys.service_token,
        "AGENT_HNP_OPERATOR_TOKEN": keys.operator_token,
        "AGENT_HNP_SELECTOR": selector,
        "AGENT_HNP_REVIEWER": reviewer,
    }


@dataclass(frozen=True)
class Service:
    name: str
    label: str
    command: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(repr=False)
    origin: str
    ready_path: str
    # The readiness answer must be JSON with status "ok" (Python services).
    expects_ok: bool

    @property
    def ready_url(self) -> str:
        return self.origin + self.ready_path

    @property
    def log(self) -> str:
        return f"{self.name}.log"


@dataclass(frozen=True)
class DemoPlan:
    mode: DemoMode
    ports: Ports
    run_dir: Path
    merchant_repo: Path
    payment_repo: Path
    env_file: Path
    secrets: StackSecrets = field(repr=False)
    model: Mapping[str, str] = field(repr=False)
    skip_postgres_start: bool = False
    keep_offer: bool = False
    base: Mapping[str, str] = field(default_factory=base_environment, repr=False)
    # b2-hnp only.
    hnp_selector: str = "llm"
    hnp_trigger_delay: float = HNP_TRIGGER_DELAY_SECONDS
    hnp_reviewer: str = "off"

    @property
    def merchant_origin(self) -> str:
        return self.ports.origin("merchant")

    @property
    def ui_origin(self) -> str:
        return self.ports.origin("ui")

    def secret_values(self, *extra: str) -> tuple[str, ...]:
        model_secret = self.model.get("ANTHROPIC_API_KEY", "")
        return (*self.secrets.values(), model_secret, *extra)

    def mode_environment(self) -> dict[str, str]:
        environment = mode_environment(self.mode, self.merchant_origin, self.secrets.d1)
        if not self.mode.hnp:
            return environment
        if self.secrets.hnp is None:
            raise DemoError(f"El modo {self.mode.name} necesita las credenciales HNP del arranque")
        return environment | hnp_environment(self.secrets.hnp, self.hnp_selector, self.hnp_reviewer)

    def hnp_line(self) -> str:
        reviewer = " · revisor llm" if self.hnp_reviewer == "llm" else ""
        return (
            f"selector {self.hnp_selector}{reviewer} · disparo automático a los "
            f"{self.hnp_trigger_delay:g} s"
        )

    def warnings(self) -> list[str]:
        """What differs from the evaluation's setup without stopping the demo."""
        model = self.model.get("AGENT_MODEL")
        if model is None or model == SUITE_MODEL:
            return []
        return [f"AGENT_MODEL={model} no es el modelo de la evaluación ({SUITE_MODEL})"]

    def setup_steps(self) -> list[tuple[str, tuple[str, ...]]]:
        steps: list[tuple[str, tuple[str, ...]]] = []
        if not self.skip_postgres_start:
            steps.append(
                ("PostgreSQL del comercio", ("docker", "compose", "up", "-d", "--wait", "postgres"))
            )
        steps.append(
            (
                "Migraciones del comercio",
                merchant_cli(self.merchant_repo, "commerce_lab.db", "migrate"),
            )
        )
        return steps

    def step_context(self, command: Sequence[str]) -> tuple[Path, dict[str, str]]:
        """Where and with what a merchant step runs. docker compose needs the merchant's checkout
        (its compose.yaml), with the published port named so the checkout's .env cannot move it;
        every merchant module runs from the launch directory with its database named."""
        if tuple(command[:2]) == ("docker", "compose"):
            return self.merchant_repo, dict(self.base) | {"MERCHANT_PG_PORT": MERCHANT_PG_PORT}
        return self.run_dir, merchant_cli_environment(self.base)

    def import_catalog(self) -> tuple[str, ...]:
        return merchant_cli(self.merchant_repo, "commerce_lab.managed_catalog", "import-current")

    def reset_offer(self) -> tuple[str, ...]:
        return update_offer_command(self.merchant_repo, OFFER_PRICE_MINOR, stock=OFFER_STOCK)

    def issue_session(self, catalog_run_id: str) -> tuple[str, ...]:
        return merchant_cli(
            self.merchant_repo,
            "commerce_lab.db",
            "issue-session",
            catalog_run_id,
            MANAGED_ACTOR,
            "--ttl-seconds",
            str(SESSION_TTL_SECONDS),
        )

    def payment_env(self) -> dict[str, str]:
        keys = self.secrets
        return dict(self.base) | {
            "PAYMENT_DATABASE_PATH": str(self.run_dir / "payments.db"),
            "PAYMENT_AGENT_BEARER_TOKEN": keys.payment_agent_token,
            "PAYMENT_MERCHANT_BEARER_TOKEN": keys.payment_merchant_token,
            "PAYMENT_TOKEN_SECRET": keys.payment_token_secret,
            "PAYMENT_MERCHANT_ID": PAYMENT_MERCHANT_ID,
            "PAYMENT_AP2_REQUIRED": "true",
            "PAYMENT_AP2_AGENT_JWK_JSON": keys.agent_jwk,
            "PAYMENT_AP2_MERCHANT_JWK_JSON": keys.merchant_jwk,
            "PAYMENT_AP2_MERCHANT_ISSUER": self.merchant_origin,
            "PAYMENT_AP2_PRIVATE_KEY_PEM": keys.payment_ap2_pem,
        }

    def webhook_env(self) -> dict[str, str]:
        """Only what the merchant's dispatcher reads: its database, the agent's origin without
        the webhook path, and the secret the agent verifies."""
        return merchant_cli_environment(self.base) | {
            "WEBHOOK_RECEIVER_URL": self.ports.origin("agent"),
            "MERCHANT_WEBHOOK_SECRET": self.secrets.merchant_webhook,
        }

    def dispatch_webhooks(self) -> tuple[str, ...]:
        return merchant_cli(
            self.merchant_repo, "commerce_lab.db", "webhook-dispatch", "--limit", str(WEBHOOK_BATCH)
        )

    def merchant_env(self) -> dict[str, str]:
        keys = self.secrets
        return self.webhook_env() | {
            "ACP_API_BASE_URL": self.merchant_origin,
            "PAYMENT_PROVIDER_URL": self.ports.origin("payment"),
            "PAYMENT_MERCHANT_BEARER_TOKEN": keys.payment_merchant_token,
            "PAYMENT_MERCHANT_ID": PAYMENT_MERCHANT_ID,
            "MERCHANT_AP2_REQUIRED": "true",
            "MERCHANT_AP2_PRIVATE_KEY_PEM": keys.merchant_ap2_pem,
            "MERCHANT_AP2_AGENT_JWK_JSON": keys.agent_jwk,
        }

    def auth_env(self) -> dict[str, str]:
        # The UI origin is the issuer: Vite forwards /api/auth to this service.
        return dict(self.base) | {
            "BETTER_AUTH_URL": self.ui_origin,
            "BETTER_AUTH_SECRET": self.secrets.auth_secret,
            "AUTH_DATABASE_PATH": str(self.run_dir / "auth.db"),
            "PORT": str(self.ports.auth),
        }

    def agent_env(self, merchant_token: str) -> dict[str, str]:
        keys = self.secrets
        return (
            dict(self.base)
            | dict(self.model)
            | {
                "MERCHANT_BASE_URL": self.merchant_origin,
                "MERCHANT_BEARER_TOKEN": merchant_token,
                "MERCHANT_WEBHOOK_SECRET": keys.merchant_webhook,
                "AGENT_APPROVAL_SECRET": keys.approval,
                "AGENT_AP2_REQUIRED": "true",
                "AGENT_AP2_PRIVATE_KEY_PEM": keys.agent_ap2_pem,
                "PAYMENT_PROVIDER_URL": self.ports.origin("payment"),
                "PAYMENT_AGENT_BEARER_TOKEN": keys.payment_agent_token,
                # A new database per launch: an `uncertain` purchase blocks new ones.
                "AGENT_DATABASE_PATH": str(self.run_dir / "agent.db"),
                "AGENT_FEED_SOURCE": "merchant",
                "AGENT_AUTH_MODE": "jwt",
                "AGENT_AUTH_ISSUER": self.ui_origin,
                "AGENT_UI_ORIGIN": self.ui_origin,
            }
            | self.mode_environment()
        )

    def ui_env(self) -> dict[str, str]:
        return dict(self.base) | {
            "AUTH_DEV_UPSTREAM": self.ports.origin("auth"),
            "VITE_AGENT_API_URL": self.ports.origin("agent"),
        }

    def services(self, merchant_token: str) -> list[Service]:
        """In start order; each one only needs the ones before it."""
        ports = self.ports
        payment_app = "tesis_payment_provider.api:app_factory"
        vite = ("node", "node_modules/vite/bin/vite.js", "--strictPort")
        return [
            Service(
                "payment",
                "Proveedor de pagos",
                (
                    *uv_project(self.payment_repo),
                    *uvicorn(payment_app, ports.payment, factory=True),
                ),
                # The run directory, never the checkout: no service reads a developer .env.
                self.run_dir,
                self.payment_env(),
                ports.origin("payment"),
                "/health",
                expects_ok=True,
            ),
            Service(
                "merchant",
                "Comercio",
                (
                    *uv_project(self.merchant_repo),
                    *uvicorn("commerce_lab.api:app", ports.merchant, factory=False),
                ),
                self.run_dir,
                self.merchant_env(),
                self.merchant_origin,
                "/health/ready",
                expects_ok=True,
            ),
            Service(
                "auth",
                "Auth",
                ("node", "server.js"),
                AUTH,
                self.auth_env(),
                ports.origin("auth"),
                "/api/auth/jwks",
                expects_ok=False,
            ),
            Service(
                "agent",
                "Agente",
                (sys.executable, "-m", *uvicorn(AGENT_APP, ports.agent, factory=True)),
                # The run directory, never the checkout: the agent reads no developer .env.
                self.run_dir,
                self.agent_env(merchant_token),
                ports.origin("agent"),
                "/health",
                expects_ok=True,
            ),
            Service(
                "ui",
                "UI",
                (*vite, "--host", "127.0.0.1", "--port", str(ports.ui)),
                UI,
                self.ui_env(),
                self.ui_origin,
                "/",
                expects_ok=False,
            ),
        ]


def uvicorn(app: str, port: int, *, factory: bool) -> tuple[str, ...]:
    return (
        "uvicorn",
        app,
        *(("--factory",) if factory else ()),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    )


def uv_project(project: Path) -> tuple[str, ...]:
    """`uv run` under a sibling's interpreter and lock, from whatever directory the caller
    gives the process; uv itself reads no .env."""
    return ("uv", "run", "--frozen", "--project", str(project))


def merchant_cli(merchant_repo: Path, *arguments: str) -> tuple[str, ...]:
    """A merchant module under its interpreter; the caller runs it outside the checkout."""
    return (*uv_project(merchant_repo), "python", "-m", *arguments)


def merchant_cli_environment(base: Mapping[str, str]) -> dict[str, str]:
    """What every merchant module reads besides its defaults: its database."""
    return dict(base) | {"DATABASE_URL": MERCHANT_DATABASE_URL}


def update_offer_command(
    merchant_repo: Path, price_minor: int, *, stock: int | None = None
) -> tuple[str, ...]:
    """The merchant's managed-catalog edit (`changed_offer`): new revision, same offer id."""
    command = merchant_cli(
        merchant_repo,
        "commerce_lab.managed_catalog",
        "update-offer",
        OFFER_ID,
        "--price-minor",
        str(price_minor),
    )
    return command if stock is None else (*command, "--stock", str(stock))


def offer_summary(offer: Mapping[str, Any]) -> str:
    """One line from `update-offer`'s JSON: price without shipping, stock and revision."""
    try:
        price = format_minor_amount(int(offer["price_minor"]), "COP")
        return (
            f"{OFFER_TITLE} ({offer['offer_id']}) a {price} más envío, {offer['stock']} "
            f"unidades (revisión {offer['revision']})"
        )
    except (KeyError, TypeError, ValueError):
        raise DemoError("El comercio respondió con una oferta inesperada") from None


def redact(text: str, values: Iterable[str]) -> str:
    """Remove every secret, also JSON-escaped and line by line for multi-line keys, and every
    secret shape, PostgreSQL URLs among them: a merchant step that fails to connect prints its
    DSN, password included (like commerce_lab.db). The demo marks it in Spanish."""
    return redact_secrets(text, values, marker=REDACTED)


def tail(text: str, values: Iterable[str], lines: int = 30) -> str:
    """The last lines of a step's output, redacted."""
    return redact("\n".join(text.strip().splitlines()[-lines:]), values)


def port_in_use(port: int) -> bool:
    """A listener answers there, or the port cannot be bound. Like uvicorn and node, the bind
    uses SO_REUSEADDR, so connections in TIME_WAIT from the previous launch do not count."""
    with socket.socket() as probe:
        probe.settimeout(0.3)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return True
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def docker_responds() -> bool:
    """Whether the Docker daemon answers, not only whether its CLI is installed: `docker compose
    up` needs it, and a Docker Desktop that is closed leaves the CLI in the PATH."""
    try:
        result = subprocess.run(
            ("docker", "info", "--format", "{{.ServerVersion}}"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=DOCKER_INFO_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def hnp_problems(plan: DemoPlan) -> list[str]:
    """Whether the agent's HNP startup accepts this launch (`load_hnp_trust` and the enforce D1
    configuration), checked like the HNP pilot's dry run: nothing starts and nothing is written."""
    if not plan.mode.hnp:
        return []
    check = startup_check(plan.agent_env(REDACTED), {})
    if check["ok"]:
        return []
    return [f"HNP: el agente no arrancaría ({redact(str(check['error']), plan.secret_values())})"]


def due_hnp_tasks(database: Path, delay: float, now: datetime) -> list[str]:
    """The `scheduled` tasks created at least `delay` seconds before `now`, read from the agent's
    database in read-only mode. None while the agent has not created it."""
    if not database.is_file():
        return []
    cutoff = utc_text(now - timedelta(seconds=delay))
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as connection:
        rows = connection.execute(HNP_DUE_TASKS, (HNP_SCHEDULED, cutoff)).fetchall()
    return [str(task_id) for (task_id,) in rows]


def preflight(plan: DemoPlan) -> list[str]:
    """Why the stack cannot start here; empty when it can. Names only, never values."""
    problems = [
        f"{label}: {path} no es un checkout ({marker} no existe)"
        for label, path, marker in (
            ("--merchant-repo", plan.merchant_repo, "src/commerce_lab"),
            ("--payment-repo", plan.payment_repo, "src/tesis_payment_provider"),
        )
        if not (path / marker).is_dir()
    ]
    tools = ("uv", "node") if plan.skip_postgres_start else ("uv", "node", "docker")
    problems += [f"{tool} no está en el PATH" for tool in tools if shutil.which(tool) is None]
    if "docker" in tools and shutil.which("docker") is not None and not docker_responds():
        problems.append(
            "Docker está instalado pero no responde: abre Docker Desktop (o arranca el daemon) y "
            "vuelve a intentarlo"
        )
    problems += [
        f"falta `npm ci` en {directory.name}/"
        for directory in (AUTH, UI)
        if not (directory / "node_modules").is_dir()
    ]
    problems += [
        f"falta {name} en {plan.env_file}"
        for name in REQUIRED_MODEL_SETTINGS
        if name not in plan.model
    ]
    named = plan.ports.named()
    if len(set(named.values())) != len(named):
        problems.append("los puertos deben ser distintos")
    problems += [
        f"el puerto {port} ({name}) está ocupado"
        for name, port in named.items()
        if port_in_use(port)
    ]
    return problems + hnp_problems(plan)


class Process(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class Launcher:
    """Runs a plan. Subprocesses and HTTP go through five methods tests replace."""

    def __init__(self, plan: DemoPlan, out: TextIO | None = None) -> None:
        self.plan = plan
        self.out = out or sys.stdout
        self.running: list[tuple[Service, Process]] = []
        self.health: dict[str, str] | None = None
        self._relay_down = False
        self._dispatcher: subprocess.Popen[str] | None = None
        # Tasks the HNP trigger already ran: each one once, whatever the answer.
        self._hnp_triggered: set[str] = set()
        self._hnp_unreadable = False
        self._secrets = list(plan.secret_values())
        plan.run_dir.mkdir(parents=True, exist_ok=True)
        self._log = (plan.run_dir / "launcher.log").open("a", encoding="utf-8")

    # Replaceable boundaries.
    def _run(
        self, command: Sequence[str], cwd: Path, env: Mapping[str, str]
    ) -> tuple[int, str, str]:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr

    def _dispatch(
        self,
        command: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout: float = WEBHOOK_DISPATCH_TIMEOUT_SECONDS,
    ) -> tuple[int, str, str]:
        """The merchant's dispatcher in its own process group, bounded by `timeout`: past it, or
        on Ctrl-C, the whole group is killed (`uv run` leaves a python child) and
        TimeoutExpired or the interruption propagates."""
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self._dispatcher = process
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return process.returncode, stdout, stderr
        finally:
            self._kill_dispatcher()

    def _kill_dispatcher(self) -> None:
        """SIGKILL to the group of a dispatcher still in flight, and reap it. One that finished
        was reaped already: its group id may belong to someone else by now."""
        process, self._dispatcher = self._dispatcher, None
        if process is None or process.returncode is not None:
            return
        self._signal(process, signal.SIGKILL)
        # Its pipes close once the group is gone; a process that escaped the group is not waited.
        with suppress(subprocess.TimeoutExpired, OSError, ValueError):
            process.communicate(timeout=STOP_TIMEOUT_SECONDS)

    def _spawn(self, service: Service) -> Process:
        with (self.plan.run_dir / service.log).open("ab") as log:
            return subprocess.Popen(
                list(service.command),
                cwd=service.cwd,
                env=dict(service.env),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                # Its own process group: Ctrl-C reaches only this launcher, which stops them all.
                start_new_session=True,
            )

    def _probe(self, url: str) -> tuple[int, Any]:
        try:
            with httpx.Client(trust_env=False, timeout=2) as client:
                response = client.get(url)
        except httpx.HTTPError:
            return 0, None
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None

    def _post(self, url: str, token: str) -> tuple[int, Any]:
        """A bearer POST without Origin: the agent's internal routes refuse a browser's."""
        with httpx.Client(trust_env=False, timeout=HNP_TRIGGER_TIMEOUT_SECONDS) as client:
            response = client.post(url, headers={"Authorization": f"Bearer {token}"})
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None

    def _signal(self, process: Process, signum: int) -> None:
        # macOS answers EPERM for a group whose processes are all zombies: it is gone as well.
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signum)

    def _group_alive(self, process: Process) -> bool:
        """Whether any process of the service's group still runs, its leader or not."""
        try:
            os.killpg(process.pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    # Output.
    def say(self, message: str = "") -> None:
        # The log first: it still records the line when the terminal is already gone.
        text = redact(message, self._secrets)
        self._log.write(text + "\n")
        self._log.flush()
        print(text, file=self.out, flush=True)

    def _tail(self, text: str, lines: int = 30) -> str:
        return tail(text, self._secrets, lines)

    # Steps.
    def command(self, label: str, command: Sequence[str], *, json_output: bool = False) -> Any:
        """A one-shot merchant step; its stdout is parsed, never printed or logged."""
        code, stdout, stderr = self._run(command, *self.plan.step_context(command))
        result: Any = None
        if code == 0 and json_output:
            try:
                result = json.loads(stdout.strip().splitlines()[-1])
            except (IndexError, json.JSONDecodeError):
                result = {}
            # A credential in the answer is redacted before anything else is written.
            if isinstance(result, dict) and isinstance(result.get("session_token"), str):
                self._secrets.append(result["session_token"])
        with (self.plan.run_dir / "setup.log").open("a", encoding="utf-8") as log:
            log.write(f"$ {label}\n{redact(stderr, self._secrets)}\n")
        if code != 0:
            raise DemoError(f"{label} falló (código {code}):\n{self._tail(stderr)}")
        if json_output and not (isinstance(result, dict) and result):
            raise DemoError(f"{label} no devolvió un objeto JSON")
        return result

    def prepare_merchant(self) -> str:
        """PostgreSQL, migrations, managed catalog, scripted offer; returns the agent's session."""
        for label, command in self.plan.setup_steps():
            self.command(label, command)
            self.say(f"· {label}: listo")
        catalog = self.command(
            "Catálogo administrado", self.plan.import_catalog(), json_output=True
        )
        run_id = catalog.get("run_id")
        if not isinstance(run_id, str):
            raise DemoError("El catálogo administrado no devolvió un run_id")
        status = "importado" if catalog.get("status") == "imported" else "ya estaba importado"
        self.say(f"· Catálogo administrado: {status}")
        if not self.plan.keep_offer:
            offer = self.command("Oferta del guion", self.plan.reset_offer(), json_output=True)
            self.say(f"· Oferta del guion: {offer_summary(offer)}")
        session = self.command(
            "Sesión del agente en el comercio", self.plan.issue_session(run_id), json_output=True
        )
        token = session.get("session_token")
        if not isinstance(token, str) or not token:
            raise DemoError("El comercio no emitió la sesión del agente")
        self.say(f"· Sesión del agente en el comercio: emitida ({SESSION_TTL_SECONDS // 3600} h)")
        return token

    def start_service(self, service: Service) -> Any:
        process = self._spawn(service)
        self.running.append((service, process))
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while True:
            if process.poll() is not None:
                raise DemoError(
                    f"{service.label} se detuvo antes de estar listo. Log: "
                    f"{self.plan.run_dir / service.log}\n{self.service_tail(service)}"
                )
            status, body = self._probe(service.ready_url)
            if status == 200 and (
                not service.expects_ok or (isinstance(body, dict) and body.get("status") == "ok")
            ):
                self.say(f"· {service.label}: listo")
                return body
            if time.monotonic() > deadline:
                raise DemoError(
                    f"{service.label} no respondió en {READY_TIMEOUT_SECONDS:.0f} s: "
                    f"{service.ready_url}\n{self.service_tail(service)}"
                )
            time.sleep(0.3)

    def service_tail(self, service: Service) -> str:
        path = self.plan.run_dir / service.log
        if not path.is_file():
            return ""
        return self._tail(path.read_text(encoding="utf-8", errors="replace"))

    def start(self) -> dict[str, str]:
        plan = self.plan
        self.say(f"Demo local · modo {plan.mode.name} · {plan.mode.description}")
        self.say(f"Directorio del arranque: {plan.run_dir}")
        for warning in plan.warnings():
            self.say(f"Aviso: {warning}")
        token = self.prepare_merchant()
        for service in plan.services(token):
            health = self.start_service(service)
            if service.name == "agent":
                # Stop here if the agent does not run the requested condition.
                try:
                    self.health = verify_agent_condition(plan.mode.condition, health)
                except ConditionMismatchError as error:
                    raise DemoError(str(error)) from None
                if plan.mode.hnp:
                    verify_hnp_health(health, plan.hnp_reviewer)
        assert self.health is not None
        self.summary()
        return self.health

    def summary(self) -> None:
        plan, health = self.plan, self.health or {}
        self.say()
        self.say(
            f"Modo efectivo (de /health): governance_mode={health.get('governance_mode')} · "
            f"hp_flow={health.get('hp_flow')}"
        )
        environment = plan.mode_environment()
        allowlist = environment.get("AGENT_D1_ALLOWED_MERCHANT_ORIGINS")
        if allowlist is not None:
            excluded = "" if plan.mode.allows_merchant else " (excluye al comercio de la demo)"
            self.say(f"Comercios permitidos por POL-02: {allowlist}{excluded}")
            self.say(
                f"Política D1: {environment['AGENT_D1_POLICY_SET_ID']} · "
                f"revisión {environment['AGENT_D1_POLICY_REVISION']}"
            )
        if plan.mode.hnp:
            self.say(f"HNP: activo · {plan.hnp_line()} (hnp.log)")
        self.say(f"UI:    {plan.ui_origin}")
        self.say(f"Auth:  {plan.ports.origin('auth')}")
        self.say(f"Logs:  {plan.run_dir}")
        self.say(
            f"Webhooks del comercio: se entregan al agente cada {WEBHOOK_RELAY_SECONDS:.0f} s "
            "(webhooks.log)"
        )
        self.say("Ctrl-C apaga todo. PostgreSQL sigue corriendo con sus datos.")

    def watch(self, interval: float = WEBHOOK_RELAY_SECONDS) -> NoReturn:
        """Until Ctrl-C, or until a service stops on its own; relays webhooks meanwhile."""
        while True:
            for service, process in self.running:
                code = process.poll()
                if code is not None:
                    raise DemoError(
                        f"{service.label} se detuvo (código {code}). Log: "
                        f"{self.plan.run_dir / service.log}\n{self.service_tail(service)}"
                    )
            try:
                self.relay_webhooks()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                # Nothing the relay does (launching, its output, its log, the terminal) may stop
                # the demo or delay noticing a service that stopped.
                self._relay_failed(f"error: {type(error).__name__}", str(error))
            if self.plan.mode.hnp:
                try:
                    self.trigger_hnp_tasks()
                except Exception as error:
                    # Nothing the trigger does, not even an unexpected error, stops the demo.
                    self._hnp_log(f"error del disparo: {type(error).__name__}")
            time.sleep(interval)

    def relay_webhooks(self) -> list[dict[str, Any]]:
        """One pass of the merchant's dispatcher. A failure is announced once, never fatal: the
        purchase does not depend on it, only the trace's «El comercio registró la orden»."""
        plan = self.plan
        try:
            code, stdout, stderr = self._dispatch(
                plan.dispatch_webhooks(), plan.run_dir, plan.webhook_env()
            )
        except subprocess.TimeoutExpired:
            self._relay_failed(
                f"no respondió en {WEBHOOK_DISPATCH_TIMEOUT_SECONDS:.0f} s y se detuvo", ""
            )
            return []
        except OSError as error:
            self._relay_failed(f"no se pudo lanzar: {type(error).__name__}", str(error))
            return []
        deliveries = _deliveries(stdout) if code == 0 else None
        if deliveries is None:
            self._relay_failed(f"código {code}", stderr)
            return []
        with suppress(OSError), (plan.run_dir / "webhooks.log").open("a", encoding="utf-8") as out:
            for item in deliveries:
                out.write(
                    f"{datetime.now(UTC).isoformat()} {item.get('delivery_id')} "
                    f"{item.get('outcome')} http={item.get('http_status')} "
                    f"error={item.get('error_code')}\n"
                )
        if self._relay_down:
            self.say("· Webhooks del comercio: entrega restablecida")
            self._relay_down = False
        if deliveries:
            self.say(f"· Webhooks del comercio: {_delivery_summary(deliveries)}")
        return deliveries

    def _relay_failed(self, reason: str, detail: str) -> None:
        """Logged every time, announced once until a pass succeeds again."""
        log = self.plan.run_dir / "webhooks.log"
        with suppress(OSError), log.open("a", encoding="utf-8") as output:
            output.write(f"$ webhook-dispatch ({reason})\n{self._tail(detail)}\n")
        if not self._relay_down:
            self._relay_down = True
            # Not even a terminal that is gone may stop the demo from here.
            with suppress(OSError, ValueError):
                self.say(
                    f"Aviso: el comercio no pudo entregar sus webhooks al agente ({reason}). "
                    f"La compra sigue; detalle en {log}"
                )

    def trigger_hnp_tasks(self, now: datetime | None = None) -> list[str]:
        """One pass of the HNP trigger: each task still `scheduled` `hnp_trigger_delay` seconds
        after the consent is run once, never again, whatever the answer. Returns the tasks run
        in this pass. Every attempt goes to hnp.log; a failure is announced, never fatal."""
        plan = self.plan
        if not plan.mode.hnp or plan.secrets.hnp is None:
            return []
        try:
            due = due_hnp_tasks(
                plan.run_dir / "agent.db", plan.hnp_trigger_delay, now or datetime.now(UTC)
            )
        except sqlite3.Error as error:
            self._hnp_log(f"lectura de agent.db: {type(error).__name__}: {error}")
            if not self._hnp_unreadable:
                self._hnp_unreadable = True
                self.say(
                    "Aviso: el lanzador no pudo leer las tareas HNP en agent.db "
                    f"({type(error).__name__}); sigue intentando. Detalle en {self._hnp_path}"
                )
            return []
        if self._hnp_unreadable:
            self._hnp_unreadable = False
            self.say("· HNP: lectura de las tareas restablecida")
        pending = [task_id for task_id in due if task_id not in self._hnp_triggered]
        for task_id in pending:
            # Recorded before the request: a task that fails here is never retried in a loop.
            self._hnp_triggered.add(task_id)
            self._trigger_hnp_task(task_id, plan.secrets.hnp.service_token)
        return pending

    @property
    def _hnp_path(self) -> Path:
        return self.plan.run_dir / "hnp.log"

    def _hnp_log(self, line: str) -> None:
        with suppress(OSError), self._hnp_path.open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now(UTC).isoformat()} {redact(line, self._secrets)}\n")

    def _trigger_hnp_task(self, task_id: str, token: str) -> None:
        url = self.plan.ports.origin("agent") + HNP_RUN_ROUTE.format(
            task_id=quote(task_id, safe="")
        )
        try:
            status, body = self._post(url, token)
        except httpx.HTTPError as error:
            self._hnp_log(f"{task_id} error={type(error).__name__}: {error}")
            self.say(
                f"Aviso: la tarea HNP {task_id} no se pudo disparar ({type(error).__name__}); no "
                f"se reintenta. Detalle en {self._hnp_path}"
            )
            return
        answer = body if isinstance(body, dict) else {}
        state, code = answer.get("state"), answer.get("code")
        self._hnp_log(
            f"{task_id} http={status} state={state} outcome={answer.get('outcome_code')} "
            f"code={code}"
        )
        if status in (200, 202):
            self.say(f"· HNP: tarea {task_id} disparada (HTTP {status}, {state})")
        else:
            self.say(
                f"Aviso: la tarea HNP {task_id} no se pudo disparar (HTTP {status}, {code}); no "
                f"se reintenta. Detalle en {self._hnp_path}"
            )

    def stop(self) -> None:
        """SIGTERM to every group first; when every group is gone or the deadline passes,
        SIGKILL to every group that got SIGTERM. Nothing it prints can stop it: after SIGHUP
        the terminal is gone."""
        # A second Ctrl-C, SIGTERM or SIGHUP must not leave services behind.
        previous = {signum: signal.signal(signum, signal.SIG_IGN) for signum in STOP_SIGNALS}
        try:
            # A dispatcher pass still in flight leaves no orphan behind.
            self._kill_dispatcher()
            running = [(s, p) for s, p in reversed(self.running) if p.poll() is None]
            for _, process in running:
                self._signal(process, signal.SIGTERM)
            deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
            for _, process in running:
                _exited(process, deadline - time.monotonic())
            # A leader can exit on SIGTERM while a child it started (uv run, npm run) ignores
            # it, so the wait is for the whole group, not only for the process we started.
            while time.monotonic() < deadline and any(
                self._group_alive(process) for _, process in running
            ):
                time.sleep(GROUP_POLL_SECONDS)
            # _signal skips a group that no longer exists.
            for _, process in running:
                self._signal(process, signal.SIGKILL)
            alive = {
                process.pid for _, process in running if not _exited(process, STOP_TIMEOUT_SECONDS)
            }
            for service, process in reversed(self.running):
                state = f"no se detuvo (pid {process.pid})" if process.pid in alive else "apagado"
                with suppress(OSError, ValueError):
                    self.say(f"· {service.label}: {state}")
            self.running.clear()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

    def close(self) -> None:
        self._log.close()


def verify_hnp_health(health: Mapping[str, Any], reviewer: str = "off") -> None:
    """b2-hnp needs the agent to report HNP on and its kill switch off, and the intent reviewer
    the launch asked for (an agent that does not report it has none)."""
    enabled, kill_switch = health.get("hnp_enabled"), health.get("hnp_kill_switch")
    if enabled is not True or kill_switch is not False:
        raise DemoError(
            f"El agente reporta en /health hnp_enabled={enabled} y hnp_kill_switch={kill_switch}, "
            "pero el modo b2-hnp exige hnp_enabled=True y hnp_kill_switch=False"
        )
    reported = health.get("hnp_reviewer", "off")
    if reported != reviewer:
        raise DemoError(
            f"El agente reporta en /health hnp_reviewer={reported}, pero el arranque pidió "
            f"--hnp-reviewer {reviewer}"
        )


def _deliveries(stdout: str) -> list[dict[str, Any]] | None:
    """`webhook-dispatch` prints one JSON list of delivery results; anything else is a failure."""
    try:
        value = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return None
    return value


def _delivery_summary(deliveries: Sequence[Mapping[str, Any]]) -> str:
    delivered = sum(item.get("outcome") == "delivered" for item in deliveries)
    failed = [item for item in deliveries if item.get("outcome") != "delivered"]
    summary = f"{delivered} entregado{'s' if delivered != 1 else ''} al agente"
    if failed:
        codes = ", ".join(
            sorted({str(item.get("error_code") or item.get("outcome")) for item in failed})
        )
        summary += f", {len(failed)} sin entregar ({codes})"
    return summary


def _exited(process: Process, timeout: float) -> bool:
    try:
        process.wait(timeout=max(timeout, 0.0))
    except subprocess.TimeoutExpired:
        return False
    return True


def _interrupt(signum: int, frame: object) -> None:
    # From the first signal on, another Ctrl-C, SIGTERM or SIGHUP is ignored. Otherwise one
    # that arrives before stop() ignores them ends the launcher with a traceback (exit 130).
    for stop_signal in STOP_SIGNALS:
        signal.signal(stop_signal, signal.SIG_IGN)
    raise KeyboardInterrupt


def _up_parser() -> argparse.ArgumentParser:
    defaults = Ports()
    parser = argparse.ArgumentParser(
        prog="demo-stack",
        description=(
            "Levanta en local todo lo que necesita la demo: el comercio con su PostgreSQL, el "
            "proveedor de pagos, el servicio de cuentas, el agente y la interfaz web. Los pasos "
            "están en el README («Probar la demo»)."
        ),
        epilog=(
            "Con la demo corriendo, `uv run demo-stack s04` en otra terminal sube el precio del "
            "teléfono de la demo después de que el agente mostró los términos, para ver que la "
            "compra no sigue con un precio distinto; `uv run demo-stack s04 --restore` lo "
            "devuelve. Detalles: `uv run demo-stack s04 --help`."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=tuple(MODES),
        default="b2",
        help=(
            "cómo compra el agente: b2, con gobernanza (por defecto); b0, el mismo agente sin "
            "gobernanza; pol02-deny, con gobernanza y una lista de comercios permitidos que deja "
            "fuera al de la demo, así que la política niega la compra antes de crear el "
            "checkout; b2-hnp, con gobernanza y además compra delegada: la persona deja "
            "autorizada una compra y el agente la hace después, sin que ella esté presente"
        ),
    )
    parser.add_argument("--merchant-port", type=int, default=defaults.merchant)
    parser.add_argument("--payment-port", type=int, default=defaults.payment)
    parser.add_argument("--agent-port", type=int, default=defaults.agent)
    parser.add_argument("--auth-port", type=int, default=defaults.auth)
    parser.add_argument("--ui-port", type=int, default=defaults.ui)
    _merchant_argument(parser)
    parser.add_argument(
        "--payment-repo", type=Path, default=ROOT.parent / "tesis-payment-provider-sandbox"
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / ".env",
        help=(
            "de aquí solo se leen ANTHROPIC_API_KEY y las variables AGENT_MODEL* del modelo, "
            "solo para el agente"
        ),
    )
    parser.add_argument(
        "--skip-postgres-start",
        action="store_true",
        help="usar el PostgreSQL del comercio que ya está corriendo",
    )
    parser.add_argument(
        "--keep-offer",
        action="store_true",
        help=f"no restaurar el precio y el stock de {OFFER_ID} al arrancar",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="mostrar el plan y las comprobaciones sin arrancar nada",
    )
    parser.add_argument(
        "--hnp-selector",
        choices=HNP_SELECTORS,
        help=(
            "solo con --mode b2-hnp: quién elige la variante de una compra delegada (por defecto "
            "llm, con el modelo; deterministic es el respaldo si falla el modelo)"
        ),
    )
    parser.add_argument(
        "--hnp-reviewer",
        choices=HNP_REVIEWERS,
        help=(
            "solo con --mode b2-hnp: llm agrega el revisor de intención, que revisa la elección "
            "contra lo que pidió el comprador y escala si no la confirma (por defecto off)"
        ),
    )
    parser.add_argument(
        "--hnp-trigger-delay",
        type=float,
        metavar="SEGUNDOS",
        help=(
            "solo con --mode b2-hnp: segundos entre la autorización de la delegación y el "
            "momento en que el lanzador ejecuta la compra "
            f"(por defecto {HNP_TRIGGER_DELAY_SECONDS:g})"
        ),
    )
    return parser


def _merchant_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--merchant-repo", type=Path, default=ROOT.parent / "tesis-commerce-merchant"
    )


def build_plan(arguments: argparse.Namespace, *, now: datetime | None = None) -> DemoPlan:
    mode = MODES[arguments.mode]
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return DemoPlan(
        mode=mode,
        ports=Ports(
            merchant=arguments.merchant_port,
            payment=arguments.payment_port,
            agent=arguments.agent_port,
            auth=arguments.auth_port,
            ui=arguments.ui_port,
        ),
        run_dir=DEMO_RUNS / stamp,
        merchant_repo=arguments.merchant_repo.expanduser().resolve(),
        payment_repo=arguments.payment_repo.expanduser().resolve(),
        env_file=arguments.env_file.expanduser().resolve(),
        secrets=StackSecrets.generate(
            governed=mode.condition.governance_mode != "off", hnp=mode.hnp
        ),
        model=read_model_settings(arguments.env_file.expanduser()),
        skip_postgres_start=arguments.skip_postgres_start,
        keep_offer=arguments.keep_offer,
        hnp_selector=arguments.hnp_selector or "llm",
        hnp_reviewer=arguments.hnp_reviewer or "off",
        hnp_trigger_delay=(
            HNP_TRIGGER_DELAY_SECONDS
            if arguments.hnp_trigger_delay is None
            else arguments.hnp_trigger_delay
        ),
    )


def describe(plan: DemoPlan, problems: list[str], out: TextIO) -> None:
    """The dry-run: what would start, where, and with which variable names (no values)."""
    print(f"Modo {plan.mode.name}: {plan.mode.description}", file=out)
    for name, value in sorted(plan.mode_environment().items()):
        shown = REDACTED if value in plan.secret_values() else value
        print(f"  {name}={shown}", file=out)
    if plan.mode.hnp:
        print(f"HNP: {plan.hnp_line()} (hnp.log)", file=out)
    print(f"Directorio del arranque: {plan.run_dir}", file=out)
    for warning in plan.warnings():
        print(f"Aviso: {warning}", file=out)
    for service in plan.services(merchant_token=REDACTED):
        print(f"· {service.label}: {service.origin} (cwd {service.cwd})", file=out)
        print(f"    variables: {', '.join(sorted(set(service.env) - set(plan.base)))}", file=out)
    print("Comprobaciones: " + ("con problemas" if problems else "todo listo"), file=out)
    for problem in problems:
        print(f"  ✗ {problem}", file=out)


def up(argv: Sequence[str]) -> int:
    parser = _up_parser()
    arguments = parser.parse_args(argv)
    hnp_options = (arguments.hnp_selector, arguments.hnp_trigger_delay, arguments.hnp_reviewer)
    if not MODES[arguments.mode].hnp and hnp_options != (None, None, None):
        parser.error(
            "--hnp-selector, --hnp-reviewer y --hnp-trigger-delay solo valen con --mode b2-hnp"
        )
    if arguments.hnp_trigger_delay is not None and arguments.hnp_trigger_delay < 0:
        parser.error("--hnp-trigger-delay no puede ser negativo")
    plan = build_plan(arguments)
    problems = preflight(plan)
    if arguments.dry_run:
        describe(plan, problems, sys.stdout)
        return 1 if problems else 0
    if problems:
        print("No se puede arrancar la demo:", file=sys.stderr)
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        return 2
    launcher = Launcher(plan)
    previous = {signum: signal.signal(signum, _interrupt) for signum in STOP_SIGNALS}
    try:
        launcher.start()
        launcher.watch()
    except KeyboardInterrupt:
        with suppress(OSError, ValueError):
            launcher.say("\nApagando…")
        return 0
    except DemoError as error:
        with suppress(OSError, ValueError):
            launcher.say(f"\nLa demo se detuvo: {error}")
        return 1
    finally:
        launcher.stop()
        launcher.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _s04_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="demo-stack s04",
        description=(
            f"Cambia el precio de {OFFER_TITLE} ({OFFER_ID}) en el catálogo del comercio mientras "
            "la demo corre. Úsalo después de que el agente mostró los términos de la compra y "
            "antes de aprobarla: al aprobar, el agente ve que el precio cambió, no cobra y pide "
            "cancelar y volver a pedir la compra con el precio nuevo. El nombre viene del "
            "escenario de cambio de precio de la evaluación."
        ),
    )
    parser.add_argument(
        "--price-minor",
        type=int,
        default=S04_PRICE_MINOR,
        help=f"nuevo precio en centavos de COP (por defecto {S04_PRICE_MINOR})",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help=f"volver al precio del catálogo ({OFFER_PRICE_MINOR}) y a {OFFER_STOCK} unidades",
    )
    _merchant_argument(parser)
    return parser


def s04(argv: Sequence[str], out: TextIO | None = None) -> int:
    out = out or sys.stdout
    arguments = _s04_parser().parse_args(argv)
    merchant = arguments.merchant_repo.expanduser().resolve()
    if not (merchant / "src" / "commerce_lab").is_dir():
        print(f"--merchant-repo: {merchant} no es un checkout del comercio", file=sys.stderr)
        return 2
    command = (
        update_offer_command(merchant, OFFER_PRICE_MINOR, stock=OFFER_STOCK)
        if arguments.restore
        else update_offer_command(merchant, arguments.price_minor)
    )
    # An empty directory, never the checkout: the merchant reads no developer .env.
    with tempfile.TemporaryDirectory(prefix="demo-stack-s04-") as directory:
        result = subprocess.run(
            list(command),
            cwd=directory,
            env=merchant_cli_environment(base_environment()),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    # No launch secret exists here; the merchant's DSN is still redacted.
    details = tail(result.stderr, (), lines=15)
    if result.returncode != 0:
        print(f"No se pudo cambiar la oferta (código {result.returncode}):", file=sys.stderr)
        print(details, file=sys.stderr)
        return 1
    try:
        offer = json.loads(result.stdout.strip().splitlines()[-1])
        summary = offer_summary(offer if isinstance(offer, dict) else {})
    except (IndexError, json.JSONDecodeError, DemoError):
        print(
            "El comercio respondió con una oferta inesperada: no se pudo confirmar el cambio.",
            file=sys.stderr,
        )
        print(details, file=sys.stderr)
        return 1
    print(f"Oferta actualizada: {summary}.", file=out)
    if not arguments.restore:
        print("Para volver al precio del catálogo: uv run demo-stack s04 --restore", file=out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["s04"]:
        return s04(arguments[1:])
    return up(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
