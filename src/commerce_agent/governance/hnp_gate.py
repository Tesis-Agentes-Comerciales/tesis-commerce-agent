"""D2-HNP and D3-HNP for the executor (docs/design/hnp-governed.md §3.4, §4.3, §4.5).

The gate assembles each input from the backend's own stores (vault, governance ledger,
`hp_evidence` and the delegation tables) and the merchant's authoritative checkout, never from
the model, and evaluates it outside any transaction. It applies nothing itself: the hooks it
returns write the evidence inside the delegation ledger's unit of work. An allow is recorded with
the reservation (UoW-1) or the outbox row (UoW-2) it names, a deny with the task's terminal, and
an allow whose unit of work lost its CAS as `blocked` plus `GOV_STATE_CONFLICT`.
"""

import hmac
import json
import logging
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Literal
from uuid import uuid4

import jwt
from jwcrypto.jwk import JWK

from commerce_agent.acp.models import CheckoutSession
from commerce_agent.ap2_hnp import (
    PROFILE_DIGEST,
    PROFILE_VERSION,
    MandateTrust,
    closed_checkout_jwt,
    verify_closed_proof,
    verify_open_proof,
)
from commerce_agent.delegation.models import EXECUTOR_REF
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.provenance_sqlite import SqliteD1ProvenanceStore

from .authorization import (
    FRESHNESS_CONTRACT,
    SqliteD2AuthorizationGate,
    record_input_rejected,
    resolved_d1,
)
from .canonical import canonical_bytes, parse_model, sha256_digest
from .enforce import _committed_transition
from .evaluator import (
    D3PolicyBoundary,
    GovernanceError,
    HnpD2PolicyBoundary,
    ReferenceDispatchEvaluator,
    ReferenceHnpAuthorizationEvaluator,
    measure_reference_engine,
)
from .evidence import (
    ApplicationFailure,
    DecisionRecord,
    EvidenceApplication,
    EvidenceScope,
    required_refs,
)
from .evidence_artifacts import capture_reference_sources
from .hnp_models import (
    DispatchPolicyInput,
    HnpAuthorizationPolicyInput,
    ProofStatus,
    parse_consent_scope,
    scope_digest,
)
from .hnp_policy import (
    HNP_AUTHORIZATION_PROFILE_ID,
    HnpGrant,
    build_hnp_policy,
    hnp_d1_configuration,
)
from .hnp_ports import HnpGateDecision
from .models import SCHEMA, EvaluationMetadata, ObjectRef
from .provenance import (
    ClockReader,
    D1EnforceConfiguration,
    ProvenanceKeys,
    merchant_pseudonym,
    pseudonym,
    utc_milliseconds,
)

Phase = Literal["D2", "D3"]
# The grant authorizes the whole purchase (§7, «Hechos del executor»).
GRANT_ACTIONS = ("prepare_checkout", "complete_purchase", "commit_dispatch")
INSTRUMENT_REF = "tesis_sandbox"
CLOSE_FRESHNESS_CONTRACT = {**FRESHNESS_CONTRACT, "stage": "close"}
_HOLDING = ("reserved", "committed", "uncertain")
_RESERVATION = (
    "SELECT reservation_id, operation_id, run_id, owner_id, expected_epoch, amount_minor, "
    "currency, state, state_version, d2_evidence_id FROM grant_reservations WHERE grant_id=?"
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Assembled:
    data: HnpAuthorizationPolicyInput | DispatchPolicyInput
    scope: EvidenceScope
    evidence_id: str
    sequence: int
    policy: bytes
    proof: ProofStatus
    transition: "_Transition"


@dataclass(frozen=True)
class _Transition:
    """Verifies, on the ledger's connection, the reservation (D2) or the outbox row (D3) that an
    allow names, and the grant version it left: what `_ApprovalTransition` is to D2-HP."""

    ref: str
    sql: str
    expected: tuple[object, ...]

    def verify(
        self, connection: sqlite3.Connection, run_id: str, application: EvidenceApplication
    ) -> bool:
        row = connection.execute(self.sql, (application.transition_ref,)).fetchone()
        return row is not None and tuple(row) == (
            *self.expected,
            application.resulting_state_version,
        )


class _Record:
    """A hook: the prepared records, written in the delegation ledger's unit of work."""

    def __init__(
        self,
        ledger: SqliteGovernanceEvidenceStore,
        record: DecisionRecord,
        transition: _Transition | None = None,
        *,
        conflict: str | None = None,
    ):
        self._ledger, self._record, self._transition = ledger, record, transition
        self._conflict = conflict

    def __call__(self, connection: sqlite3.Connection) -> None:
        record = self._record
        expected = record.application.expected_state_version
        try:
            store = self._ledger.on_connection(connection)
            store.record_decision(
                record, expected_state_version=expected, transition_verifier=self._transition
            )
            if self._conflict is not None:
                observed = connection.execute(
                    "SELECT state_version FROM delegation_grants WHERE grant_id=?",
                    (self._conflict,),
                ).fetchone()
                data = record.any_input()
                failure = ApplicationFailure(
                    schema_version=SCHEMA,
                    record_kind="application_failure",
                    evidence_id="failure:" + sha256_digest(record.evidence_id.encode())[7:],
                    sequence=record.sequence + 1,
                    recorded_at=utc_milliseconds(datetime.now(UTC)),
                    run_id=data.run_id,
                    trace_id=data.trace_id,
                    decision_id=data.decision_id,
                    reason_code="GOV_STATE_CONFLICT",
                    expected_state_version=expected,
                    observed_state_version=None if observed is None else observed[0],
                    replayability="not_evaluable",
                )
                store.record_decision(failure, expected_state_version=expected)
        except Exception:
            # Raising rolls the whole unit of work back (§4.5): no reservation, no outbox row.
            raise GovernanceError("GOV_EVIDENCE_UNAVAILABLE") from None


class SqliteHnpPolicyGate:
    """`HnpPolicyGate` in enforce. Backend injection only: no model-facing tool reaches it, and
    it holds no secret in its output. Same artifact and context as the D1-HNP gate."""

    def __init__(
        self,
        path: Path,
        configuration: D1EnforceConfiguration,
        keys: ProvenanceKeys,
        clocks: ClockReader,
        *,
        trust: MandateTrust,
        merchant_refs: tuple[str, ...] | None = None,
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
    ):
        configuration = hnp_d1_configuration(D1EnforceConfiguration.model_validate(configuration))
        if clocks.profile != configuration.clock:
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        self._path, self._configuration, self._clocks = path, configuration, clocks
        self._keys = ProvenanceKeys.model_validate(keys)
        self._trust = trust
        self._policy, self._binding = build_hnp_policy(
            merchant_ref=merchant_pseudonym(keys, configuration.merchant_base_url)
            if merchant_refs is None
            else merchant_refs
        )
        self._connection_factory = connection_factory
        self._identity = measure_reference_engine()
        self._engine_bytes = capture_reference_sources()
        assert self._identity.artifact_digest is not None
        self._engine_ref = ObjectRef(
            schema_version=SCHEMA,
            id="reference:" + self._identity.artifact_digest[7:],
            version=1,
            digest=self._identity.artifact_digest,
        )
        self._profile = {
            "schema_version": SCHEMA,
            "id": HNP_AUTHORIZATION_PROFILE_ID,
            "version": PROFILE_VERSION,
            "digest": PROFILE_DIGEST,
        }
        self._agent_key_ref = JWK.from_json(json.dumps(dict(trust.agent_key_jwk))).thumbprint()
        self.lost_diagnostics = 0

    def _connect(self) -> sqlite3.Connection:
        connection = (
            self._connection_factory()
            if self._connection_factory
            else sqlite3.connect(self._path, timeout=10)
        )
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def ready(self) -> None:
        """Startup check: a ledger created before D3-HNP is refused, not migrated."""
        with closing(self._connect()) as connection:
            if not SqliteGovernanceEvidenceStore(connection).admits_phase_d3():
                raise GovernanceError("GOV_EVIDENCE_UNAVAILABLE")

    def decide_d2(
        self,
        *,
        run_id: str,
        grant_id: str,
        checkout: CheckoutSession,
        reservation_id: str,
        operation_id: str,
    ) -> HnpGateDecision:
        return self._decide("D2", run_id, grant_id, checkout, reservation_id, operation_id)

    def decide_d3(
        self,
        *,
        run_id: str,
        grant_id: str,
        checkout: CheckoutSession,
        reservation_id: str,
        operation_id: str,
    ) -> HnpGateDecision:
        return self._decide("D3", run_id, grant_id, checkout, reservation_id, operation_id)

    def _decide(
        self,
        phase: Phase,
        run_id: str,
        grant_id: str,
        checkout: CheckoutSession,
        reservation_id: str,
        operation_id: str,
    ) -> HnpGateDecision:
        try:
            with closing(self._connect()) as connection:
                built = self._assemble(
                    connection, phase, run_id, grant_id, checkout, reservation_id, operation_id
                )
        except Exception:
            self._reject(run_id, phase, grant_id)
            raise GovernanceError("GOV_FACTS_UNAVAILABLE") from None
        record = self._evaluate(built)
        allow = record.decision.outcome == "allow"
        blocked = _blocked(record)
        try:
            with closing(self._connect()) as connection:
                ledger = SqliteGovernanceEvidenceStore(connection)
                if record.decision.origin == "policy":
                    ledger.prepare_decision(record)
                    if allow:
                        ledger.prepare_decision(blocked)
        except Exception:
            self._lost()
            raise GovernanceError("GOV_EVIDENCE_UNAVAILABLE") from None
        return HnpGateDecision(
            decision=record.decision,
            proof=built.proof,
            evidence_id=record.evidence_id,
            hooks=(_Record(ledger, record, built.transition if allow else None),),
            conflict_hooks=(_Record(ledger, blocked, conflict=grant_id),) if allow else (),
            transition_ref=built.transition.ref if allow and phase == "D3" else None,
        )

    def _evaluate(self, built: _Assembled) -> DecisionRecord:
        """The reference engine behind its fail-closed boundary, outside any transaction."""
        data, binding, identity = built.data, built.data.expected_policy, self._identity
        metadata = EvaluationMetadata(
            schema_version=SCHEMA, decided_at=utc_milliseconds(datetime.now(UTC)), latency_ms=0
        )
        engine = {"binding": binding, "artifact_bytes": built.policy, "metadata": metadata}
        boundary = {**engine, "engine_identity": identity}
        started = perf_counter_ns()
        if isinstance(data, HnpAuthorizationPolicyInput):
            result = HnpD2PolicyBoundary(
                **boundary,
                evaluator_factory=lambda: ReferenceHnpAuthorizationEvaluator(
                    **engine, identity=identity
                ),
            ).evaluate(data)
        else:
            result = D3PolicyBoundary(
                **boundary,
                evaluator_factory=lambda: ReferenceDispatchEvaluator(**engine, identity=identity),
            ).evaluate(data)
        result = result.model_copy(
            update={
                "decided_at": utc_milliseconds(datetime.now(UTC)),
                "latency_ms": (perf_counter_ns() - started) // 1_000_000,
            }
        )
        allow, expected = result.outcome == "allow", data.expected_local_state_version
        policy = result.origin == "policy"
        return DecisionRecord(
            schema_version=SCHEMA,
            record_kind="decision_record",
            evidence_id=built.evidence_id,
            sequence=built.sequence,
            recorded_at=result.decided_at,
            canonicalization_profile="commerce-canonical-json/1",
            canonical_input_bytes=canonical_bytes(data).decode(),
            decision=result,
            policy_artifact_ref=built.scope.policy_artifact_ref,
            engine_artifact_ref=built.scope.engine_artifact_ref if policy else None,
            source_refs=built.scope.source_refs,
            application=EvidenceApplication(
                schema_version=SCHEMA,
                disposition="transition_committed" if allow else "blocked",
                transition_ref=built.transition.ref if allow else None,
                expected_state_version=expected,
                resulting_state_version=expected + 1 if allow else None,
            ),
            replayability="full" if policy else "policy_not_evaluated",
        )

    # Assembly: every fact from the backend's stores or the merchant's authoritative GET.

    def _assemble(
        self,
        connection: sqlite3.Connection,
        phase: Phase,
        run_id: str,
        grant_id: str,
        checkout: CheckoutSession,
        reservation_id: str,
        operation_id: str,
    ) -> _Assembled:
        keys = self._keys
        vault = SqliteD1ProvenanceStore(connection, keys)
        ledger = SqliteGovernanceEvidenceStore(connection)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            vault.initialize()
            ledger.initialize()
            ledger.initialize_decision_scopes()
            purchase = connection.execute(
                "SELECT owner_id, selected_variant_id, checkout_id, purchase_mode, grant_id "
                "FROM purchases WHERE run_id=?",
                (run_id,),
            ).fetchone()
            grant = connection.execute(
                "SELECT owner_id, state, authority_epoch, state_version, valid_from, expires_at, "
                "agent_key_kid, scope_json, scope_digest, proposal_id, grant_version "
                "FROM delegation_grants WHERE grant_id=?",
                (grant_id,),
            ).fetchone()
            if (
                purchase is None
                or grant is None
                or purchase[1] is None
                or tuple(purchase[2:]) != (checkout.id, "HNP", grant_id)
            ):
                raise GovernanceError("GOV_FACTS_UNAVAILABLE")
            owner_id, version = str(purchase[0]), int(grant[3])
            consent = parse_consent_scope(str(grant[7]).encode())
            if not hmac.compare_digest(scope_digest(consent), str(grant[8])):
                raise GovernanceError("GOV_FACTS_UNAVAILABLE")
            # The context D1-HNP registered: same configuration, artifact and grant.
            context = vault.register_context(
                run_id,
                owner_id,
                self._configuration,
                policy_bytes=self._policy,
                policy_binding=self._binding,
                grant=HnpGrant(grant_id=grant_id, variant_ids=consent.scope.variant_ids),
            )
            ctx = context.data
            clocks = vault.observe_context(run_id, owner_id, context.ref, self._clocks).clocks
            history = ledger.for_run(run_id)
            d1 = _committed_transition(history)
            if d1 is None:
                raise GovernanceError("GOV_FACTS_UNAVAILABLE")
            d1_input = d1.policy_input()
            # Projected as D2-HP projects them (§7): same material contract and binding.
            terms = SqliteD2AuthorizationGate.material_terms(
                keys, vault, run_id, owner_id, checkout, ctx.merchant_ref
            )
            freshness = SqliteD2AuthorizationGate.offer_freshness(
                connection, vault, run_id, owner_id, checkout, terms, CLOSE_FRESHNESS_CONTRACT
            )
            grant_ref = {
                "schema_version": SCHEMA,
                "id": grant_id,
                "version": grant[10],
                "digest": grant[8],
            }
            reservations = connection.execute(_RESERVATION, (grant_id,)).fetchall()
            own = next((row for row in reservations if row[1] == operation_id), None)
            mandates = {
                row[0]: row[1:]
                for row in connection.execute(
                    "SELECT kind, mandate_id, reservation_id, token, token_digest "
                    "FROM delegation_mandates WHERE grant_id=?",
                    (grant_id,),
                )
            }
            evidence_id = "evidence:" + secrets.token_hex(16)
            payload: dict[str, Any] = {
                "schema_version": SCHEMA,
                "decision_id": "decision:" + secrets.token_hex(16),
                "run_id": run_id,
                "trace_id": ctx.trace_id,
                "owner_ref": ctx.owner_ref,
                "purchase_mode": "HNP",
                "governance_mode": "enforce",
                "phase": phase,
                "context_version": context.ref.version,
                # The grant's version, not the purchase's (§4.3).
                "expected_local_state_version": version,
                "expected_policy": ctx.expected_policy.model_dump(mode="json"),
                **clocks.model_dump(mode="json", exclude={"schema_version"}),
                "clock_profile_ref": ctx.clock_profile_ref.model_dump(mode="json"),
                "experiment_run_id": ctx.experiment_run_id,
                "conversation_id": ctx.conversation_id,
                "principal": {
                    "schema_version": SCHEMA,
                    "principal_ref": EXECUTOR_REF,
                    "authentication_basis": "service_and_grant",
                    "authentication_verified": True,
                    "owns_run": hmac.compare_digest(str(grant[0]).encode(), owner_id.encode()),
                    "permitted_actions": list(GRANT_ACTIONS),
                },
                "intent": d1_input.intent.model_dump(mode="json"),
                "terms": terms,
                "freshness": freshness,
                "operation_id": operation_id,
            }
            grant_facts = {
                "schema_version": SCHEMA,
                "grant_ref": grant_ref,
                "owner_ref": consent.owner_ref,
                "agent_key_ref": grant[6],
                "state": grant[1],
                "authority_epoch": grant[2],
                "valid_from": grant[4],
                "expires_at": grant[5],
                "permitted_actions": list(GRANT_ACTIONS),
                "scope": consent.scope.model_dump(mode="json"),
            }
            usage = {
                "schema_version": SCHEMA,
                "grant_ref": grant_ref,
                "state_version": version,
                "uses_consumed": 0,
                "uses_reserved_other": 0,
                "amount_consumed_minor": 0,
                "amount_reserved_other_minor": 0,
                "same_operation_reservation": None
                if own is None
                else self._reservation(own, grant_ref),
            }
            for row in reservations:
                kind = "consumed" if row[7] == "consumed" else "reserved_other"
                if kind == "consumed" or (row[7] in _HOLDING and row is not own):
                    usage[f"uses_{kind}"] += 1
                    usage[f"amount_{kind}_minor"] += row[5]
            now = datetime.fromisoformat(clocks.authority_now)
            if phase == "D2":
                candidate = vault.recover_feed(
                    run_id, owner_id, d1_input.feed_snapshot_ref
                ).candidate(d1_input.selected_variant_id)
                if candidate is None:
                    raise GovernanceError("GOV_FACTS_UNAVAILABLE")
                opened = [mandates[kind] for kind in ("open_checkout", "open_payment")]
                proof = verify_open_proof(
                    opened[0][2],
                    opened[1][2],
                    trust=self._trust,
                    variant_ids=consent.scope.variant_ids,
                    total_max_minor=consent.scope.total_max_minor,
                    expires_at=datetime.fromisoformat(str(grant[5])),
                    now=now,
                ).status
                payload |= {
                    "action": "complete_purchase",
                    "selected_variant_id": str(purchase[1]),
                    "constraint_matches": [
                        SqliteD2AuthorizationGate.constraint_match(
                            vault, run_id, owner_id, ref, candidate, d1_input.feed_snapshot_ref
                        )
                        for ref in d1_input.intent.hard_constraint_refs
                    ],
                    "d1": resolved_d1(d1),
                    "authorization_id": "authorization:"
                    + pseudonym(
                        keys,
                        "authorization",
                        canonical_bytes([run_id, operation_id, payload["decision_id"]]),
                    ),
                    "authorization_profile": {
                        "schema_version": SCHEMA,
                        "status": "validated",
                        "profile_ref": self._profile,
                    },
                    "hnp": {
                        "schema_version": SCHEMA,
                        "grant": grant_facts,
                        "usage": usage,
                        "reservation_intent": {
                            "schema_version": SCHEMA,
                            "reservation_id": reservation_id,
                            "operation_id": operation_id,
                            "run_id": run_id,
                            "owner_ref": ctx.owner_ref,
                            "grant_ref": grant_ref,
                            "expected_epoch": grant[2],
                            "amount_minor": checkout.total_minor,
                            "currency": "COP",
                        },
                        "requested_instrument_ref": INSTRUMENT_REF,
                        "open_proof": {
                            "schema_version": SCHEMA,
                            "status": proof,
                            "profile_ref": self._profile,
                            "open_checkout_ref": _mandate_ref(opened[0]),
                            "open_payment_ref": _mandate_ref(opened[1]),
                            "consent_ref": {
                                "schema_version": SCHEMA,
                                "id": grant[9],
                                "version": 1,
                                "digest": grant[8],
                            },
                            "verified_agent_key_ref": self._agent_key_ref,
                        },
                    },
                }
                data = parse_model(HnpAuthorizationPolicyInput, canonical_bytes(payload))
                transition = _Transition(
                    reservation_id,
                    "SELECT r.grant_id, r.run_id, r.operation_id, r.amount_minor, r.state, "
                    "r.d2_evidence_id, g.state_version FROM grant_reservations r "
                    "JOIN delegation_grants g ON g.grant_id = r.grant_id "
                    "WHERE r.reservation_id=?",
                    (grant_id, run_id, operation_id, checkout.total_minor, "reserved", evidence_id),
                )
            else:
                if own is None or (own[0], own[2]) != (reservation_id, run_id) or own[9] is None:
                    raise GovernanceError("GOV_FACTS_UNAVAILABLE")
                d2 = ledger.get(str(own[9]))
                if not isinstance(d2, DecisionRecord) or d2.application.transition_ref is None:
                    raise GovernanceError("GOV_FACTS_UNAVAILABLE")
                d2_input = d2.hnp_authorization_input()
                closed = [mandates[kind] for kind in ("closed_checkout", "closed_payment")]
                if any(mandate[1] != reservation_id for mandate in closed):
                    raise GovernanceError("GOV_FACTS_UNAVAILABLE")
                signed_jwt = closed_checkout_jwt(closed[0][2])
                proof = verify_closed_proof(
                    closed[0][2],
                    closed[1][2],
                    trust=self._trust,
                    open_checkout=mandates["open_checkout"][2],
                    open_payment=mandates["open_payment"][2],
                    checkout_jwt=signed_jwt,
                    now=now,
                ).status
                snapshot_ref, material_digest = _signed_terms(signed_jwt, checkout, terms)
                payload |= {
                    "action": "commit_dispatch",
                    "grant": grant_facts,
                    "usage": usage,
                    "reservation": self._reservation(own, grant_ref),
                    "d2": _resolved_d2(d2, d2_input),
                    "closed_proof": {
                        "schema_version": SCHEMA,
                        "status": proof,
                        "profile_ref": self._profile,
                        "checkout_chain_ref": _mandate_ref(closed[0]),
                        "payment_chain_ref": _mandate_ref(closed[1]),
                        "original_checkout_jwt_digest": sha256_digest(signed_jwt.encode()),
                        "verified_agent_key_ref": self._agent_key_ref,
                        # What UoW-C bound these mandates to: the reservation D2 authorized.
                        "authorization_id": d2_input.authorization_id,
                        "operation_id": own[1],
                        "grant_ref": grant_ref,
                        "snapshot_ref": snapshot_ref,
                        "material_digest": material_digest,
                        "requested_instrument_ref": INSTRUMENT_REF,
                    },
                }
                data = parse_model(DispatchPolicyInput, canonical_bytes(payload))
                transition = _Transition(
                    f"ob_{uuid4().hex}",
                    "SELECT o.reservation_id, o.run_id, o.state, o.d3_evidence_id, r.state, "
                    "g.state_version FROM dispatch_outbox o "
                    "JOIN grant_reservations r ON r.reservation_id = o.reservation_id "
                    "JOIN delegation_grants g ON g.grant_id = o.grant_id WHERE o.outbox_id=?",
                    (reservation_id, run_id, "committed", evidence_id, "committed"),
                )
            scope = EvidenceScope(
                schema_version=SCHEMA,
                run_id=run_id,
                trace_id=data.trace_id,
                owner_ref=data.owner_ref,
                purchase_mode="HNP",
                governance_mode="enforce",
                phase=phase,
                context_version=data.context_version,
                policy_binding=data.expected_policy,
                source_refs=tuple(
                    dict.fromkeys(
                        (
                            *required_refs(data),
                            context.ref,
                            data.terms.material_contract_ref,
                            data.freshness.contract_ref,
                        )
                    )
                ),
                policy_artifact_ref=ctx.policy_artifact_ref,
                engine_artifact_ref=self._engine_ref,
            )
            policy = vault.public_source(run_id, owner_id, ctx.policy_artifact_ref, "policy")
            ledger.register_decision_scope(
                data.decision_id, scope, policy_bytes=policy, engine_bytes=self._engine_bytes
            )
        return _Assembled(data, scope, evidence_id, len(history) + 1, policy, proof, transition)

    def _reservation(self, row: tuple[Any, ...], grant_ref: dict[str, Any]) -> dict[str, Any]:
        """`ReservationFacts` of one `grant_reservations` row."""
        return {
            "schema_version": SCHEMA,
            "reservation_id": row[0],
            "operation_id": row[1],
            "run_id": row[2],
            "owner_ref": "owner:" + pseudonym(self._keys, "owner", str(row[3]).encode()),
            "grant_ref": grant_ref,
            "expected_epoch": row[4],
            "amount_minor": row[5],
            "currency": row[6],
            "state": row[7],
            "state_version": row[8],
        }

    def _reject(self, run_id: str, phase: Phase, grant_id: str) -> None:
        """Best effort: name a decision whose facts could not be assembled (`input_rejected`)."""
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute(
                    "SELECT state_version FROM delegation_grants WHERE grant_id=?", (grant_id,)
                ).fetchone()
                record_input_rejected(
                    connection, run_id, phase, None if version is None else int(version[0])
                )
        except Exception:
            self._lost()

    def _lost(self) -> None:
        self.lost_diagnostics += 1
        logger.warning("HNP decision diagnostic unavailable: GOV_EVIDENCE_UNAVAILABLE")


def _blocked(record: DecisionRecord) -> DecisionRecord:
    """The record kept when its unit of work did not apply it: never a fake transition."""
    application = record.application
    return record.model_copy(
        update={
            "application": EvidenceApplication(
                schema_version=SCHEMA,
                disposition="blocked",
                transition_ref=None,
                expected_state_version=application.expected_state_version,
                resulting_state_version=None,
            )
        }
    )


def _mandate_ref(mandate: tuple[Any, ...]) -> dict[str, Any]:
    """An opaque reference to a stored mandate: its id and token digest, never the token."""
    return {"schema_version": SCHEMA, "id": mandate[0], "version": 1, "digest": mandate[3]}


def _signed_terms(
    signed_jwt: str, checkout: CheckoutSession, terms: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """The snapshot and material the closed mandates bind: those of the authoritative checkout
    only when the JWT carries exactly that checkout; otherwise others, and POL-10 fails."""
    try:
        signed = CheckoutSession.model_validate(
            jwt.decode(signed_jwt, options={"verify_signature": False})["checkout"]
        )
    except Exception:
        signed = None
    if signed == checkout:
        return terms["snapshot_ref"], terms["material_digest"]
    digest = sha256_digest(signed_jwt.encode())
    return {"schema_version": SCHEMA, "id": "checkout-jwt", "version": 1, "digest": digest}, digest


def _resolved_d2(d2: DecisionRecord, data: HnpAuthorizationPolicyInput) -> dict[str, Any]:
    """`ResolvedD2`: the D2 allow the reservation names (`d2_evidence_id`), from the ledger."""
    return {
        "schema_version": SCHEMA,
        "decision_id": d2.decision.decision_id,
        "evidence_id": d2.evidence_id,
        "evidence_digest": sha256_digest(canonical_bytes(d2)),
        "outcome": d2.decision.outcome,
        "run_id": data.run_id,
        "owner_ref": data.owner_ref,
        "purchase_mode": data.purchase_mode,
        "governance_mode": data.governance_mode,
        "policy_binding": d2.decision.policy_binding.model_dump(mode="json"),
        "context_version": data.context_version,
        "input_digest": d2.decision.input_digest,
        "authorization_id": data.authorization_id,
        "intent_ref": data.intent.intent_ref.model_dump(mode="json"),
        "selected_variant_id": data.selected_variant_id,
        "snapshot_ref": data.terms.snapshot_ref.model_dump(mode="json"),
        "material_digest": data.terms.material_digest,
        "grant_ref": data.hnp.grant.grant_ref.model_dump(mode="json"),
        "authority_epoch": data.hnp.grant.authority_epoch,
        "reservation_id": data.hnp.reservation_intent.reservation_id,
        "operation_id": data.operation_id,
        "requested_instrument_ref": data.hnp.requested_instrument_ref,
        "application_transition_ref": d2.application.transition_ref,
    }
