"""Opt-in D1 assembly from Settings. Off never reaches this module.

Shadow degrades to B0 when these settings are unusable; enforce refuses to start.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import SecretBytes, SecretStr

from commerce_agent.config import Settings

from .evaluator import verify_artifact
from .models import SCHEMA, PolicyBinding
from .provenance import (
    ClockProfile,
    ClockReader,
    D1Configuration,
    D1EnforceConfiguration,
    ProvenanceKeys,
    build_d1_policy,
    merchant_pseudonym,
    utc_milliseconds,
)

if TYPE_CHECKING:
    from commerce_agent.ap2_hnp import MandateTrust

    from .authorization import SqliteD2AuthorizationGate
    from .enforce import SqliteD1EnforceGate
    from .hnp_gate import SqliteHnpPolicyGate


@dataclass(frozen=True)
class ConfiguredD1:
    configuration: D1Configuration | D1EnforceConfiguration
    keys: ProvenanceKeys
    clocks: ClockReader
    policy_bytes: bytes | None
    policy_binding: PolicyBinding | None


def configured_d1(
    settings: Settings, commercial_clock: Callable[[], datetime] | None
) -> ConfiguredD1:
    """Decode and verify B2 settings; any missing or malformed value raises."""
    mode = settings.agent_governance_mode
    if mode == "off":
        raise ValueError("D1 is off")
    revision = settings.agent_d1_policy_revision
    if revision is None or re.fullmatch(r"[1-9][0-9]*", revision) is None:
        raise ValueError("Missing policy revision")

    def key(secret: SecretStr | None) -> SecretBytes:
        raw = secret.get_secret_value() if secret is not None else ""
        if re.fullmatch(r"(?:[0-9a-fA-F]{2})+", raw) is None:
            raise ValueError("Invalid secret encoding")
        return SecretBytes(bytes.fromhex(raw))

    if settings.agent_d1_key_id is None or settings.agent_d1_policy_set_id is None:
        raise ValueError("Missing D1 configuration")
    keys = ProvenanceKeys(
        key_id=settings.agent_d1_key_id,
        pseudonym_key=key(settings.agent_d1_hmac_key_hex),
        encryption_key=key(settings.agent_d1_aes_key_hex),
    )
    sandbox = settings.agent_sandbox_clock_at
    profile = ClockProfile(
        authority_source="system_utc",
        authority_provider=None,
        commercial_source="injected_utc"
        if commercial_clock
        else "fixed_sandbox"
        if sandbox
        else "system_utc",
        commercial_provider=settings.agent_d1_commercial_clock_id if commercial_clock else None,
        fixed_commercial_at=utc_milliseconds(sandbox)
        if sandbox and commercial_clock is None
        else None,
    )
    common = {
        "merchant_base_url": settings.merchant_base_url,
        "policy_set_id": settings.agent_d1_policy_set_id,
        "policy_revision": int(revision),
        "currency": "COP",
        "quantity": 1,
        "clock": profile,
    }
    configuration = (
        D1EnforceConfiguration(governance_mode="enforce", **common)
        if mode == "enforce"
        else D1Configuration(governance_mode="shadow", **common)
    )
    policy, binding = None, None
    origins = settings.agent_d1_allowed_merchant_origins
    if settings.agent_d1_policy_path is not None or settings.agent_d1_policy_digest is not None:
        if settings.agent_d1_policy_path is None or settings.agent_d1_policy_digest is None:
            raise ValueError("Policy file requires a trusted digest")
        if origins is not None:
            raise ValueError("Configure either a policy file or a merchant allowlist")
        policy = settings.agent_d1_policy_path.read_bytes()
        binding = PolicyBinding(
            schema_version=SCHEMA,
            policy_set_id=configuration.policy_set_id,
            version=configuration.policy_revision,
            artifact_digest=settings.agent_d1_policy_digest,
        )
        verify_artifact(policy, binding)
    elif origins is not None:
        # Same keyed refs as each context's merchant_ref; a malformed origin raises.
        refs = {merchant_pseudonym(keys, origin.strip()) for origin in origins.split(",")}
        policy, binding = build_d1_policy(
            policy_set_id=configuration.policy_set_id,
            version=configuration.policy_revision,
            merchant_ref=tuple(sorted(refs)),
        )
    return ConfiguredD1(
        configuration,
        keys,
        ClockReader(profile, commercial=commercial_clock),
        policy,
        binding,
    )


def build_enforce_gate(
    settings: Settings, commercial_clock: Callable[[], datetime] | None
) -> "SqliteD1EnforceGate":
    """Runtime gate. Only the governed HP flow gets dispatch ports; with the blocked
    default an allow stops at its durable reservation (`d1_dispatch_blocked`)."""
    from .enforce import SqliteD1EnforceGate
    from .runtime_dispatch import SingleAttemptCheckoutDispatch, SingleAttemptPostCreateDispatch

    governed = settings.agent_hp_flow == "governed"
    try:
        configured = configured_d1(settings, commercial_clock)
        if not isinstance(configured.configuration, D1EnforceConfiguration):
            raise ValueError("D1 enforce configuration required")
        return SqliteD1EnforceGate(
            settings.agent_database_path,
            configured.configuration,
            configured.keys,
            configured.clocks,
            dispatch=SingleAttemptCheckoutDispatch() if governed else None,
            post_create_dispatch=SingleAttemptPostCreateDispatch() if governed else None,
            policy_bytes=configured.policy_bytes,
            policy_binding=configured.policy_binding,
        )
    except Exception:
        # Never fall back to ungoverned B0, and never echo settings or secrets.
        raise RuntimeError("AGENT_GOVERNANCE_MODE=enforce requires valid D1 settings") from None


def build_authorization_gate(
    settings: Settings, commercial_clock: Callable[[], datetime] | None, *, ap2: bool
) -> "SqliteD2AuthorizationGate | None":
    """D2-HP gate: enforce, governed flow and a policy artifact with D2 rules (b2-hp-v2).

    Any other configuration approves as before; b2-pilot-v1 carries no D2 rules.
    """
    from .authorization import SqliteD2AuthorizationGate

    if settings.agent_governance_mode != "enforce" or settings.agent_hp_flow != "governed":
        return None
    try:
        configured = configured_d1(settings, commercial_clock)
        if not isinstance(configured.configuration, D1EnforceConfiguration):
            raise ValueError("D1 enforce configuration required")
        policy, binding = configured.policy_bytes, configured.policy_binding
        if policy is None or binding is None:
            # The same artifact every D1 context of this configuration registers.
            policy, binding = build_d1_policy(
                policy_set_id=configured.configuration.policy_set_id,
                version=configured.configuration.policy_revision,
                merchant_ref=merchant_pseudonym(configured.keys, settings.merchant_base_url),
            )
        if verify_artifact(policy, binding).authorization is None:
            return None
        gate = SqliteD2AuthorizationGate(
            settings.agent_database_path,
            configured.configuration,
            configured.keys,
            configured.clocks,
            profile="hp-ap2/v1" if ap2 else "hp-hmac/v1",
            policy_bytes=configured.policy_bytes,
            policy_binding=configured.policy_binding,
        )
        # A ledger created before D2 only admits D1: refuse it rather than lose evidence.
        gate.ready()
        return gate
    except Exception:
        raise RuntimeError("AGENT_GOVERNANCE_MODE=enforce requires valid D2 settings") from None


def build_hnp_policy_gate(
    settings: Settings, commercial_clock: Callable[[], datetime] | None, *, trust: "MandateTrust"
) -> "SqliteHnpPolicyGate":
    """D2-HNP and D3-HNP for the executor in enforce (hnp-governed.md §3.4): `b2-hnp-v1` with
    the merchants of D1-HNP (K11) and the AP2 trust startup verified (`HnpTrust.mandates`)."""
    from .hnp_gate import SqliteHnpPolicyGate
    from .hnp_policy import operator_merchant_refs

    try:
        configured = configured_d1(settings, commercial_clock)
        if not isinstance(configured.configuration, D1EnforceConfiguration):
            raise ValueError("D1 enforce configuration required")
        gate = SqliteHnpPolicyGate(
            settings.agent_database_path,
            configured.configuration,
            configured.keys,
            configured.clocks,
            trust=trust,
            merchant_refs=operator_merchant_refs(settings, configured),
        )
        # A ledger created before D3-HNP is refused rather than losing evidence.
        gate.ready()
        return gate
    except Exception:
        raise RuntimeError("AGENT_HNP_ENABLED in enforce requires valid D2-HNP settings") from None
