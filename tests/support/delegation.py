"""Builders for the delegation ledger tests: a settable authority clock and synthetic rows.

The scope is a minimal ConsentScope-shaped object (HNP-03 owns the real type) and the mandates are
opaque strings: the ledger stores them, it does not verify them (HNP-04 does).
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from commerce_agent.delegation.models import (
    ClosedMandates,
    ConsentRequest,
    ConsentResult,
    DispatchCommit,
    Execution,
    KillSwitchStage,
    OutcomeCode,
    ProposalDraft,
    ReservationRequest,
    SignedMandate,
    Step,
    TaskFinish,
)
from commerce_agent.delegation.ports import TransactionHook
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


@dataclass
class Flow:
    """One delegation driven through the executor's units of work, step by step."""

    ledger: SqliteDelegationLedger
    clock: AuthorityClock
    task_id: str
    grant_id: str
    lease_id: str = ""

    @classmethod
    def consented(
        cls,
        ledger: SqliteDelegationLedger,
        clock: AuthorityClock,
        draft: ProposalDraft | None = None,
    ) -> "Flow":
        result = consented(ledger, draft)
        assert result.task is not None and result.grant is not None
        return cls(ledger, clock, result.task.task_id, result.grant.grant_id)

    def state(self) -> Execution:
        execution = self.ledger.execution(self.task_id)
        assert execution is not None
        return execution

    def claim(self) -> Step:
        step = self.ledger.claim(self.task_id)
        if step.status == "applied":
            assert step.execution is not None and step.execution.task.lease_id is not None
            self.lease_id = step.execution.task.lease_id
        return step

    def reservation(self, number: int = 1, *, amount_minor: int = 120_000) -> ReservationRequest:
        return ReservationRequest(
            task_id=self.task_id,
            lease_id=self.lease_id,
            expected=self.state().authority,
            reservation_id=f"rsv_{number}",
            operation_id=f"op_{number}",
            checkout_id=f"checkout_{number}",
            amount_minor=amount_minor,
            d2_evidence_id=f"gov_d2_{number}",
        )

    def reserve(self, number: int = 1, *, hooks: Sequence[TransactionHook] = ()) -> Step:
        return self.ledger.reserve(self.reservation(number), hooks=hooks)

    def close(self, number: int = 1) -> Step:
        jwk = '{"kty":"EC","crv":"P-256","x":"agent-x","y":"agent-y"}'
        return self.ledger.record_closed_mandates(
            ClosedMandates(
                task_id=self.task_id,
                lease_id=self.lease_id,
                reservation_id=f"rsv_{number}",
                checkout_hash="checkout-hash",
                closed_checkout=SignedMandate(f"{OPEN_CHECKOUT_TOKEN}~closed", "sd-hash-c", jwk),
                closed_payment=SignedMandate(f"{OPEN_PAYMENT_TOKEN}~closed", "sd-hash-p", jwk),
            )
        )

    def commitment(self, number: int = 1) -> DispatchCommit:
        execution = self.state()
        reservation = execution.reservation
        return DispatchCommit(
            task_id=self.task_id,
            lease_id=self.lease_id,
            expected=execution.authority,
            reservation_id=f"rsv_{number}",
            reservation_version=1 if reservation is None else reservation.state_version,
            outbox_id=f"ob_{number}",
            checkout_expires_at=utc_text(self.clock.now + timedelta(hours=1)),
            payment_request_digest="sha256:" + "b" * 64,
            d3_evidence_id=f"gov_d3_{number}",
        )

    def commit(self, *, hooks: Sequence[TransactionHook] = ()) -> Step:
        return self.ledger.commit_dispatch(self.commitment(), hooks=hooks)

    def committed(self) -> None:
        """Claim, UoW-1, UoW-C and UoW-2: the purchase is in flight."""
        for step in (self.claim(), self.reserve(), self.close(), self.commit()):
            assert step.status == "applied", step

    def finish(
        self,
        state: Literal["escalated", "failed", "canceled"],
        outcome: OutcomeCode,
        *,
        stage: KillSwitchStage | None = None,
        hooks: Sequence[TransactionHook] = (),
    ) -> Step:
        request = TaskFinish(self.task_id, self.lease_id, state, outcome, stage)
        return self.ledger.finish_task(request, hooks=hooks)
