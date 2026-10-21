"""Ports of the delegation ledger (spec §4.5).

A `TransactionHook` runs on the ledger's own connection, inside its `BEGIN IMMEDIATE`, after the
ledger's writes and before `COMMIT`; if it raises, the whole unit of work rolls back. HNP-06 writes
the D2/D3 evidence through it, and HNP-05/09b the purchase transitions.
"""

import sqlite3
from collections.abc import Callable
from typing import Protocol

from .models import (
    ConsentRequest,
    ConsentResult,
    DelegationEvent,
    GrantRecord,
    KillSwitchState,
    MandateRecord,
    ProposalDraft,
    ProposalRecord,
    TaskRecord,
)

TransactionHook = Callable[[sqlite3.Connection], None]


class DelegationConsentLedger(Protocol):
    """Proposal, consent, owner-scoped reads and the operator's kill switch."""

    def create_proposal(self, draft: ProposalDraft) -> ProposalRecord: ...

    def proposal(self, owner_id: str, proposal_id: str) -> ProposalRecord | None: ...

    def consent(self, request: ConsentRequest) -> ConsentResult: ...

    def grant(self, owner_id: str, grant_id: str) -> GrantRecord | None: ...

    def grants(self, owner_id: str) -> tuple[GrantRecord, ...]: ...

    def task_for_grant(self, grant_id: str) -> TaskRecord | None: ...

    def mandates(self, grant_id: str) -> tuple[MandateRecord, ...]: ...

    def events(self, grant_id: str) -> tuple[DelegationEvent, ...]: ...

    def kill_switch(self) -> KillSwitchState: ...

    def set_kill_switch(self, enabled: bool) -> KillSwitchState: ...
