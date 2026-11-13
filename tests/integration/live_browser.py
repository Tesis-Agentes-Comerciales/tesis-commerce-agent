"""The buyer UI over a live purchase lane: auth service, Agent API, Vite and Chromium.

The browser signs up through this checkout's auth service, talks to the Agent API with the
short-lived JWT it issues and uses the purchase or the delegation the Agent prepares against the
lane's real Merchant and payment provider. auth/ and ui/ run with `node` after `npm ci`; the
Agent API runs in this process so the model boundary can be scripted. Each journey is recorded
(`journey`): a video, a screenshot per named step and a sanitized Playwright trace, under
`.local/browser-journeys/<journey>/`. Replaces the September browser smoke; the mapping is in
tests/integration/README.md.
"""

import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from playwright.sync_api import Locator, Page, Playwright, Response, expect
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from commerce_agent.api import create_app
from commerce_agent.config import Settings
from commerce_agent.llm import ConversationRouter
from commerce_evaluation.stack.environment import (
    AGENT,
    MODEL_PREFIXES,
    clean_environment,
    closed,
    free_port,
    wait_until,
)
from commerce_evaluation.stack.hnp_agent import HNP_TERMINAL_STATES
from commerce_evaluation.stack.purchase_lane import SANDBOX_CLOCK, Episode, PurchaseLane
from tests.support.agent_server import AgentServer
from tests.support.browser_trace import sanitize_trace

AUTH = AGENT / "auth"
UI = AGENT / "ui"
# One directory per journey, replaced on every run; `.local/` is ignored by Git.
JOURNEYS = AGENT / ".local" / "browser-journeys"
DESKTOP = {"width": 1440, "height": 900}
PHONE_SCREEN = {"width": 390, "height": 844}


def browser_requirements() -> list[str]:
    """Why the browser test cannot start here; empty when it can."""
    problems = [] if shutil.which("node") else ["node on PATH"]
    problems += [
        f"`npm ci` in {directory.name}/"
        for directory in (AUTH, UI)
        if not (directory / "node_modules").is_dir()
    ]
    return problems


class ExplicitSettings(Settings):
    """The Agent's settings from the values given and nothing else: no developer .env and no
    variable of this process (ANTHROPIC_* included), as `clean_environment` does for the
    children."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def agent_settings(variables: Mapping[str, str]) -> Settings:
    """Settings from variables spelled as in the environment (`AGENT_HNP_ENABLED`)."""
    values: dict[str, Any] = {name.lower(): value for name, value in variables.items()}
    return ExplicitSettings(**values)


class NodeServer:
    """A Node service of this checkout, in a session of its own, logging to the run directory."""

    def __init__(
        self,
        root: Path,
        name: str,
        directory: Path,
        command: tuple[str, ...],
        env: dict[str, str],
        port: int,
        ready: str,
    ):
        self.root = root
        self.name = name
        self.directory = directory
        self.command = command
        self.env = env
        self.port = port
        self.ready = ready
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        assert self.process is None
        with (self.root / f"{self.name}.log").open("ab") as log:
            self.process = subprocess.Popen(
                ["node", *self.command],
                cwd=self.directory,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )

        def ready():
            assert self.process is not None and self.process.poll() is None
            with httpx.Client(trust_env=False, timeout=2) as client:
                assert client.get(self.origin + self.ready).status_code == 200

        wait_until(ready, self.name)

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=15)
        self.process = None
        wait_until(lambda: closed(self.port), f"{self.name} listener closed", timeout=15)


class BuyerUi:
    """Auth service, Agent API and Vite for one episode; the UI's origin is the JWT issuer.

    `variables` adds Agent settings, spelled as in the environment: a governance condition or
    the HNP Agent of `hnp_condition`. The Agent reads nothing else (`ExplicitSettings`).
    """

    def __init__(
        self,
        lane: PurchaseLane,
        episode: Episode,
        router: ConversationRouter | None,
        variables: Mapping[str, str] | None = None,
    ):
        self.lane = lane
        self.episode = episode
        self.router = router
        self.root = lane.root / f"browser-{episode.actor_id}"
        self.root.mkdir()
        self.database = self.root / "agent.sqlite"
        ui_port, auth_port = free_port(), free_port()
        self.origin = f"http://127.0.0.1:{ui_port}"
        env = clean_environment()
        assert not any(name.startswith(MODEL_PREFIXES) for name in env)
        self.auth = NodeServer(
            self.root,
            "auth",
            AUTH,
            ("server.js",),
            env
            | {
                "BETTER_AUTH_URL": self.origin,
                "BETTER_AUTH_SECRET": secrets.token_urlsafe(32),
                "AUTH_DATABASE_PATH": str(self.root / "auth.sqlite"),
                "PORT": str(auth_port),
            },
            auth_port,
            "/api/auth/jwks",
        )
        agent = {
            "MERCHANT_BASE_URL": lane.origin,
            "MERCHANT_BEARER_TOKEN": episode.bearer.get_secret_value(),
            "MERCHANT_WEBHOOK_SECRET": lane.webhook_secret.get_secret_value(),
            "AGENT_APPROVAL_SECRET": lane.approval_secret.get_secret_value(),
            "AGENT_AP2_REQUIRED": "true" if lane.ap2 else "false",
            "PAYMENT_PROVIDER_URL": lane.payment_origin,
            "PAYMENT_AGENT_BEARER_TOKEN": lane.payment_token.get_secret_value(),
            "AGENT_DATABASE_PATH": str(self.database),
            "AGENT_FEED_SOURCE": "file",
            "AGENT_FEED_PATH": str(episode.feed),
            "AGENT_AUTH_MODE": "jwt",
            "AGENT_AUTH_ISSUER": self.origin,
            "AGENT_UI_ORIGIN": self.origin,
            "AGENT_SANDBOX_CLOCK_AT": SANDBOX_CLOCK,
        }
        if lane.agent_key is not None:
            agent["AGENT_AP2_PRIVATE_KEY_PEM"] = lane.agent_key.get_secret_value()
        self.settings = agent_settings(agent | dict(variables or {}))
        if router is not None:
            # A scripted boundary: no model configured anywhere behind it.
            assert self.settings.anthropic_api_key is None
        self.agent = AgentServer(self._app())
        vite = ("node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--strictPort")
        self.ui = NodeServer(
            self.root,
            "ui",
            UI,
            (*vite, "--port", str(ui_port)),
            env
            | {
                "AUTH_DEV_UPSTREAM": self.auth.origin,
                "VITE_AGENT_API_URL": self.agent.origin,
            },
            ui_port,
            "/",
        )

    def _app(self) -> FastAPI:
        return create_app(settings=self.settings, conversation_router=self.router)

    def __enter__(self) -> "BuyerUi":
        try:
            self.auth.start()
            self.agent.start()
            self.ui.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.ui.stop()
            self.auth.stop()
        finally:
            self.agent.stop()

    def restart_agent(self) -> None:
        """A new Agent on the same port and SQLite, as a declared restart: it loads the feed again
        (HN03 of hnp-scenarios.json). The open page keeps its session and its Agent origin."""
        port = self.agent.port
        self.agent.stop()
        self.agent = AgentServer(self._app(), port)
        self.agent.start()

    def rows(self, query: str, *parameters: Any) -> list[tuple[Any, ...]]:
        """A read of the Agent's SQLite while it runs; WAL readers never block its writers."""
        with closing(sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)) as db:
            return db.execute(query, parameters).fetchall()

    # The scheduler's side of Human Not Present (hnp-governed.md §5.2): the service token, never
    # the buyer's JWT, and no `Origin` header.

    def as_service(self, method: str, path: str) -> httpx.Response:
        token = self.settings.agent_hnp_service_token
        assert token is not None
        with httpx.Client(trust_env=False, timeout=30) as client:
            return client.request(
                method,
                self.agent.origin + path,
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            )

    def trigger(self, task_id: str) -> httpx.Response:
        return self.as_service("POST", f"/internal/hnp/tasks/{task_id}/run")

    def finished(self, task_id: str, timeout: float = 90) -> dict[str, Any]:
        """The task once terminal; the run goes on in the Agent's background after the 202."""
        deadline = time.monotonic() + timeout
        while True:
            response = self.as_service("GET", f"/internal/hnp/tasks/{task_id}")
            assert response.status_code == 200, response.text
            task = response.json()
            if task["state"] in HNP_TERMINAL_STATES:
                return task
            assert time.monotonic() < deadline, f"task still {task['state']}"
            time.sleep(0.2)


def sign_up(page: Page, email: str, name: str) -> None:
    """A new account in `auth/`, as a person creates it: the session opens the chat."""
    page.get_by_role("button", name="Crear cuenta").click()
    page.get_by_label("Nombre").fill(name)
    page.get_by_label("Correo").fill(email)
    page.get_by_label("Contraseña").fill(secrets.token_urlsafe(18))
    page.get_by_role("button", name="Crear cuenta").click()
    expect(page.get_by_role("button", name="Salir")).to_be_visible(timeout=15_000)


def agent_errors(page: Page, origin: str) -> list[str]:
    """Every answer of the Agent API to the page with status 400 or more, as it arrives."""
    errors: list[str] = []

    def observe(response: Response) -> None:
        if response.url.startswith(origin) and response.status >= 400:
            errors.append(f"{response.status} {response.url.split('?')[0]}")

    page.on("response", observe)
    return errors


class Journey:
    """A journey on video in its own browser context. `step` leaves a numbered full-page
    screenshot and sanitized Playwright trace (`trace.zip`). The last screen also goes to
    `evidence/browser.png`, the run directory the nightly artifact uploads."""

    def __init__(self, playwright: Playwright, name: str, evidence: Path, *, phone: bool = False):
        self.evidence = evidence
        self.directory = JOURNEYS / name
        shutil.rmtree(self.directory, ignore_errors=True)
        self.directory.mkdir(parents=True)
        screen = PHONE_SCREEN if phone else DESKTOP
        # A buyer in Colombia: dates and times read as the person would see them.
        options: dict[str, Any] = {
            "viewport": screen,
            "record_video_dir": str(self.directory),
            "record_video_size": screen,
            "locale": "es-CO",
            "timezone_id": "America/Bogota",
        }
        if phone:
            options |= {"device_scale_factor": 3, "is_mobile": True, "has_touch": True}
        self.browser = playwright.chromium.launch()
        self.context = self.browser.new_context(**options)
        self.context.tracing.start(screenshots=True, snapshots=True)
        self.page = self.context.new_page()
        self.steps = 0

    # Up and down only, as a person scrolls: every ancestor keeps its sideways scroll, so a box
    # that clips what sticks out (`overflow: hidden`) shows it cut, as the person sees it.
    REVEAL = """(element) => {
      const ancestors = []
      for (let node = element.parentElement; node; node = node.parentElement) {
        ancestors.push([node, node.scrollLeft])
      }
      element.scrollIntoView({ block: "center", inline: "nearest" })
      for (const [node, left] of ancestors) node.scrollLeft = left
    }"""

    def step(self, label: str, focus: Locator | None = None) -> None:
        """A screenshot of what the person sees, with `focus` scrolled into view (the chat
        scrolls inside its own panel, so a full-page capture would not reach it)."""
        self.observe(label)
        if focus is not None:
            focus.evaluate(self.REVEAL)
        self.steps += 1
        self.page.screenshot(path=self.directory / f"{self.steps:02d}-{label}.png")

    def observe(self, label: str) -> None:
        """Non-waiting control state, including the moment before a failing locator waits."""
        state = self.page.locator(
            "[data-testid='purchase-activity'], [data-testid='delegations'], "
            "[data-testid='delegation-in-chat'], [role='radio'], button[aria-expanded]"
        ).evaluate_all("""elements => elements.map(e => ({
          testid: e.getAttribute('data-testid'), role: e.getAttribute('role'),
          state: e.getAttribute('data-state'), expanded: e.getAttribute('aria-expanded'),
          checked: e.getAttribute('aria-checked'), text: (e.textContent || '').slice(0, 300)
        }))""")
        with (self.directory / "controls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"label": label, "controls": state}, ensure_ascii=False) + "\n")

    def close(self, failed: bool) -> None:
        try:
            with suppress(Exception):
                self.observe("close-failed" if failed else "close-passed")
            # Evidence for the nightly artifact; the run directory holds no secret.
            with suppress(Exception):
                self.page.screenshot(path=self.evidence / "browser.png", full_page=True)
            raw = self.directory / "trace.raw.zip"
            self.context.tracing.stop(path=raw)
            manifest = sanitize_trace(raw, self.directory / "trace.zip")
            raw.unlink()
            (self.directory / "trace-manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            video = self.page.video
            self.context.close()
            if video is not None:
                Path(video.path()).rename(self.directory / "video.webm")
        finally:
            self.browser.close()


@contextmanager
def journey(
    playwright: Playwright, name: str, evidence: Path, *, phone: bool = False
) -> Iterator[Journey]:
    recorded = Journey(playwright, name, evidence, phone=phone)
    failed = True
    try:
        yield recorded
        failed = False
    finally:
        recorded.close(failed)
