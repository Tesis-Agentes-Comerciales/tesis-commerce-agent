"""JointCase: real Agent D1 state for one episode of the stack's Merchant (06D-C, 07A)."""

import hashlib
import secrets
import signal
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.creation_lookup import BackendLookupSession, CreationLookupClient
from commerce_agent.application import PurchaseService, PurchaseServiceError
from commerce_agent.approval import ApprovalService
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.dispatch_envelope import ExpectedRemoteBinding
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.models import ObjectRef
from commerce_agent.governance.provenance import (
    ClockProfile,
    ClockReader,
    D1EnforceConfiguration,
    ProvenanceKeys,
)
from commerce_agent.governance.remote_creation import LOOKUP_PATH, SessionScope, VerificationRequest
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.d1_envelope_sqlite import SqliteD1EnvelopeStore
from commerce_agent.persistence.remote_creation_sqlite import RemoteCreationVerifier
from commerce_evaluation.stack.agent_process import AgentProcess
from commerce_evaluation.stack.environment import connect
from commerce_evaluation.stack.fake_checkout import NoEffectsGateway, details, single_offer_feed
from commerce_evaluation.stack.fault_proxy import CREATE_PATH
from commerce_evaluation.stack.live_stack import LiveStack


def ephemeral_keys(key_id: str) -> ProvenanceKeys:
    return ProvenanceKeys(
        key_id=key_id,
        pseudonym_key=SecretBytes(secrets.token_bytes(32)),
        encryption_key=SecretBytes(secrets.token_bytes(32)),
    )


FIXED_CLOCK = ClockProfile(
    authority_source="system_utc",
    commercial_source="fixed_sandbox",
    authority_provider=None,
    commercial_provider=None,
    fixed_commercial_at="2026-09-10T14:00:00.000Z",
)


@dataclass
class JointCase:
    """Real Agent D1 state for one Merchant episode; the verifier runs in this process."""

    stack: LiveStack
    index: str
    origin: str
    episode: dict[str, Any]
    credential: dict[str, Any] = field(repr=False)
    path: Path
    keys: ProvenanceKeys = field(repr=False)
    owner: str
    local_run: str
    operation_id: str
    envelope: Any = field(repr=False)
    original: dict[str, Any] | None = None
    setup_creations: int = 0

    @classmethod
    def open(cls, stack: LiveStack, prefix: str) -> "JointCase":
        index = secrets.token_hex(6)
        episode = stack.rpc("episode", actor=f"{prefix}-owner-{index}")
        credential = stack.rpc("session", run_id=episode["run_id"], actor_id=episode["actor_id"])
        return cls(
            stack,
            index,
            stack.origin,
            episode,
            credential,
            stack.root / (f"{prefix}-agent-{index}.sqlite"),
            ephemeral_keys(prefix + "-ephemeral"),
            f"local-owner:{index}",
            f"local-run:{index}",
            "",
            None,
        )

    @classmethod
    def prepare(cls, stack: LiveStack, *, binding: bool = True) -> "JointCase":
        """06D-C: the gate commits a D1 reservation and stops before any Agent dispatch."""
        case = cls.open(stack, "06dc")
        store = SqliteAgentStore(case.path)
        store.initialize()
        gate = SqliteD1EnforceGate(
            case.path,
            case.configuration("06dc-cop-d1"),
            case.keys,
            ClockReader(FIXED_CLOCK),
            expected_remote_binding=case.expected_binding() if binding else None,
        )
        service = case.service(store, gate)
        try:
            case.start(service, "Isolated recovery fixture")
        except PurchaseServiceError as error:
            assert error.code == "d1_dispatch_blocked"
        else:
            raise AssertionError("Fixture must stop before Agent dispatch")
        case.recover_envelope()
        assert case.local_run != case.credential["run_id"]
        return case

    def configuration(self, policy_set_id: str) -> D1EnforceConfiguration:
        return D1EnforceConfiguration(
            governance_mode="enforce",
            merchant_base_url=self.origin,
            policy_set_id=policy_set_id,
            policy_revision=1,
            currency="COP",
            quantity=1,
            clock=FIXED_CLOCK,
        )

    def expected_binding(self) -> ExpectedRemoteBinding:
        return ExpectedRemoteBinding(
            assurance="backend_expected_not_authenticated",
            remote_run_id=self.credential["run_id"],
            remote_actor_id=self.credential["actor_id"],
            source_ref=self.source_ref(self.credential),
        )

    def service(self, store: SqliteAgentStore, gate: SqliteD1EnforceGate) -> PurchaseService:
        return PurchaseService(
            feed=single_offer_feed(self.episode["offer"], self.origin),
            checkout=NoEffectsGateway(),
            store=store,
            approvals=ApprovalService(store, SecretStr(secrets.token_hex(32))),
            d1_gate=gate,
        )

    def start(self, service: PurchaseService, request: str) -> None:
        service.start(
            run_id=self.local_run,
            owner_id=self.owner,
            request=request,
            intent=PurchaseIntent(hard=HardConstraints(currency="COP")),
            selected_variant_id=self.episode["offer"]["id"],
            details=details(),
        )

    def recover_envelope(self) -> None:
        with closing(connect(self.path)) as connection:
            self.operation_id = connection.execute(
                "SELECT operation_id FROM d1_checkout_operations"
            ).fetchone()[0]
            self.envelope = SqliteD1EnvelopeStore(connection, self.keys).recover(
                self.operation_id, run_id=self.local_run, owner_id=self.owner
            )
        assert self.envelope is not None

    @staticmethod
    def source_ref(credential: dict[str, Any]) -> ObjectRef:
        # Registry data came from issue + authenticate in the Merchant control worker.
        public = {key: credential[key] for key in ("run_id", "actor_id", "session_sha256")}
        digest = sha256_digest(canonical_bytes(public))
        return ObjectRef(
            schema_version="commerce-policy/1.0",
            id="provision:" + digest[7:],
            version=1,
            digest=digest,
        )

    def scope(self, credential: dict[str, Any] | None = None) -> SessionScope:
        credential = credential or self.credential
        return SessionScope(
            assurance="backend_session_registry/1",
            remote_run_id=credential["run_id"],
            remote_actor_id=credential["actor_id"],
            source_ref=self.source_ref(credential),
        )

    def request(self, query: str = "query:one", version: int = 0) -> VerificationRequest:
        return VerificationRequest(
            query_id=query,
            operation_id=self.operation_id,
            run_id=self.local_run,
            owner_id=self.owner,
            expected_version=version,
        )

    def client(
        self, *, credential: dict[str, Any] | None = None, timeout: float = 5.0
    ) -> CreationLookupClient:
        credential = credential or self.credential
        return CreationLookupClient(
            BackendLookupSession(
                self.origin, self.scope(credential), SecretStr(credential["bearer"])
            ),
            timeout_seconds=timeout,
        )

    def verifier(self, client: CreationLookupClient, factory=None) -> RemoteCreationVerifier:
        return RemoteCreationVerifier(
            factory or (lambda: connect(self.path)),
            self.keys,
            client,
            clock=lambda: datetime.now(UTC),
        )

    def setup_create(self) -> dict[str, Any]:
        """The one fixture POST of 06D-C: the creation an Agent sent and lost the answer to."""
        assert self.setup_creations == 0
        with httpx.Client(trust_env=False, timeout=10) as client:
            result = client.post(
                self.origin + CREATE_PATH,
                content=self.envelope.body_canonical,
                headers={
                    "Authorization": "Bearer " + self.credential["bearer"],
                    "API-Version": "2026-04-17",
                    "Content-Type": "application/json",
                    "Idempotency-Key": self.envelope.idempotency_key.get_secret_value(),
                },
            )
        assert result.status_code == 201, f"Setup creation rejected: HTTP {result.status_code}"
        value = result.json()
        assert isinstance(value, dict)
        self.original = value
        self.setup_creations = 1
        return value

    def key_sha256(self) -> str:
        return hashlib.sha256(self.envelope.idempotency_key.get_secret_value().encode()).hexdigest()

    def raw_lookup(
        self, *, credential: dict[str, Any] | None = None, digest: str | None = None
    ) -> httpx.Response:
        with httpx.Client(trust_env=False, timeout=5) as client:
            return client.post(
                self.origin + LOOKUP_PATH,
                headers={
                    "Authorization": "Bearer " + (credential or self.credential)["bearer"],
                    "API-Version": "2026-04-17",
                    "Request-Id": "client-is-not-authority",
                },
                json={
                    "schema_version": "merchant-acp-creation-lookup/1",
                    "idempotency_key_sha256": digest or self.key_sha256(),
                },
            )

    def verify_zero(
        self,
        *,
        credential: dict[str, Any] | None = None,
        request: VerificationRequest | None = None,
        expected: str = "found_verified",
        timeout: float = 5.0,
        factory=None,
    ):
        """One verification; the Merchant's tables must be byte-identical before and after."""
        before = self.stack.rpc("snapshot")
        client = self.client(credential=credential, timeout=timeout)
        started = time.perf_counter()
        try:
            verifier = self.verifier(client, factory)
            result = verifier.verify(request or self.request())
            elapsed = (time.perf_counter() - started) * 1000
            assert result.verification.outcome == expected
            assert (
                not result.verification.replay_allowed
                and not result.verification.continuation_allowed
            )
            if result.durable:
                assert result.event in verifier.inspect(self.request())
        finally:
            client.close()
        after = self.stack.rpc("snapshot")
        assert before == after, "Lookup mutated Merchant tables"
        self.stack.record(
            f"06dc:{self.index}:{expected}",
            observed=result.verification.outcome,
            durable=result.durable,
            total_verifier_ms=elapsed,
            setup_creations=self.setup_creations,
            new_checkouts=0,
            commercial_mutations=0,
        )
        return result

    def probe(self, *, crash: bool = False) -> dict[str, Any] | None:
        """A separate Agent process verifies or inspects this case's SQLite, then exits."""
        agent = AgentProcess(self.stack, self.stack.root)
        try:
            agent.call(
                "verifier",
                database=str(self.path),
                key_id=self.keys.key_id,
                hmac=self.keys.pseudonym_key.get_secret_value().hex(),
                aes=self.keys.encryption_key.get_secret_value().hex(),
                request=self.request().model_dump(mode="json"),
                origin=self.origin,
                scope=self.scope().model_dump(mode="json"),
                bearer=self.credential["bearer"],
            )
            if crash:
                assert agent.kill_at("verification_commit", "verify") == -signal.SIGKILL
                return None
            return agent.call("inspect")
        finally:
            agent.close()
