"""D2-HP terms consent only. No payment/checkout authority or runtime wiring."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol, Self

from pydantic import Field, SecretStr, model_validator

from .canonical import canonical_bytes, sha256_digest
from .dispatch_envelope import EnvelopeModel, Identity
from .models import Digest
from .preselection import instant

ACTION = "request_checkout_preparation_review"


class ConsentBlocked(RuntimeError):
    """Stable, sanitized code; never attach credentials or input payloads."""


class TermsAlreadyDecided(ConsentBlocked):
    """Another presentation of the same checkout terms holds the opposite terminal decision."""

    def __init__(self):
        super().__init__("TERMS_ALREADY_DECIDED")


class HumanIdentity(EnvelopeModel):
    principal_id: Identity
    owner_id: Identity
    run_id: Identity
    kind: Literal["human"] = "human"
    issuer: Identity
    authentication_id: Identity
    authenticated_at: str
    expires_at: str
    assurance: Literal["synthetic_test_human/1", "authenticated_human_ui/1"]


class HumanAuthenticator(Protocol):
    """Trusted backend injection, never constructed from a UI/model identity field.

    A production adapter must verify its IdP session, human kind, expiry, owner
    membership, episode ACL, revocation, and (for real UI assurance) UI origin.
    No production adapter is supplied or enabled by this experiment.
    """

    def authenticate(self, credential: SecretStr) -> HumanIdentity: ...


class CommercialTerms(EnvelopeModel):
    merchant_id: Identity
    merchant_origin: str
    remote_actor_id: Identity
    remote_run_id: Identity
    checkout_id: Identity
    checkout_status: Literal["not_ready_for_payment"]
    offer_id: Identity
    product_id: Identity
    variant_id: Identity
    offer_revision: int = Field(gt=0)
    offer_name: str
    checkout_item_name: str
    quantity: Literal[1] = 1
    unit_amount_minor: int = Field(ge=0)
    shipping_amount_minor: int = Field(ge=0)
    total_minor: int = Field(ge=0)
    currency: Literal["COP"] = "COP"
    tax_included: Literal[True] = True
    destination_country: Literal["CO"]
    destination_city: Literal["Bogota"]
    destination_postal_code: Literal["110111"]
    delivery_days: int = Field(gt=0)
    product_condition: Literal["new", "refurbished"]
    # Exact canonical ACP option list. Text, never interpreted as UI HTML.
    fulfillment_options_canonical: str
    offer_expires_at: str
    checkout_expires_at: str
    future_action: Literal["request_checkout_preparation_review"] = ACTION
    action_description: Literal[
        "Solicitar una revisión futura de preparación del checkout con estos términos."
    ] = "Solicitar una revisión futura de preparación del checkout con estos términos."
    pending_for_commercial_execution: tuple[str, ...] = (
        "Nueva autorización y comprobación de estado y vigencia",
        "Datos de comprador y dirección detallada verificados",
        "Selección de envío y preparación de pago verificadas",
        "Autorización específica de la acción comercial posterior",
    )
    offer_digest: Digest
    d17_proof_digest: Digest
    d17_envelope_digest: Digest
    d17_history_digest: Digest
    original_response_digest: Digest
    selection_seal_digest: Digest
    shadow_binding_digest: Digest
    applied_decision_id: Identity
    applied_evidence_digest: Digest
    operation_id: Identity

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.total_minor != self.unit_amount_minor + self.shipping_amount_minor:
            raise ValueError("Inconsistent commercial amounts")
        instant(self.offer_expires_at)
        instant(self.checkout_expires_at)
        return self

    def fingerprint(self) -> str:
        return sha256_digest(b"commerce-d2-hp-terms/1\0" + canonical_bytes(self))


class Presentation(EnvelopeModel):
    schema_version: Literal["commerce-d2-hp-presentation/1"] = "commerce-d2-hp-presentation/1"
    presentation_id: Identity
    episode_id: Identity
    owner_id: Identity
    run_id: Identity
    terms: CommercialTerms
    terms_fingerprint: Digest
    presented_at: str
    valid_until: str
    continuation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            self.terms_fingerprint != self.terms.fingerprint()
            or not instant(self.presented_at) < instant(self.valid_until)
            or instant(self.valid_until)
            > min(instant(self.terms.offer_expires_at), instant(self.terms.checkout_expires_at))
        ):
            raise ValueError("Invalid presentation")
        return self


class Presented(EnvelopeModel):
    presentation: Presentation
    challenge: SecretStr


class ConsentCommand(EnvelopeModel):
    presentation_id: Identity
    checkout_id: Identity
    terms_fingerprint: Digest
    future_action: Literal["request_checkout_preparation_review"]
    challenge: SecretStr
    decision: Literal["accept", "reject"]
    affirmative: bool

    @model_validator(mode="after")
    def explicit(self) -> Self:
        if self.affirmative != (self.decision == "accept"):
            raise ValueError("Explicit affirmative acceptance required")
        return self


class ConsentReceipt(EnvelopeModel):
    schema_version: Literal["commerce-d2-hp-consent/1"] = "commerce-d2-hp-consent/1"
    event_id: Identity
    presentation_id: Identity
    presentation_digest: Digest
    terms_fingerprint: Digest
    checkout_id: Identity
    future_action: Literal["request_checkout_preparation_review"] = ACTION
    identity: HumanIdentity
    decision: Literal["accept", "reject"]
    affirmative: bool
    decided_at: str
    continuation_allowed: Literal[False] = False


Clock = Callable[[], datetime]


def timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ConsentBlocked("AWARE_BACKEND_CLOCK_REQUIRED")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
