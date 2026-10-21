"""Recoverable reference artifacts; stored code is hashed, never executed."""

from pathlib import Path
from typing import Literal

from pydantic import model_validator

from .canonical import canonical_bytes, parse_model, sha256_digest
from .evaluator import (
    ReferenceAuthorizationEvaluator,
    ReferencePolicyEvaluator,
    measure_reference_engine,
    verify_artifact,
)
from .evidence import DecisionRecord, EvidenceError, EvidenceScope
from .models import SCHEMA, ContractModel, EvaluationMetadata, PolicyDecision

SOURCE_NAMES = ("__init__.py", "canonical.py", "models.py", "evaluator.py")


class EngineSource(ContractModel):
    name: Literal["__init__.py", "canonical.py", "models.py", "evaluator.py"]
    content: str


class ReferenceSourceArchive(ContractModel):
    sources: tuple[EngineSource, ...]

    @model_validator(mode="after")
    def exact_sources(self) -> "ReferenceSourceArchive":
        if tuple(source.name for source in self.sources) != SOURCE_NAMES:
            raise ValueError("Reference engine requires exactly four sources")
        return self

    def engine_digest(self) -> str:
        manifest = {
            source.name: sha256_digest(source.content.encode("utf-8")) for source in self.sources
        }
        return sha256_digest(b"commerce-policy-python/v1\0" + canonical_bytes(manifest))


def capture_reference_sources() -> bytes:
    """Explicit setup I/O, restricted to the four existing reference source files."""
    root = Path(__file__).parent
    return canonical_bytes(
        {
            "schema_version": SCHEMA,
            "sources": [
                {
                    "schema_version": SCHEMA,
                    "name": name,
                    "content": (root / name).read_bytes().decode("utf-8"),
                }
                for name in SOURCE_NAMES
            ],
        }
    )


def verify_artifacts(
    scope: EvidenceScope,
    policy_bytes: bytes | None,
    engine_bytes: bytes | None,
) -> None:
    try:
        if (scope.policy_artifact_ref is None) != (policy_bytes is None):
            raise ValueError("Policy reference/body missing")
        if (scope.engine_artifact_ref is None) != (engine_bytes is None):
            raise ValueError("Engine reference/body missing")
        if policy_bytes is not None:
            assert scope.policy_artifact_ref is not None
            if scope.policy_artifact_ref.digest != scope.policy_binding.artifact_digest:
                raise ValueError("Policy reference mismatch")
            verify_artifact(policy_bytes, scope.policy_binding)
        if engine_bytes is not None:
            assert scope.engine_artifact_ref is not None
            archive = parse_model(ReferenceSourceArchive, engine_bytes)
            if archive.engine_digest() != scope.engine_artifact_ref.digest:
                raise ValueError("Engine archive mismatch")
    except Exception:
        raise EvidenceError("GOV_ARTIFACT_MISMATCH") from None


def logical_result(decision: PolicyDecision) -> tuple[object, ...]:
    return decision.outcome, decision.reason_codes, decision.rule_results


def replay_reference(
    record: DecisionRecord,
    scope: EvidenceScope,
    policy_bytes: bytes | None,
    engine_bytes: bytes | None,
) -> PolicyDecision:
    """Replay only when available installed code equals the archived identity.

    Historical source bytes remain recoverable even when their installed version
    is no longer present. Never exec archived Python or use it as a capability.
    """
    verify_artifacts(scope, policy_bytes, engine_bytes)
    if record.decision.origin != "policy" or policy_bytes is None or engine_bytes is None:
        raise EvidenceError("GOV_FACTS_UNAVAILABLE")
    identity = measure_reference_engine()
    if (
        record.decision.engine != identity
        or scope.engine_artifact_ref is None
        or identity.artifact_digest != scope.engine_artifact_ref.digest
    ):
        raise EvidenceError("GOV_FACTS_UNAVAILABLE")
    metadata = EvaluationMetadata(
        schema_version=SCHEMA,
        decided_at=record.decision.decided_at,
        latency_ms=record.decision.latency_ms,
    )
    if record.decision.phase == "D2":
        result = ReferenceAuthorizationEvaluator(
            artifact_bytes=policy_bytes,
            binding=scope.policy_binding,
            identity=identity,
            metadata=metadata,
        ).evaluate(record.authorization_input())
    else:
        result = ReferencePolicyEvaluator(
            artifact_bytes=policy_bytes,
            binding=scope.policy_binding,
            identity=identity,
            metadata=metadata,
        ).evaluate(record.policy_input())
    if logical_result(result) != logical_result(record.decision):
        raise EvidenceError("GOV_OUTPUT_INVALID")
    return result
