"""Builders for the delegation ledger tests: a settable authority clock and synthetic rows.

The scope is a minimal ConsentScope-shaped object (HNP-03 owns the real type) and the mandates are
opaque strings: the ledger stores them, it does not verify them (HNP-04 does).
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from commerce_agent.delegation.models import (
    ConsentRequest,
    ConsentResult,
    ProposalDraft,
    SignedMandate,
)
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger, utc_text

START = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
OWNER = "buyer-a"
OWNER_REF = "owner:" + "a" * 64
OTHER_OWNER = "buyer-b"
CHECKOUT_DETAILS = '{"buyer":{"email":"ana@example.com"},"fulfillment":{"address":"Calle 1"}}'
OPEN_CHECKOUT_TOKEN = "eyJ.open-checkout.sig~disclosure~"
OPEN_PAYMENT_TOKEN = "eyJ.open-payment.sig~disclosure~"


@dataclass
class AuthorityClock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class Ids:
    """Deterministic ids with the spec prefixes, so rows can be named in assertions."""

    counts: dict[str, int] = field(default_factory=dict)

    def __call__(self, prefix: str) -> str:
        self.counts[prefix] = self.counts.get(prefix, 0) + 1
        return f"{prefix}_{self.counts[prefix]}"


def make_ledger(path: Path, clock: AuthorityClock) -> SqliteDelegationLedger:
    ledger = SqliteDelegationLedger(path / "agent.db", clock=clock, id_factory=Ids())
    ledger.initialize()
    return ledger


def scope_json(
    proposal_id: str, *, presented_at: datetime, valid_for_seconds: int, owner_ref: str = OWNER_REF
) -> str:
    scope = {
        "schema_version": "commerce-delegation/1.0",
        "proposal_id": proposal_id,
        "owner_ref": owner_ref,
        "scope": {"variant_ids": ["variant-1"], "currency": "COP", "total_max_minor": 150_000},
        "display": {"merchant_name": "Tesis Commerce Merchant"},
        "presented_at": utc_text(presented_at),
        "expires_at": utc_text(presented_at + timedelta(seconds=valid_for_seconds)),
        "preference_text": None,
    }
    return canonical_bytes(scope).decode()


def proposal_draft(
    proposal_id: str = "dpr_1",
    *,
    owner_id: str = OWNER,
    presented_at: datetime = START,
    ttl_seconds: int = 600,
    valid_for_seconds: int = 3600,
) -> ProposalDraft:
    scope = scope_json(proposal_id, presented_at=presented_at, valid_for_seconds=valid_for_seconds)
    return ProposalDraft(
        proposal_id=proposal_id,
        owner_id=owner_id,
        scope_json=scope,
        scope_digest=sha256_digest(b"commerce-delegation-scope/v1\0" + scope.encode()),
        checkout_details_json=CHECKOUT_DETAILS,
        presented_at=utc_text(presented_at),
        expires_at=utc_text(presented_at + timedelta(seconds=ttl_seconds)),
    )


def consent_request(draft: ProposalDraft, *, owner_id: str | None = None) -> ConsentRequest:
    jwk = json.dumps({"kty": "EC", "crv": "P-256", "x": "issuer-x", "y": "issuer-y"})
    return ConsentRequest(
        owner_id=owner_id or draft.owner_id,
        proposal_id=draft.proposal_id,
        scope_digest=draft.scope_digest,
        intent_json='{"hard":{"currency":"COP","max_total_amount_minor":150000}}',
        agent_key_kid="agent-key-thumbprint",
        issuer_kid="issuer-thumbprint",
        open_checkout=SignedMandate(OPEN_CHECKOUT_TOKEN, "open-checkout-hash", jwk),
        open_payment=SignedMandate(OPEN_PAYMENT_TOKEN, "open-payment-hash", jwk),
    )


def consented(ledger: SqliteDelegationLedger, draft: ProposalDraft | None = None) -> ConsentResult:
    draft = draft or proposal_draft()
    ledger.create_proposal(draft)
    result = ledger.consent(consent_request(draft))
    assert result.status == "created"
    return result
