"""Purchase lanes of the live stack: the Agent, the real Merchant and the real payment provider.

A lane is one completion profile over the stack's PostgreSQL. `b0` is the B0 baseline (ACP
plus AP2 Human Present: signed mandates and receipts, order webhooks); `acp` is the plain ACP
completion the Agent still runs when AGENT_AP2_REQUIRED is off; `hnp` is Human Not Present
(docs/design/hnp-governed.md): the same AP2 Merchant and provider, and an Agent API with
AGENT_HNP_ENABLED in a process of its own (`HnpAgent`). Each lane owns a Merchant process behind
its own FaultProxy, a payment provider from PAYMENT_REPO (behind a second FaultProxy in `hnp`),
the Agent's webhook receiver and a Merchant control worker with the same settings. Keys, tokens
and secrets are synthetic, created per run and never written to the evidence.
"""

import os
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwcrypto.jwk import JWK
from pydantic import SecretStr

from commerce_agent.persistence import SqliteAgentStore
from commerce_evaluation.stack.environment import MERCHANT_REPO, clean_environment
from commerce_evaluation.stack.fault_proxy import FaultProxy
from commerce_evaluation.stack.live_stack import LiveStack, MerchantControl
from commerce_evaluation.stack.service_server import ServiceServer
from commerce_evaluation.stack.webhook_receiver import WebhookReceiver

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
# What the HNP Agent sends the payment provider, behind the `hnp` lane's second FaultProxy.
DELEGATE_PAYMENT = "/agentic_commerce/delegate_payment"
PAYMENT_ROUTES = frozenset(
    {
        ("POST", DELEGATE_PAYMENT),
        ("POST", "/agentic_commerce/payment_receipt"),
        ("GET", "/.well-known/ap2/jwks.json"),
    }
)
# A source line each verifier gained with its HNP chain support (HNP-M1 and HNP-P1).
HNP_MERCHANT: tuple[Path, str, str] = (
    Path("src/commerce_lab/ap2_checkout.py"),
    'CHAIN_SEPARATOR = "~~"',
    "HNP-M1",
)
HNP_PROVIDER: tuple[Path, str, str] = (
    Path("src/tesis_payment_provider/ap2.py"),
    "def is_mandate_chain(",
    "HNP-P1",
)


def payment_requirements() -> list[str]:
    """Why a purchase lane cannot start here; empty when it can. Never prints values."""
    repository = os.environ.get(PAYMENT_REPO)
    if not repository:
        return [f"{PAYMENT_REPO} (payment-provider checkout)"]
    if not (Path(repository).expanduser() / "src" / "tesis_payment_provider").is_dir():
        return [f"{PAYMENT_REPO} pointing to a payment-provider checkout"]
    return []


def hnp_requirements(merchant: Path) -> list[str]:
    """Why the `hnp` lane cannot run on these checkouts: a Merchant without HNP-M1 or a provider
    without HNP-P1 cannot verify `open~~closed` chains. The lane skips then; a missing
    PAYMENT_REPO still fails, through `payment_requirements`."""
    repository = os.environ.get(PAYMENT_REPO)
    checkouts: list[tuple[Path, tuple[Path, str, str], str]] = [
        (merchant, HNP_MERCHANT, MERCHANT_REPO)
    ]
    if repository:
        checkouts.append((Path(repository).expanduser(), HNP_PROVIDER, PAYMENT_REPO))
    missing = []
    for root, (source, marker, ticket), variable in checkouts:
        path = root / source
        if not path.is_file() or marker not in path.read_text(encoding="utf-8"):
            missing.append(f"{variable} with {ticket} (no `{marker}` in {source})")
    return missing


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
        # HNP also puts a proxy between its Agents and the provider; the Merchant reaches the
        # provider directly, as it would in production.
        self.psp_proxy = FaultProxy(PAYMENT_ROUTES) if profile == "hnp" else None
        # Injected faults and whether each ended closed, for `fail_open_rate` (`fault`).
        self.faults: dict[str, bool | None] = {}
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
            self.psp_proxy,
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

    @property
    def payment_origin(self) -> str:
        """The provider as the lane's Agents reach it: through its proxy when there is one."""
        return self.payment.origin if self.psp_proxy is None else self.psp_proxy.origin

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

    @contextmanager
    def fault(self, case: str) -> Iterator[Callable[..., None]]:
        """One injected fault for `fail_open_rate` (hnp-pilot-v1.json, `out_of_matrix`). Once
        the block measured the effects it calls `observe(closed=..., **facts)`: closed when the
        fault left no order, token or send beyond what the authority in force allowed, open
        otherwise. Facts are public counts and codes only. A block that ends before observing
        (the test broke first) leaves the case unobserved."""
        assert case not in self.faults, case

        def observe(*, closed: bool, **facts: Any) -> None:
            assert case not in self.faults, case
            self.faults[case] = closed
            self.stack.record(f"{self.profile}:fault", case=case, closed=closed, **facts)

        try:
            yield observe
        finally:
            if case not in self.faults:
                self.faults[case] = None
                self.stack.record(f"{self.profile}:fault", case=case, closed=None)

    def fail_open_rate(self) -> dict[str, Any]:
        """Open faults over the observed ones, with the cases behind both numbers."""
        observed = {case: closed for case, closed in self.faults.items() if closed is not None}
        opened = sorted(case for case, closed in observed.items() if not closed)
        return {
            "fail_open_rate": len(opened) / len(observed) if observed else None,
            "open": len(opened),
            "n": len(observed),
            "open_cases": opened,
            "cases": sorted(observed),
            "unobserved": sorted(case for case, closed in self.faults.items() if closed is None),
        }

    def close(self) -> None:
        try:
            if self.faults:
                self.stack.record(f"{self.profile}:fail_open_rate", **self.fail_open_rate())
            self.merchant.stop()
            self.payment.stop()
            if self.control is not None:
                self.control.close()
        finally:
            self.proxy.close()
            if self.psp_proxy is not None:
                self.psp_proxy.close()
            self.receiver.close()
