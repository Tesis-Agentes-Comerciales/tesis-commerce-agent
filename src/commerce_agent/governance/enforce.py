"""Experimental backend-injected D1 gate. Runtime assembly is opt-in (bootstrap.py);
only AGENT_HP_FLOW=governed gives it dispatch ports (runtime_dispatch.py).

An explicit envelope transport can be injected by the backend for the first POST
only. Its creation observation stops before every post-create commercial effect.
There is deliberately no automatic remote replay, even after an unsent claim.
"""

import hmac
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
from typing import Literal, Protocol

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
    creation_idempotency_key,
    merchant_creation_digest,
)
from .evaluator import (
    D1PolicyBoundary,
    GovernanceError,
    PolicyEvaluator,
    ReferencePolicyEvaluator,
    measure_reference_engine,
)
from .evidence import (
    ApplicationFailure,
    DecisionEvidence,
    DecisionRecord,
    EvidenceApplication,
    EvidenceScope,
)
from .evidence_artifacts import capture_reference_sources
from .hnp_policy import (
    HnpGrant,
    build_hnp_policy,
    executor_principal,
    grant_shortlist,
    hnp_d1_configuration,
)
from .models import (
    SCHEMA,
    EvaluationMetadata,
    ObjectRef,
    PolicyBinding,
    PolicyDecision,
    PurchaseMode,
    SelectionPolicyInput,
)
from .post_create import ExperimentalPostCreateDispatch, PostCreateFlow
from .provenance import (
    ClockReader,
    D1EnforceConfiguration,
    ProvenanceKeys,
    merchant_pseudonym,
    utc_milliseconds,
)
from .provenance_artifacts import measure_d1_sources
from .shadow_artifacts import verify_loaded_selection
from .uow import D1Application

RefusalCode = Literal["out_of_stock", "offer_expired", "invalid_item"]


class CheckoutCreationRefused(Exception):
    """A dispatch port's proof that its one POST created nothing: the Merchant refused it.

    A port raises it only for a refusal the Merchant answers after finding no checkout for
    the key and before creating one (runtime_dispatch.DEFINITIVE_CREATION_REFUSALS). The
    gate then records `definitive_failure`; every other failure stays `uncertain`.
    """

    def __init__(self, code: RefusalCode) -> None:
        super().__init__(code)
        self.code: RefusalCode = code


class InitialCheckoutDispatch(Protocol):
    """Trusted backend transport boundary, admitted only after a committed claim.

    The governed runtime injects SingleAttemptCheckoutDispatch; tests may inject a
    spy. This interface itself is not evidence that a Merchant offers
    replay/reconciliation. It may raise CheckoutCreationRefused, and nothing else
    proves that the POST created nothing.
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
    """D1 before `create_checkout`. With `purchase_mode="HNP"` it is D1-HNP: it decides only
    purchases that execute a grant, always under b2-hnp-v1 built in code (hnp_policy.py)."""

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
        purchase_mode: PurchaseMode = "HP",
        merchant_refs: tuple[str, ...] | None = None,
    ):
        started = perf_counter_ns()
        self._mode: PurchaseMode = purchase_mode
        if purchase_mode == "HNP":
            # Never AGENT_D1_POLICY_*: those configure the HP policy set (§7). The operator's
            # merchant list arrives as `merchant_refs` (hnp_policy.operator_merchant_refs, K11).
            if policy_bytes is not None or policy_binding is not None:
                raise GovernanceError("GOV_ARTIFACT_MISMATCH")
            configuration = hnp_d1_configuration(configuration)
            policy_bytes, policy_binding = build_hnp_policy(
                merchant_ref=merchant_pseudonym(keys, configuration.merchant_base_url)
                if merchant_refs is None
                else merchant_refs
            )
        elif merchant_refs is not None:
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
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
        grant: HnpGrant | None,
    ) -> tuple[SelectionPolicyInput, ObjectRef, str, str, str, int, EvidenceScope, bytes]:
        vault = SqliteD1ProvenanceStore(connection, self._keys)
        ledger = SqliteGovernanceEvidenceStore(connection)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            vault.initialize()
            ledger.initialize()
            SqliteD1DispatchStore(connection).initialize()
            # The purchase names its mode and grant when created; the gate never infers them.
            purchase = connection.execute(
                "SELECT purchase_mode,grant_id FROM purchases WHERE run_id=?", (run_id,)
            ).fetchone()
            if purchase is None or tuple(purchase) != (
                self._mode,
                None if grant is None else grant.grant_id,
            ):
                raise GovernanceError("GOV_INPUT_INVALID")
            vault.register_episode(run_id, owner_id, experiment_run_id=None)
            context = vault.register_context(
                run_id,
                owner_id,
                self._configuration,
                policy_bytes=self._policy,
                policy_binding=self._binding,
                grant=grant,
            )
            ctx = context.data
            intent_ref = vault.register_intent(run_id, owner_id, capture.intent)
            feed_ref = vault.register_feed(run_id, owner_id, capture.feed)
            facts = vault.intent_facts(run_id, owner_id, intent_ref, context.ref)
            observed = vault.observe_context(run_id, owner_id, context.ref, self._clocks)
            ranked = sorted(item.candidate.variant_id for item in selection.ranked)
            if len(ranked) != len(set(ranked)) or candidate.variant_id not in ranked:
                raise GovernanceError("GOV_INPUT_INVALID")
            # In HNP, POL-01 decides whether the selection is inside the grant (§3.3).
            eligible = ranked if grant is None else list(grant_shortlist(ranked, grant))
            unit = vault.selected_unit_minor(run_id, owner_id, feed_ref, candidate.variant_id)
            template = {
                "schema_version": SCHEMA,
                "run_id": run_id,
                "trace_id": ctx.trace_id,
                "owner_ref": ctx.owner_ref,
                "purchase_mode": self._mode,
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
                **({} if unit is None else {"selected_unit_minor": unit}),
                # D1-HNP names the executor for attribution (§9.1); no D1 rule reads it.
                **(
                    {}
                    if grant is None
                    else {
                        "principal": executor_principal(
                            owns_run=_grant_owned_by(connection, grant.grant_id, owner_id)
                        )
                    }
                ),
            }
            key = creation_idempotency_key(
                self._keys.pseudonym_key,
                run_id=run_id,
                owner_id=owner_id,
                merchant_origin=self._configuration.merchant_base_url,
                request=build_checkout_create(candidate.variant_id, candidate.currency),
            )
            history = ledger.for_run(run_id)
            previous = connection.execute(
                "SELECT attempt,canonical_input,context_ref,operation_id,evidence_id,"
                "idempotency_key FROM d1_enforce_inputs WHERE run_id=? "
                "ORDER BY attempt DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if previous is None or self._settled(history, previous[1]):
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
                connection.execute(
                    "INSERT INTO d1_enforce_inputs VALUES (?,?,?,?,?,?,?)",
                    (
                        run_id,
                        1 if previous is None else previous[0] + 1,
                        canonical_bytes(data),
                        canonical_bytes(context.ref),
                        operation_id,
                        evidence_id,
                        key,
                    ),
                )
            else:
                data = parse_model(SelectionPolicyInput, previous[1])
                if canonical_bytes(
                    data.model_dump(
                        mode="json", exclude={"decision_id", "authority_now", "commercial_now"}
                    )
                ) != canonical_bytes(template) or previous[2] != canonical_bytes(context.ref):
                    raise GovernanceError("GOV_INPUT_INVALID")
                if previous[5] != key:
                    raise GovernanceError("GOV_STATE_CONFLICT")
                operation_id, evidence_id = previous[3:5]
            # Ledger sequences are contiguous per run; this attempt appends after them.
            sequence = len(history) + 1
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
                purchase_mode=self._mode,
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
        return data, context.ref, operation_id, evidence_id, key, sequence, scope, policy

    @staticmethod
    def _settled(history: tuple[DecisionEvidence, ...], canonical_input: bytes) -> bool:
        """Only an attempt that ended without a reservation admits a new one.

        Recorded deny/blocked/failure evidence is kept and a retry appends after it.
        An attempt without evidence may be in flight or lost before commit, so it is
        continued with its identities. A committed transition is never superseded.
        """
        if _committed_transition(history) is not None:
            return False
        decision_id = parse_model(SelectionPolicyInput, canonical_input).decision_id
        return any(
            (item.decision.decision_id if isinstance(item, DecisionRecord) else item.decision_id)
            == decision_id
            for item in history
        )

    def create_checkout(
        self,
        capture: object,
        *,
        run_id: str,
        owner_id: str,
        selection: SelectionResult,
        candidate: ProductCandidate,
        gateway: CheckoutGateway,
        grant: HnpGrant | None = None,
    ) -> CheckoutSession:
        record: DecisionRecord | None = None
        uow: SqliteD1UnitOfWork | None = None
        try:
            # A gate decides only purchases of its own mode: HNP ones execute a grant.
            if not isinstance(capture, _Capture) or (grant is None) != (self._mode == "HP"):
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
                (
                    data,
                    context_ref,
                    operation_id,
                    evidence_id,
                    key,
                    sequence,
                    scope,
                    policy,
                ) = self._assemble(
                    connection, capture, run_id, owner_id, selection, candidate, grant
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
                    sequence=sequence,
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
            if record is not None and record.decision.outcome == "allow":
                # The policy allowed; committing, verifying or claiming it failed before any POST.
                raise PurchaseServiceError(
                    "D1 allow could not be applied before dispatch", code="d1_allow_unapplied"
                ) from None
            raise PurchaseServiceError(
                "D1 policy review unavailable", code="d1_unavailable"
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
                    sequence=record.sequence + 1,
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
        except CheckoutCreationRefused as refusal:
            # Only the POST itself can prove that nothing was created: once its response
            # arrived (`invalid`), a refusal from a later step proves nothing.
            if invalid:
                self._uncertain(run_id, owner_id, operation_id, invalid)
            elif self._refused(run_id, owner_id, operation_id, refusal.code):
                raise PurchaseServiceError(
                    "Merchant refused to create the checkout", code=refusal.code
                ) from None
            raise PurchaseServiceError(
                "Checkout dispatch requires reconciliation", code="d1_dispatch_uncertain"
            ) from None
        except Exception:
            self._uncertain(run_id, owner_id, operation_id, invalid)
            raise PurchaseServiceError(
                "Checkout dispatch requires reconciliation", code="d1_dispatch_uncertain"
            ) from None

    def _refused(self, run_id, owner_id, operation_id, code: RefusalCode) -> bool:
        """Record that the one POST created nothing; the purchase fails as in B0.

        False if it could not be recorded: the claim then has no result, which reads as
        uncertain, and the purchase stays pending.
        """
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                SqliteD1DispatchStore(connection).observe(
                    operation_id,
                    state="definitive_failure",
                    checkout_id=None,
                    code="VERIFIED_NOT_CREATED",
                )
                # `updated_at` is when the purchase failed: the trace's `failed` step.
                failed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                failed = connection.execute(
                    "UPDATE purchases SET stage='failed',failure_code=?,updated_at=?,"
                    "state_version=state_version+1 "
                    "WHERE run_id=? AND owner_id=? AND stage='selected'",
                    (code, failed_at, run_id, owner_id),
                ).rowcount
                if failed != 1:
                    raise GovernanceError("GOV_STATE_CONFLICT")
            return True
        except Exception:
            self.lost_diagnostics += 1
            logging.getLogger(__name__).warning("D1 dispatch observation unavailable")
            return False

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

    def committed_allow(self, run_id: str) -> str | None:
        """Operation a committed D1 allow authorized for this run, if any. Read only."""
        with closing(self._connect()) as connection:
            record = _committed_transition(
                SqliteGovernanceEvidenceStore(connection).for_run(run_id)
            )
        if record is None or record.decision.outcome != "allow":
            return None
        return record.application.transition_ref

    def recovery_status(self, run_id: str) -> tuple[str, str | None, str] | None:
        """Read only, backend-only. No resend and no idempotency key export."""
        with closing(self._connect()) as connection:
            return SqliteD1DispatchStore(connection).status(run_id)

    def post_create_flow(self, run_id: str, owner_id: str) -> PostCreateFlow:
        return PostCreateFlow(
            self._connect, self._keys, run_id, owner_id, self._post_create_dispatch
        )


def _committed_transition(history: tuple[DecisionEvidence, ...]) -> DecisionRecord | None:
    """The run's committed D1 transition; at most one exists and none supersedes it.

    A D2 allow is also a committed transition (the approval), never the D1 one.
    """
    return next(
        (
            item
            for item in history
            if isinstance(item, DecisionRecord)
            and item.decision.phase == "D1"
            and item.application.disposition == "transition_committed"
        ),
        None,
    )


def _grant_owned_by(connection: sqlite3.Connection, grant_id: str, owner_id: str) -> bool:
    """`owns_run` of the D1-HNP principal: the delegation ledger shows the grant's owner as the
    run's owner (§7). No D1 rule reads it, so D1-HNP does not require the ledger: without the
    grant's row it is `False`, and D2-HNP, which evaluates it, fails closed without that row."""
    ledger = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delegation_grants'"
    ).fetchone()
    row = (
        None
        if ledger is None
        else connection.execute(
            "SELECT owner_id FROM delegation_grants WHERE grant_id=?", (grant_id,)
        ).fetchone()
    )
    return row is not None and hmac.compare_digest(str(row[0]).encode(), owner_id.encode())
