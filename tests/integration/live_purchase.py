"""Purchase lanes of the live stack: the Agent, the real Merchant and the real payment provider.

A lane is one completion profile over the stack's PostgreSQL. `b0` is the B0 baseline (ACP
plus AP2 Human Present: signed mandates and receipts, order webhooks); `acp` is the plain ACP
completion the Agent still runs when AGENT_AP2_REQUIRED is off; `hnp` is Human Not Present
(docs/design/hnp-governed.md): the same AP2 Merchant and provider, and an Agent API with
AGENT_HNP_ENABLED in a process of its own (`HnpAgent`). Each lane owns a Merchant process behind
its own FaultProxy, a payment provider from PAYMENT_REPO, the Agent's webhook receiver and a
Merchant control worker with the same settings. Keys, tokens and secrets are synthetic, created
per run and never written to the evidence.
"""

import hmac
import json
import os
import secrets
import signal
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwcrypto.jwk import JWK
from pydantic import SecretStr

from commerce_agent.acp.feed import ProductCandidate
from commerce_agent.acp.models import Address, Buyer, FulfillmentDetails
from commerce_agent.api import create_app
from commerce_agent.application import CheckoutDetails
from commerce_agent.buyer_auth import BuyerPrincipal
from commerce_agent.config import Settings
from commerce_agent.domain import PurchaseIntent, RankedCandidate
from commerce_agent.llm import (
    CandidateDecision,
    CatalogRecovery,
    CommerceDecision,
    ConversationMessage,
)
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.webhooks import create_webhook_app
from tests.integration.live_stack import (
    AGENT,
    FaultProxy,
    LiveStack,
    MerchantControl,
    ServiceServer,
    clean_environment,
    wait_until,
)

PAYMENT_REPO = "PAYMENT_REPO"
# The B0 episode runs on a fixed synthetic clock; the provider must agree with its checkouts.
SANDBOX_CLOCK = "2026-09-10T14:05:00Z"
# The provider's synthetic test cards; it alone decides the outcome.
APPROVED_CARD = "4242424242424242"
DECLINED_CARD = "4000000000000002"
# Everything a purchase sends to the Merchant; `*` is one identifier segment.
PURCHASE_ROUTES = frozenset(
    {
        ("GET", "/.well-known/acp.json"),
        ("GET", "/.well-known/ap2/jwks.json"),
        ("POST", "/checkout_sessions"),
        ("GET", "/checkout_sessions/*"),
        ("POST", "/checkout_sessions/*"),
        ("GET", "/checkout_sessions/*/ap2/checkout-jwt"),
        ("POST", "/checkout_sessions/*/complete"),
        ("POST", "/checkout_sessions/*/ap2/complete"),
        ("GET", "/orders/*"),
        # Governed offer freshness reads the live offer before presenting and approving.
        ("POST", "/lab/offers/get"),
    }
)
# Human Not Present sends the same, plus the cancel of its own checkout when it escalates (§8.2).
HNP_ROUTES = PURCHASE_ROUTES | {("POST", "/checkout_sessions/*/cancel")}
# The conditions of hnp-governed.md §11 the product runs, and their governance mode.
HNP_MODES = {"B0-HNP": "off", "B2-HNP": "enforce"}
HNP_TERMINAL_STATES = frozenset({"purchased", "escalated", "failed", "canceled", "uncertain"})
# The two seams of the HNP Agent process (`hnp_app`); harness variables, never product settings.
HNP_BUYER_ID = "LIVE_HNP_BUYER_ID"
HNP_BUYER_TOKEN = "LIVE_HNP_BUYER_TOKEN"
HNP_CLOCK_OFFSET = "LIVE_HNP_CLOCK_OFFSET_SECONDS"

# A natural request the seeded B0 catalog can meet, and the typed intent a model extracts.
PHONE_REQUEST = (
    "Quiero comprar un telefono nuevo de 256 GB que cueste maximo dos millones "
    "incluyendo el envio. Prefiero la marca Sony."
)
PHONE_INTENT = PurchaseIntent.model_validate(
    {
        "hard": {
            "currency": "COP",
            "max_total_amount_minor": 2_000_000_00,
            "categories": ["smartphones"],
            "conditions": ["new"],
            "required_options": [{"name": "almacenamiento", "value": "256 gb"}],
        },
        "preferences": {"terms": ["sony"]},
    }
)


def payment_requirements() -> list[str]:
    """Why a purchase lane cannot start here; empty when it can. Never prints values."""
    repository = os.environ.get(PAYMENT_REPO)
    if not repository:
        return [f"{PAYMENT_REPO} (payment-provider checkout)"]
    if not (Path(repository).expanduser() / "src" / "tesis_payment_provider").is_dir():
        return [f"{PAYMENT_REPO} pointing to a payment-provider checkout"]
    return []


def ap2_key() -> tuple[str, str]:
    """A new ES256 key as (PKCS8 PEM, public JWK JSON); it lives only in this run's memory."""
    pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    return pem, JWK.from_pem(pem.encode()).export_public()


def buyer_details(name: str) -> CheckoutDetails:
    email = f"buyer.{name}@example.test"
    return CheckoutDetails(
        buyer=Buyer(email=email, first_name="Buyer", last_name=name.upper()),
        fulfillment=FulfillmentDetails(
            name=f"Buyer {name.upper()}",
            email=email,
            phone_number="+573000000000",
            address=Address(
                name=f"Buyer {name.upper()}",
                line_one="Calle 100 # 10-20",
                city="Bogota",
                state="DC",
                country="CO",
                postal_code="110111",
            ),
        ),
    )


class WebhookReceiver:
    """The Agent's webhook app behind a loopback HTTP server the Merchant posts to."""

    def __init__(self, store: SqliteAgentStore, secret: str):
        self._secret = secret
        self.client = self._client(store)
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                response = receiver.client.post(self.path, content=body, headers=dict(self.headers))
                self.send_response(response.status_code)
                self.send_header(
                    "Content-Type", response.headers.get("content-type", "application/json")
                )
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _client(self, store: SqliteAgentStore) -> TestClient:
        # Explicit store and secret; defaults only, so no developer .env or variable leaks in.
        settings = Settings.model_construct()
        return TestClient(create_webhook_app(store=store, secret=self._secret, settings=settings))

    @contextmanager
    def delivering_to(self, store: SqliteAgentStore) -> Iterator[None]:
        """Webhooks go to another Agent store (a runtime's own database) inside the block."""
        lane = self.client
        self.client = self._client(store)
        try:
            yield
        finally:
            self.client.close()
            self.client = lane

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.client.close()


@dataclass
class Episode:
    """A seeded B0 run as the Agent sees it: its ACP feed and its Merchant session."""

    run_id: str
    actor_id: str
    bearer: SecretStr = field(repr=False)
    feed: Path


class PurchaseLane:
    """One Merchant, one payment provider and one Agent store for a completion profile."""

    def __init__(self, stack: LiveStack, profile: str):
        assert profile in {"b0", "acp", "hnp"}
        self.stack = stack
        self.profile = profile
        # HNP chains reuse the AP2 trust of B0: the provider key is their root (ap2-profile.md).
        self.ap2 = profile in {"b0", "hnp"}
        self.root = stack.root / profile
        self.root.mkdir()
        self.proxy = FaultProxy(HNP_ROUTES if profile == "hnp" else PURCHASE_ROUTES)
        self.database = self.root / "agent.sqlite"
        self.store = SqliteAgentStore(self.database)
        self.store.initialize()
        self.webhook_secret = SecretStr(secrets.token_urlsafe(32))
        self.approval_secret = SecretStr(secrets.token_urlsafe(32))
        self.payment_token = SecretStr(secrets.token_urlsafe(32))
        self.receiver = WebhookReceiver(self.store, self.webhook_secret.get_secret_value())
        merchant_token = secrets.token_urlsafe(32)
        payment_env = clean_environment()
        payment_env.update(
            PAYMENT_DATABASE_PATH=str(self.root / "payments.sqlite"),
            PAYMENT_AGENT_BEARER_TOKEN=self.payment_token.get_secret_value(),
            PAYMENT_MERCHANT_BEARER_TOKEN=merchant_token,
            PAYMENT_TOKEN_SECRET=secrets.token_urlsafe(32),
            PAYMENT_SANDBOX_CLOCK_AT=SANDBOX_CLOCK,
        )
        merchant_env = clean_environment()
        merchant_env.update(
            DATABASE_URL=stack.postgres.dsn,
            ACP_API_BASE_URL=self.proxy.origin,
            LIVE_STACK_CLUSTER=stack.postgres.name,
            PGCONNECT_TIMEOUT="5",
            WEBHOOK_RECEIVER_URL=self.receiver.origin,
            MERCHANT_WEBHOOK_SECRET=self.webhook_secret.get_secret_value(),
            PAYMENT_MERCHANT_BEARER_TOKEN=merchant_token,
        )
        self.agent_key: SecretStr | None = None
        if self.ap2:
            agent_pem, agent_jwk = ap2_key()
            merchant_pem, merchant_jwk = ap2_key()
            payment_pem, _ = ap2_key()
            self.agent_key = SecretStr(agent_pem)
            payment_env.update(
                PAYMENT_AP2_REQUIRED="true",
                PAYMENT_AP2_AGENT_JWK_JSON=agent_jwk,
                PAYMENT_AP2_MERCHANT_JWK_JSON=merchant_jwk,
                PAYMENT_AP2_MERCHANT_ISSUER=self.proxy.origin,
                PAYMENT_AP2_PRIVATE_KEY_PEM=payment_pem,
            )
            merchant_env.update(
                MERCHANT_AP2_REQUIRED="true",
                MERCHANT_AP2_PRIVATE_KEY_PEM=merchant_pem,
                MERCHANT_AP2_AGENT_JWK_JSON=agent_jwk,
            )
        repository = Path(os.environ[PAYMENT_REPO]).expanduser().resolve()
        self.payment = ServiceServer(
            self.root,
            "payment",
            repository,
            ("tesis_payment_provider.api:app_factory", "--factory"),
            payment_env,
            "/health",
        )
        self.merchant = ServiceServer(
            self.root,
            "merchant",
            stack.merchant,
            ("commerce_lab.api:app",),
            merchant_env,
            "/health/ready",
            self.proxy,
        )
        self.control: MerchantControl | None = None

    @property
    def origin(self) -> str:
        return self.proxy.origin

    def start(self) -> None:
        self.payment.start()
        self.merchant.env["PAYMENT_PROVIDER_URL"] = self.payment.origin
        self.merchant.start()
        self.control = MerchantControl(self.root, self.stack.merchant, self.merchant.env)
        self.stack.record(f"{self.profile}:lane", ap2=self.ap2, payment_pid=self.payment.pids)

    def rpc(self, operation: str, **values: Any) -> Any:
        assert self.control is not None
        return self.control(operation, **values)

    def episode(self, name: str) -> Episode:
        actor = f"{self.profile}-{name}-{secrets.token_hex(4)}"
        feed = self.root / f"feed-{actor}"
        data = self.rpc("purchase_episode", actor=actor, feed_dir=str(feed))
        assert data["products"] > 0
        return Episode(data["run_id"], data["actor_id"], SecretStr(data["bearer"]), feed)

    def payment_tokens(self) -> int:
        """`vt_` tokens the provider issued: one row per accepted `delegate_payment`."""
        with closing(
            sqlite3.connect(f"file:{self.root / 'payments.sqlite'}?mode=ro", uri=True)
        ) as db:
            return db.execute("SELECT count(*) FROM delegations").fetchone()[0]

    def close(self) -> None:
        try:
            self.merchant.stop()
            self.payment.stop()
            if self.control is not None:
                self.control.close()
        finally:
            self.proxy.close()
            self.receiver.close()


class StaticConversationRouter:
    """The model boundary without a model: the typed decisions an LLM would return."""

    def __init__(self, request: str, intent: PurchaseIntent) -> None:
        self._request = request
        self._intent = intent

    def route(self, messages: tuple[ConversationMessage, ...]) -> CommerceDecision:
        assert messages[-1].content == self._request
        return CommerceDecision(
            action="purchase",
            assistant_message="Voy a preparar la compra.",
            purchase_request=self._request,
            intent=self._intent,
        )

    def choose_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        candidates: tuple[RankedCandidate, ...],
    ) -> CandidateDecision:
        assert messages and intent == self._intent and candidates
        return CandidateDecision(
            action="purchase",
            assistant_message="Elegí la opción disponible que cumple tu solicitud.",
            selected_variant_id=candidates[0].candidate.variant_id,
        )

    def recover_no_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        rejection_counts: dict[str, int],
        alternatives: tuple[ProductCandidate, ...],
    ) -> CatalogRecovery:
        assert messages and intent == self._intent and rejection_counts and alternatives
        return CatalogRecovery(
            action="clarify",
            assistant_message="No encontré una coincidencia exacta.",
        )


# Human Not Present ---------------------------------------------------------------------------


class EpisodeBuyer:
    """The episode's buyer: a bearer token in this run's memory stands for the JWT that `auth/`
    would issue. The delegation routes still take the buyer from `current_buyer`."""

    def __init__(self, principal_id: str, token: str) -> None:
        self._principal = BuyerPrincipal(principal_id)
        self._token = token.encode()

    def authenticate(self, authorization: str | None) -> BuyerPrincipal | None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(token.encode(), self._token):
            return self._principal
        return None


def hnp_app() -> FastAPI:
    """The HNP Agent API of `HnpAgent`: production `create_app` over the variables the process
    was started with, never a developer .env. Two seams are the harness's: the episode's buyer
    (`EpisodeBuyer`) and an authority clock that may run ahead after a crash, so a re-claim finds
    the lease the killed run left lapsed (hnp-governed.md §4.5) without waiting its 120 s."""
    offset = timedelta(seconds=float(os.environ[HNP_CLOCK_OFFSET]))

    def authority_clock() -> datetime:
        return datetime.now(UTC) + offset

    return create_app(
        settings=Settings(_env_file=None),  # pyright: ignore[reportCallIssue]
        buyer_authenticator=EpisodeBuyer(os.environ[HNP_BUYER_ID], os.environ[HNP_BUYER_TOKEN]),
        authority_clock=authority_clock,
    )


def delegation_request(
    variant_ids: list[str], total_max_minor: int, *, name: str, city: str = "Bogotá"
) -> dict[str, Any]:
    """`POST /delegations/proposals` for the lane's synthetic buyer, delivered to `city`."""
    details = buyer_details(name)
    address = details.fulfillment.address.model_copy(update={"city": city})
    fulfillment = details.fulfillment.model_copy(update={"address": address})
    return {
        "variant_ids": variant_ids,
        "total_max_minor": total_max_minor,
        "currency": "COP",
        "valid_for_seconds": 3_600,
        "details": details.model_copy(update={"fulfillment": fulfillment}).model_dump(
            mode="json", exclude_none=True
        ),
        "preference_text": None,
    }


class HnpAgent:
    """The Agent API with AGENT_HNP_ENABLED for one episode of the `hnp` lane and one condition.

    It runs in a process of its own (`hnp_app` under uvicorn, from this checkout), so a test can
    kill it mid-run, and keeps its SQLite in the run directory. The buyer proposes and consents
    with its token; the service token triggers the task; the selector is the deterministic one
    and no model is configured (`clean_environment` drops every OPENAI_* variable).
    """

    def __init__(
        self, lane: PurchaseLane, episode: Episode, condition: str, **settings: str
    ) -> None:
        assert lane.profile == "hnp" and lane.agent_key is not None
        self.lane = lane
        self.episode = episode
        self.condition = condition
        self.root = lane.root / f"agent-{episode.actor_id}"
        self.root.mkdir()
        self.database = self.root / "agent.sqlite"
        self._buyer = secrets.token_urlsafe(32)
        self._service = secrets.token_urlsafe(32)
        self._operator = secrets.token_urlsafe(32)
        agent_key, _ = ap2_key()
        # The Agent Key that closes this episode's mandates (`cnf` of its open mandates).
        self.agent_key = SecretStr(agent_key)
        env = clean_environment()
        env.update(
            MERCHANT_BASE_URL=lane.origin,
            MERCHANT_BEARER_TOKEN=episode.bearer.get_secret_value(),
            MERCHANT_WEBHOOK_SECRET=lane.webhook_secret.get_secret_value(),
            AGENT_APPROVAL_SECRET=lane.approval_secret.get_secret_value(),
            PAYMENT_PROVIDER_URL=lane.payment.origin,
            PAYMENT_AGENT_BEARER_TOKEN=lane.payment_token.get_secret_value(),
            AGENT_DATABASE_PATH=str(self.database),
            AGENT_FEED_SOURCE="file",
            AGENT_FEED_PATH=str(episode.feed),
            # The offers of a B0 episode are observed on its synthetic clock; HNP authority
            # (grant, lease, mandates) runs on the real one (hnp-governed.md §3.4).
            AGENT_SANDBOX_CLOCK_AT=SANDBOX_CLOCK,
            # HNP requires jwt; `hnp_app` authenticates the buyer, so the issuer is never called.
            AGENT_AUTH_MODE="jwt",
            AGENT_AUTH_ISSUER="http://127.0.0.1:9",
            AGENT_AP2_REQUIRED="true",
            # The provider key the Merchant and the provider trust signs the open mandates.
            AGENT_AP2_PRIVATE_KEY_PEM=lane.agent_key.get_secret_value(),
            AGENT_HNP_ENABLED="true",
            AGENT_HNP_AGENT_KEY_PEM=agent_key,
            AGENT_HNP_SERVICE_TOKEN=self._service,
            AGENT_HNP_OPERATOR_TOKEN=self._operator,
            AGENT_HNP_SELECTOR="deterministic",
            AGENT_D1_KEY_ID="live-hnp",
            AGENT_D1_HMAC_KEY_HEX=secrets.token_hex(32),
            AGENT_D1_AES_KEY_HEX=secrets.token_hex(32),
            AGENT_GOVERNANCE_MODE=HNP_MODES[condition],
            **{HNP_BUYER_ID: episode.actor_id, HNP_BUYER_TOKEN: self._buyer, HNP_CLOCK_OFFSET: "0"},
        )
        if condition == "B2-HNP":
            # The HP policy set the enforce gate needs to start; HNP is evaluated with
            # b2-hnp-v1, built in code (§7), and POL-02 allows this lane's Merchant (K11).
            env.update(
                AGENT_HP_FLOW="governed",
                AGENT_D1_POLICY_SET_ID="live-hnp-hp",
                AGENT_D1_POLICY_REVISION="1",
                AGENT_D1_ALLOWED_MERCHANT_ORIGINS=lane.origin,
            )
        env.update({name.upper(): value for name, value in settings.items()})
        assert not any(name.startswith("OPENAI_") for name in env)
        self.server = ServiceServer(
            self.root,
            "agent",
            AGENT,
            ("tests.integration.live_purchase:hnp_app", "--factory", "--app-dir", str(AGENT)),
            env,
            "/health",
        )
        self.http = httpx.Client(trust_env=False, timeout=30)

    @property
    def origin(self) -> str:
        return self.server.origin

    def start(self) -> None:
        self.server.start()
        health = self.http.get(self.origin + "/health").json()
        assert health["hnp_enabled"] is True and health["hnp_kill_switch"] is False
        assert health["governance_mode"] == HNP_MODES[self.condition]

    def crash(self) -> None:
        """SIGKILL to the whole process group, as a crash: no shutdown runs."""
        process = self.server.process
        assert process is not None
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=15)
        self.server.process = None

    def restart(self, *, clock_ahead_seconds: int = 0) -> None:
        self.server.env[HNP_CLOCK_OFFSET] = str(clock_ahead_seconds)
        self.start()

    def close(self) -> None:
        try:
            self.server.stop()
        finally:
            self.http.close()

    # The routes of hnp-governed.md §5.1 and §5.2.

    def _call(self, method: str, path: str, token: str, body: Any = None) -> httpx.Response:
        return self.http.request(
            method, self.origin + path, json=body, headers={"Authorization": f"Bearer {token}"}
        )

    def as_buyer(self, method: str, path: str, body: Any = None) -> httpx.Response:
        return self._call(method, path, self._buyer, body)

    def as_service(self, method: str, path: str) -> httpx.Response:
        return self._call(method, path, self._service)

    def as_operator(self, method: str, path: str, body: Any = None) -> httpx.Response:
        return self._call(method, path, self._operator, body)

    def delegate(self, request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Proposal and consent, as the buyer's two clicks: the proposal and the grant's view."""
        proposed = self.as_buyer("POST", "/delegations/proposals", request)
        assert proposed.status_code == 201, proposed.text
        proposal = proposed.json()
        consent = {"proposal_id": proposal["proposal_id"], "scope_digest": proposal["scope_digest"]}
        consented = self.as_buyer("POST", "/delegations", consent)
        assert consented.status_code == 201, consented.text
        return proposal, consented.json()

    def trigger(self, task_id: str) -> httpx.Response:
        return self.as_service("POST", f"/internal/hnp/tasks/{task_id}/run")

    def trigger_at_once(self, task_id: str, count: int = 2) -> list[httpx.Response]:
        """`count` triggers of the scheduler released together, each on its own connection."""
        barrier = threading.Barrier(count)

        def send() -> httpx.Response:
            with httpx.Client(trust_env=False, timeout=30) as client:
                barrier.wait(timeout=10)
                return client.post(
                    f"{self.origin}/internal/hnp/tasks/{task_id}/run",
                    headers={"Authorization": f"Bearer {self._service}"},
                )

        with ThreadPoolExecutor(count) as pool:
            return [future.result() for future in [pool.submit(send) for _ in range(count)]]

    def revoke(self, grant_id: str) -> httpx.Response:
        """The buyer's revocation on its own connection, so a proxy hook can send it mid-run."""
        with httpx.Client(trust_env=False, timeout=30) as client:
            return client.post(
                f"{self.origin}/delegations/{grant_id}/revoke",
                headers={"Authorization": f"Bearer {self._buyer}"},
            )

    def task(self, task_id: str) -> dict[str, Any]:
        response = self.as_service("GET", f"/internal/hnp/tasks/{task_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def finished(self, task_id: str, timeout: float = 90) -> dict[str, Any]:
        """The task once terminal; the run goes on in the Agent's background after the 202."""
        deadline = time.monotonic() + timeout
        while (task := self.task(task_id))["state"] not in HNP_TERMINAL_STATES:
            assert time.monotonic() < deadline, f"task still {task['state']}"
            time.sleep(0.2)
        return task

    def delegation(self, grant_id: str) -> dict[str, Any]:
        response = self.as_buyer("GET", f"/delegations/{grant_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def rows(self, query: str, *parameters: Any) -> list[tuple[Any, ...]]:
        """A read of the Agent's SQLite while it runs; WAL readers never block its writers."""
        with closing(sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)) as db:
            return db.execute(query, parameters).fetchall()


@contextmanager
def hnp_agent(lane: PurchaseLane, name: str, condition: str, **settings: str) -> Iterator[HnpAgent]:
    """A new episode of the lane and its HNP Agent, stopped and closed on the way out."""
    agent = HnpAgent(lane, lane.episode(name), condition, **settings)
    try:
        agent.start()
        yield agent
    finally:
        agent.close()


def await_call(proxy: FaultProxy, method: str, path: str, timeout: float = 30) -> None:
    """Until the Merchant answered `method path` through the proxy, even if the Agent never saw
    that answer (a dropped, delayed or orphaned response)."""

    def answered() -> None:
        calls = [call for call in proxy.calls if (call.method, call.path) == (method, path)]
        assert calls and calls[-1].answered.is_set()

    wait_until(answered, f"{method} {path}", timeout=timeout)


def on_checkout(proxy: FaultProxy, prepare: Callable[[str], None]) -> None:
    """Once the Merchant answered the run's `POST /checkout_sessions`, `prepare(checkout_id)`
    runs in the proxy, before the Agent sees the answer: paths with the id get their hooks and
    faults before the Agent can call them."""

    def created() -> None:
        [call] = [item for item in proxy.calls if item.path == "/checkout_sessions"]
        assert call.status == 201 and call.response_body is not None, call.status
        prepare(json.loads(call.response_body)["id"])

    proxy.after_answer["/checkout_sessions"] = created
