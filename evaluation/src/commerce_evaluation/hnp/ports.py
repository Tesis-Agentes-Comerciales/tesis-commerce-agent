"""Role ports contain no commercial or signing capability."""

from collections.abc import Callable
from typing import Protocol

from .models import (
    BuyerContext,
    BuyerDraft,
    ProposalView,
    ReviewerContext,
    SourceRef,
    TrustedReviewContext,
    ValidatorAssessment,
)


class BuyerAgent(Protocol):
    adapter_id: str

    def propose(self, context: BuyerContext) -> BuyerDraft: ...


class ValidatorAgent(Protocol):
    """Receives the same public facts as a human reviewer, never backend verdicts."""

    adapter_id: str

    def review(self, proposal: ProposalView, context: ReviewerContext) -> ValidatorAssessment: ...


class TrustedContextResolver(Protocol):
    def resolve(self) -> TrustedReviewContext: ...

    def commit_if_current(self, expected: SourceRef, action: Callable[[], None]) -> bool: ...


class RoleOutputError(ValueError):
    """Malformed adapter output, without retaining its payload or exception text."""


class InfrastructureError(RuntimeError):
    """Provider, network or harness failure: not an observation of role behaviour.

    Episodes that end with this error stay blocked (fail closed) but are reported apart from
    experimental results and excluded from their denominators.
    """
