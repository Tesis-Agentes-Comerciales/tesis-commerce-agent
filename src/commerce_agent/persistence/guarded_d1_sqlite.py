"""Experimental backend coordinator: fresh applied D1, one D17 send, read-only recovery.

P, registry and Q remain independent historical stores. The applied D1 database
is new. A signed attempt precedes evaluation; explicit recovery only links an
already committed 07A reservation. No path reevaluates or releases an old claim.
"""

import hashlib
import hmac
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import SecretBytes

from commerce_agent.acp.authority_observation import ObservationBlocked
from commerce_agent.acp.guarded_creation import HttpGuardedCheckout
from commerce_agent.acp.models import CheckoutCreateRequest
from commerce_agent.application.purchase import CheckoutGateway, PurchaseServiceError
from commerce_agent.domain import rank_candidates
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.dispatch_envelope import ExpectedRemoteBinding
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.guarded_creation import (
    GuardedEnvelope,
    GuardedInput,
    GuardedObservation,
)
from commerce_agent.governance.preselection import instant, now
from commerce_agent.governance.preselection_d1 import PreselectionD1Request
from commerce_agent.governance.provenance import (
    ClockReader,
    D1EnforceConfiguration,
    ProvenanceKeys,
    utc_milliseconds,
)
from commerce_agent.governance.uow import D1Application
from commerce_agent.persistence.d1_envelope_sqlite import SqliteD1EnvelopeStore
from commerce_agent.persistence.d1_uow_sqlite import SqliteD1UnitOfWork
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.preselection_d1_sqlite import SqlitePreselectionD1
from commerce_agent.persistence.provenance_sqlite import SqliteD1ProvenanceStore
from commerce_agent.persistence.sqlite import SqliteAgentStore


class _NoGateway:
    def __getattr__(self, name):
        raise ObservationBlocked("NO_LEGACY_GATEWAY_ALLOWED")


class SqliteGuardedD1:
    def __init__(
        self,
        path: Path,
        *,
        shadow: SqlitePreselectionD1,
        custody_key: SecretBytes,
        keys: ProvenanceKeys,
        configuration: D1EnforceConfiguration,
        clocks: ClockReader,
        transport: HttpGuardedCheckout,
        policy_bytes=None,
        policy_binding=None,
    ):
        if len(custody_key.get_secret_value()) < 32 or path.resolve() == shadow.path.resolve():
            raise ValueError("Independent applied D1 store and persistent custody required")
        self.path, self.shadow, self.keys = path, shadow, keys
        self.configuration, self.clocks, self.transport = configuration, clocks, transport
        self._key, self._policy, self._binding = custody_key, policy_bytes, policy_binding

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _mac(self, kind, body):
        return hmac.new(
            self._key.get_secret_value(), kind.encode() + b"\0" + body, hashlib.sha256
        ).hexdigest()

    def _configuration(self):
        return canonical_bytes(
            {
                "profile": "experimental_applied_d1_d17/1",
                "shadow_store": self.shadow.store_id(),
                "configuration": self.configuration.model_dump(mode="json"),
                "key_id": self.keys.key_id,
                "policy_bytes": self._policy.hex() if self._policy is not None else None,
                "policy_binding": self._binding.model_dump(mode="json") if self._binding else None,
            }
        )

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "b_d17_meta" not in tables:
                raise ObservationBlocked("NEW_APPLIED_STORE_REQUIRED")
        SqliteAgentStore(self.path).initialize()
        with closing(self._connect()) as db, db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS b_d17_meta(
                id TEXT PRIMARY KEY,body BLOB NOT NULL,seal TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS b_d17_attempts(
                run_id TEXT PRIMARY KEY REFERENCES purchases(run_id),
                body BLOB NOT NULL,seal TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS b_d17_envelopes(
                run_id TEXT PRIMARY KEY REFERENCES b_d17_attempts(run_id),
                operation_id TEXT NOT NULL UNIQUE,body BLOB NOT NULL,seal TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS b_d17_events(
                run_id TEXT NOT NULL REFERENCES b_d17_envelopes(run_id),
                sequence INTEGER NOT NULL,body BLOB NOT NULL,seal TEXT NOT NULL,
                PRIMARY KEY(run_id,sequence));
            """)
            for table, predicate in (
                ("b_d17_meta", "1"),
                ("b_d17_attempts", "run_id=NEW.run_id"),
                ("b_d17_envelopes", "run_id=NEW.run_id OR operation_id=NEW.operation_id"),
                ("b_d17_events", "run_id=NEW.run_id AND sequence=NEW.sequence"),
            ):
                for action in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_{action} BEFORE {action} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT,'immutable D17'); END"
                    )
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_replace BEFORE INSERT ON {table} "
                    f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {predicate}) "
                    "BEGIN SELECT RAISE(ABORT,'immutable D17'); END"
                )
            body = self._configuration()
            row = db.execute("SELECT body,seal FROM b_d17_meta").fetchall()
            if not row:
                db.execute(
                    "INSERT INTO b_d17_meta VALUES(?,?,?)",
                    (str(uuid4()), body, self._mac("meta", body)),
                )
            elif len(row) != 1 or row[0] != (body, self._mac("meta", body)):
                raise ObservationBlocked("APPLIED_STORE_CONFLICT")

    def _source(self, request):
        request = parse_model(PreselectionD1Request, canonical_bytes(request))
        # The credential supplied to the new HTTP port must resolve independently
        # through the same Q registry, not just match an echoed scope.
        seal = self.shadow.source.recover(request.episode_id, request.owner_id)
        resolution = self.shadow.registry.resolve(
            seal,
            run_id=request.run_id,
            owner_id=request.owner_id,
            store_id=self.shadow.store_id(),
            bearer=self.transport.session.bearer,
        )
        shadow = self.shadow.recover(request)
        if (
            resolution.model_dump(exclude={"verified_at"})
            != shadow.provisioning.model_dump(exclude={"verified_at"})
            or self.transport.session.binding != seal.episode.binding
            or self.configuration.merchant_base_url != seal.episode.binding.merchant_origin
        ):
            raise ObservationBlocked("APPLIED_PROVISIONING_CONFLICT")
        return seal, shadow

    def _read(self, db, table, run_id):
        row = db.execute(f"SELECT body,seal FROM {table} WHERE run_id=?", (run_id,)).fetchone()
        if row is None or not hmac.compare_digest(self._mac(table, row[0]), row[1]):
            raise ObservationBlocked("D17_CUSTODY_UNAVAILABLE")
        return row[0]

    def _attempt(self, db, request, shadow):
        meta = db.execute("SELECT body,seal FROM b_d17_meta").fetchall()
        config = self._configuration()
        if len(meta) != 1 or meta[0] != (config, self._mac("meta", config)):
            raise ObservationBlocked("APPLIED_STORE_CONFLICT")
        raw = self._read(db, "b_d17_attempts", request.run_id)
        value = json.loads(raw)
        if (
            canonical_bytes(value) != raw
            or value["request"] != request.model_dump(mode="json")
            or value["shadow_digest"] != sha256_digest(canonical_bytes(shadow))
            or value["configuration_digest"] != sha256_digest(config)
        ):
            raise ObservationBlocked("APPLIED_ATTEMPT_CONFLICT")
        return value

    def prepare(self, request: PreselectionD1Request) -> GuardedEnvelope:
        seal, shadow = self._source(request)
        selected = seal.selection.selected
        if selected is None:
            raise ObservationBlocked("NO_SELECTED_OFFER")
        gate = SqliteD1EnforceGate(
            self.path,
            self.configuration,
            self.keys,
            self.clocks,
            policy_bytes=self._policy,
            policy_binding=self._binding,
            expected_remote_binding=ExpectedRemoteBinding(
                assurance="backend_expected_not_authenticated",
                remote_run_id=seal.episode.binding.scope.remote_run_id,
                remote_actor_id=seal.episode.binding.scope.remote_actor_id,
                source_ref=shadow.provisioning.credential_ref,
            ),
        )
        attempt = canonical_bytes(
            {
                "version": "commerce-applied-d1-attempt/1",
                "request": request.model_dump(mode="json"),
                "shadow_digest": sha256_digest(canonical_bytes(shadow)),
                "configuration_digest": sha256_digest(self._configuration()),
                "claimed_at": now(),
            }
        )
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in (
                "purchases",
                "governance_evidence",
                "d1_provenance_episodes",
                "d1_enforce_inputs",
                "d1_checkout_operations",
            ):
                if (
                    table in tables
                    and db.execute(
                        f"SELECT 1 FROM {table} WHERE run_id=?", (request.run_id,)
                    ).fetchone()
                ):
                    raise ObservationBlocked("FRESH_APPLIED_EPISODE_REQUIRED")
            stamp = now()
            db.execute(
                "INSERT INTO purchases(run_id,owner_id,request_text,stage,created_at,updated_at) "
                "VALUES(?,?,?,'received',?,?)",
                (request.run_id, request.owner_id, "D17 experimental only", stamp, stamp),
            )
            db.execute(
                "INSERT INTO b_d17_attempts VALUES(?,?,?)",
                (request.run_id, attempt, self._mac("b_d17_attempts", attempt)),
            )
        capture = gate.capture(seal.episode.feed, seal.episode.intent, rank_candidates)
        try:
            gate.create_checkout(
                capture,
                run_id=request.run_id,
                owner_id=request.owner_id,
                selection=seal.selection,
                candidate=selected,
                gateway=cast(CheckoutGateway, _NoGateway()),
            )
        except PurchaseServiceError as error:
            if error.code != "d1_dispatch_blocked":
                raise
        else:
            raise ObservationBlocked("UNEXPECTED_COMMERCIAL_GATE_RESULT")
        return self.recover_preparation(request)

    def _authorization(self, db, request, shadow):
        self._attempt(db, request, shadow)
        ledger = SqliteGovernanceEvidenceStore(db)
        records = ledger.for_run(request.run_id)
        if len(records) != 1 or not isinstance(records[0], DecisionRecord):
            raise ObservationBlocked("APPLIED_D1_UNAVAILABLE")
        record = records[0]
        if (
            record.evidence_id == shadow.evidence_id
            or record.decision.decision_id == shadow.decision_id
        ):
            raise ObservationBlocked("SHADOW_CANNOT_BE_PROMOTED")
        # Real reference replay before the SQL transaction, never a new decision.
        ledger.prepare_decision(record)
        data = record.policy_input()
        operation = record.application.transition_ref
        if (
            data.governance_mode != "enforce"
            or record.decision.outcome != "allow"
            or record.decision.origin != "policy"
            or operation is None
            or record.application.disposition != "transition_committed"
            or data.selected_variant_id != request.selected_variant_id
            or instant(data.authority_now) < instant(shadow.bound_at)
        ):
            raise ObservationBlocked("APPLIED_D1_REQUIRED")
        envelope = SqliteD1EnvelopeStore(db, self.keys).recover(
            operation, run_id=request.run_id, owner_id=request.owner_id
        )
        if envelope is None:
            raise ObservationBlocked("APPLIED_ENVELOPE_REQUIRED")
        db.execute("BEGIN IMMEDIATE")
        try:
            vault = SqliteD1ProvenanceStore(db, self.keys)
            feed = vault.recover_feed(request.run_id, request.owner_id, data.feed_snapshot_ref)
            intent = vault.recover_intent(request.run_id, request.owner_id, data.intent.intent_ref)
            seal = self.shadow.source.recover(request.episode_id, request.owner_id)
            context = vault.recover_context(request.run_id, request.owner_id, envelope.context_ref)
            configuration = parse_model(
                D1EnforceConfiguration,
                vault._get(
                    request.run_id, request.owner_id, context.data.commercial_ref, "commercial"
                )[0],
            )
            if (
                canonical_bytes(feed) != canonical_bytes(seal.episode.feed)
                or canonical_bytes(intent) != canonical_bytes(seal.episode.intent)
                or configuration != self.configuration
                or envelope.expected_remote_binding is None
                or envelope.expected_remote_binding.source_ref != shadow.provisioning.credential_ref
                or envelope.expected_remote_binding.remote_run_id
                != shadow.merchant.scope.remote_run_id
                or envelope.expected_remote_binding.remote_actor_id
                != shadow.merchant.scope.remote_actor_id
                or ledger.get(record.evidence_id) != record
            ):
                raise ObservationBlocked("APPLIED_FACTS_CONFLICT")
        finally:
            db.rollback()
        policy = db.execute(
            "SELECT policy_bytes FROM governance_episodes WHERE run_id=?", (request.run_id,)
        ).fetchone()[0]
        uow = SqliteD1UnitOfWork(
            db, keys=self.keys, policy_bytes=policy, policy_binding=data.expected_policy
        )
        uow.prepare(
            D1Application(
                schema_version="commerce-policy/1.0",
                evidence=record,
                context_ref=envelope.context_ref,
                operation_id=operation,
                idempotency_key=envelope.idempotency_key,
            )
        )
        return record, envelope, uow

    def recover_preparation(self, request: PreselectionD1Request) -> GuardedEnvelope:
        """Explicitly link an existing reservation; never evaluate or dispatch."""
        seal, shadow = self._source(request)
        with closing(self._connect()) as db:
            _record, legacy, _uow = self._authorization(db, request, shadow)
            with db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute(
                    "SELECT 1 FROM b_d17_envelopes WHERE run_id=?", (request.run_id,)
                ).fetchone()
                if old:
                    return self._guard(db, request, shadow, legacy)
                state = db.execute(
                    "SELECT state FROM d1_checkout_operations WHERE operation_id=?",
                    (legacy.operation_id,),
                ).fetchone()
                if state != ("reserved",):
                    raise ObservationBlocked("CLAIMED_HISTORY_CANNOT_BE_BACKFILLED")
                guarded_request = GuardedInput(
                    acp_request=parse_model(CheckoutCreateRequest, legacy.body_canonical.encode()),
                    offer_id=shadow.offer_id,
                    expected_offer_revision=shadow.offer_revision,
                    offer_snapshot_sha256=shadow.offer_snapshot_sha256,
                )
                guard = GuardedEnvelope(
                    operation_id=legacy.operation_id,
                    run_id=request.run_id,
                    owner_id=request.owner_id,
                    merchant_origin=legacy.merchant_origin,
                    idempotency_key_sha256=hashlib.sha256(
                        legacy.idempotency_key.get_secret_value().encode()
                    ).hexdigest(),
                    decision_id=legacy.decision_id,
                    evidence_id=legacy.evidence_id,
                    evidence_digest=legacy.evidence_digest,
                    d1_input_digest=legacy.d1_input_digest,
                    context_ref=legacy.context_ref,
                    legacy_envelope_digest=legacy.digest(),
                    shadow_binding=shadow,
                    shadow_binding_digest=sha256_digest(canonical_bytes(shadow)),
                    selected_offer=next(
                        o.offer
                        for o in seal.observations
                        if o.observation_id == shadow.observation_id
                    ),
                    request=guarded_request,
                    guard_fingerprint=guarded_request.fingerprint(),
                    prepared_at=utc_milliseconds(datetime.now(UTC)),
                )
                body = canonical_bytes(guard)
                db.execute(
                    "INSERT INTO b_d17_envelopes VALUES(?,?,?,?)",
                    (request.run_id, legacy.operation_id, body, self._mac("b_d17_envelopes", body)),
                )
            return guard

    def _guard(self, db, request, shadow, legacy):
        guard = parse_model(GuardedEnvelope, self._read(db, "b_d17_envelopes", request.run_id))
        row = db.execute(
            "SELECT operation_id FROM b_d17_envelopes WHERE run_id=?", (request.run_id,)
        ).fetchone()
        if (
            guard.shadow_binding != shadow
            or guard.legacy_envelope_digest != legacy.digest()
            or guard.operation_id != legacy.operation_id
            or row != (guard.operation_id,)
            or guard.merchant_origin != legacy.merchant_origin
            or guard.decision_id != legacy.decision_id
            or guard.evidence_id != legacy.evidence_id
            or guard.evidence_digest != legacy.evidence_digest
            or guard.d1_input_digest != legacy.d1_input_digest
            or guard.context_ref != legacy.context_ref
            or guard.idempotency_key_sha256
            != hashlib.sha256(legacy.idempotency_key.get_secret_value().encode()).hexdigest()
            or canonical_bytes(guard.request.acp_request) != legacy.body_canonical.encode()
        ):
            raise ObservationBlocked("D17_ENVELOPE_CONFLICT")
        return guard

    def dispatch(self, request: PreselectionD1Request) -> GuardedObservation:
        _seal, shadow = self._source(request)
        with closing(self._connect()) as db:
            record, legacy, uow = self._authorization(db, request, shadow)
            with db:
                db.execute("BEGIN IMMEDIATE")
                guard = self._guard(db, request, shadow, legacy)
                self.transport.validate(guard, legacy)
                if SqliteGovernanceEvidenceStore(db).get(record.evidence_id) != record:
                    raise ObservationBlocked("APPLIED_DECISION_CHANGED")
                claimant = "d17:" + str(uuid4())
                if not uow.claim(guard.operation_id, claimant=claimant, owner_id=request.owner_id):
                    raise ObservationBlocked("INITIAL_DISPATCH_ALREADY_CLAIMED")
            # Independent connection after commit. An old claimed state is never
            # a capability; only this invocation that just won the claim proceeds.
            with closing(self._connect()) as reader:
                self._guard(reader, request, shadow, legacy)
                if reader.execute(
                    "SELECT state,claimant FROM d1_checkout_operations WHERE operation_id=?",
                    (guard.operation_id,),
                ).fetchone() != ("claimed", claimant):
                    raise ObservationBlocked("D17_CLAIM_NOT_DURABLE")
        self._source(request)
        observation = self.transport.create_once(guard, legacy)
        return self._record(request, guard, "initial", observation)

    def recover(self, request: PreselectionD1Request) -> GuardedObservation:
        _seal, shadow = self._source(request)
        with closing(self._connect()) as db:
            _record, legacy, _uow = self._authorization(db, request, shadow)
            guard = self._guard(db, request, shadow, legacy)
            if db.execute(
                "SELECT state FROM d1_checkout_operations WHERE operation_id=?",
                (guard.operation_id,),
            ).fetchone() != ("claimed",):
                raise ObservationBlocked("NO_INITIAL_CLAIM_TO_RECOVER")
        observation = self.transport.lookup(guard, legacy)
        return self._record(request, guard, "lookup", observation)

    def _events(self, db, run_id):
        previous = None
        values = []
        for sequence, body, seal in db.execute(
            "SELECT sequence,body,seal FROM b_d17_events WHERE run_id=? ORDER BY sequence",
            (run_id,),
        ):
            value = json.loads(body)
            if (
                not hmac.compare_digest(self._mac("event", body), seal)
                or sequence != len(values) + 1
                or value["sequence"] != sequence
                or value["previous_digest"] != previous
                or value["run_id"] != run_id
            ):
                raise ObservationBlocked("D17_HISTORY_CONFLICT")
            previous = sha256_digest(body)
            values.append(value)
        return values, previous

    def _record(self, request, guard, kind, observation):
        observation = parse_model(GuardedObservation, canonical_bytes(observation))
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            events, previous = self._events(db, request.run_id)
            verified = [
                v["observation"]
                for v in events
                if v["observation"]["result"] in {"found", "rejected"}
            ]
            for old in verified:
                if observation.result in {"found", "rejected"} and old != observation.model_dump(
                    mode="json"
                ):
                    raise ObservationBlocked("D17_HISTORICAL_PROOF_CHANGED")
            sequence = len(events) + 1
            value = canonical_bytes(
                {
                    "schema_version": "commerce-d17-event/1",
                    "run_id": request.run_id,
                    "sequence": sequence,
                    "previous_digest": previous,
                    "kind": kind,
                    "guard_digest": sha256_digest(canonical_bytes(guard)),
                    "recorded_at": now(),
                    "observation": observation.model_dump(mode="json"),
                }
            )
            db.execute(
                "INSERT INTO b_d17_events VALUES(?,?,?,?)",
                (request.run_id, sequence, value, self._mac("event", value)),
            )
        return observation

    def history(self, request):
        self._source(request)
        with closing(self._connect()) as db:
            events, _previous = self._events(db, request.run_id)
        return tuple(events)
