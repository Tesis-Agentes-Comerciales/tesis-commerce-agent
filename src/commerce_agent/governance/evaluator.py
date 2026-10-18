"""Synchronous D1 reference and fail-closed boundary. No application effects."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .canonical import canonical_bytes, input_digest, parse_model, sha256_digest
from .models import (
    SCHEMA,
    D1PolicyArtifact,
    EngineIdentity,
    EvaluationMetadata,
    GuardReason,
    PolicyBinding,
    PolicyDecision,
    RuleResult,
    SelectionPolicyInput,
)

ENGINE_VERSION = "reference-d1:1"


class GovernanceError(ValueError):
    """Safe rejection: deliberately retains no payload, exception text or field values."""

    def __init__(self, reason: GuardReason):
        self.reason: GuardReason = reason
        super().__init__(reason)


class PolicyEvaluator(Protocol):
    def evaluate(self, policy_input: SelectionPolicyInput) -> PolicyDecision: ...


def parse_input(payload: bytes) -> SelectionPolicyInput:
    try:
        return parse_model(SelectionPolicyInput, payload)
    except Exception:
        raise GovernanceError("GOV_INPUT_INVALID") from None


def measure_reference_engine() -> EngineIdentity:
    """Local setup I/O, outside evaluation: hash the actual implementation sources.

    This is reproducible file identity, not attestation of a remote process. No
    policy input or configured version is used to manufacture a measured identity.
    """
    root = Path(__file__).parent
    manifest = {
        name: sha256_digest((root / name).read_bytes())
        for name in ("__init__.py", "canonical.py", "models.py", "evaluator.py")
    }
    return EngineIdentity(
        schema_version=SCHEMA,
        kind="python",
        version=ENGINE_VERSION,
        artifact_digest=sha256_digest(b"commerce-policy-python/v1\0" + canonical_bytes(manifest)),
        attempted=True,
    )


def verify_artifact(payload: bytes | None, binding: PolicyBinding) -> D1PolicyArtifact:
    if payload is None:
        raise GovernanceError("GOV_FACTS_UNAVAILABLE")
    try:
        verified_binding = parse_model(PolicyBinding, canonical_bytes(binding))
        artifact = parse_model(D1PolicyArtifact, payload)
        if (
            sha256_digest(payload) != verified_binding.artifact_digest
            or artifact.policy_set_id != verified_binding.policy_set_id
            or artifact.version != verified_binding.version
        ):
            raise ValueError("Artifact mismatch")
        return artifact
    except Exception:
        raise GovernanceError("GOV_ARTIFACT_MISMATCH") from None


class ReferencePolicyEvaluator:
    """Configured, verified data + measured local code. Evaluation itself is pure."""

    def __init__(
        self,
        *,
        artifact_bytes: bytes,
        binding: PolicyBinding,
        metadata: EvaluationMetadata,
        identity: EngineIdentity,
    ):
        self._artifact = verify_artifact(artifact_bytes, binding)
        self._binding = parse_model(PolicyBinding, canonical_bytes(binding))
        self._metadata = parse_model(EvaluationMetadata, canonical_bytes(metadata))
        measured = measure_reference_engine()
        if identity != measured:
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        self._identity = measured

    def evaluate(self, policy_input: SelectionPolicyInput) -> PolicyDecision:
        data = parse_input(canonical_bytes(policy_input))
        if data.governance_mode == "off":
            raise GovernanceError("GOV_INPUT_INVALID")
        if data.expected_policy != self._binding:
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        passes = (
            data.selected_variant_id in data.eligible_variant_ids,
            data.action in self._artifact.allowed_actions
            and data.merchant_ref in self._artifact.allowed_merchant_refs,
        )
        rules = (
            RuleResult(
                schema_version=SCHEMA, policy_id="POL-01", result="pass" if passes[0] else "fail"
            ),
            RuleResult(
                schema_version=SCHEMA, policy_id="POL-02", result="pass" if passes[1] else "fail"
            ),
        )
        # Build through the wire parser so catalog and consistency checks also
        # apply to dynamically assembled reasons, not only to external responses.
        reasons = (
            ("SELECTION_NOT_ELIGIBLE", "ACTION_OR_MERCHANT_NOT_ALLOWED")[index]
            for index, passed in enumerate(passes)
            if not passed
        )
        return parse_model(
            PolicyDecision,
            canonical_bytes(
                {
                    "schema_version": SCHEMA,
                    "decision_id": data.decision_id,
                    "phase": data.phase,
                    "purchase_mode": data.purchase_mode,
                    "governance_mode": data.governance_mode,
                    "outcome": "allow" if all(passes) else "deny",
                    "origin": "policy",
                    "reason_codes": tuple(reasons) or ("ALL_APPLICABLE_POLICIES_SATISFIED",),
                    "rule_results": [rule.model_dump(mode="json") for rule in rules],
                    "input_digest": input_digest(data),
                    "policy_binding": self._binding.model_dump(mode="json"),
                    "engine": self._identity.model_dump(mode="json"),
                    "engine_decision_id": None,
                    **self._metadata.model_dump(mode="json"),
                }
            ),
        )


class D1PolicyBoundary:
    """Explicit caller wrapper, unused by B0. Invalid input has no fake decision.

    The factory is configuration-owned. Failures before a valid engine result
    become operational denials; no persistence or protected effect is claimed.
    """

    def __init__(
        self,
        *,
        binding: PolicyBinding,
        artifact_bytes: bytes | None,
        engine_identity: EngineIdentity,
        metadata: EvaluationMetadata,
        evaluator_factory: Callable[[], PolicyEvaluator],
    ):
        self._binding = parse_model(PolicyBinding, canonical_bytes(binding))
        self._bytes = artifact_bytes
        self._identity = parse_model(EngineIdentity, canonical_bytes(engine_identity))
        self._metadata = parse_model(EvaluationMetadata, canonical_bytes(metadata))
        self._factory = evaluator_factory
        self._identity_failure: GuardReason | None = None
        try:
            if self._identity.kind != "python" or self._identity != measure_reference_engine():
                self._identity_failure = "GOV_ARTIFACT_MISMATCH"
        except Exception:
            self._identity_failure = "GOV_ENGINE_UNAVAILABLE"

    def evaluate(self, policy_input: SelectionPolicyInput) -> PolicyDecision:
        try:
            data = parse_input(canonical_bytes(policy_input))
        except Exception:
            raise GovernanceError("GOV_INPUT_INVALID") from None
        if data.governance_mode == "off":
            raise GovernanceError("GOV_INPUT_INVALID")
        try:
            verify_artifact(self._bytes, self._binding)
            if data.expected_policy != self._binding:
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            if self._identity_failure is not None:
                raise GovernanceError(self._identity_failure)
        except GovernanceError as exc:
            return self._deny(data, exc.reason, attempted=False)
        except Exception:
            return self._deny(data, "GOV_ENGINE_UNAVAILABLE", attempted=False)
        try:
            evaluator = self._factory()
        except Exception:
            return self._deny(data, "GOV_ENGINE_UNAVAILABLE", attempted=False)
        try:
            raw = evaluator.evaluate(data)
        except Exception:
            return self._deny(data, "GOV_ENGINE_UNAVAILABLE", attempted=True)
        try:
            result = parse_model(PolicyDecision, canonical_bytes(raw))
            if (
                result.origin != "policy"
                or result.decision_id != data.decision_id
                or result.phase != data.phase
                or result.purchase_mode != data.purchase_mode
                or result.governance_mode != data.governance_mode
                or result.input_digest != input_digest(data)
            ):
                raise ValueError("Output is unrelated to request")
            if result.policy_binding != self._binding or result.engine != self._identity:
                return self._deny(data, "GOV_ARTIFACT_MISMATCH", attempted=True)
            return result
        except Exception:
            return self._deny(data, "GOV_OUTPUT_INVALID", attempted=True)

    def _deny(
        self, data: SelectionPolicyInput, reason: GuardReason, *, attempted: bool
    ) -> PolicyDecision:
        return PolicyDecision(
            schema_version=SCHEMA,
            decision_id=data.decision_id,
            phase="D1",
            purchase_mode=data.purchase_mode,
            governance_mode=data.governance_mode,
            outcome="deny",
            origin="runtime_guard",
            reason_codes=(reason,),
            rule_results=(),
            input_digest=input_digest(data),
            policy_binding=self._binding,
            engine=EngineIdentity(
                schema_version=SCHEMA,
                kind="python",
                version=None,
                artifact_digest=None,
                attempted=attempted,
            ),
            engine_decision_id=None,
            decided_at=self._metadata.decided_at,
            latency_ms=self._metadata.latency_ms,
        )
