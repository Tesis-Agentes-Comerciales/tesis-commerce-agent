"""Backend-only D1 provenance contracts. No runtime hooks or evaluated decisions."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, SecretBytes, field_validator, model_validator

from commerce_agent.config import validate_service_base_url

from .evaluator import GovernanceError, verify_artifact
from .models import SCHEMA, D1PolicyArtifact, Id, ObjectRef, One, PolicyBinding, UtcTime, Version


class ProvenanceModel(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, revalidate_instances="always"
    )
    schema_version: Literal["d1-provenance/1"] = "d1-provenance/1"


class ProvenanceKeys(ProvenanceModel):
    key_id: Id
    pseudonym_key: SecretBytes
    encryption_key: SecretBytes

    @model_validator(mode="after")
    def adequate(self) -> Self:
        if len(self.pseudonym_key.get_secret_value()) < 32:
            raise ValueError("A pseudonym key of at least 32 bytes is required")
        if len(self.encryption_key.get_secret_value()) != 32:
            raise ValueError("A 32-byte AES-256 key is required")
        if self.pseudonym_key.get_secret_value() == self.encryption_key.get_secret_value():
            raise ValueError("Independent encryption and pseudonym keys are required")
        return self


class ClockProfile(ProvenanceModel):
    authority_source: Literal["system_utc", "injected_utc"]
    commercial_source: Literal["system_utc", "fixed_sandbox", "injected_utc"]
    authority_provider: Id | None
    commercial_provider: Id | None
    fixed_commercial_at: UtcTime | None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.authority_source == "injected_utc") != (self.authority_provider is not None):
            raise ValueError("Authority provider must identify the injected clock")
        if (self.commercial_source == "injected_utc") != (self.commercial_provider is not None):
            raise ValueError("Commercial provider must identify the injected clock")
        if (self.commercial_source == "fixed_sandbox") != (self.fixed_commercial_at is not None):
            raise ValueError("Fixed sandbox time must be explicit")
        return self


class ClockObservation(ProvenanceModel):
    authority_now: UtcTime
    commercial_now: UtcTime


class _D1Configuration(ProvenanceModel):
    """Explicit trusted configuration; no deployment defaults and no secret settings loader.

    policy_revision is operator-managed: changing artifact bytes under the same
    policy_set_id requires a strictly newer revision in the local registry.
    """

    merchant_base_url: str
    policy_set_id: Id
    policy_revision: Version
    currency: Literal["COP"]
    quantity: One
    clock: ClockProfile

    @field_validator("merchant_base_url")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        return validate_service_base_url(value)


class D1Configuration(_D1Configuration):
    """Existing shadow configuration; still rejects enforce."""

    governance_mode: Literal["shadow"]


class D1EnforceConfiguration(_D1Configuration):
    """Explicit backend-only UoW configuration; no Settings/runtime loader."""

    governance_mode: Literal["enforce"]


class ContextData(ProvenanceModel):
    run_id: Id
    trace_id: Id
    owner_ref: Id
    purchase_mode: Literal["HP"]
    governance_mode: Literal["shadow", "enforce"]
    conversation_id: Id | None
    experiment_run_id: Id | None
    merchant_ref: Id
    commercial_ref: ObjectRef
    clock_profile_ref: ObjectRef
    normalization_ref: ObjectRef
    eligibility_algorithm_ref: ObjectRef
    policy_artifact_ref: ObjectRef
    expected_policy: PolicyBinding


class RegisteredContext(ProvenanceModel):
    ref: ObjectRef
    data: ContextData


class ContextObservation(ProvenanceModel):
    context: RegisteredContext
    expected_local_state_version: Version
    clocks: ClockObservation


def utc_milliseconds(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GovernanceError("GOV_FACTS_UNAVAILABLE")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ClockReader:
    def __init__(
        self,
        profile: ClockProfile,
        *,
        authority: Callable[[], datetime] | None = None,
        commercial: Callable[[], datetime] | None = None,
    ):
        self.profile = ClockProfile.model_validate(profile)
        if (profile.authority_source == "injected_utc") != (authority is not None):
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        if (profile.commercial_source == "injected_utc") != (commercial is not None):
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        self._authority = authority
        self._commercial = commercial

    def read(self) -> ClockObservation:
        try:
            authority = self._authority() if self._authority else datetime.now(UTC)
            fixed = self.profile.fixed_commercial_at
            commercial = (
                self._commercial()
                if self._commercial
                else datetime.fromisoformat(fixed)
                if fixed
                else datetime.now(UTC)
            )
            return ClockObservation(
                authority_now=utc_milliseconds(authority),
                commercial_now=utc_milliseconds(commercial),
            )
        except Exception:
            raise GovernanceError("GOV_FACTS_UNAVAILABLE") from None


def build_d1_policy(
    *, policy_set_id: str, version: int, merchant_ref: str
) -> tuple[bytes, PolicyBinding]:
    """Build the COP/one-unit D1 profile from trusted configuration, never a fixture."""
    from .canonical import canonical_bytes, sha256_digest

    artifact = D1PolicyArtifact(
        schema_version=SCHEMA,
        policy_set_id=policy_set_id,
        version=version,
        policy_ids=("POL-01", "POL-02"),
        allowed_actions=("prepare_checkout",),
        allowed_merchant_refs=(merchant_ref,),
        currency="COP",
        quantity=1,
    )
    payload = canonical_bytes(artifact)
    binding = PolicyBinding(
        schema_version=SCHEMA,
        policy_set_id=policy_set_id,
        version=version,
        artifact_digest=sha256_digest(payload),
    )
    verify_artifact(payload, binding)
    return payload, binding


def configured_provenance[T](mode: Literal["off", "shadow"], factory: Callable[[], T]) -> T | None:
    """Opt-in boundary: off does not invoke the factory or require keys/tables."""
    if mode == "off":
        return None
    if mode != "shadow":
        raise GovernanceError("GOV_INPUT_INVALID")
    return factory()
