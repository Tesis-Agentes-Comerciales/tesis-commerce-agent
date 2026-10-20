"""Experimental backend-injected D1 gate. Runtime assembly is opt-in, without dispatch.

An explicit envelope transport can be injected by the backend for the first POST
only. Its creation observation stops before every post-create commercial effect.
There is deliberately no automatic remote replay, even after an unsent claim.
"""

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
from typing import Protocol

from pydantic import SecretStr

from commerce_agent.acp.creation import build_checkout_create
from commerce_agent.acp.feed import ProductCandidate, ProductFeed
from commerce_agent.acp.initial_dispatch import EnvelopeInitialDispatch
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.application.purchase import (
    CheckoutGateway,
    PurchaseService,
    PurchaseServiceError,
)
from commerce_agent.domain import PurchaseIntent, SelectionResult, rank_candidates
from commerce_agent.persistence.d1_dispatch_sqlite import SqliteD1DispatchStore
from commerce_agent.persistence.d1_envelope_sqlite import SqliteD1EnvelopeStore
from commerce_agent.persistence.d1_uow_sqlite import SqliteD1UnitOfWork
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.provenance_sqlite import (
    SqliteD1ProvenanceStore,
    _model_values,
    _operational_bytes,
)

from .canonical import canonical_bytes, parse_model, sha256_digest
from .dispatch_envelope import (
    CheckoutCreationEnvelope,
    ExpectedRemoteBinding,
    merchant_creation_digest,
)
from .evaluator import (
    D1PolicyBoundary,
    GovernanceError,
    PolicyEvaluator,
    ReferencePolicyEvaluator,
    measure_reference_engine,
)
from .evidence import ApplicationFailure, DecisionRecord, EvidenceApplication, EvidenceScope
from .evidence_artifacts import capture_reference_sources
from .models import (
    SCHEMA,
    EvaluationMetadata,
    ObjectRef,
    PolicyBinding,
    PolicyDecision,
    SelectionPolicyInput,
)
from .post_create import ExperimentalPostCreateDispatch, PostCreateFlow
from .provenance import ClockReader, D1EnforceConfiguration, ProvenanceKeys, utc_milliseconds
from .provenance_artifacts import measure_d1_sources
from .shadow_artifacts import verify_loaded_selection
from .uow import D1Application


class InitialCheckoutDispatch(Protocol):
    """Trusted backend transport boundary. No production implementation in 06B.

    Tests may inject a single-attempt spy. This interface itself is not evidence
    that a Merchant offers replay/reconciliation, and does not enable Settings.
    """

    def create_once(
        self,
        gateway: CheckoutGateway,
        *,
        variant_id: str,
        currency: str,
        idempotency_key: str,
    ) -> CheckoutSession: ...


class _MeasuredEvaluator:
    def __init__(self, evaluator: PolicyEvaluator, timings: dict[str, float]):
        self._evaluator, self._timings = evaluator, timings

    def evaluate(self, policy_input: SelectionPolicyInput) -> PolicyDecision:
        started = perf_counter_ns()
        try:
            return self._evaluator.evaluate(policy_input)
        finally:
            self._timings["evaluation_ms"] = (perf_counter_ns() - started) / 1e6


@dataclass(frozen=True)
class _Capture:
    feed: ProductFeed = field(repr=False)
    intent: PurchaseIntent = field(repr=False)
    live_feed: ProductFeed = field(repr=False)
    live_intent: PurchaseIntent = field(repr=False)
    ranking: Callable[[ProductFeed, PurchaseIntent], SelectionResult] = field(repr=False)


class SqliteD1EnforceGate:
    def __init__(
        self,
        path: Path,
        configuration: D1EnforceConfiguration,
        keys: ProvenanceKeys,
        clocks: ClockReader,
        *,
        dispatch: InitialCheckoutDispatch | None = None,
        initial_dispatch: EnvelopeInitialDispatch | None = None,
        post_create_dispatch: ExperimentalPostCreateDispatch | None = None,
        policy_bytes: bytes | None = None,
        policy_binding: PolicyBinding | None = None,
        evaluator_factory: Callable[[bytes, PolicyBinding, EvaluationMetadata], PolicyEvaluator]
        | None = None,
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
        expected_remote_binding: ExpectedRemoteBinding | None = None,
    ):
        started = perf_counter_ns()
        self._path = path
        self._configuration = D1EnforceConfiguration.model_validate(configuration)
        self._keys = ProvenanceKeys.model_validate(keys)
        if clocks.profile != configuration.clock:
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        self._clocks = clocks
        self._dispatch = dispatch
        if dispatch is not None and initial_dispatch is not None:
            raise ValueError("Initial dispatch ports must be configured separately")
        self._initial_dispatch = initial_dispatch
        self._post_create_dispatch = post_create_dispatch
        self._policy, self._binding = policy_bytes, policy_binding
        if (policy_bytes is None) != (policy_binding is None):
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        self._factory = evaluator_factory
        self._connection_factory = connection_factory
        self._expected_remote_binding = (
            parse_model(ExpectedRemoteBinding, canonical_bytes(expected_remote_binding))
            if expected_remote_binding is not None
            else None
        )
        self._sources = measure_d1_sources()
        verify_loaded_selection(json.loads(self._sources["algorithm"])["sources"], rank_candidates)
        self._identity = measure_reference_engine()
        self._engine_bytes = capture_reference_sources()
        assert self._identity.artifact_digest is not None
        self._engine_ref = ObjectRef(
            schema_version=SCHEMA,
            id="reference:" + self._identity.artifact_digest[7:],
            version=1,
            digest=self._identity.artifact_digest,
        )
        self.timings: dict[str, float] = {"bootstrap_ms": (perf_counter_ns() - started) / 1e6}
        self.lost_diagnostics = 0

    def _connect(self) -> sqlite3.Connection:
        connection = (
            self._connection_factory()
            if self._connection_factory
            else sqlite3.connect(self._path, timeout=10)
        )
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def capture(
        self,
        feed: ProductFeed,
        intent: PurchaseIntent,
        ranking: Callable[[ProductFeed, PurchaseIntent], SelectionResult],
    ) -> object:
        started = perf_counter_ns()
        if measure_d1_sources() != self._sources or measure_reference_engine() != self._identity:
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        verify_loaded_selection(json.loads(self._sources["algorithm"])["sources"], ranking)
        result = _Capture(deepcopy(feed), deepcopy(intent), feed, intent, ranking)
        self.timings["capture_ms"] = (perf_counter_ns() - started) / 1e6
        return result

    def _assemble(
        self,
        connection: sqlite3.Connection,
        capture: _Capture,
        run_id: str,
        owner_id: str,
        selection: SelectionResult,
        candidate: ProductCandidate,
    ) -> tuple[SelectionPolicyInput, ObjectRef, str, str, str, EvidenceScope, bytes]:
        vault = SqliteD1ProvenanceStore(connection, self._keys)
        ledger = SqliteGovernanceEvidenceStore(connection)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            vault.initialize()
            ledger.initialize()
            SqliteD1DispatchStore(connection).initialize()
            vault.register_episode(run_id, owner_id, experiment_run_id=None)
            context = vault.register_context(
                run_id,
                owner_id,
                self._configuration,
                policy_bytes=self._policy,
                policy_binding=self._binding,
            )
            ctx = context.data
            intent_ref = vault.register_intent(run_id, owner_id, capture.intent)
            feed_ref = vault.register_feed(run_id, owner_id, capture.feed)
            facts = vault.intent_facts(run_id, owner_id, intent_ref, context.ref)
            observed = vault.observe_context(run_id, owner_id, context.ref, self._clocks)
            eligible = sorted(item.candidate.variant_id for item in selection.ranked)
            if len(eligible) != len(set(eligible)) or candidate.variant_id not in eligible:
                raise GovernanceError("GOV_INPUT_INVALID")
            template = {
                "schema_version": SCHEMA,
                "run_id": run_id,
                "trace_id": ctx.trace_id,
                "owner_ref": ctx.owner_ref,
                "purchase_mode": "HP",
                "governance_mode": "enforce",
                "phase": "D1",
                "action": "prepare_checkout",
                "context_version": context.ref.version,
                "expected_local_state_version": observed.expected_local_state_version,
                "expected_policy": ctx.expected_policy.model_dump(mode="json"),
                "clock_profile_ref": ctx.clock_profile_ref.model_dump(mode="json"),
                "experiment_run_id": ctx.experiment_run_id,
                "conversation_id": ctx.conversation_id,
                "intent": facts.model_dump(mode="json"),
                "feed_snapshot_ref": feed_ref.model_dump(mode="json"),
                "eligibility_algorithm_ref": ctx.eligibility_algorithm_ref.model_dump(mode="json"),
                "eligible_variant_ids": eligible,
                "selected_variant_id": candidate.variant_id,
                "merchant_ref": ctx.merchant_ref,
            }
            previous = connection.execute(
                "SELECT canonical_input,context_ref,operation_id,evidence_id,idempotency_key "
                "FROM d1_enforce_inputs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if previous is None:
                data = parse_model(
                    SelectionPolicyInput,
                    canonical_bytes(
                        {
                            **template,
                            "decision_id": "decision:" + secrets.token_hex(16),
                            **observed.clocks.model_dump(mode="json", exclude={"schema_version"}),
                        }
                    ),
                )
                operation_id, evidence_id = (
                    "operation:" + secrets.token_hex(16),
                    "evidence:" + secrets.token_hex(16),
                )
                key = "agent_" + secrets.token_urlsafe(24)
                connection.execute(
                    "INSERT INTO d1_enforce_inputs VALUES (?,?,?,?,?,?)",
                    (
                        run_id,
                        canonical_bytes(data),
                        canonical_bytes(context.ref),
                        operation_id,
                        evidence_id,
                        key,
                    ),
                )
            else:
                data = parse_model(SelectionPolicyInput, previous[0])
                if canonical_bytes(
                    data.model_dump(
                        mode="json", exclude={"decision_id", "authority_now", "commercial_now"}
                    )
                ) != canonical_bytes(template) or previous[1] != canonical_bytes(context.ref):
                    raise GovernanceError("GOV_INPUT_INVALID")
                operation_id, evidence_id, key = previous[2:]
            refs = (
                intent_ref,
                feed_ref,
                ctx.eligibility_algorithm_ref,
                ctx.normalization_ref,
                ctx.clock_profile_ref,
                ctx.commercial_ref,
                context.ref,
            )
            scope = EvidenceScope(
                schema_version=SCHEMA,
                run_id=run_id,
                trace_id=data.trace_id,
                owner_ref=ctx.owner_ref,
                purchase_mode="HP",
                governance_mode="enforce",
                phase="D1",
                context_version=data.context_version,
                policy_binding=ctx.expected_policy,
                source_refs=refs,
                policy_artifact_ref=ctx.policy_artifact_ref,
                engine_artifact_ref=self._engine_ref,
            )
            policy = vault.public_source(run_id, owner_id, ctx.policy_artifact_ref, "policy")
            ledger.register_episode(scope, policy_bytes=policy, engine_bytes=self._engine_bytes)
        return data, context.ref, operation_id, evidence_id, key, scope, policy

    def create_checkout(
        self,
        capture: object,
        *,
        run_id: str,
        owner_id: str,
        selection: SelectionResult,
        candidate: ProductCandidate,
        gateway: CheckoutGateway,
    ) -> CheckoutSession:
        record: DecisionRecord | None = None
        uow: SqliteD1UnitOfWork | None = None
        try:
            if not isinstance(capture, _Capture):
                raise GovernanceError("GOV_INPUT_INVALID")
            if _operational_bytes(capture.live_feed) != _operational_bytes(
                capture.feed
            ) or _operational_bytes(capture.live_intent) != _operational_bytes(capture.intent):
                raise GovernanceError("GOV_FACTS_UNAVAILABLE")
            verify_loaded_selection(
                json.loads(self._sources["algorithm"])["sources"], capture.ranking
            )
            with closing(self._connect()) as connection:
                started = perf_counter_ns()
                data, context_ref, operation_id, evidence_id, key, scope, policy = self._assemble(
                    connection, capture, run_id, owner_id, selection, candidate
                )
                self.timings["snapshots_ms"] = (perf_counter_ns() - started) / 1e6
                started = perf_counter_ns()
                uow = SqliteD1UnitOfWork(
                    connection,
                    keys=self._keys,
                    policy_bytes=policy,
                    policy_binding=data.expected_policy,
                )
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    uow.initialize()
                    SqliteD1EnvelopeStore(connection, self._keys).initialize()
                self.timings["uow_setup_ms"] = (perf_counter_ns() - started) / 1e6
                metadata = EvaluationMetadata(
                    schema_version=SCHEMA,
                    decided_at=utc_milliseconds(datetime.now(UTC)),
                    latency_ms=0,
                )

                def factory() -> PolicyEvaluator:
                    if self._factory:
                        evaluator = self._factory(policy, data.expected_policy, metadata)
                    else:
                        evaluator = ReferencePolicyEvaluator(
                            artifact_bytes=policy,
                            binding=data.expected_policy,
                            metadata=metadata,
                            identity=self._identity,
                        )
                    return _MeasuredEvaluator(evaluator, self.timings)

                started = perf_counter_ns()
                result = D1PolicyBoundary(
                    binding=data.expected_policy,
                    artifact_bytes=policy,
                    engine_identity=self._identity,
                    metadata=metadata,
                    evaluator_factory=factory,
                ).evaluate(data)
                elapsed = perf_counter_ns() - started
                self.timings["evaluation_boundary_ms"] = elapsed / 1e6
                result = result.model_copy(
                    update={
                        "decided_at": utc_milliseconds(datetime.now(UTC)),
                        "latency_ms": elapsed // 1_000_000,
                    }
                )
                allow = result.outcome == "allow"
                record = DecisionRecord(
                    schema_version=SCHEMA,
                    record_kind="decision_record",
                    evidence_id=evidence_id,
                    sequence=1,
                    recorded_at=result.decided_at,
                    canonicalization_profile="commerce-canonical-json/1",
                    canonical_input_bytes=canonical_bytes(data).decode(),
                    decision=result,
                    policy_artifact_ref=scope.policy_artifact_ref,
                    engine_artifact_ref=scope.engine_artifact_ref
                    if result.origin == "policy"
                    else None,
                    source_refs=scope.source_refs,
                    application=EvidenceApplication(
                        schema_version=SCHEMA,
                        disposition="transition_committed" if allow else "blocked",
                        transition_ref=operation_id if allow else None,
                        expected_state_version=data.expected_local_state_version,
                        resulting_state_version=data.expected_local_state_version + 1
                        if allow
                        else None,
                    ),
                    replayability="full" if result.origin == "policy" else "policy_not_evaluated",
                )
                request = D1Application(
                    schema_version=SCHEMA,
                    evidence=record,
                    context_ref=context_ref,
                    operation_id=operation_id,
                    idempotency_key=SecretStr(key),
                )
                started = perf_counter_ns()
                uow.prepare(request)
                envelope = None
                if allow:
                    body = canonical_bytes(
                        build_checkout_create(candidate.variant_id, candidate.currency)
                    )
                    envelope = CheckoutCreationEnvelope(
                        schema_version="commerce-acp-create-envelope/1",
                        operation_id=operation_id,
                        run_id=run_id,
                        owner_id=owner_id,
                        merchant_origin=self._configuration.merchant_base_url,
                        merchant_ref=data.merchant_ref,
                        api_version="2026-04-17",
                        creation_profile="merchant-p0-create/1",
                        method="POST",
                        path="/checkout_sessions",
                        body_canonical=body.decode("utf-8"),
                        body_digest=merchant_creation_digest(body),
                        idempotency_key=SecretStr(key),
                        decision_id=data.decision_id,
                        evidence_id=evidence_id,
                        evidence_digest=sha256_digest(canonical_bytes(record)),
                        d1_input_digest=result.input_digest,
                        context_ref=context_ref,
                        prepared_at=record.recorded_at,
                        binding_status="backend_expected_unverified"
                        if self._expected_remote_binding is not None
                        else "unavailable",
                        expected_remote_binding=self._expected_remote_binding,
                    )
                self.timings["evidence_verification_ms"] = (perf_counter_ns() - started) / 1e6
                started = perf_counter_ns()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    applied = uow.apply(request, owner_id=owner_id)
                    if envelope is not None:
                        envelopes = SqliteD1EnvelopeStore(connection, self._keys)
                        if applied.reused:
                            # Never invent a request for a historical 06B reservation.
                            if (
                                envelopes.recover(operation_id, run_id=run_id, owner_id=owner_id)
                                != envelope
                            ):
                                raise GovernanceError("GOV_STATE_CONFLICT")
                        else:
                            envelopes.persist(envelope)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                self.timings["authorization_transaction_ms"] = (perf_counter_ns() - started) / 1e6
                if not allow:
                    raise PurchaseServiceError("D1 denied checkout", code="d1_denied")
                # Independently read committed state; neither an in-memory allow nor a
                # successful UPDATE is a dispatch permission before durable commit.
                with closing(self._connect()) as verifier:
                    if SqliteGovernanceEvidenceStore(verifier).get(
                        evidence_id
                    ) != record or SqliteD1DispatchStore(verifier).status(run_id) != (
                        "reserved",
                        None,
                        operation_id,
                    ):
                        raise GovernanceError("GOV_STATE_CONFLICT")
                    if (
                        envelope is None
                        or SqliteD1EnvelopeStore(verifier, self._keys).recover(
                            operation_id, run_id=run_id, owner_id=owner_id
                        )
                        != envelope
                    ):
                        raise GovernanceError("GOV_STATE_CONFLICT")
                if applied.reused or (self._dispatch is None and self._initial_dispatch is None):
                    raise PurchaseServiceError(
                        "D1 dispatch protocol unavailable or recovery required",
                        code="d1_dispatch_blocked",
                    )
                if self._initial_dispatch is not None:
                    assert envelope is not None
                    # Missing credentials/scope must fail before committing a claim.
                    self._initial_dispatch.validate(envelope)
                started = perf_counter_ns()
                claimant = "worker:" + secrets.token_hex(16)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    claimed = uow.claim(operation_id, claimant=claimant, owner_id=owner_id)
                    if not claimed:
                        raise GovernanceError("GOV_STATE_CONFLICT")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                self.timings["claim_ms"] = (perf_counter_ns() - started) / 1e6
                with closing(self._connect()) as verifier:
                    claim = verifier.execute(
                        "SELECT state,claimant FROM d1_checkout_operations WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
                    if claim is None or tuple(claim) != ("claimed", claimant):
                        raise GovernanceError("GOV_STATE_CONFLICT")
            return self._send(run_id, owner_id, operation_id, key, candidate, gateway)
        except PurchaseServiceError:
            raise
        except Exception:
            if record is not None and uow is not None:
                self._diagnose(record, owner_id)
            raise PurchaseServiceError(
                "D1 authorization unavailable", code="d1_unavailable"
            ) from None

    def _diagnose(self, record: DecisionRecord, owner_id: str) -> None:
        try:
            with closing(self._connect()) as connection:
                ledger = SqliteGovernanceEvidenceStore(connection)
                if ledger.get(record.evidence_id) is not None:
                    return
                data = record.policy_input()
                uow = SqliteD1UnitOfWork(
                    connection,
                    keys=self._keys,
                    policy_bytes=self._policy_for(connection, data.run_id),
                    policy_binding=data.expected_policy,
                )
                blocked = record.model_copy(
                    update={
                        "application": EvidenceApplication(
                            schema_version=SCHEMA,
                            disposition="blocked",
                            transition_ref=None,
                            expected_state_version=data.expected_local_state_version,
                            resulting_state_version=None,
                        )
                    }
                )
                if blocked.decision.origin == "policy":
                    uow.ledger.prepare_decision(blocked)
                observed = connection.execute(
                    "SELECT state_version FROM purchases WHERE run_id=?", (data.run_id,)
                ).fetchone()
                failure = ApplicationFailure(
                    schema_version=SCHEMA,
                    record_kind="application_failure",
                    evidence_id="failure:" + sha256_digest(record.evidence_id.encode())[7:],
                    sequence=2,
                    recorded_at=utc_milliseconds(datetime.now(UTC)),
                    run_id=data.run_id,
                    trace_id=data.trace_id,
                    decision_id=data.decision_id,
                    reason_code="GOV_EVIDENCE_UNAVAILABLE",
                    expected_state_version=data.expected_local_state_version,
                    observed_state_version=observed[0] if observed else None,
                    replayability="not_evaluable",
                )
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    uow.record_failure(blocked, failure, owner_id=owner_id)
        except Exception:
            self.lost_diagnostics += 1
            logging.getLogger(__name__).warning(
                "D1 diagnostic unavailable: GOV_EVIDENCE_UNAVAILABLE"
            )

    @staticmethod
    def _policy_for(connection: sqlite3.Connection, run_id: str) -> bytes:
        return connection.execute(
            "SELECT policy_bytes FROM governance_episodes WHERE run_id=?", (run_id,)
        ).fetchone()[0]

    def _send(
        self,
        run_id: str,
        owner_id: str,
        operation_id: str,
        key: str,
        candidate: ProductCandidate,
        gateway: CheckoutGateway,
    ) -> CheckoutSession:
        invalid = False
        try:
            with closing(self._connect()) as reader:
                envelope = SqliteD1EnvelopeStore(reader, self._keys).recover(
                    operation_id, run_id=run_id, owner_id=owner_id
                )
                if (
                    envelope is None
                    or envelope.idempotency_key.get_secret_value() != key
                    or envelope.merchant_origin != self._configuration.merchant_base_url
                    or envelope.expected_remote_binding != self._expected_remote_binding
                    or envelope.body_canonical.encode("utf-8")
                    != canonical_bytes(
                        build_checkout_create(candidate.variant_id, candidate.currency)
                    )
                ):
                    raise GovernanceError("GOV_STATE_CONFLICT")
            # The experimental legacy port only accepts variant/currency. Reject
            # any envelope it cannot represent exactly, rather than fill defaults.
            # The separate initial-only port sends the exact recovered bytes;
            # neither port promotes expected scope to authenticated evidence.
            if self._initial_dispatch is not None:
                response = self._initial_dispatch.create_once(envelope)
            else:
                assert self._dispatch is not None
                response = self._dispatch.create_once(
                    gateway,
                    variant_id=envelope.body()["line_items"][0]["id"],
                    currency=envelope.body()["currency"],
                    idempotency_key=envelope.idempotency_key.get_secret_value(),
                )
            invalid = True
            checkout = CheckoutSession.model_validate(_model_values(response))
            PurchaseService._validate_created_checkout(checkout, candidate)
            flow = self.post_create_flow(run_id, owner_id)
            flow.begin(checkout)
            if self._initial_dispatch is not None:
                # Durable creation_observed is not creation_confirmed, a fresh
                # D2-HP authorization, or permission to invoke even the next GET.
                raise PurchaseServiceError(
                    "Initial creation observed; post-create flow blocked",
                    code="d1_initial_creation_observed",
                )

            def verify_creation() -> CheckoutSession:
                current = CheckoutSession.model_validate(
                    _model_values(gateway.get_checkout(checkout.id))
                )
                if current != checkout or current.status != "not_ready_for_payment":
                    raise GovernanceError("GOV_INPUT_INVALID")
                PurchaseService._validate_created_checkout(current, candidate)
                return current

            current = flow.effect(
                "verify_creation_get", canonical_bytes(["GET", checkout.id]), None, verify_creation
            )
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT o.resulting_version FROM d1_checkout_operations o "
                    "JOIN purchases p ON p.run_id=o.run_id WHERE o.operation_id=? "
                    "AND o.run_id=? AND o.state='claimed' AND p.owner_id=? "
                    "AND p.stage='selected' AND p.state_version=o.resulting_version",
                    (operation_id, run_id, owner_id),
                ).fetchone()
                if row is None:
                    raise GovernanceError("GOV_STATE_CONFLICT")
                updated = connection.execute(
                    "UPDATE purchases SET stage='checkout_prepared',checkout_id=?,"
                    "state_version=state_version+1 WHERE run_id=? AND owner_id=? "
                    "AND state_version=? AND stage='selected'",
                    (current.id, run_id, owner_id, row[0]),
                ).rowcount
                if updated != 1:
                    raise GovernanceError("GOV_STATE_CONFLICT")
                SqliteD1DispatchStore(connection).observe(
                    operation_id,
                    state="confirmed",
                    checkout_id=current.id,
                    code="CHECKOUT_VERIFIED",
                )
                flow.confirm_creation(connection)
            return current
        except PurchaseServiceError as error:
            if error.code == "d1_initial_creation_observed":
                raise
            self._uncertain(run_id, owner_id, operation_id, invalid)
            raise PurchaseServiceError(
                "Checkout dispatch requires reconciliation", code="d1_dispatch_uncertain"
            ) from None
        except Exception:
            self._uncertain(run_id, owner_id, operation_id, invalid)
            raise PurchaseServiceError(
                "Checkout dispatch requires reconciliation", code="d1_dispatch_uncertain"
            ) from None

    def _uncertain(self, run_id, owner_id, operation_id, invalid):
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                # No checkout identity is stored when the response is not verified.
                SqliteD1DispatchStore(connection).observe(
                    operation_id,
                    state="uncertain",
                    checkout_id=None,
                    code="CHECKOUT_INVALID" if invalid else "DISPATCH_UNCERTAIN",
                )
                connection.execute(
                    "UPDATE purchases SET stage='uncertain',state_version=state_version+1 "
                    "WHERE run_id=? AND owner_id=? AND stage='selected'",
                    (run_id, owner_id),
                )
        except Exception:
            self.lost_diagnostics += 1
            logging.getLogger(__name__).warning("D1 dispatch observation unavailable")

    def recovery_status(self, run_id: str) -> tuple[str, str | None, str] | None:
        """Read only, backend-only. No resend and no idempotency key export."""
        with closing(self._connect()) as connection:
            return SqliteD1DispatchStore(connection).status(run_id)

    def post_create_flow(self, run_id: str, owner_id: str) -> PostCreateFlow:
        return PostCreateFlow(
            self._connect, self._keys, run_id, owner_id, self._post_create_dispatch
        )
