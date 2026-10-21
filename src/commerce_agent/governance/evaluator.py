"""Synchronous D1, D2 and D3 reference engine and its fail-closed boundaries. No effects."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from .canonical import canonical_bytes, input_digest, parse_model, sha256_digest
from .hnp_models import DispatchPolicyInput, HnpAuthorizationPolicyInput, parse_d2_input
from .hnp_rules import (
    delegation_prerequisite,
    delegation_results,
    dispatch_prerequisite,
    dispatch_results,
)
from .models import (
    APPLICABLE_RULES,
    POLICY_REASONS,
    SCHEMA,
    Action,
    AuthorizationRules,
    D1PolicyArtifact,
    DispatchRules,
    EngineIdentity,
    EvaluationMetadata,
    GuardReason,
    HnpAuthorizationRules,
    HpAuthorizationPolicyInput,
    Ids,
    ObjectRef,
    PolicyBinding,
    PolicyDecision,
    PolicyId,
    RuleResult,
    Rules,
    SelectionPolicyInput,
    policy_set_matches_mode,
    rules_apply,
)

# The same measured engine evaluates D1, D2 (HP since b2-hp-v2, HNP) and D3 (HNP, b2-hnp-v1).
ENGINE_VERSION = "reference-d1-d2-d3:3"
# The files whose bytes are the engine's identity: everything the evaluation imports.
ENGINE_SOURCES = (
    "__init__.py",
    "canonical.py",
    "models.py",
    "evaluator.py",
    "hnp_models.py",
    "hnp_rules.py",
)


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
    manifest = {name: sha256_digest((root / name).read_bytes()) for name in ENGINE_SOURCES}
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
        if (
            data.expected_policy != self._binding
            or not policy_set_matches_mode(self._artifact.policy_set_id, data.purchase_mode)
            or not rules_apply(self._artifact.policy_ids, data.phase, data.purchase_mode)
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
            # A policy only evaluates the rules of its own phase and purchase mode, and an HNP
            # purchase only b2-hnp-v1 (C02): both deny before the engine is called.
            if not policy_set_matches_mode(
                artifact.policy_set_id, data.purchase_mode
            ) or not rules_apply(artifact.policy_ids, data.phase, data.purchase_mode):
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


# D2 and D3: POL-03..06 when the human approves (docs/design/hp-p3-d2-authorization.md), plus
# POL-07..09 for an autonomous purchase (D2-HNP) and POL-09/10 before its dispatch (D3-HNP,
# docs/design/hnp-governed.md §7).

type D2Input = HpAuthorizationPolicyInput | HnpAuthorizationPolicyInput
type PhaseInput = HpAuthorizationPolicyInput | HnpAuthorizationPolicyInput | DispatchPolicyInput
type PhaseRules = AuthorizationRules | HnpAuthorizationRules | DispatchRules


class RulesEvaluator[T: PhaseInput](Protocol):
    def evaluate(self, policy_input: T) -> PolicyDecision: ...


class AuthorizationEvaluator(Protocol):
    def evaluate(self, policy_input: HpAuthorizationPolicyInput) -> PolicyDecision: ...


class HnpAuthorizationEvaluator(Protocol):
    def evaluate(self, policy_input: HnpAuthorizationPolicyInput) -> PolicyDecision: ...


class DispatchEvaluator(Protocol):
    def evaluate(self, policy_input: DispatchPolicyInput) -> PolicyDecision: ...


def parse_authorization_input(payload: bytes) -> HpAuthorizationPolicyInput:
    """The HP branch of D2; an HNP input is not one (C02)."""
    try:
        return parse_model(HpAuthorizationPolicyInput, payload)
    except Exception:
        raise GovernanceError("GOV_INPUT_INVALID") from None


def parse_hnp_authorization_input(payload: bytes) -> HnpAuthorizationPolicyInput:
    """The HNP branch of D2, selected by `purchase_mode` like `hnp_models.parse_d2_input`."""
    try:
        data = parse_d2_input(payload)
    except Exception:
        raise GovernanceError("GOV_INPUT_INVALID") from None
    if not isinstance(data, HnpAuthorizationPolicyInput):
        raise GovernanceError("GOV_INPUT_INVALID")
    return data


def parse_dispatch_input(payload: bytes) -> DispatchPolicyInput:
    try:
        return parse_model(DispatchPolicyInput, payload)
    except Exception:
        raise GovernanceError("GOV_INPUT_INVALID") from None


def verify_authorization_artifact(
    payload: bytes | None, binding: PolicyBinding
) -> AuthorizationRules:
    """The D2-HP rules of a verified artifact; an artifact without them cannot authorize."""
    rules = verify_artifact(payload, binding).authorization
    if not isinstance(rules, AuthorizationRules):
        raise GovernanceError("GOV_ARTIFACT_MISMATCH")
    return rules


def verify_hnp_authorization_artifact(
    payload: bytes | None, binding: PolicyBinding
) -> HnpAuthorizationRules:
    """The D2-HNP rules of a verified artifact (only `b2-hnp-v1` has them)."""
    rules = verify_artifact(payload, binding).authorization
    if not isinstance(rules, HnpAuthorizationRules):
        raise GovernanceError("GOV_ARTIFACT_MISMATCH")
    return rules


def verify_dispatch_artifact(payload: bytes | None, binding: PolicyBinding) -> DispatchRules:
    """The D3-HNP rules of a verified artifact (only `b2-hnp-v1` has them)."""
    rules = verify_artifact(payload, binding).dispatch
    if rules is None:
        raise GovernanceError("GOV_ARTIFACT_MISMATCH")
    return rules


def _principal_admitted(data: D2Input, actions: Sequence[Action], bases: Sequence[str]) -> bool:
    """POL-03 of both branches: authenticated, owning the run, and the action and the basis
    admitted by the principal's profile and by the artifact."""
    principal = data.principal
    return (
        principal.authentication_verified
        and principal.owns_run
        and data.action in principal.permitted_actions
        and data.action in actions
        and principal.authentication_basis in bases
    )


def _principal_allowed(data: HpAuthorizationPolicyInput, rules: AuthorizationRules) -> bool:
    """POL-03 (HP): an authenticated owner whose profile and the artifact both admit the action."""
    return data.principal.principal_ref == data.owner_ref and _principal_admitted(
        data, rules.allowed_actions, rules.authentication_bases
    )


def _executor_allowed(data: HnpAuthorizationPolicyInput, rules: HnpAuthorizationRules) -> bool:
    """POL-03 (HNP): the executor service, authenticated by the grant of the run's owner."""
    return _principal_admitted(data, rules.allowed_actions, rules.authentication_bases)


def _d1_allows_this_episode(data: D2Input) -> bool:
    """POL-04, both branches: the D1 allow of this run, owner, modes, intent and selection."""
    d1 = data.d1
    return (
        d1.outcome == "allow"
        and (d1.run_id, d1.owner_ref) == (data.run_id, data.owner_ref)
        and (d1.purchase_mode, d1.governance_mode) == (data.purchase_mode, data.governance_mode)
        and d1.intent_ref == data.intent.intent_ref
        and d1.selected_variant_id == data.selected_variant_id == data.terms.variant_id
        and d1.merchant_ref == data.terms.merchant_ref
    )


def _same_episode(data: HpAuthorizationPolicyInput) -> bool:
    """POL-04 (HP): the D1 allow, the selection, the terms and the click are one episode."""
    hp = data.hp
    return (
        _d1_allows_this_episode(data)
        and hp.attempt_owner_ref == data.owner_ref
        and hp.click_received
        and hp.presented_at <= hp.clicked_at <= data.authority_now
        # The attempt lasts as long as the checkout terms, which run on the commercial clock.
        and data.commercial_now < hp.attempt_valid_until
        and hp.material_match
    )


def _same_delegation(data: HnpAuthorizationPolicyInput) -> bool:
    """POL-04 (HNP): the D1 allow, and the grant, reservation and operation of this run and
    owner (policy-v1, POL-04)."""
    grant, intent = data.hnp.grant, data.hnp.reservation_intent
    return (
        _d1_allows_this_episode(data)
        and grant.owner_ref == data.owner_ref
        and intent.grant_ref == grant.grant_ref
        and (intent.run_id, intent.owner_ref, intent.operation_id)
        == (data.run_id, data.owner_ref, data.operation_id)
    )


def _terms_within_intent(data: D2Input) -> bool:
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


def _same_policy(data: D2Input) -> bool:
    """POL-06: D1 was decided under the policy and context expected now."""
    return (
        data.d1.policy_binding == data.expected_policy
        and data.d1.context_version == data.context_version
    )


def _results(policies: Rules, passes: Sequence[bool]) -> tuple[RuleResult, ...]:
    return tuple(
        RuleResult(schema_version=SCHEMA, policy_id=policy, result="pass" if passed else "fail")
        for policy, passed in zip(policies, passes, strict=True)
    )


def authorization_results(
    data: HpAuthorizationPolicyInput, rules: AuthorizationRules
) -> tuple[RuleResult, ...]:
    """Every D2-HP rule, without short-circuit, in catalog order."""
    passes = (
        _principal_allowed(data, rules),
        _same_episode(data),
        _terms_within_intent(data),
        _same_policy(data),
    )
    return _results(APPLICABLE_RULES[("D2", "HP")], passes)


def hnp_authorization_results(
    data: HnpAuthorizationPolicyInput, rules: HnpAuthorizationRules
) -> tuple[RuleResult, ...]:
    """Every D2-HNP rule, POL-03..09, without short-circuit, in catalog order."""
    passes = (
        _executor_allowed(data, rules),
        _same_delegation(data),
        _terms_within_intent(data),
        _same_policy(data),
    )
    return (*_results(APPLICABLE_RULES[("D2", "HNP")][:4], passes), *delegation_results(data))


def _policy_decision(
    data: PhaseInput,
    rules: tuple[RuleResult, ...],
    binding: PolicyBinding,
    identity: EngineIdentity,
    metadata: EvaluationMetadata,
) -> PolicyDecision:
    failed = tuple(POLICY_REASONS[rule.policy_id] for rule in rules if rule.result == "fail")
    # Built through the wire parser so catalog and consistency checks apply here too.
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
                "policy_binding": binding.model_dump(mode="json"),
                "engine": identity.model_dump(mode="json"),
                "engine_decision_id": None,
                **metadata.model_dump(mode="json"),
            }
        ),
    )


class _ReferenceRules[T: PhaseInput, R: PhaseRules]:
    """A D2 or D3 reference engine: configured, verified rules and measured local code. Pure."""

    def __init__(
        self,
        *,
        artifact_bytes: bytes,
        binding: PolicyBinding,
        metadata: EvaluationMetadata,
        identity: EngineIdentity,
    ):
        artifact = verify_artifact(artifact_bytes, binding)
        self._rules = self._section(artifact_bytes, binding)
        self._policy_set_id = artifact.policy_set_id
        self._binding = parse_model(PolicyBinding, canonical_bytes(binding))
        self._metadata = parse_model(EvaluationMetadata, canonical_bytes(metadata))
        measured = measure_reference_engine()
        if identity != measured:
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        self._identity = measured

    def _section(self, payload: bytes, binding: PolicyBinding) -> R:
        raise NotImplementedError

    def _parse(self, payload: bytes) -> T:
        raise NotImplementedError

    def _results(self, data: T) -> tuple[RuleResult, ...]:
        raise NotImplementedError

    def evaluate(self, policy_input: T) -> PolicyDecision:
        data = self._parse(canonical_bytes(policy_input))
        if data.governance_mode == "off":
            raise GovernanceError("GOV_INPUT_INVALID")
        if (
            data.expected_policy != self._binding
            or not policy_set_matches_mode(self._policy_set_id, data.purchase_mode)
            or not rules_apply(self._rules.policy_ids, data.phase, data.purchase_mode)
        ):
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        return _policy_decision(
            data, self._results(data), self._binding, self._identity, self._metadata
        )


class ReferenceAuthorizationEvaluator(
    _ReferenceRules[HpAuthorizationPolicyInput, AuthorizationRules]
):
    """D2-HP reference engine (POL-03..06)."""

    def _section(self, payload: bytes, binding: PolicyBinding) -> AuthorizationRules:
        return verify_authorization_artifact(payload, binding)

    def _parse(self, payload: bytes) -> HpAuthorizationPolicyInput:
        return parse_authorization_input(payload)

    def _results(self, data: HpAuthorizationPolicyInput) -> tuple[RuleResult, ...]:
        return authorization_results(data, self._rules)


class ReferenceHnpAuthorizationEvaluator(
    _ReferenceRules[HnpAuthorizationPolicyInput, HnpAuthorizationRules]
):
    """D2-HNP reference engine (POL-03..09)."""

    def _section(self, payload: bytes, binding: PolicyBinding) -> HnpAuthorizationRules:
        return verify_hnp_authorization_artifact(payload, binding)

    def _parse(self, payload: bytes) -> HnpAuthorizationPolicyInput:
        return parse_hnp_authorization_input(payload)

    def _results(self, data: HnpAuthorizationPolicyInput) -> tuple[RuleResult, ...]:
        return hnp_authorization_results(data, self._rules)


class ReferenceDispatchEvaluator(_ReferenceRules[DispatchPolicyInput, DispatchRules]):
    """D3-HNP reference engine (POL-09, POL-10)."""

    def _section(self, payload: bytes, binding: PolicyBinding) -> DispatchRules:
        return verify_dispatch_artifact(payload, binding)

    def _parse(self, payload: bytes) -> DispatchPolicyInput:
        return parse_dispatch_input(payload)

    def _results(self, data: DispatchPolicyInput) -> tuple[RuleResult, ...]:
        return dispatch_results(data, self._rules.allowed_actions)


def authorization_prerequisite(data: HpAuthorizationPolicyInput) -> GuardReason | None:
    """Application prerequisites of D2-HP: a policy allow never skips them."""
    freshness = data.freshness
    if freshness.status != "current" or not (
        data.commercial_now < data.terms.commercial_expires_at
    ):
        return "GOV_FRESHNESS_UNRESOLVED"
    if data.authorization_profile.status != "validated":
        return "GOV_AUTHORIZATION_PROFILE_UNRESOLVED"
    return None


def _admitted_profile(proof: ObjectRef | None, profile: ObjectRef | None, admitted: Ids) -> bool:
    """A proof checked against the validated profile, one the artifact admits (K6)."""
    return profile is not None and profile.id in admitted and (proof is None or proof == profile)


def hnp_authorization_prerequisite(
    data: HnpAuthorizationPolicyInput, rules: HnpAuthorizationRules
) -> GuardReason | None:
    """D2-HNP guards (hnp_rules), then the open proof under the admitted profile."""
    reason = delegation_prerequisite(data)
    if reason is not None:
        return reason
    profile = data.authorization_profile.profile_ref
    if not _admitted_profile(data.hnp.open_proof.profile_ref, profile, rules.profiles):
        return "GOV_AUTHORIZATION_PROFILE_UNRESOLVED"
    return None


def dispatch_authority_prerequisite(
    data: DispatchPolicyInput, rules: HnpAuthorizationRules
) -> GuardReason | None:
    """D3-HNP guards (hnp_rules), then the closed proof under a profile the artifact admits."""
    reason = dispatch_prerequisite(data)
    if reason is not None:
        return reason
    proof = data.closed_proof.profile_ref
    if proof is not None and proof.id not in rules.profiles:
        return "GOV_AUTHORIZATION_PROFILE_UNRESOLVED"
    return None


class _PolicyBoundary[T: PhaseInput, R: PhaseRules]:
    """Fail-closed wrapper of a D2 or D3 engine, as D1PolicyBoundary is for D1.

    Every failure before a valid engine result becomes an operational denial (`runtime_guard`,
    no rule results); C02 denies before the engine is called. Persistence and effects belong to
    the caller.
    """

    def __init__(
        self,
        *,
        binding: PolicyBinding,
        artifact_bytes: bytes | None,
        engine_identity: EngineIdentity,
        metadata: EvaluationMetadata,
        evaluator_factory: Callable[[], RulesEvaluator[T]],
    ):
        self._binding = parse_model(PolicyBinding, canonical_bytes(binding))
        self._bytes = artifact_bytes
        self._identity = parse_model(EngineIdentity, canonical_bytes(engine_identity))
        self._metadata = parse_model(EvaluationMetadata, canonical_bytes(metadata))
        self._factory = evaluator_factory

    def _parse(self, payload: bytes) -> T:
        raise NotImplementedError

    def _section(self, payload: bytes | None) -> R:
        raise NotImplementedError

    def _prerequisite(self, data: T, rules: R, artifact: D1PolicyArtifact) -> GuardReason | None:
        raise NotImplementedError

    def evaluate(self, policy_input: T) -> PolicyDecision:
        data = self._parse(canonical_bytes(policy_input))
        if data.governance_mode == "off":
            raise GovernanceError("GOV_INPUT_INVALID")
        try:
            artifact = verify_artifact(self._bytes, self._binding)
            rules = self._section(self._bytes)
            # An HNP purchase only under b2-hnp-v1 and an HP one never (C02), both with the
            # rules of their phase and mode: a mismatch denies before the engine is called.
            if (
                data.expected_policy != self._binding
                or not policy_set_matches_mode(artifact.policy_set_id, data.purchase_mode)
                or not rules_apply(rules.policy_ids, data.phase, data.purchase_mode)
            ):
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            if self._identity.kind != "python" or self._identity != measure_reference_engine():
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            prerequisite = self._prerequisite(data, rules, artifact)
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
                # An engine may not skip or add a rule of the verified artifact.
                or tuple(rule.policy_id for rule in result.rule_results) != rules.policy_ids
            ):
                raise ValueError("Output is unrelated to request")
            if result.policy_binding != self._binding or result.engine != self._identity:
                return self._deny(data, "GOV_ARTIFACT_MISMATCH", attempted=True)
            return result
        except Exception:
            return self._deny(data, "GOV_OUTPUT_INVALID", attempted=True)

    def _deny(self, data: T, reason: GuardReason, *, attempted: bool) -> PolicyDecision:
        return PolicyDecision(
            schema_version=SCHEMA,
            decision_id=data.decision_id,
            phase=data.phase,
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


class D2PolicyBoundary(_PolicyBoundary[HpAuthorizationPolicyInput, AuthorizationRules]):
    """D2-HP: POL-03..06 when the human approves."""

    def _parse(self, payload: bytes) -> HpAuthorizationPolicyInput:
        return parse_authorization_input(payload)

    def _section(self, payload: bytes | None) -> AuthorizationRules:
        return verify_authorization_artifact(payload, self._binding)

    def _prerequisite(
        self,
        data: HpAuthorizationPolicyInput,
        rules: AuthorizationRules,
        artifact: D1PolicyArtifact,
    ) -> GuardReason | None:
        return authorization_prerequisite(data)


class HnpD2PolicyBoundary(_PolicyBoundary[HnpAuthorizationPolicyInput, HnpAuthorizationRules]):
    """D2-HNP: POL-03..09 after the `close` freshness check, before the reservation."""

    def _parse(self, payload: bytes) -> HnpAuthorizationPolicyInput:
        return parse_hnp_authorization_input(payload)

    def _section(self, payload: bytes | None) -> HnpAuthorizationRules:
        return verify_hnp_authorization_artifact(payload, self._binding)

    def _prerequisite(
        self,
        data: HnpAuthorizationPolicyInput,
        rules: HnpAuthorizationRules,
        artifact: D1PolicyArtifact,
    ) -> GuardReason | None:
        return hnp_authorization_prerequisite(data, rules)


class D3PolicyBoundary(_PolicyBoundary[DispatchPolicyInput, DispatchRules]):
    """D3-HNP: POL-09 and POL-10 after the close, before the outbox row is committed."""

    def _parse(self, payload: bytes) -> DispatchPolicyInput:
        return parse_dispatch_input(payload)

    def _section(self, payload: bytes | None) -> DispatchRules:
        return verify_dispatch_artifact(payload, self._binding)

    def _prerequisite(
        self, data: DispatchPolicyInput, rules: DispatchRules, artifact: D1PolicyArtifact
    ) -> GuardReason | None:
        authorization = artifact.authorization
        if not isinstance(authorization, HnpAuthorizationRules):
            return "GOV_ARTIFACT_MISMATCH"
        return dispatch_authority_prerequisite(data, authorization)
