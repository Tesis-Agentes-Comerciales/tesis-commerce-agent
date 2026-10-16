"""Independent Agent restart/crash probe. Private stdin only; stdout has public digests."""

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime

from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.creation_lookup import BackendLookupSession, CreationLookupClient
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.provenance import ProvenanceKeys
from commerce_agent.governance.remote_creation import SessionScope, VerificationRequest
from commerce_agent.persistence.remote_creation_sqlite import RemoteCreationVerifier


def main():
    data = json.load(sys.stdin)
    keys = ProvenanceKeys(
        key_id=data["key_id"],
        pseudonym_key=SecretBytes(bytes.fromhex(data["hmac"])),
        encryption_key=SecretBytes(bytes.fromhex(data["aes"])),
    )
    request = VerificationRequest.model_validate(data["request"])

    class CrashConnection(sqlite3.Connection):
        def commit(self):
            # Agent has received/validated real HTTP and inserted provisional
            # evidence. Process death here must not make it durable.
            os._exit(81)

    def connect():
        connection = sqlite3.connect(
            data["database"],
            factory=(CrashConnection if data["mode"] == "crash" else sqlite3.Connection),
        )
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    client = CreationLookupClient(
        BackendLookupSession(
            data["origin"], SessionScope.model_validate(data["scope"]), SecretStr(data["bearer"])
        )
    )
    try:
        verifier = RemoteCreationVerifier(connect, keys, client, clock=lambda: datetime.now(UTC))
        if data["mode"] == "crash":
            verifier.verify(request)
            raise RuntimeError("Crash probe did not reach commit")
        history = verifier.inspect(request)
        print(
            json.dumps(
                {
                    "count": len(history),
                    "digests": [sha256_digest(canonical_bytes(e)) for e in history],
                }
            )
        )
    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("G06_AGENT_PROBE_FAILED:" + type(error).__name__, file=sys.stderr)
        raise SystemExit(82) from None
