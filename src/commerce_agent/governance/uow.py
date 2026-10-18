"""Backend-only D1 application port. Results remain provisional until caller commit.

No gateway, service hook, endpoint, worker or remote retry is provided here.
"""

import sqlite3
from dataclasses import dataclass
from typing import Protocol

from pydantic import Field, SecretStr

from .evidence import ApplicationFailure, DecisionRecord, EvidenceError
from .models import ContractModel, Id, ObjectRef


class D1Application(ContractModel):
    evidence: DecisionRecord
    context_ref: ObjectRef
    operation_id: Id
    idempotency_key: SecretStr = Field(repr=False, min_length=1, max_length=128)


@dataclass(frozen=True)
class D1ApplicationResult:
    transitioned: bool
    reused: bool
    operation_id: str | None
    state_version: int


class D1ApplicationError(EvidenceError):
    """Safe diagnostic plus original decision; no claim that a failed write persisted."""

    def __init__(self, original: DecisionRecord, cause: EvidenceError):
        super().__init__(cause.code)
        self.original = original


class D1UnitOfWork(Protocol):
    connection: sqlite3.Connection

    def initialize(self) -> None: ...

    def prepare(self, request: D1Application) -> None:
        """Verify reference evaluation outside SQL transaction."""
        ...

    def apply(self, request: D1Application, *, owner_id: str) -> D1ApplicationResult:
        """Require caller transaction; never commit, dispatch or silently retry."""
        ...

    def record_failure(
        self,
        original: DecisionRecord,
        diagnostic: ApplicationFailure,
        *,
        owner_id: str,
    ) -> None: ...

    def claim(self, operation_id: str, *, claimant: str, owner_id: str) -> bool: ...

    def recovery(self) -> tuple[tuple[str, str, str | None], ...]: ...
