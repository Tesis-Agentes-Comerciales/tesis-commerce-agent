"""Purchase lanes of the live stack: the Agent, the real Merchant and the real payment provider.

A lane is one completion profile over the stack's PostgreSQL. `b0` is the B0 baseline (ACP
plus AP2 Human Present: signed mandates and receipts, order webhooks); `acp` is the plain ACP
completion the Agent still runs when AGENT_AP2_REQUIRED is off. Each lane owns a Merchant
process behind its own FaultProxy, a payment provider from PAYMENT_REPO, the Agent's webhook
receiver and a Merchant control worker with the same settings. Keys, tokens and secrets are
synthetic, created per run and never written to the evidence.
"""

import os
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jwcrypto.jwk import JWK
from pydantic import SecretStr

from commerce_agent.acp.feed import ProductCandidate
from commerce_agent.acp.models import Address, Buyer, FulfillmentDetails
from commerce_agent.application import CheckoutDetails
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
    FaultProxy,
    LiveStack,
    MerchantControl,
    ServiceServer,
    clean_environment,
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
        assert profile in {"b0", "acp"}
        self.stack = stack
        self.profile = profile
        self.ap2 = profile == "b0"
        self.root = stack.root / profile
        self.root.mkdir()
        self.proxy = FaultProxy(PURCHASE_ROUTES)
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
