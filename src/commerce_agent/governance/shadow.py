"""Experimental D1 observation; never a commercial authorization."""

import json
import logging
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Literal

from commerce_agent.acp.feed import ProductFeed
from commerce_agent.config import Settings
from commerce_agent.domain import PurchaseIntent, SelectionResult, rank_candidates
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.provenance_sqlite import SqliteD1ProvenanceStore, _operational_bytes

from .bootstrap import configured_d1
from .canonical import canonical_bytes, parse_model, sha256_digest
from .evaluator import (
    GovernanceError,
    ReferencePolicyEvaluator,
    measure_reference_engine,
    verify_artifact,
)
from .evidence import DecisionRecord, EvidenceApplication, EvidenceScope
from .evidence_artifacts import capture_reference_sources
from .models import (
    SCHEMA,
    EvaluationMetadata,
    GuardReason,
    ObjectRef,
    PolicyBinding,
    PolicyDecision,
    SelectionPolicyInput,
)
from .provenance import ClockReader, D1Configuration, ProvenanceKeys, utc_milliseconds
from .provenance_artifacts import measure_d1_sources
from .shadow_artifacts import verify_loaded_selection


@dataclass
class ShadowDiagnostics:
    status: Literal["configured", "observed", "degraded"] = "configured"
    experimental_reference: bool = True
    observed: int = 0
    reused: int = 0
    lost: int = 0
    reason: GuardReason | None = None
    last_outcome: Literal["allow", "deny"] | None = None
    last_evidence_id: str | None = None
    capture_ms: float = 0
    observation_ms: float = 0
    evaluation_ms: float = 0


@dataclass(frozen=True)
class _Capture:
    feed: ProductFeed = field(repr=False)
    intent: PurchaseIntent = field(repr=False)
    live_feed: ProductFeed = field(repr=False)
    live_intent: PurchaseIntent = field(repr=False)
    feed_bytes: bytes = field(repr=False)
    intent_bytes: bytes = field(repr=False)
    ranking: Callable[[ProductFeed, PurchaseIntent], SelectionResult] = field(repr=False)


class ShadowD1Observer:
    def __init__(
        self,
        path: Path,
        configuration: D1Configuration,
        keys: ProvenanceKeys,
        clocks: ClockReader,
        *,
        policy_bytes: bytes | None = None,
        policy_binding: PolicyBinding | None = None,
    ):
        self.diagnostics = ShadowDiagnostics()
        self._path = path
        self._configuration = D1Configuration.model_validate(configuration)
        if self._configuration.governance_mode != "shadow":
            raise GovernanceError("GOV_INPUT_INVALID")
        self._keys = ProvenanceKeys.model_validate(keys)
        self._clocks = clocks
        if clocks.profile != configuration.clock:
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        self._sources = measure_d1_sources()
        verify_loaded_selection(json.loads(self._sources["algorithm"])["sources"], rank_candidates)
        self._identity = measure_reference_engine()
        self._engine_bytes = capture_reference_sources()
        assert self._identity.artifact_digest is not None
        self._engine_ref = ObjectRef(
            schema_version=SCHEMA,
            id="python-reference:" + self._identity.artifact_digest.removeprefix("sha256:"),
            version=1,
            digest=self._identity.artifact_digest,
        )
        self._policy_bytes, self._policy_binding = policy_bytes, policy_binding
        if policy_bytes is not None or policy_binding is not None:
            if policy_bytes is None or policy_binding is None:
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            verify_artifact(policy_bytes, policy_binding)

    def failure(self, reason: GuardReason = "GOV_EVIDENCE_UNAVAILABLE") -> None:
        self.diagnostics.status = "degraded"
        self.diagnostics.reason = reason
        self.diagnostics.lost += 1
        self.diagnostics.last_outcome = None
        self.diagnostics.last_evidence_id = None
        logging.getLogger(__name__).warning("D1 shadow observation lost: %s", reason)

    def capture(
        self,
        feed: ProductFeed,
        intent: PurchaseIntent,
        ranking: Callable[[ProductFeed, PurchaseIntent], SelectionResult],
    ) -> object | None:
        started = perf_counter_ns()
        try:
            if measure_d1_sources() != self._sources:
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            verify_loaded_selection(json.loads(self._sources["algorithm"])["sources"], ranking)
            copied_feed, copied_intent = deepcopy(feed), deepcopy(intent)
            return _Capture(
                copied_feed,
                copied_intent,
                feed,
                intent,
                _operational_bytes(copied_feed),
                _operational_bytes(copied_intent),
                ranking,
            )
        except Exception as error:
            self.failure(
                error.reason if isinstance(error, GovernanceError) else "GOV_FACTS_UNAVAILABLE"
            )
            return None
        finally:
            self.diagnostics.capture_ms = (perf_counter_ns() - started) / 1_000_000

    def observe(
        self,
        capture: object,
        *,
        run_id: str,
        owner_id: str,
        selection: SelectionResult,
        selected_variant_id: str,
    ) -> None:
        started = perf_counter_ns()
        self.diagnostics.evaluation_ms = 0
        try:
            if not isinstance(capture, _Capture):
                raise GovernanceError("GOV_INPUT_INVALID")
            if any(item.candidate.currency.upper() != "COP" for item in selection.ranked):
                # Do not misrepresent a valid wider B0 operation as this COP-only profile.
                raise GovernanceError("GOV_FACTS_UNAVAILABLE")
            if _operational_bytes(capture.live_feed) != capture.feed_bytes or (
                _operational_bytes(capture.live_intent) != capture.intent_bytes
            ):
                raise GovernanceError("GOV_FACTS_UNAVAILABLE")
            verify_loaded_selection(
                json.loads(self._sources["algorithm"])["sources"], capture.ranking
            )
            if measure_reference_engine() != self._identity:
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            with closing(sqlite3.connect(self._path, timeout=5)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                ledger = SqliteGovernanceEvidenceStore(connection)
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    vault = SqliteD1ProvenanceStore(connection, self._keys)
                    vault.initialize()
                    vault.register_episode(run_id, owner_id, experiment_run_id=None)
                    context = vault.register_context(
                        run_id,
                        owner_id,
                        self._configuration,
                        policy_bytes=self._policy_bytes,
                        policy_binding=self._policy_binding,
                    )
                    algorithm = vault.public_source(
                        run_id, owner_id, context.data.eligibility_algorithm_ref, "algorithm"
                    )
                    normalization = vault.public_source(
                        run_id, owner_id, context.data.normalization_ref, "normalization"
                    )
                    if (
                        algorithm != self._sources["algorithm"]
                        or normalization != self._sources["normalization"]
                    ):
                        raise GovernanceError("GOV_ARTIFACT_MISMATCH")
                    intent_ref = vault.register_intent(run_id, owner_id, capture.intent)
                    feed_ref = vault.register_feed(run_id, owner_id, capture.feed)
                    facts = vault.intent_facts(run_id, owner_id, intent_ref, context.ref)
                    observation = vault.observe_context(run_id, owner_id, context.ref, self._clocks)
                    eligible = tuple(item.candidate.variant_id for item in selection.ranked)
                    if selected_variant_id not in eligible or len(set(eligible)) != len(eligible):
                        raise GovernanceError("GOV_INPUT_INVALID")
                    algorithm_ref = context.data.eligibility_algorithm_ref
                    template = {
                        "schema_version": SCHEMA,
                        "run_id": run_id,
                        "trace_id": context.data.trace_id,
                        "owner_ref": context.data.owner_ref,
                        "purchase_mode": "HP",
                        "governance_mode": "shadow",
                        "phase": "D1",
                        "action": "prepare_checkout",
                        "context_version": context.ref.version,
                        "expected_local_state_version": observation.expected_local_state_version,
                        "expected_policy": context.data.expected_policy.model_dump(mode="json"),
                        "clock_profile_ref": context.data.clock_profile_ref.model_dump(mode="json"),
                        "experiment_run_id": context.data.experiment_run_id,
                        "conversation_id": context.data.conversation_id,
                        "intent": facts.model_dump(mode="json"),
                        "feed_snapshot_ref": feed_ref.model_dump(mode="json"),
                        "eligibility_algorithm_ref": algorithm_ref.model_dump(mode="json"),
                        "eligible_variant_ids": sorted(eligible),
                        "selected_variant_id": selected_variant_id,
                        "merchant_ref": context.data.merchant_ref,
                    }
                    key = sha256_digest(
                        b"commerce-d1-shadow-attempt/v1\0"
                        + canonical_bytes(
                            {
                                "input_facts": template,
                                "engine": self._engine_ref.model_dump(mode="json"),
                            }
                        )
                    )
                    ledger.initialize()
                    ledger.initialize_shadow_scopes()
                    self._initialize_attempts(connection)
                    previous = connection.execute(
                        "SELECT canonical_input,evidence_id FROM d1_shadow_attempts "
                        "WHERE observation_key=?",
                        (key,),
                    ).fetchone()
                    if previous is None:
                        data = parse_model(
                            SelectionPolicyInput,
                            canonical_bytes(
                                {
                                    **template,
                                    "decision_id": "decision:" + secrets.token_hex(16),
                                    **observation.clocks.model_dump(
                                        mode="json", exclude={"schema_version"}
                                    ),
                                }
                            ),
                        )
                        evidence_id = "evidence:" + secrets.token_hex(16)
                        connection.execute(
                            "INSERT INTO d1_shadow_attempts VALUES (?,?,?,?)",
                            (key, run_id, canonical_bytes(data), evidence_id),
                        )
                    else:
                        data = parse_model(SelectionPolicyInput, previous[0])
                        evidence_id = previous[1]
                        without_clocks = data.model_dump(
                            mode="json", exclude={"decision_id", "authority_now", "commercial_now"}
                        )
                        if canonical_bytes(without_clocks) != canonical_bytes(template):
                            raise GovernanceError("GOV_INPUT_INVALID")
                    refs = (
                        intent_ref,
                        feed_ref,
                        context.data.eligibility_algorithm_ref,
                        context.data.normalization_ref,
                        context.data.clock_profile_ref,
                        context.data.commercial_ref,
                        context.ref,
                    )
                    scope = EvidenceScope(
                        schema_version=SCHEMA,
                        run_id=run_id,
                        trace_id=data.trace_id,
                        owner_ref=data.owner_ref,
                        purchase_mode="HP",
                        governance_mode="shadow",
                        phase="D1",
                        context_version=data.context_version,
                        policy_binding=data.expected_policy,
                        source_refs=refs,
                        policy_artifact_ref=context.data.policy_artifact_ref,
                        engine_artifact_ref=self._engine_ref,
                    )
                    policy = vault.public_source(
                        run_id, owner_id, context.data.policy_artifact_ref, "policy"
                    )
                    if not connection.execute(
                        "SELECT 1 FROM governance_episodes WHERE run_id=?", (run_id,)
                    ).fetchone():
                        ledger.register_episode(
                            scope, policy_bytes=policy, engine_bytes=self._engine_bytes
                        )
                    ledger.register_shadow_scope(data.decision_id, scope)
                existing = ledger.get(evidence_id)
                if existing is not None:
                    if not isinstance(existing, DecisionRecord) or existing.policy_input() != data:
                        raise GovernanceError("GOV_INPUT_INVALID")
                    self._observed(existing, reused=True)
                    return
                evaluation_start = perf_counter_ns()
                try:
                    result = ReferencePolicyEvaluator(
                        artifact_bytes=policy,
                        binding=data.expected_policy,
                        identity=self._identity,
                        metadata=EvaluationMetadata(
                            schema_version=SCHEMA,
                            decided_at=utc_milliseconds(datetime.now(UTC)),
                            latency_ms=0,
                        ),
                    ).evaluate(data)
                except Exception:
                    raise GovernanceError("GOV_ENGINE_UNAVAILABLE") from None
                elapsed = (perf_counter_ns() - evaluation_start) / 1_000_000
                self.diagnostics.evaluation_ms = elapsed
                result = PolicyDecision.model_validate(
                    result.model_copy(
                        update={
                            "latency_ms": int(elapsed),
                            "decided_at": utc_milliseconds(datetime.now(UTC)),
                        }
                    )
                )
                for _ in range(3):
                    sequence = connection.execute(
                        "SELECT COALESCE(MAX(sequence),0)+1 FROM governance_evidence "
                        "WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                    record = DecisionRecord(
                        schema_version=SCHEMA,
                        evidence_id=evidence_id,
                        sequence=sequence,
                        recorded_at=utc_milliseconds(datetime.now(UTC)),
                        record_kind="decision_record",
                        canonicalization_profile="commerce-canonical-json/1",
                        canonical_input_bytes=canonical_bytes(data).decode(),
                        decision=result,
                        policy_artifact_ref=context.data.policy_artifact_ref,
                        engine_artifact_ref=self._engine_ref,
                        source_refs=refs,
                        application=EvidenceApplication(
                            schema_version=SCHEMA,
                            disposition="observed_shadow",
                            transition_ref=None,
                            expected_state_version=data.expected_local_state_version,
                            resulting_state_version=None,
                        ),
                        replayability="full",
                    )
                    ledger.prepare_decision(record)
                    with connection:
                        connection.execute("BEGIN IMMEDIATE")
                        existing = ledger.get(evidence_id)
                        if isinstance(existing, DecisionRecord):
                            self._observed(existing, reused=True)
                            return
                        actual_next = connection.execute(
                            "SELECT COALESCE(MAX(sequence),0)+1 FROM governance_evidence "
                            "WHERE run_id=?",
                            (run_id,),
                        ).fetchone()[0]
                        if actual_next != sequence:
                            continue
                        ledger.record_decision(
                            record, expected_state_version=data.expected_local_state_version
                        )
                    self._observed(record, reused=False)
                    return
                raise GovernanceError("GOV_STATE_CONFLICT")
        except Exception as error:
            self.failure(
                error.reason if isinstance(error, GovernanceError) else "GOV_EVIDENCE_UNAVAILABLE"
            )
        finally:
            self.diagnostics.observation_ms = (perf_counter_ns() - started) / 1_000_000

    def _observed(self, record: DecisionRecord, *, reused: bool) -> None:
        self.diagnostics.status = "observed"
        self.diagnostics.reason = None
        self.diagnostics.last_outcome = record.decision.outcome
        self.diagnostics.last_evidence_id = record.evidence_id
        if reused:
            self.diagnostics.reused += 1
        else:
            self.diagnostics.observed += 1

    @staticmethod
    def _initialize_attempts(connection: sqlite3.Connection) -> None:
        connection.execute("""CREATE TABLE IF NOT EXISTS d1_shadow_attempts (
            observation_key TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES purchases(run_id),
            canonical_input BLOB NOT NULL, evidence_id TEXT NOT NULL UNIQUE)""")
        for action in ("UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS d1_shadow_attempts_no_{action.lower()} "
                f"BEFORE {action} ON d1_shadow_attempts "
                "BEGIN SELECT RAISE(ABORT, 'd1_shadow_append_only'); END"
            )
        connection.execute("""CREATE TRIGGER IF NOT EXISTS d1_shadow_attempts_no_replace
            BEFORE INSERT ON d1_shadow_attempts
            WHEN EXISTS(SELECT 1 FROM d1_shadow_attempts WHERE observation_key=NEW.observation_key
                        OR evidence_id=NEW.evidence_id)
            BEGIN SELECT RAISE(ABORT, 'd1_shadow_identity_conflict'); END""")


class UnavailableShadowObserver:
    def __init__(self):
        self.diagnostics = ShadowDiagnostics(status="degraded", reason="GOV_FACTS_UNAVAILABLE")
        logging.getLogger(__name__).warning("D1 shadow configuration unavailable")

    def failure(self) -> None:
        self.diagnostics.lost += 1

    def capture(
        self,
        feed: ProductFeed,
        intent: PurchaseIntent,
        ranking: Callable[[ProductFeed, PurchaseIntent], SelectionResult],
    ) -> object | None:
        self.diagnostics.lost += 1
        return None

    def observe(
        self,
        capture: object,
        *,
        run_id: str,
        owner_id: str,
        selection: SelectionResult,
        selected_variant_id: str,
    ) -> None:
        self.diagnostics.lost += 1


def build_shadow_observer(
    settings: Settings, commercial_clock: Callable[[], datetime] | None
) -> ShadowD1Observer | UnavailableShadowObserver | None:
    if settings.agent_governance_mode == "off":
        return None
    try:
        configured = configured_d1(settings, commercial_clock)
        if not isinstance(configured.configuration, D1Configuration):
            raise ValueError("Shadow configuration required")
        return ShadowD1Observer(
            settings.agent_database_path,
            configured.configuration,
            configured.keys,
            configured.clocks,
            policy_bytes=configured.policy_bytes,
            policy_binding=configured.policy_binding,
        )
    except Exception:
        return UnavailableShadowObserver()
