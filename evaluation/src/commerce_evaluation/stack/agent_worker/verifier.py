"""06D-C and 07A: the restart probe over the same Agent SQLite."""

import sqlite3
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

from commerce_agent.acp.creation_lookup import BackendLookupSession, CreationLookupClient
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.remote_creation import SessionScope, VerificationRequest
from commerce_agent.persistence.remote_creation_sqlite import RemoteCreationVerifier
from commerce_evaluation.stack.agent_worker.runtime import halt, keys_from


class Verifier:
    """06D-C/07A restart probe: an independent process over the same Agent SQLite."""

    def __init__(self, data: dict[str, Any]):
        self.database, self.keys = data["database"], keys_from(data)
        self.request = VerificationRequest.model_validate(data["request"])
        self.client = CreationLookupClient(
            BackendLookupSession(
                data["origin"],
                SessionScope.model_validate(data["scope"]),
                SecretStr(data["bearer"]),
            )
        )

    def _verifier(self, cutpoint: str | None) -> RemoteCreationVerifier:
        class ParkedCommit(sqlite3.Connection):
            def commit(self):
                # Real lookup received and validated, provisional evidence inserted:
                # dying here must not make it durable.
                halt("verification_commit")

        def factory():
            connection = sqlite3.connect(
                self.database,
                factory=ParkedCommit if cutpoint == "verification_commit" else sqlite3.Connection,
            )
            connection.execute("PRAGMA foreign_keys=ON")
            return connection

        return RemoteCreationVerifier(
            factory, self.keys, self.client, clock=lambda: datetime.now(UTC)
        )

    def verify(self, value: dict[str, Any]) -> dict[str, Any]:
        result = self._verifier(value.get("cutpoint")).verify(self.request)
        return {"outcome": result.verification.outcome, "durable": result.durable}

    def inspect(self, _: dict[str, Any]) -> dict[str, Any]:
        history = self._verifier(None).inspect(self.request)
        return {
            "count": len(history),
            "digests": [sha256_digest(canonical_bytes(event)) for event in history],
        }

    def close(self) -> None:
        self.client.close()
