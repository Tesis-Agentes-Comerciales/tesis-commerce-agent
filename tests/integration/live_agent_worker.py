"""Agent process of the live stack. JSON lines on stdin/stdout; real HTTP never uses the pipe.

The pipe only provisions synthetic fixture secrets in memory and starts backend actions.
Named cutpoints are test-only injection through ports the Agent already accepts (the
verifier's connection factory, SqliteGuardedD1's transport): at a cutpoint the process
announces it and parks until the harness kills it. Production code is not patched.
"""

import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.authority_observation import AuthorityObservationClient, AuthoritySession
from commerce_agent.acp.creation_lookup import BackendLookupSession, CreationLookupClient
from commerce_agent.acp.guarded_creation import HttpGuardedCheckout
from commerce_agent.acp.offer_snapshot import DeliveryContext
from commerce_agent.domain import PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.models import SCHEMA, D1PolicyArtifact, PolicyBinding
from commerce_agent.governance.preselection import PreselectionAuthority
from commerce_agent.governance.preselection_d1 import PreselectionD1Request
from commerce_agent.governance.provenance import (
    ClockProfile,
    ClockReader,
    D1Configuration,
    D1EnforceConfiguration,
    ProvenanceKeys,
)
from commerce_agent.governance.provisioning import CatalogSource, EpisodeGrant
from commerce_agent.governance.remote_creation import SessionScope, VerificationRequest
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.guarded_d1_sqlite import SqliteGuardedD1
from commerce_agent.persistence.preselection_d1_sqlite import SqlitePreselectionD1
from commerce_agent.persistence.preselection_sqlite import SqlitePreselectionStore
from commerce_agent.persistence.provisioning_sqlite import SqliteTestProvisioningRegistry
from commerce_agent.persistence.remote_creation_sqlite import RemoteCreationVerifier

ROOT = Path(os.environ["LIVE_STACK_ROOT"]).resolve()
# The proxy is the only peer: the Merchant's public origin for this run.
PORT = int(os.environ["LIVE_STACK_PORT"])
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
POLICY_SET = "b-live-policy"


def confine(event: str, args: tuple[Any, ...]) -> None:
    """Loopback to the proxy only; SQLite and file writes only inside this run's directory."""
    if event == "socket.connect":
        address = args[1]
        if not isinstance(address, tuple) or address[:2] != ("127.0.0.1", PORT):
            raise RuntimeError("LIVE_AGENT_SOCKET_REJECTED")
    elif event == "sqlite3.connect":
        if args[0] != ":memory:" and not Path(args[0]).resolve().is_relative_to(ROOT):
            raise RuntimeError("LIVE_AGENT_SQLITE_REJECTED")
    elif event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.name == ".env" or (
            (args[2] or 0) & WRITE_FLAGS
            and not path.is_relative_to(ROOT)
            and str(path) != os.devnull
        ):
            raise RuntimeError("LIVE_AGENT_FILE_REJECTED")


def halt(cutpoint: str) -> None:
    """Announce the cutpoint and park; the harness SIGKILLs this process right here."""
    print(json.dumps({"cutpoint": cutpoint}), flush=True)
    threading.Event().wait()


def keys_from(data: dict[str, Any]) -> ProvenanceKeys:
    return ProvenanceKeys(
        key_id=data["key_id"],
        pseudonym_key=SecretBytes(bytes.fromhex(data["hmac"])),
        encryption_key=SecretBytes(bytes.fromhex(data["aes"])),
    )


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


class CutpointTransport(HttpGuardedCheckout):
    """The guarded HTTP port, parked before the send or after the Merchant's answer."""

    cutpoint: str | None = None

    def create_once(self, guard, legacy):
        if self.cutpoint == "claimed_before_http_send":
            halt(self.cutpoint)
        observation = super().create_once(guard, legacy)
        if self.cutpoint == "response_before_record":
            halt(self.cutpoint)
        return observation


class Guarded:
    """07B-B: authoritative P, Q shadow, fresh applied D1 and one guarded D17 POST."""

    def __init__(self, data: dict[str, Any], *, fresh: bool):
        root = Path(data["directory"]).resolve()
        assert root.is_relative_to(ROOT)
        root.mkdir(parents=True, exist_ok=True)
        seed = bytes.fromhex(data["ephemeral_secret"])

        def key(domain: str) -> SecretBytes:
            return SecretBytes(hashlib.sha256(seed + domain.encode()).digest())

        origin = data["origin"]
        source = SqlitePreselectionStore(root / "source.sqlite", key("p"))
        registry = SqliteTestProvisioningRegistry(root / "registry.sqlite", key("registry"))
        profile = ClockProfile(
            authority_source="system_utc",
            commercial_source="system_utc",
            authority_provider=None,
            commercial_provider=None,
            fixed_commercial_at=None,
        )
        common = dict(
            merchant_base_url=origin,
            policy_set_id=POLICY_SET,
            policy_revision=1,
            currency="COP",
            quantity=1,
            clock=profile,
        )
        keys = ProvenanceKeys(
            key_id="b-live", pseudonym_key=key("pseudonym"), encryption_key=key("encryption")
        )
        self.shadow = SqlitePreselectionD1(
            root / "shadow.sqlite",
            source=source,
            registry=registry,
            bearer=SecretStr(data["token"]),
            custody_key=key("q"),
            keys=keys,
            configuration=D1Configuration.model_validate({**common, "governance_mode": "shadow"}),
            clocks=ClockReader(profile),
        )
        self.shadow_binding = None
        if fresh:
            source.initialize()
            registry.initialize()
            session = registry.issue_test_session(
                CatalogSource(
                    event_id=str(uuid4()),
                    merchant_origin=origin,
                    merchant_id=data["merchant_id"],
                    remote_run_id=data["run_id"],
                    remote_actor_id=data["actor_id"],
                ),
                SecretStr(data["token"]),
            )
            observation = AuthorityObservationClient(session)
            try:
                flow = PreselectionAuthority(observation, source)
                episode = flow.begin(
                    owner_id="b-live-owner",
                    intent=PurchaseIntent(),
                    destination=DeliveryContext(country="CO", city="Bogota", postal_code="110111"),
                )
                seal = flow.capture(episode, owner_id="b-live-owner")
            finally:
                observation.close()
            store = self.shadow.initialize()
            registry.grant_test_episode(
                EpisodeGrant(
                    event_id=str(uuid4()),
                    episode_id=episode,
                    owner_id=seal.episode.owner_id,
                    d1_run_id=episode,
                    d1_store_id=store,
                    selection_digest=sha256_digest(canonical_bytes(seal)),
                    receipt_ref=session.binding.source_ref,
                )
            )
            self.request = self.shadow.request(episode, seal.episode.owner_id)
            self.shadow_binding = self.shadow.evaluate(self.request).model_dump(mode="json")
            (root / "request.json").write_bytes(canonical_bytes(self.request))
        else:
            self.request = parse_model(PreselectionD1Request, (root / "request.json").read_bytes())
            seal = source.recover(self.request.episode_id, self.request.owner_id)
            session = AuthoritySession(seal.episode.binding, SecretStr(data["token"]))
        self.transport = CutpointTransport(session, timeout_seconds=data["timeout"])
        policy: dict[str, Any] = {}
        if data["policy"] == "deny_foreign_merchant":
            # I05/I06: a trusted constructor policy whose allowlist excludes this Merchant.
            raw = canonical_bytes(
                D1PolicyArtifact(
                    schema_version=SCHEMA,
                    policy_set_id=POLICY_SET,
                    version=1,
                    policy_ids=("POL-01", "POL-02"),
                    allowed_actions=("prepare_checkout",),
                    allowed_merchant_refs=("merchant:forbidden",),
                    currency="COP",
                    quantity=1,
                )
            )
            policy = dict(
                policy_bytes=raw,
                policy_binding=PolicyBinding(
                    schema_version=SCHEMA,
                    policy_set_id=POLICY_SET,
                    version=1,
                    artifact_digest=sha256_digest(raw),
                ),
            )
        self.applied = SqliteGuardedD1(
            root / "applied.sqlite",
            shadow=self.shadow,
            custody_key=key("b"),
            keys=keys,
            configuration=D1EnforceConfiguration.model_validate(
                {**common, "governance_mode": "enforce"}
            ),
            clocks=ClockReader(profile),
            transport=self.transport,
            **policy,
        )
        self.applied.initialize()

    def opened(self, _: dict[str, Any]) -> dict[str, Any]:
        return {"pid": os.getpid(), "run_id": self.request.run_id, "shadow": self.shadow_binding}

    def prepare(self, _: dict[str, Any]) -> dict[str, Any]:
        return self.applied.prepare(self.request).model_dump(mode="json")

    def dispatch(self, value: dict[str, Any]) -> dict[str, Any]:
        self.transport.cutpoint = value.get("cutpoint")
        return self.applied.dispatch(self.request).model_dump(mode="json")

    def recover(self, _: dict[str, Any]) -> dict[str, Any]:
        return self.applied.recover(self.request).model_dump(mode="json")

    def history(self, _: dict[str, Any]) -> list[dict[str, Any]]:
        return list(self.applied.history(self.request))

    def decisions(self, _: dict[str, Any]) -> list[dict[str, Any]]:
        """Applied D1 ledger for this run; read-only."""
        with closing(sqlite3.connect(self.applied.path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            records = SqliteGovernanceEvidenceStore(db).for_run(self.request.run_id)
        return [
            {
                "outcome": record.decision.outcome,
                "governance_mode": record.policy_input().governance_mode,
                "disposition": record.application.disposition,
            }
            for record in records
            if isinstance(record, DecisionRecord)
        ]

    def close(self) -> None:
        self.transport.close()


def failure(error: Exception) -> dict[str, Any]:
    # No traceback, exception text, credential, request or header export.
    code = (
        str(error)
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", str(error))
        else getattr(error, "code", None)
    )
    if code is not None and not re.fullmatch(r"[a-zA-Z0-9_]{1,80}", code):
        code = None
    return {"ok": False, "error_type": type(error).__name__, "error_code": code}


def main() -> None:
    sys.addaudithook(confine)
    case: Verifier | Guarded | None = None
    for line in sys.stdin:
        value = json.loads(line)
        action = value["action"]
        if action == "close":
            break
        try:
            if action == "verifier":
                case = Verifier(value)
                result: Any = {"pid": os.getpid()}
            elif action == "guarded":
                case = Guarded(value["data"], fresh=value["fresh"])
                result = case.opened(value)
            else:
                operation = getattr(case, action, None)
                if action.startswith("_") or not callable(operation):
                    raise RuntimeError("LIVE_AGENT_ACTION_REJECTED")
                result = operation(value)
            print(json.dumps({"ok": True, "result": result}), flush=True)
        except Exception as error:
            diagnostic = failure(error)
            print(json.dumps(diagnostic), file=sys.stderr, flush=True)
            print(json.dumps(diagnostic), flush=True)
    if case is not None:
        case.close()


if __name__ == "__main__":
    main()
