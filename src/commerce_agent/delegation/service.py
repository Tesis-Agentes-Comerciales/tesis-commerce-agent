"""Proposal, consent, views and revocation of delegations (docs/design/hnp-governed.md §3.2, §5).

The buyer proposes a typed scope and consents to its exact digest. Only then does the provider
key sign the open mandates (HNP-04), and the ledger (HNP-02) creates the grant, the mandates and
the task in one transaction. No model reaches this service, and no view carries a mandate, a
token, the address or the owner's identity.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from commerce_agent.acp.feed import ProductFeed
from commerce_agent.ap2_hnp import (
    MERCHANT_NAME,
    AcceptableItem,
    MandateTrust,
    OpenMandateIssuer,
    verify_open_proof,
)
from commerce_agent.ap2_hnp import SignedMandate as IssuedMandate
from commerce_agent.application.purchase import CheckoutDetails
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes
from commerce_agent.governance.destination import destination_binding
from commerce_agent.governance.hnp_models import (
    MAX_SCOPE_VARIANTS,
    ConsentScope,
    PositiveMinor,
    PreferenceText,
    parse_consent_scope,
    scope_digest,
)
from commerce_agent.governance.models import SCHEMA, Digest, Id
from commerce_agent.governance.provenance import ProvenanceKeys, merchant_pseudonym, pseudonym
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger, utc_text

from .contract import (
    DelegationList,
    DelegationProposal,
    DelegationView,
    ProposalId,
    RevokeAllReceipt,
    RevokeReceipt,
    TaskView,
)
from .models import (
    ConsentRequest,
    Execution,
    KillSwitchState,
    LedgerCounts,
    ProposalDraft,
    ProposalRecord,
    SignedMandate,
)
from .views import Purchase, delegation_view, revoke_receipt, scope_view, task_view

logger = logging.getLogger(__name__)

INSTRUMENT_LABEL = "Tarjeta de prueba del sandbox"
_CONSENT_ERRORS = {
    "not_found": "delegation_proposal_not_found",
    "scope_mismatch": "delegation_scope_mismatch",
    "proposal_expired": "delegation_proposal_expired",
}


class DelegationError(Exception):
    """An expected refusal; `code` is one of §5.1/§5.2 and never carries internal detail."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DelegationProposalRequest(_Body):
    """`POST /delegations/proposals` (§5.1); the variants come back sorted, as the scope keeps
    them."""

    variant_ids: Annotated[tuple[Id, ...], Field(min_length=1, max_length=MAX_SCOPE_VARIANTS)]
    total_max_minor: PositiveMinor
    currency: Literal["COP"]
    valid_for_seconds: Annotated[int, Field(ge=60)]
    details: CheckoutDetails
    preference_text: PreferenceText | None = None

    @field_validator("variant_ids")
    @classmethod
    def _distinct(cls, ids: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(ids)) != len(ids):
            raise ValueError("The variants of a scope are distinct")
        return tuple(sorted(ids))


class DelegationConsentRequest(_Body):
    """`POST /delegations`: the click on «Autorizar» names the exact digest it saw."""

    proposal_id: ProposalId
    scope_digest: Digest


@dataclass(frozen=True)
class HnpTrust:
    """What startup verified (delegation/bootstrap.py): the vault keys of the owner and
    destination pseudonyms, the provider-key issuer and the distinct Agent Key it binds."""

    keys: ProvenanceKeys
    issuer: OpenMandateIssuer
    agent_public_jwk: dict[str, Any]
    agent_key_kid: str
    merchant_issuer: str

    @property
    def mandates(self) -> MandateTrust:
        return MandateTrust(self.issuer.public_jwk, self.agent_public_jwk, self.merchant_issuer)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class DelegationService:
    def __init__(
        self,
        ledger: SqliteDelegationLedger,
        trust: HnpTrust,
        *,
        feed: Callable[[], ProductFeed],
        purchase: Callable[[str], Purchase | None],
        max_validity_seconds: int,
        proposal_ttl_seconds: int,
        clock: Callable[[], datetime] = _utc_now,
        new_id: Callable[[str], str] = _new_id,
    ) -> None:
        self._ledger = ledger
        self._trust = trust
        self._feed = feed
        self._purchase = purchase
        self._max_validity = max_validity_seconds
        self._ttl = timedelta(seconds=proposal_ttl_seconds)
        self._clock = clock
        self._new_id = new_id

    # Proposal and consent (§3.2)

    def propose(self, owner_id: str, request: DelegationProposalRequest) -> DelegationProposal:
        if request.valid_for_seconds > self._max_validity:
            raise DelegationError("delegation_validity_exceeded")
        feed = self._feed()
        offered = {
            item.variant_id: item for item in feed.candidates if item.currency.upper() == "COP"
        }
        if any(variant not in offered for variant in request.variant_ids):
            raise DelegationError("delegation_variant_unavailable")
        now = self._clock()
        proposal_id = self._new_id("dpr")
        presented_at = utc_text(now)
        expires_at = utc_text(now + timedelta(seconds=request.valid_for_seconds))
        keys = self._trust.keys
        fulfillment = request.details.fulfillment
        consent = {
            "schema_version": "commerce-delegation/1.0",
            "proposal_id": proposal_id,
            "owner_ref": self._owner_ref(owner_id),
            "scope": {
                "schema_version": SCHEMA,
                "merchant_refs": [merchant_pseudonym(keys, self._trust.merchant_issuer)],
                "variant_ids": list(request.variant_ids),
                "currency": "COP",
                "quantity_max": 1,
                "total_max_minor": request.total_max_minor,
                # K8: the bytes the merchant echoes as `fulfillment_details`, which D2 binds.
                "destination_binding": destination_binding(
                    keys, f"destination:{proposal_id}", fulfillment.model_dump(mode="json")
                ),
                "instrument_ref": "tesis_sandbox",
                "uses_max": 1,
            },
            "display": {
                "merchant_name": feed.seller_name or MERCHANT_NAME,
                "variants": [
                    {"variant_id": variant, "title": offered[variant].variant_title}
                    for variant in request.variant_ids
                ],
                "total_max": {"minor": request.total_max_minor, "currency": "COP"},
                "expires_at": expires_at,
                "destination_label": f"{fulfillment.address.city}, {fulfillment.address.state}",
                "instrument_label": INSTRUMENT_LABEL,
                "uses": 1,
                "preference_text": request.preference_text,
            },
            "presented_at": presented_at,
            "expires_at": expires_at,
            "preference_text": request.preference_text,
        }
        # The ledger stores these bytes as received: they are validated here, before storing.
        payload = canonical_bytes(consent)
        scope = parse_consent_scope(payload)
        draft = ProposalDraft(
            proposal_id=proposal_id,
            owner_id=owner_id,
            scope_json=payload.decode(),
            scope_digest=scope_digest(scope),
            checkout_details_json=canonical_bytes(request.details).decode(),
            presented_at=presented_at,
            expires_at=utc_text(now + self._ttl),
        )
        self._ledger.create_proposal(draft)
        return DelegationProposal.model_validate(
            {
                "proposal_id": proposal_id,
                "scope": scope_view(scope),
                "scope_digest": draft.scope_digest,
                "presented_at": presented_at,
                "expires_at": draft.expires_at,
            }
        )

    def consent(
        self, owner_id: str, request: DelegationConsentRequest
    ) -> tuple[DelegationView, bool]:
        """The view and whether this call created the grant (201) or found it (200)."""
        proposal = self._ledger.proposal(owner_id, request.proposal_id)
        if proposal is None:
            raise DelegationError("delegation_proposal_not_found")
        if proposal.scope_digest != request.scope_digest:
            raise DelegationError("delegation_scope_mismatch")
        if proposal.grant_id is not None:
            return self._view(proposal.grant_id), False
        scope = parse_consent_scope(proposal.scope_json.encode())
        now = self._clock()
        # The grant's expiry was fixed at proposal time: it can lapse before the proposal does.
        if utc_text(now) >= min(proposal.expires_at, scope.expires_at):
            raise DelegationError("delegation_proposal_expired")
        result = self._ledger.consent(self._signed(owner_id, proposal, scope, now))
        if result.grant is None:
            raise DelegationError(_CONSENT_ERRORS[result.status])
        return self._view(result.grant.grant_id), result.status == "created"

    def _signed(
        self, owner_id: str, proposal: ProposalRecord, consent: ConsentScope, now: datetime
    ) -> ConsentRequest:
        """The open mandates of exactly this scope, checked as the merchant and the PSP will."""
        scope, issuer = consent.scope, self._trust.issuer
        expires_at = datetime.fromisoformat(consent.expires_at)
        items = [AcceptableItem(item.variant_id, item.title) for item in consent.display.variants]
        try:
            pair = issuer.issue(
                items,
                total_max_minor=scope.total_max_minor,
                consented_at=now,
                expires_at=expires_at,
            )
            proof = verify_open_proof(
                pair.checkout.token,
                pair.payment.token,
                trust=self._trust.mandates,
                variant_ids=scope.variant_ids,
                total_max_minor=scope.total_max_minor,
                expires_at=expires_at,
                now=now,
            )
        except Exception:
            # No detail: a signing error may carry key material.
            logger.warning("Open mandate signing failed for proposal %s", proposal.proposal_id)
            raise DelegationError("delegation_signer_unavailable") from None
        if proof.status != "valid":
            logger.warning("Open mandates of proposal %s do not verify", proposal.proposal_id)
            raise DelegationError("delegation_signer_unavailable")
        # §3.3: the intent of an HNP run is derived from the scope, never from the model.
        intent = PurchaseIntent(
            hard=HardConstraints(currency="COP", max_total_amount_minor=scope.total_max_minor)
        )
        return ConsentRequest(
            owner_id=owner_id,
            proposal_id=proposal.proposal_id,
            scope_digest=proposal.scope_digest,
            intent_json=canonical_bytes(intent).decode(),
            agent_key_kid=self._trust.agent_key_kid,
            issuer_kid=issuer.kid,
            open_checkout=_stored(pair.checkout),
            open_payment=_stored(pair.payment),
        )

    # Views (§5.3). Owner-scoped reads answer None for another owner: not found, as missing.

    def delegations(self, owner_id: str) -> DelegationList:
        grants = self._ledger.grants(owner_id)
        return DelegationList(delegations=tuple(self._view(grant.grant_id) for grant in grants))

    def delegation(self, owner_id: str, grant_id: str) -> DelegationView:
        if self._ledger.grant(owner_id, grant_id) is None:
            raise DelegationError("delegation_not_found")
        return self._view(grant_id)

    def task(self, task_id: str) -> TaskView:
        execution = self._ledger.execution(task_id)
        if execution is None:
            raise DelegationError("hnp_task_not_found")
        return task_view(execution, self._run_purchase(execution), utc_text(self._clock()))

    def _view(self, grant_id: str) -> DelegationView:
        task = self._ledger.task_for_grant(grant_id)
        execution = self._ledger.execution(task.task_id) if task is not None else None
        if execution is None:
            raise LookupError("A grant without its task")
        return delegation_view(execution, self._run_purchase(execution), utc_text(self._clock()))

    def _run_purchase(self, execution: Execution) -> Purchase | None:
        run_id = execution.task.run_id
        return self._purchase(run_id) if run_id is not None else None

    # Revocation (§8.4)

    def revoke(self, owner_id: str, grant_id: str) -> RevokeReceipt:
        revocation = self._ledger.revoke(owner_id, grant_id)
        if revocation is None:
            raise DelegationError("delegation_not_found")
        if revocation.status == "not_revocable":
            raise DelegationError("delegation_not_revocable")
        return revoke_receipt(revocation)

    def revoke_all(self, owner_id: str) -> RevokeAllReceipt:
        """One receipt per grant that was active; any other grant is left as it is."""
        receipts = []
        for grant in self._ledger.grants(owner_id):
            if grant.state != "active":
                continue
            revocation = self._ledger.revoke(owner_id, grant.grant_id)
            if revocation is not None and revocation.status == "revoked":
                receipts.append(revoke_receipt(revocation))
        return RevokeAllReceipt(receipts=tuple(receipts))

    # Operator (§5.2, §5.4)

    def set_kill_switch(self, enabled: bool) -> KillSwitchState:
        return self._ledger.set_kill_switch(enabled)

    def health(self) -> tuple[bool, LedgerCounts]:
        return self._ledger.kill_switch().enabled, self._ledger.counts()

    def _owner_ref(self, owner_id: str) -> str:
        """§4.2: the D1/D2 pseudonym of the owner, never the JWT `sub` itself."""
        return "owner:" + pseudonym(self._trust.keys, "owner", owner_id.encode("utf-8"))


def _stored(mandate: IssuedMandate) -> SignedMandate:
    return SignedMandate(
        token=mandate.token,
        link_hash=mandate.link_hash,
        public_jwk_json=canonical_bytes(mandate.public_jwk).decode(),
    )
