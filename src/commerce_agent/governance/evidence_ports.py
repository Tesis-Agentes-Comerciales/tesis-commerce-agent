"""Transaction-owned ledger port; no implicit commit or commercial effect."""

import sqlite3
from typing import Protocol

from .evidence import DecisionEvidence, DecisionRecord, EvidenceApplication, EvidenceScope


class LocalTransitionVerifier(Protocol):
    """Backend verifies its actual local transition on this same connection."""

    def verify(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        application: EvidenceApplication,
    ) -> bool: ...


class GovernanceEvidenceStore(Protocol):
    def prepare_decision(self, evidence: DecisionRecord) -> None: ...

    def register_episode(
        self,
        scope: EvidenceScope,
        *,
        policy_bytes: bytes | None,
        engine_bytes: bytes | None,
    ) -> None: ...

    def record_decision(
        self,
        evidence: DecisionEvidence,
        *,
        expected_state_version: int,
        transition_verifier: LocalTransitionVerifier | None = None,
    ) -> None: ...

    def get(self, evidence_id: str) -> DecisionEvidence | None: ...

    def for_run(self, run_id: str) -> tuple[DecisionEvidence, ...]: ...
