"""Minimal structured outputs. Models judge facts; adapters derive every digest and ref.

The harness contracts (ValidatorAssessment, BuyerDraft) carry digests, source refs, policy
links and coverage lists that a model would have to copy verbatim. These schemas ask only for
judgements; the adapters assemble the harness contracts deterministically.

No length or count limits appear here: structured-output providers do not enforce all JSON
Schema keywords, and a verbose but honest answer must not become an "invalid" observation.
Adapters bound what they retain instead.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

CheckOutcome = Literal["pass", "fail", "unresolved"]
BusinessCheckId = Literal[
    "selection",
    "merchant",
    "budget",
    "offer",
    "consent",
    "validity",
    "revocation",
    "usage",
    "constraints",
    "scope",
]
# "binding" is protocol bookkeeping (proposal/context refs), derived by the adapter.
BUSINESS_CHECKS: tuple[BusinessCheckId, ...] = (
    "selection",
    "merchant",
    "budget",
    "offer",
    "consent",
    "validity",
    "revocation",
    "usage",
    "constraints",
    "scope",
)


class ModelOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ReviewChecks(ModelOutput):
    selection: CheckOutcome
    merchant: CheckOutcome
    budget: CheckOutcome
    offer: CheckOutcome
    consent: CheckOutcome
    validity: CheckOutcome
    revocation: CheckOutcome
    usage: CheckOutcome
    constraints: CheckOutcome
    scope: CheckOutcome

    def outcomes(self) -> dict[BusinessCheckId, CheckOutcome]:
        values = self.model_dump()
        return {check: values[check] for check in BUSINESS_CHECKS}


class ValidatorDecision(ModelOutput):
    """The validator model's answer. Reasons are non-authoritative prose for analysis only."""

    checks: ReviewChecks
    decision: Literal["accept", "reject", "abstain"]
    reasons: list[str]
