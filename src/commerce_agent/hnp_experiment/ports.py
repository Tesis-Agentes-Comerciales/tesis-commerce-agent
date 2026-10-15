"""Role ports contain no commercial or signing capability."""

from collections.abc import Callable
from typing import Protocol

from .models import (
    BuyerContext,
    BuyerDraft,
    ProposalEnvelope,
    SourceRef,
    TrustedReviewContext,
    ValidatorAssessment,
)


class BuyerAgent(Protocol):
    adapter_id: str

    def propose(self, context: BuyerContext) -> BuyerDraft: ...


class ValidatorAgent(Protocol):
    adapter_id: str

    def review(
        self, proposal: ProposalEnvelope, context: TrustedReviewContext
    ) -> ValidatorAssessment: ...


class TrustedContextResolver(Protocol):
    def resolve(self) -> TrustedReviewContext: ...

    def commit_if_current(self, expected: SourceRef, action: Callable[[], None]) -> bool: ...


class RoleOutputError(ValueError):
    """Malformed adapter output, without retaining its payload or exception text."""
