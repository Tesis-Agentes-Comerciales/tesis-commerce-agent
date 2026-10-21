"""Synchronous D1 reference and fail-closed boundary. No application effects."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .canonical import canonical_bytes, input_digest, parse_model, sha256_digest
from .models import (
    APPLICABLE_RULES,
    POLICY_REASONS,
    SCHEMA,
    AuthorizationPolicyInput,
    AuthorizationRules,
    D1PolicyArtifact,
    EngineIdentity,
    EvaluationMetadata,
    GuardReason,
    PolicyBinding,
    PolicyDecision,
    PolicyId,
    RuleResult,
    SelectionPolicyInput,
    rules_apply,
)

# The same measured engine evaluates D1 and, since b2-hp-v2, D2.
ENGINE_VERSION = "reference-d1-d2:2"


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


def budget_satisfiable(data: SelectionPolicyInput) -> bool:
    """POL-11: the merchant's total is never below the catalog unit price times the quantity.

    Without a total budget there is nothing to exceed; without an observed price the policy
    cannot show the budget is reachable, so it fails closed.
    """
    budget = data.intent.max_total_minor
    if budget is None:
        return True
    price = data.selected_unit_minor
    return price is not None and price * data.intent.quantity <= budget


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
        if data.expected_policy != self._binding or not rules_apply(
            self._artifact.policy_ids, data.phase, data.purchase_mode
        ):
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        checks: dict[PolicyId, Callable[[], bool]] = {
            "POL-01": lambda: data.selected_variant_id in data.eligible_variant_ids,
            "POL-02": lambda: (
                data.action in self._artifact.allowed_actions
                and data.merchant_ref in self._artifact.allowed_merchant_refs
            ),
            "POL-11": lambda: budget_satisfiable(data),
        }
        # Exactly the rules the verified artifact lists, in its order.
        passes: dict[PolicyId, bool] = {
            policy_id: checks[policy_id]() for policy_id in self._artifact.policy_ids
        }
        rules = tuple(
            RuleResult(schema_version=SCHEMA, policy_id=policy_id, result="pass" if ok else "fail")
            for policy_id, ok in passes.items()
        )
        # Build through the wire parser so catalog and consistency checks also
        # apply to dynamically assembled reasons, not only to external responses.
        reasons = tuple(POLICY_REASONS[policy_id] for policy_id, ok in passes.items() if not ok)
        return parse_model(
            PolicyDecision,
            canonical_bytes(
                {
                    "schema_version": SCHEMA,
                    "decision_id": data.decision_id,
                    "phase": data.phase,
                    "purchase_mode": data.purchase_mode,
                    "governance_mode": data.governance_mode,
                    "outcome": "allow" if all(passes.values()) else "deny",
                    "origin": "policy",
                    "reason_codes": reasons or ("ALL_APPLICABLE_POLICIES_SATISFIED",),
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
            artifact = verify_artifact(self._bytes, self._binding)
            if data.expected_policy != self._binding:
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            # A policy only evaluates the rules of its own phase and purchase mode.
            if not rules_apply(artifact.policy_ids, data.phase, data.purchase_mode):
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
                # An engine may not skip or add a rule of the verified artifact (POL-11).
                or tuple(rule.policy_id for rule in result.rule_results) != artifact.policy_ids
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


# D2-HP: POL-03..06 when the human approves (docs/design/hp-p3-d2-authorization.md).


class AuthorizationEvaluator(Protocol):
    def evaluate(self, policy_input: AuthorizationPolicyInput) -> PolicyDecision: ...


def parse_authorization_input(payload: bytes) -> AuthorizationPolicyInput:
    try:
        return parse_model(AuthorizationPolicyInput, payload)
    except Exception:
        raise GovernanceError("GOV_INPUT_INVALID") from None


def verify_authorization_artifact(
    payload: bytes | None, binding: PolicyBinding
) -> AuthorizationRules:
    """The D2 rules of a verified artifact; an artifact without them cannot authorize."""
    rules = verify_artifact(payload, binding).authorization
    if rules is None:
        raise GovernanceError("GOV_ARTIFACT_MISMATCH")
    return rules


def _principal_allowed(data: AuthorizationPolicyInput, rules: AuthorizationRules) -> bool:
    """POL-03: an authenticated owner whose profile and the artifact both admit the action."""
    principal = data.principal
    return (
        principal.authentication_verified
        and principal.owns_run
        and principal.principal_ref == data.owner_ref
        and data.action in principal.permitted_actions
        and data.action in rules.allowed_actions
        and principal.authentication_basis in rules.authentication_bases
    )


def _same_episode(data: AuthorizationPolicyInput) -> bool:
    """POL-04: the D1 allow, the selection, the terms and the click are one episode."""
    d1, hp = data.d1, data.hp
    return (
        d1.outcome == "allow"
        and (d1.run_id, d1.owner_ref) == (data.run_id, data.owner_ref)
        and (d1.purchase_mode, d1.governance_mode) == (data.purchase_mode, data.governance_mode)
        and d1.intent_ref == data.intent.intent_ref
        and d1.selected_variant_id == data.selected_variant_id == data.terms.variant_id
        and d1.merchant_ref == data.terms.merchant_ref
        and hp.attempt_owner_ref == data.owner_ref
        and hp.click_received
        and hp.presented_at <= hp.clicked_at <= data.authority_now
        # The attempt lasts as long as the checkout terms, which run on the commercial clock.
        and data.commercial_now < hp.attempt_valid_until
        and hp.material_match
    )


def _terms_within_intent(data: AuthorizationPolicyInput) -> bool:
    """POL-05: the merchant's authoritative terms against the intent's hard limits."""
    intent, terms = data.intent, data.terms
    matches = data.constraint_matches
    return (
        terms.currency == intent.currency
        and terms.quantity == intent.quantity
        and terms.variant_id == data.selected_variant_id
        and (intent.max_unit_minor is None or terms.unit_minor <= intent.max_unit_minor)
        and (intent.max_total_minor is None or terms.total_minor <= intent.max_total_minor)
        and tuple(match.constraint_ref for match in matches) == intent.hard_constraint_refs
        and all(match.matches for match in matches)
    )


def _same_policy(data: AuthorizationPolicyInput) -> bool:
    """POL-06: D1 was decided under the policy and context expected now."""
    return (
        data.d1.policy_binding == data.expected_policy
        and data.d1.context_version == data.context_version
    )


def authorization_results(
    data: AuthorizationPolicyInput, rules: AuthorizationRules
) -> tuple[RuleResult, ...]:
    """Every D2 rule, without short-circuit, in catalog order."""
    passes = (
        _principal_allowed(data, rules),
        _same_episode(data),
        _terms_within_intent(data),
        _same_policy(data),
    )
    return tuple(
        RuleResult(schema_version=SCHEMA, policy_id=policy, result="pass" if passed else "fail")
        for policy, passed in zip(APPLICABLE_RULES[("D2", "HP")], passes, strict=True)
    )


class ReferenceAuthorizationEvaluator:
    """D2 reference engine: configured, verified rules and measured local code. Pure."""

    def __init__(
        self,
        *,
        artifact_bytes: bytes,
        binding: PolicyBinding,
        metadata: EvaluationMetadata,
        identity: EngineIdentity,
    ):
        self._rules = verify_authorization_artifact(artifact_bytes, binding)
        self._binding = parse_model(PolicyBinding, canonical_bytes(binding))
        self._metadata = parse_model(EvaluationMetadata, canonical_bytes(metadata))
        measured = measure_reference_engine()
        if identity != measured:
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        self._identity = measured

    def evaluate(self, policy_input: AuthorizationPolicyInput) -> PolicyDecision:
        data = parse_authorization_input(canonical_bytes(policy_input))
        if data.governance_mode == "off":
            raise GovernanceError("GOV_INPUT_INVALID")
        if data.expected_policy != self._binding or not rules_apply(
            self._rules.policy_ids, data.phase, data.purchase_mode
        ):
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        rules = authorization_results(data, self._rules)
        failed = tuple(POLICY_REASONS[rule.policy_id] for rule in rules if rule.result == "fail")
        return parse_model(
            PolicyDecision,
            canonical_bytes(
                {
                    "schema_version": SCHEMA,
                    "decision_id": data.decision_id,
                    "phase": data.phase,
                    "purchase_mode": data.purchase_mode,
                    "governance_mode": data.governance_mode,
                    "outcome": "deny" if failed else "allow",
                    "origin": "policy",
                    "reason_codes": failed or ("ALL_APPLICABLE_POLICIES_SATISFIED",),
                    "rule_results": [rule.model_dump(mode="json") for rule in rules],
                    "input_digest": input_digest(data),
                    "policy_binding": self._binding.model_dump(mode="json"),
                    "engine": self._identity.model_dump(mode="json"),
                    "engine_decision_id": None,
                    **self._metadata.model_dump(mode="json"),
                }
            ),
        )


def authorization_prerequisite(data: AuthorizationPolicyInput) -> GuardReason | None:
    """Application prerequisites of D2-HP: a policy allow never skips them."""
    freshness = data.freshness
    if freshness.status != "current" or not (
        data.commercial_now < data.terms.commercial_expires_at
    ):
        return "GOV_FRESHNESS_UNRESOLVED"
    if data.authorization_profile.status != "validated":
        return "GOV_AUTHORIZATION_PROFILE_UNRESOLVED"
    return None


class D2PolicyBoundary:
    """Fail-closed wrapper of the D2 engine, as D1PolicyBoundary is for D1.

    Every failure before a valid engine result becomes an operational denial
    (`runtime_guard`, no rule results). Persistence and effects belong to the caller.
    """

    def __init__(
        self,
        *,
        binding: PolicyBinding,
        artifact_bytes: bytes | None,
        engine_identity: EngineIdentity,
        metadata: EvaluationMetadata,
        evaluator_factory: Callable[[], AuthorizationEvaluator],
    ):
        self._binding = parse_model(PolicyBinding, canonical_bytes(binding))
        self._bytes = artifact_bytes
        self._identity = parse_model(EngineIdentity, canonical_bytes(engine_identity))
        self._metadata = parse_model(EvaluationMetadata, canonical_bytes(metadata))
        self._factory = evaluator_factory

    def evaluate(self, policy_input: AuthorizationPolicyInput) -> PolicyDecision:
        data = parse_authorization_input(canonical_bytes(policy_input))
        if data.governance_mode == "off":
            raise GovernanceError("GOV_INPUT_INVALID")
        try:
            rules = verify_authorization_artifact(self._bytes, self._binding)
            if data.expected_policy != self._binding or not rules_apply(
                rules.policy_ids, data.phase, data.purchase_mode
            ):
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            if self._identity.kind != "python" or self._identity != measure_reference_engine():
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            prerequisite = authorization_prerequisite(data)
            if prerequisite is not None:
                raise GovernanceError(prerequisite)
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
        self, data: AuthorizationPolicyInput, reason: GuardReason, *, attempted: bool
    ) -> PolicyDecision:
        return PolicyDecision(
            schema_version=SCHEMA,
            decision_id=data.decision_id,
            phase="D2",
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
