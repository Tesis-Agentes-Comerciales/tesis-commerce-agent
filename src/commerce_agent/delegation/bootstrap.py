"""Human Not Present startup (docs/design/hnp-governed.md §6). Off never reaches this module.

Any combination other than the one §6 allows stops startup, and no error echoes a setting.
"""

import hmac
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.feed import ProductFeed
from commerce_agent.ap2_hnp import AgentKeyCloser, OpenMandateIssuer
from commerce_agent.application.hnp_dispatch import (
    HnpDispatcher,
    MerchantCompletion,
    PaymentProvider,
)
from commerce_agent.application.hnp_execution import (
    FreshnessCheck,
    HnpExecutionService,
    NullHnpPolicyGate,
    PurchaseStarter,
    sqlite_d1_denials,
)
from commerce_agent.application.hnp_intent_review import HnpIntentReviewer
from commerce_agent.application.hnp_selection import (
    DeterministicHnpSelector,
    HnpSelector,
    ModelHnpSelector,
)
from commerce_agent.application.offer_freshness import OfferReader
from commerce_agent.application.purchase import CheckoutGateway
from commerce_agent.config import Settings
from commerce_agent.governance.hnp_ports import HnpPolicyGate
from commerce_agent.governance.provenance import ProvenanceKeys
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger
from commerce_agent.persistence.model_requests_sqlite import SqliteModelRequests
from commerce_agent.persistence.stage_timings_sqlite import SqliteStageTimings

from .service import DelegationService, HnpTrust

if TYPE_CHECKING:
    from commerce_agent.llm.delegated_choice import OpenAIDelegatedChooser
    from commerce_agent.llm.intent_review import ModelHnpIntentReviewer

TOKEN_MIN_LENGTH = 32


def load_hnp_trust(settings: Settings) -> HnpTrust:
    """Check the condition, then load the keys; meant to run before any store exists."""
    _check_condition(settings)
    try:
        keys = ProvenanceKeys(
            key_id=settings.agent_d1_key_id or "",
            pseudonym_key=_hex(settings.agent_d1_hmac_key_hex),
            encryption_key=_hex(settings.agent_d1_aes_key_hex),
        )
    except ValueError:
        raise RuntimeError(
            "AGENT_HNP_ENABLED requires valid AGENT_D1_KEY_ID, AGENT_D1_HMAC_KEY_HEX and "
            "AGENT_D1_AES_KEY_HEX (the vault keys of the owner and destination pseudonyms)"
        ) from None
    try:
        agent = AgentKeyCloser(
            _secret(settings.agent_hnp_agent_key_pem), merchant_issuer=settings.merchant_base_url
        )
        # The issuer compares both public keys: the Agent Key must not be the provider key.
        issuer = OpenMandateIssuer(
            _secret(settings.agent_ap2_private_key_pem),
            agent_public_jwk=agent.public_jwk,
            merchant_issuer=settings.merchant_base_url,
        )
    except ValueError:
        raise RuntimeError(
            "AGENT_HNP_AGENT_KEY_PEM must be an ES256 private key distinct from "
            "AGENT_AP2_PRIVATE_KEY_PEM"
        ) from None
    return HnpTrust(keys, issuer, agent.public_jwk, agent.kid, settings.merchant_base_url)


def build_delegation_service(
    settings: Settings,
    trust: HnpTrust,
    *,
    store: SqliteAgentStore,
    feed: Callable[[], ProductFeed],
    clock: Callable[[], datetime] | None,
) -> DelegationService:
    """The ledger tables exist only from here on. `clock` is the authority clock (§3.4): real
    UTC unless a test injects one, never the sandbox or commercial clock.

    D15: the executor this startup assembles reviews `preference_text` only in enforce with
    `AGENT_HNP_REVIEWER=llm`, so only then does a proposal's card promise the review.
    """
    authority = clock or _utc_now
    ledger = SqliteDelegationLedger(settings.agent_database_path, clock=authority)
    ledger.initialize()
    return DelegationService(
        ledger,
        trust,
        feed=feed,
        purchase=store.purchase,
        max_validity_seconds=settings.agent_hnp_max_validity_seconds,
        proposal_ttl_seconds=settings.agent_hnp_proposal_ttl_seconds,
        clock=authority,
        preference_review=settings.agent_governance_mode == "enforce"
        and settings.agent_hnp_reviewer == "llm",
    )


def build_hnp_selector(
    settings: Settings, timings: SqliteStageTimings
) -> "tuple[HnpSelector, OpenAIDelegatedChooser | None]":
    """`AGENT_HNP_SELECTOR`: the selector, and the model client the runtime must close. After
    `build_delegation_service`: a model selector can fail, and a ledger created before
    `selection_unavailable` lacks it in its CHECK, so startup refuses it rather than failing a run
    halfway."""
    if settings.agent_hnp_selector == "deterministic":
        return DeterministicHnpSelector(), None
    if not SqliteDelegationLedger(settings.agent_database_path).admits_selection_unavailable():
        raise RuntimeError(
            "AGENT_HNP_SELECTOR=llm needs a delegation ledger that admits selection_unavailable; "
            "point AGENT_DATABASE_PATH to a new database"
        )
    from commerce_agent.llm.delegated_choice import OpenAIDelegatedChooser

    if settings.openai_api_key is None or settings.openai_model is None:
        raise RuntimeError("AGENT_HNP_SELECTOR=llm requires OPENAI_API_KEY and OPENAI_MODEL")
    chooser = OpenAIDelegatedChooser(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        timeout_seconds=settings.openai_timeout_seconds,
        reasoning_effort=settings.openai_reasoning_effort,
        # The runtime created the table in this same database.
        model_requests=SqliteModelRequests(settings.agent_database_path),
    )
    return ModelHnpSelector(chooser, timings), chooser


def build_hnp_reviewer(
    settings: Settings, timings: SqliteStageTimings
) -> "ModelHnpIntentReviewer | None":
    """`AGENT_HNP_REVIEWER` (hnp-intent-review.md §9): the model reviewer, which the runtime must
    close, or None when off. After `build_delegation_service`: a ledger created before the review
    lacks its event and escalation in its CHECKs, and startup refuses it rather than failing a
    run halfway."""
    if settings.agent_hnp_reviewer == "off":
        return None
    if not SqliteDelegationLedger(settings.agent_database_path).admits_choice_review():
        raise RuntimeError(
            "AGENT_HNP_REVIEWER=llm needs a delegation ledger that admits choice_reviewed; "
            "point AGENT_DATABASE_PATH to a new database"
        )
    from commerce_agent.llm.intent_review import ModelHnpIntentReviewer

    if settings.openai_api_key is None or settings.openai_model is None:
        raise RuntimeError("AGENT_HNP_REVIEWER=llm requires OPENAI_API_KEY and OPENAI_MODEL")
    return ModelHnpIntentReviewer(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        timer=timings,
        timeout_seconds=settings.openai_timeout_seconds,
        reasoning_effort=settings.openai_reasoning_effort,
        model_requests=SqliteModelRequests(settings.agent_database_path),
    )


def build_hnp_executor(
    settings: Settings,
    trust: HnpTrust,
    *,
    store: SqliteAgentStore,
    purchases: PurchaseStarter,
    checkout: CheckoutGateway,
    gate: HnpPolicyGate | None,
    freshness: FreshnessCheck | None,
    selector: HnpSelector,
    feed: Callable[[], ProductFeed],
    clock: Callable[[], datetime] | None,
    payments: PaymentProvider,
    merchant: MerchantCompletion,
    offers: OfferReader,
    reviewer: HnpIntentReviewer | None = None,
) -> HnpExecutionService:
    """The executor of a task, steps 1 to 12 of hnp-governed.md §3.3: the one place that
    assembles it with its dispatch (`HnpDispatcher` over the PSP client and the ACP client).

    `off` is B0-HNP: `NullHnpPolicyGate`, no freshness and no D1. `enforce` is B2-HNP and never
    starts without D2-HNP/D3-HNP and the `close` freshness guard. Only this service holds the
    Agent Key; `clock` is the authority clock, as for the delegation service. `reviewer` is
    the intent reviewer of `AGENT_HNP_REVIEWER=llm`, enforce only. `offers` reads the
    merchant's offers of a consented `priority` (p0-conditional-intent.md §3.9): the runtime's
    ACP client, the same one as `merchant`.
    """
    enforce = settings.agent_governance_mode == "enforce"
    if enforce and (gate is None or freshness is None):
        raise RuntimeError(
            "AGENT_HNP_ENABLED in enforce requires D2-HNP, D3-HNP and the offer freshness guard"
        )
    try:
        closer = AgentKeyCloser(
            _secret(settings.agent_hnp_agent_key_pem), merchant_issuer=settings.merchant_base_url
        )
    except ValueError:
        raise RuntimeError("AGENT_HNP_AGENT_KEY_PEM must be an ES256 private key") from None
    if closer.kid != trust.agent_key_kid:
        raise RuntimeError("AGENT_HNP_AGENT_KEY_PEM is not the Agent Key startup verified")
    authority = clock or _utc_now
    mode = "enforce" if enforce else "off"
    ledger = SqliteDelegationLedger(settings.agent_database_path, clock=authority)
    return HnpExecutionService(
        mode=mode,
        ledger=ledger,
        store=store,
        purchases=purchases,
        checkout=checkout,
        closer=closer,
        gate=gate if enforce and gate is not None else NullHnpPolicyGate(),
        selector=selector,
        feed=feed,
        clock=authority,
        id_factory=_new_id,
        freshness=freshness if enforce else None,
        d1_denials=sqlite_d1_denials(settings.agent_database_path) if enforce else None,
        dispatcher=HnpDispatcher(
            mode=mode, ledger=ledger, store=store, payments=payments, merchant=merchant
        ),
        reviewer=reviewer,
        offers=offers,
    )


def _check_condition(settings: Settings) -> None:
    if settings.agent_auth_mode != "jwt" or not settings.agent_ap2_required:
        raise RuntimeError(
            "AGENT_HNP_ENABLED requires AGENT_AUTH_MODE=jwt and AGENT_AP2_REQUIRED=true"
        )
    mode = settings.agent_governance_mode
    if mode == "shadow" or (mode == "enforce" and settings.agent_hp_flow != "governed"):
        raise RuntimeError(
            "AGENT_HNP_ENABLED runs with AGENT_GOVERNANCE_MODE=off, or enforce with "
            "AGENT_HP_FLOW=governed"
        )
    if settings.agent_hnp_selector == "llm" and (
        settings.openai_api_key is None or settings.openai_model is None
    ):
        raise RuntimeError("AGENT_HNP_SELECTOR=llm requires OPENAI_API_KEY and OPENAI_MODEL")
    if settings.agent_hnp_reviewer == "llm" and mode != "enforce":
        # B0-HNP has no escalation to stop a choice with (hnp-intent-review.md §2).
        raise RuntimeError("AGENT_HNP_REVIEWER=llm requires AGENT_GOVERNANCE_MODE=enforce")
    if settings.agent_hnp_reviewer == "llm" and (
        settings.openai_api_key is None or settings.openai_model is None
    ):
        raise RuntimeError("AGENT_HNP_REVIEWER=llm requires OPENAI_API_KEY and OPENAI_MODEL")
    service = _secret(settings.agent_hnp_service_token).encode()
    operator = _secret(settings.agent_hnp_operator_token).encode()
    if min(len(service), len(operator)) < TOKEN_MIN_LENGTH or hmac.compare_digest(
        service, operator
    ):
        raise RuntimeError(
            "AGENT_HNP_SERVICE_TOKEN and AGENT_HNP_OPERATOR_TOKEN must differ and have at least "
            f"{TOKEN_MIN_LENGTH} characters"
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _secret(value: SecretStr | None) -> str:
    return value.get_secret_value() if value is not None else ""


def _hex(value: SecretStr | None) -> SecretBytes:
    raw = _secret(value)
    if re.fullmatch(r"(?:[0-9a-fA-F]{2})+", raw) is None:
        raise ValueError("Invalid key encoding")
    return SecretBytes(bytes.fromhex(raw))
