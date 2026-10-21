"""Human Not Present startup (docs/design/hnp-governed.md §6). Off never reaches this module.

Any combination other than the one §6 allows stops startup, and no error echoes a setting.
"""

import hmac
import re
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.feed import ProductFeed
from commerce_agent.ap2_hnp import AgentKeyCloser, OpenMandateIssuer
from commerce_agent.config import Settings
from commerce_agent.governance.provenance import ProvenanceKeys
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger

from .service import DelegationService, HnpTrust

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
    UTC unless a test injects one, never the sandbox or commercial clock."""
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


def _secret(value: SecretStr | None) -> str:
    return value.get_secret_value() if value is not None else ""


def _hex(value: SecretStr | None) -> SecretBytes:
    raw = _secret(value)
    if re.fullmatch(r"(?:[0-9a-fA-F]{2})+", raw) is None:
        raise ValueError("Invalid key encoding")
    return SecretBytes(bytes.fromhex(raw))
