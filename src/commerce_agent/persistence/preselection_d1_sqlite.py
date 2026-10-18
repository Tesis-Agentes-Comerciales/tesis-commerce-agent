"""Explicit local linking protocol around real D1 shadow evaluation.

07B-P and provisioning are separate DBs. First commit a unique signed attempt
and a fresh immutable received purchase. Then evaluate D1 with its existing
capture/vault/ledger implementation. Finally link the independently read commit.
Interrupted attempts are never re-evaluated automatically. Explicit recover may
only link an already committed, verified decision; it never dispatches anything.
"""

import hashlib
import hmac
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from pydantic import SecretBytes, SecretStr

from commerce_agent.acp.authority_observation import ObservationBlocked
from commerce_agent.domain import rank_candidates
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.models import PolicyBinding
from commerce_agent.governance.preselection import episode_digest, instant, now
from commerce_agent.governance.preselection_d1 import PreselectionD1Binding, PreselectionD1Request
from commerce_agent.governance.provenance import ClockReader, D1Configuration, ProvenanceKeys
from commerce_agent.governance.provisioning import ProvisioningResolution
from commerce_agent.governance.shadow import ShadowD1Observer
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.preselection_sqlite import SqlitePreselectionStore
from commerce_agent.persistence.provenance_sqlite import SqliteD1ProvenanceStore
from commerce_agent.persistence.provisioning_sqlite import SqliteTestProvisioningRegistry
from commerce_agent.persistence.sqlite import SqliteAgentStore


class SqlitePreselectionD1:
    def __init__(
        self,
        path: Path,
        *,
        source: SqlitePreselectionStore,
        registry: SqliteTestProvisioningRegistry,
        bearer: SecretStr | None,
        custody_key: SecretBytes,
        keys: ProvenanceKeys,
        configuration: D1Configuration,
        clocks: ClockReader,
        policy_bytes: bytes | None = None,
        policy_binding: PolicyBinding | None = None,
    ):
        if len(custody_key.get_secret_value()) < 32:
            raise ValueError("Persistent link custody key required")
        self.path, self.source, self.registry = path, source, registry
        self._bearer, self._custody, self.keys = bearer, custody_key, keys
        self.configuration, self.clocks = configuration, clocks
        self.policy_bytes, self.policy_binding = policy_bytes, policy_binding

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _mac(self, kind, value):
        return hmac.new(
            self._custody.get_secret_value(), kind.encode() + b"\0" + value, hashlib.sha256
        ).hexdigest()

    def initialize(self) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables and "q_d1_meta" not in tables:
                raise ObservationBlocked("NEW_EXPERIMENTAL_D1_STORE_REQUIRED")
        SqliteAgentStore(self.path).initialize()
        with closing(self._connect()) as db, db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS q_d1_meta(id TEXT PRIMARY KEY,seal TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS q_d1_attempts(
                run_id TEXT PRIMARY KEY REFERENCES purchases(run_id),
                evaluation_id TEXT NOT NULL UNIQUE,body BLOB NOT NULL,seal TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS q_d1_bindings(
                run_id TEXT PRIMARY KEY REFERENCES q_d1_attempts(run_id),
                evidence_id TEXT NOT NULL UNIQUE,body BLOB NOT NULL,seal TEXT NOT NULL);
              CREATE TRIGGER IF NOT EXISTS q_purchase_update BEFORE UPDATE ON purchases
                WHEN EXISTS(SELECT 1 FROM q_d1_attempts WHERE run_id=OLD.run_id)
                BEGIN SELECT RAISE(ABORT,'evaluation-only purchase'); END;
              CREATE TRIGGER IF NOT EXISTS q_purchase_delete BEFORE DELETE ON purchases
                WHEN EXISTS(SELECT 1 FROM q_d1_attempts WHERE run_id=OLD.run_id)
                BEGIN SELECT RAISE(ABORT,'evaluation-only purchase'); END;
              CREATE TRIGGER IF NOT EXISTS q_purchase_replace BEFORE INSERT ON purchases
                WHEN EXISTS(SELECT 1 FROM q_d1_attempts WHERE run_id=NEW.run_id)
                BEGIN SELECT RAISE(ABORT,'evaluation-only purchase'); END;
            """)
            for table in ("q_d1_meta", "q_d1_attempts", "q_d1_bindings"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_{action} BEFORE {action} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT,'immutable link'); END"
                    )
                keys = {
                    "q_d1_meta": (),
                    "q_d1_attempts": ("run_id", "evaluation_id"),
                    "q_d1_bindings": ("run_id", "evidence_id"),
                }[table]
                predicate = " OR ".join(f"{key}=NEW.{key}" for key in keys)
                condition = f" WHERE {predicate}" if predicate else ""
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table} "
                    f"WHEN EXISTS(SELECT 1 FROM {table}{condition}) "
                    "BEGIN SELECT RAISE(ABORT,'immutable link'); END"
                )
            old = db.execute("SELECT id,seal FROM q_d1_meta").fetchall()
            if not old:
                identifier = str(uuid4())
                db.execute(
                    "INSERT INTO q_d1_meta VALUES(?,?)",
                    (identifier, self._mac("store", identifier.encode())),
                )
            elif len(old) != 1 or not hmac.compare_digest(
                old[0][1], self._mac("store", old[0][0].encode())
            ):
                raise ObservationBlocked("STORE_CUSTODY_CONFLICT")
        return self.store_id()

    def store_id(self):
        with closing(self._connect()) as db:
            row = db.execute("SELECT id,seal FROM q_d1_meta").fetchone()
        if row is None or not hmac.compare_digest(row[1], self._mac("store", row[0].encode())):
            raise ObservationBlocked("STORE_CUSTODY_CONFLICT")
        return row[0]

    def request(self, episode_id: str, owner_id: str) -> PreselectionD1Request:
        selection = self.source.recover(episode_id, owner_id)
        if selection.selection.selected is None:
            raise ObservationBlocked("NO_SEALED_SELECTION")
        return PreselectionD1Request(
            episode_id=episode_id,
            run_id=episode_id,
            owner_id=owner_id,
            seal_digest=sha256_digest(canonical_bytes(selection)),
            feed_digest=sha256_digest(canonical_bytes(selection.episode.feed)),
            intent_digest=sha256_digest(canonical_bytes(selection.episode.intent)),
            selected_variant_id=selection.selection.selected.variant_id,
        )

    def _verified(self, request: PreselectionD1Request):
        request = parse_model(PreselectionD1Request, canonical_bytes(request))
        selection = self.source.recover(request.episode_id, request.owner_id)
        if request != self.request(request.episode_id, request.owner_id):
            raise ObservationBlocked("SEALED_FACTS_CONFLICT")
        if self.configuration.merchant_base_url != selection.episode.binding.merchant_origin:
            raise ObservationBlocked("D1_MERCHANT_CONFLICT")
        resolution = self.registry.resolve(
            selection,
            run_id=request.run_id,
            owner_id=request.owner_id,
            store_id=self.store_id(),
            bearer=self._bearer,
        )
        return selection, resolution

    def _config_digest(self):
        return sha256_digest(
            canonical_bytes(
                {
                    "configuration": self.configuration.model_dump(mode="json"),
                    "policy_binding": self.policy_binding.model_dump(mode="json")
                    if self.policy_binding
                    else None,
                    "policy_bytes": self.policy_bytes.hex()
                    if self.policy_bytes is not None
                    else None,
                    "key_id": self.keys.key_id,
                }
            )
        )

    def evaluate(self, request: PreselectionD1Request) -> PreselectionD1Binding:
        selection, resolution = self._verified(request)
        # No caller-supplied evaluator or transport. Real approved reference engine only.
        observer = ShadowD1Observer(
            self.path,
            self.configuration,
            self.keys,
            self.clocks,
            policy_bytes=self.policy_bytes,
            policy_binding=self.policy_binding,
        )
        attempt = {
            "schema_version": "commerce-preselection-d1-attempt/1",
            "request": request.model_dump(mode="json"),
            "selection": selection.model_dump(mode="json"),
            "provisioning": resolution.model_dump(mode="json"),
            "config_digest": self._config_digest(),
            "claimed_at": now(),
            "evaluation_id": str(uuid4()),
            "store_id": self.store_id(),
        }
        body = canonical_bytes(attempt)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM purchases WHERE run_id=?", (request.run_id,)).fetchone():
                raise ObservationBlocked("NEW_D1_EPISODE_REQUIRED")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in (
                "governance_evidence",
                "d1_provenance_episodes",
                "d1_shadow_attempts",
                "d1_checkout_operations",
                "d1_enforce_inputs",
            ):
                if (
                    table in tables
                    and db.execute(
                        f"SELECT 1 FROM {table} WHERE run_id=?", (request.run_id,)
                    ).fetchone()
                ):
                    raise ObservationBlocked("HISTORICAL_D1_WITHOUT_SEAL")
            # This row is not a selected purchase and can never become one in this store.
            timestamp = now()
            db.execute(
                "INSERT INTO purchases(run_id,owner_id,request_text,stage,created_at,updated_at) "
                "VALUES(?,?,?,'received',?,?)",
                (request.run_id, request.owner_id, "07B-Q evaluation only", timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO q_d1_attempts VALUES(?,?,?,?)",
                (request.run_id, attempt["evaluation_id"], body, self._mac("attempt", body)),
            )
        capture = observer.capture(
            selection.episode.feed, selection.episode.intent, rank_candidates
        )
        if capture is None:
            raise ObservationBlocked("D1_CAPTURE_UNAVAILABLE")
        observer.observe(
            capture,
            run_id=request.run_id,
            owner_id=request.owner_id,
            selection=selection.selection,
            selected_variant_id=request.selected_variant_id,
        )
        if observer.diagnostics.status != "observed":
            raise ObservationBlocked("D1_EVALUATION_INCOMPLETE")
        return self.recover(request)

    def _attempt(self, db, request):
        row = db.execute(
            "SELECT body,seal,evaluation_id FROM q_d1_attempts WHERE run_id=?", (request.run_id,)
        ).fetchone()
        if row is None or not hmac.compare_digest(self._mac("attempt", row[0]), row[1]):
            raise ObservationBlocked("ATTEMPT_UNAVAILABLE")
        attempt = json.loads(row[0])
        if (
            attempt.get("schema_version") != "commerce-preselection-d1-attempt/1"
            or canonical_bytes(attempt) != row[0]
            or attempt["request"] != request.model_dump(mode="json")
            or attempt["config_digest"] != self._config_digest()
            or attempt["store_id"] != self.store_id()
            or attempt["evaluation_id"] != row[2]
        ):
            raise ObservationBlocked("ATTEMPT_CONFLICT")
        return attempt

    def recover(self, request: PreselectionD1Request) -> PreselectionD1Binding:
        """Explicit local recovery. Never invoke capture/evaluate, even with partial history."""
        selection, resolution = self._verified(request)
        with closing(self._connect()) as db:
            attempt = self._attempt(db, request)
            if canonical_bytes(selection) != canonical_bytes(attempt["selection"]):
                raise ObservationBlocked("SEALED_SNAPSHOT_SUBSTITUTED")
            stored_resolution = parse_model(
                ProvisioningResolution, canonical_bytes(attempt["provisioning"])
            )
            if resolution.model_dump(exclude={"verified_at"}) != stored_resolution.model_dump(
                exclude={"verified_at"}
            ):
                raise ObservationBlocked("PROVISIONING_SUBSTITUTED")
            ledger = SqliteGovernanceEvidenceStore(db)
            try:
                records = ledger.for_run(request.run_id)
            except Exception:
                raise ObservationBlocked("D1_COMMIT_UNAVAILABLE") from None
            if len(records) != 1 or not isinstance(records[0], DecisionRecord):
                raise ObservationBlocked("D1_COMMIT_UNAVAILABLE")
            record = records[0]
            ledger.prepare_decision(record)
            # Existing vault requires an explicit transaction even for recovery.
            # Reference replay above stays outside; all binding facts and CAS below
            # now share one write-lock snapshot through commit.
            db.execute("BEGIN IMMEDIATE")
            data = record.policy_input()
            vault = SqliteD1ProvenanceStore(db, self.keys)
            feed = vault.recover_feed(request.run_id, request.owner_id, data.feed_snapshot_ref)
            intent = vault.recover_intent(request.run_id, request.owner_id, data.intent.intent_ref)
            if (
                canonical_bytes(feed) != canonical_bytes(selection.episode.feed)
                or canonical_bytes(intent) != canonical_bytes(selection.episode.intent)
                or data.selected_variant_id != request.selected_variant_id
                or tuple(data.eligible_variant_ids)
                != tuple(sorted(item.candidate.variant_id for item in selection.selection.ranked))
                or data.governance_mode != "shadow"
                or record.application.disposition != "observed_shadow"
                or record.application.transition_ref is not None
                or instant(data.authority_now) <= instant(selection.sealed_at)
            ):
                raise ObservationBlocked("D1_SEALED_FACTS_CONFLICT")
            contexts = [
                ref
                for ref in record.source_refs
                if db.execute(
                    "SELECT 1 FROM d1_provenance_sources WHERE source_id=? AND version=? "
                    "AND kind='context'",
                    (ref.id, ref.version),
                ).fetchone()
            ]
            if len(contexts) != 1:
                raise ObservationBlocked("D1_CONTEXT_UNAVAILABLE")
            context = vault.recover_context(request.run_id, request.owner_id, contexts[0])
            commercial, _public_binding = vault._get(
                request.run_id, request.owner_id, context.data.commercial_ref, "commercial"
            )
            if (
                data.merchant_ref != context.data.merchant_ref
                or data.owner_ref != context.data.owner_ref
                or data.expected_policy != context.data.expected_policy
                or canonical_bytes(parse_model(D1Configuration, commercial))
                != canonical_bytes(self.configuration)
            ):
                raise ObservationBlocked("D1_CONTEXT_CONFLICT")
            selected = [
                o for o in selection.observations if o.candidate == selection.selection.selected
            ]
            if (
                len(selected) != 1
                or record.policy_artifact_ref is None
                or record.engine_artifact_ref is None
            ):
                raise ObservationBlocked("D1_REFERENCES_UNAVAILABLE")
            observation = selected[0]
            previous = db.execute(
                "SELECT body,seal,evidence_id FROM q_d1_bindings WHERE run_id=?", (request.run_id,)
            ).fetchone()
            old = None
            if previous:
                if not hmac.compare_digest(self._mac("binding", previous[0]), previous[1]):
                    raise ObservationBlocked("BINDING_CUSTODY_CONFLICT")
                old = parse_model(PreselectionD1Binding, previous[0])
                if old.evidence_id != previous[2]:
                    raise ObservationBlocked("BINDING_CUSTODY_CONFLICT")
            binding = PreselectionD1Binding(
                evaluation_id=attempt["evaluation_id"],
                store_id=self.store_id(),
                request=request,
                episode_digest=episode_digest(selection.episode),
                observation_id=observation.observation_id,
                observation_digest=sha256_digest(canonical_bytes(observation)),
                offer_id=observation.offer.id,
                offer_revision=observation.offer.revision,
                offer_snapshot_sha256=observation.offer_snapshot_sha256,
                merchant=selection.episode.binding,
                provisioning=stored_resolution,
                context_ref=contexts[0],
                feed_snapshot_ref=data.feed_snapshot_ref,
                intent_ref=data.intent.intent_ref,
                eligibility_algorithm_ref=data.eligibility_algorithm_ref,
                policy_binding=data.expected_policy,
                policy_artifact_ref=record.policy_artifact_ref,
                engine_artifact_ref=record.engine_artifact_ref,
                decision_id=data.decision_id,
                evidence_id=record.evidence_id,
                evidence_digest=sha256_digest(canonical_bytes(record)),
                outcome=record.decision.outcome,
                bound_at=old.bound_at if old else now(),
            )
            if old is not None and binding != old:
                raise ObservationBlocked("BINDING_SUBSTITUTED")
            body = canonical_bytes(binding)
            with db:
                row = db.execute(
                    "SELECT stage,state_version,owner_id,selected_variant_id,checkout_id,order_id "
                    "FROM purchases WHERE run_id=?",
                    (request.run_id,),
                ).fetchone()
                if row != (
                    "received",
                    data.expected_local_state_version,
                    request.owner_id,
                    None,
                    None,
                    None,
                ):
                    raise ObservationBlocked("D1_CAS_CONFLICT")
                # CAS against already committed ledger bytes, not an in-memory allow.
                current = ledger.get(record.evidence_id)
                count = db.execute(
                    "SELECT count(*) FROM governance_evidence WHERE run_id=?", (request.run_id,)
                ).fetchone()[0]
                if current != record or count != 1:
                    raise ObservationBlocked("D1_COMMIT_CHANGED")
                if previous is None:
                    db.execute(
                        "INSERT INTO q_d1_bindings VALUES(?,?,?,?)",
                        (request.run_id, record.evidence_id, body, self._mac("binding", body)),
                    )
                elif (
                    db.execute(
                        "SELECT body FROM q_d1_bindings WHERE run_id=?", (request.run_id,)
                    ).fetchone()[0]
                    != body
                ):
                    raise ObservationBlocked("BINDING_CAS_CONFLICT")
            return binding
