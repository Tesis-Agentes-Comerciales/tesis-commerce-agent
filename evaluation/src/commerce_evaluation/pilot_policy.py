"""The policy a pilot gives B2 and the buyer authority each scenario declares.

The manifest names the policy set, its revision and the rules it pre-registers. The merchant
allowlist and any declared revision change are the buyer's authority, so they belong to the
scenario: only B2 receives them, and each row records them (docs/design/b2-governed-pilot-v2.md).
"""

from __future__ import annotations

import importlib.metadata
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

from commerce_agent.config import Settings
from commerce_agent.governance.bootstrap import configured_d1
from commerce_evaluation.pilot_measures import D1, D2, DECISIONS

ALLOWLIST = "AGENT_D1_ALLOWED_MERCHANT_ORIGINS"
REVISION = "AGENT_D1_POLICY_REVISION"
RUN_MERCHANT = "run_merchant"
# The entry point P2 adds to the evaluation member (coordination decision 4).
EVIDENCE_VERIFIER = "verify-evidence"
# Any loopback origin builds the same rule list; the digest is not compared here.
PROBE_ORIGIN = "http://127.0.0.1:4120"


@dataclass(frozen=True)
class D1Policy:
    """The stable D1 identifiers of a pilot; only the keys change per invocation."""

    policy_set_id: str
    revision: str
    key_id: str
    rules: tuple[str, ...] = ()


# B2-PILOT-v1 ran with these and must stay reproducible; its manifest names no policy.
PILOT_V1_POLICY = D1Policy("b2-pilot-v1", "1", "b2-pilot-v1-run", ("POL-01", "POL-02"))


def policy_from_manifest(manifest: Mapping[str, Any]) -> D1Policy:
    policy = manifest.get("policy")
    if policy is None:
        return PILOT_V1_POLICY
    rules = tuple(rule for phase in policy["rules"].values() for rule in phase)
    return D1Policy(policy["policy_set_id"], policy["revision"], policy["key_id"], rules)


def scenario_allowlist(scenario: Mapping[str, Any], merchant_origin: str) -> tuple[str, ...]:
    """Merchant origins the scenario's buyer allows; v1 scenarios allow the run's merchant."""
    declared = scenario.get("governance_setup", {}).get("allowed_merchant_origins", RUN_MERCHANT)
    return (merchant_origin,) if declared == RUN_MERCHANT else tuple(declared)


def scenario_environment(
    condition_environment: Mapping[str, str], scenario: Mapping[str, Any], merchant_origin: str
) -> dict[str, str]:
    """The condition's variables with the scenario's allowlist; B0' has no allowlist to set."""
    if ALLOWLIST not in condition_environment:
        return dict(condition_environment)
    allowlist = scenario_allowlist(scenario, merchant_origin)
    return {**condition_environment, ALLOWLIST: ",".join(allowlist)}


def policy_revision_change(scenario: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return next(
        (item for item in scenario["stimuli"] if item["action"] == "policy_revision_change"),
        None,
    )


def revised_environment(
    environment: Mapping[str, str], change: Mapping[str, Any]
) -> dict[str, str]:
    """The agent's variables after a declared revision change; B0' restarts unchanged."""
    if ALLOWLIST not in environment:
        return dict(environment)
    return {
        **environment,
        REVISION: str(change["policy_revision"]),
        ALLOWLIST: ",".join(change["allowed_merchant_origins"]),
    }


def policy_configuration(
    environment: Mapping[str, str], merchant_origin: str | None
) -> dict[str, Any] | None:
    """What a B2 row records about the policy it ran with; null for B0'."""
    if ALLOWLIST not in environment:
        return None
    allowlist = environment[ALLOWLIST].split(",")
    return {
        "policy_set_id": environment["AGENT_D1_POLICY_SET_ID"],
        "revision": environment[REVISION],
        "allowed_merchant_origins": allowlist,
        "includes_run_merchant": merchant_origin in allowlist,
    }


type DigestKey = tuple[str, str, str, tuple[str, ...]]


@dataclass
class PolicyDigests:
    """Within one invocation, each allowlist maps to one artifact digest per decision point."""

    seen: dict[DigestKey, str | None] = field(default_factory=dict)

    def consistent(self, phase: str, configuration: Mapping[str, Any], digest: str | None) -> bool:
        """The first digest of a list is the reference; any other one is a mismatch."""
        key = (
            phase,
            str(configuration["policy_set_id"]),
            str(configuration["revision"]),
            tuple(configuration["allowed_merchant_origins"]),
        )
        return self.seen.setdefault(key, digest) == digest


def policy_mismatch(
    configurations: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any] | None,
    digests: PolicyDigests,
) -> bool | None:
    """Whether a decision ran under another policy than the one in force at its decision point.

    D1 decides under the first configuration and D2 under the last: S08 revises the policy in
    between. Each decision must carry that configuration's set and revision, and its list one
    digest within the invocation. Null without a configuration (B0') or a bound decision.
    """
    if not configurations or evidence is None:
        return None
    expected = {D1: configurations[0], D2: configurations[-1]}
    checked, mismatch = False, False
    for phase, key in DECISIONS.items():
        configuration = expected[phase]
        for decision in evidence.get(key) or []:
            binding = decision.get("policy_binding")
            if binding is None:
                continue
            checked = True
            configured = (configuration["policy_set_id"], int(configuration["revision"]))
            # Only a decision under the configured policy may set the reference digest.
            same_policy = (binding["policy_set_id"], int(binding["version"])) == configured
            consistent = same_policy and digests.consistent(
                phase, configuration, binding.get("artifact_digest")
            )
            mismatch = mismatch or not consistent
    return mismatch if checked else None


def _built_rules(policy: D1Policy) -> set[str]:
    """Rule ids in the artifact the agent itself builds for this policy and an allowlist."""
    # model_construct reads no .env and no process variable: only these values and defaults.
    settings = Settings.model_construct(
        agent_governance_mode="enforce",
        agent_hp_flow="governed",
        agent_d1_policy_set_id=policy.policy_set_id,
        agent_d1_policy_revision=policy.revision,
        agent_d1_policy_path=None,
        agent_d1_policy_digest=None,
        agent_d1_allowed_merchant_origins=PROBE_ORIGIN,
        agent_d1_key_id=policy.key_id,
        agent_d1_hmac_key_hex=SecretStr(secrets.token_hex(32)),
        agent_d1_aes_key_hex=SecretStr(secrets.token_hex(32)),
    )
    artifact = configured_d1(settings, None).policy_bytes or b""
    return set(re.findall(r"POL-\d{2}", artifact.decode()))


def _verifier_installed() -> bool:
    return bool(importlib.metadata.entry_points(group="console_scripts", name=EVIDENCE_VERIFIER))


def integration_readiness(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """What the manifest needs from P1, P2 and P3 and whether this checkout provides it.

    Policy rules: every rule the manifest pre-registers is in the artifact the agent builds (P1
    adds POL-11, P3 adds POL-03..06). Verifier: `verify-evidence` is installed (P2).
    """
    readiness: dict[str, dict[str, Any]] = {}
    policy = policy_from_manifest(manifest)
    if "policy" in manifest:
        missing = sorted(set(policy.rules) - _built_rules(policy))
        readiness["policy_rules"] = {"ready": not missing, "missing": missing}
    if "evidence_chain" in manifest:
        readiness["evidence_verifier"] = {
            "ready": _verifier_installed(),
            "entry_point": EVIDENCE_VERIFIER,
        }
    return readiness
