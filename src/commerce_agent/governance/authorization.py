"""D2-HP: POL-03..06 when the human approves (docs/design/hp-p3-d2-authorization.md).

The gate assembles `AuthorizationPolicyInput` from the backend's own store (vault, ledger,
`hp_evidence`, `approval_terms`) and the merchant's authoritative checkout, never from the
model or the request body: the click contributes only the fingerprint it echoes. A deny
records `blocked` evidence and stops before any mandate exists. An allow is applied in one
SQLite transaction together with the approval and the purchase transition.
"""

import hmac
import json
import logging
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Literal

from commerce_agent.acp.feed import ProductCandidate
from commerce_agent.acp.models import CheckoutSession
from commerce_agent.application.purchase import PurchaseServiceError
from commerce_agent.approval import ApprovalTerms
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.domain.selection import rejection_reason
from commerce_agent.persistence import ApprovalRecord
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.hp_evidence_sqlite import chain_is_valid, read_run
from commerce_agent.persistence.provenance_sqlite import SqliteD1ProvenanceStore
from commerce_agent.persistence.sqlite import write_approval

from .canonical import canonical_bytes, parse_model, sha256_digest
from .enforce import _committed_transition
from .evaluator import (
    AuthorizationEvaluator,
    D2PolicyBoundary,
    GovernanceError,
    ReferenceAuthorizationEvaluator,
    measure_reference_engine,
)
from .evidence import (
    ApplicationFailure,
    DecisionRecord,
    EvidenceApplication,
    EvidenceScope,
    InputRejected,
)
from .evidence_artifacts import capture_reference_sources
from .models import (
    SCHEMA,
    AuthorizationPolicyInput,
    EvaluationMetadata,
    ObjectRef,
    PolicyBinding,
)
from .provenance import (
    ClockReader,
    D1EnforceConfiguration,
    ProvenanceKeys,
    pseudonym,
    utc_milliseconds,
)

AuthorizationProfileName = Literal["hp-ap2/v1", "hp-hmac/v1"]
EvaluatorFactory = Callable[[bytes, PolicyBinding, EvaluationMetadata], AuthorizationEvaluator]

# Fixed public contracts each D2 input names, so a reader knows how its facts were derived.
MATERIAL_CONTRACT = {
    "schema_version": "commerce-hp-material-terms/1",
    "source": "authoritative ACP checkout GET",
    "fields": [
        "variant_id",
        "quantity",
        "unit_minor",
        "shipping_minor",
        "other_minor",
        "total_minor",
        "currency",
        "fulfillment_service_ref",
        "destination_binding",
        "commercial_expires_at",
    ],
    "other_minor": "total_minor - unit_minor * quantity - shipping_minor",
    "destination_binding": "hmac-sha256/v1 over fulfillment_details",
}
FRESHNESS_CONTRACT = {
    "schema_version": "commerce-hp-offer-freshness/1",
    "stage": "approve",
    "predicate": "merchant completion predicate (application/offer_freshness.compare_offer)",
    "evidence": "hp_evidence freshness_check",
}
_CONSTRAINT_FIELDS = {
    "category": "categories",
    "condition": "conditions",
    "term": "required_terms",
    "option": "required_options",
}
logger = logging.getLogger(__name__)


def _utc(value: str) -> str:
    """Normalize an ISO timestamp with an offset to the contract's millisecond UTC form."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return utc_milliseconds(parsed)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _unique(refs: tuple[ObjectRef, ...]) -> tuple[ObjectRef, ...]:
    return tuple(dict.fromkeys(refs))


class SqliteD2AuthorizationGate:
    """Backend injection only: no model-facing tool reaches it and it holds no secret in output."""

    def __init__(
        self,
        path: Path,
        configuration: D1EnforceConfiguration,
        keys: ProvenanceKeys,
        clocks: ClockReader,
        *,
        profile: AuthorizationProfileName,
        policy_bytes: bytes | None = None,
        policy_binding: PolicyBinding | None = None,
        evaluator_factory: EvaluatorFactory | None = None,
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
    ):
        if (policy_bytes is None) != (policy_binding is None):
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        self._path = path
        self._configuration = D1EnforceConfiguration.model_validate(configuration)
        self._keys = ProvenanceKeys.model_validate(keys)
        self._clocks = clocks
        self._profile: AuthorizationProfileName = profile
        self._policy, self._binding = policy_bytes, policy_binding
        self._factory = evaluator_factory
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
        """Startup check: a ledger created before D2 only admits D1 and is not migrated."""
        with closing(self._connect()) as connection:
            if not SqliteGovernanceEvidenceStore(connection).admits_phase_d2():
                raise GovernanceError("GOV_EVIDENCE_UNAVAILABLE")

    def refusal(self, run_id: str) -> Literal["denied", "unverified"] | None:
        """Why the run's last D2 attempt left it without an approval, if it did.

        `denied` is a policy deny; `unverified` is a runtime guard, facts that could not be
        assembled or an allow that could not be applied. Read-only, for restoring the chat: the
        purchase stays awaiting confirmation and approving again would be refused the same way.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT record_kind, outcome, json_extract(body, '$.decision.origin'), "
                "json_extract(body, '$.application.disposition') FROM governance_evidence "
                "WHERE run_id=? AND phase='D2' ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        kind, outcome, origin, disposition = row
        if kind == "decision_record" and disposition == "transition_committed":
            return None
        policy_deny = kind == "decision_record" and (outcome, origin) == ("deny", "policy")
        return "denied" if policy_deny else "unverified"

    @contextmanager
    def attempt(
        self,
        *,
        run_id: str,
        actor_id: str,
        expected_fingerprint: str,
        checkout: CheckoutSession,
    ) -> Iterator["D2Attempt"]:
        attempt = D2Attempt(self, run_id, actor_id, expected_fingerprint, checkout)
        try:
            yield attempt
        finally:
            attempt.close()

    # Assembly: every fact from the backend's store or the merchant's authoritative GET.

    def _assemble(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        actor_id: str,
        expected_fingerprint: str,
        checkout: CheckoutSession,
        current: ApprovalTerms,
    ) -> tuple[AuthorizationPolicyInput, EvidenceScope, int, bytes]:
        vault = SqliteD1ProvenanceStore(connection, self._keys)
        ledger = SqliteGovernanceEvidenceStore(connection)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            vault.initialize()
            ledger.initialize()
            ledger.initialize_decision_scopes()
            purchase = connection.execute(
                "SELECT owner_id, selected_variant_id, checkout_id FROM purchases WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if purchase is None or purchase[2] != checkout.id or purchase[1] is None:
                raise GovernanceError("GOV_FACTS_UNAVAILABLE")
            owner_id, selected_variant_id = str(purchase[0]), str(purchase[1])
            # Same configuration, same context: a changed policy shows up as a new version.
            context = vault.register_context(
                run_id,
                owner_id,
                self._configuration,
                policy_bytes=self._policy,
                policy_binding=self._binding,
            )
            ctx = context.data
            observed = vault.observe_context(run_id, owner_id, context.ref, self._clocks)
            clocks = observed.clocks
            history = ledger.for_run(run_id)
            d1 = _committed_transition(history)
            if d1 is None or d1.application.transition_ref is None:
                raise GovernanceError("GOV_FACTS_UNAVAILABLE")
            d1_input = d1.policy_input()
            feed = vault.recover_feed(run_id, owner_id, d1_input.feed_snapshot_ref)
            candidate = feed.candidate(d1_input.selected_variant_id)
            if candidate is None:
                raise GovernanceError("GOV_FACTS_UNAVAILABLE")
            matches = [
                self._constraint_match(
                    vault, run_id, owner_id, ref, candidate, d1_input.feed_snapshot_ref
                )
                for ref in d1_input.intent.hard_constraint_refs
            ]
            terms = self._material_terms(vault, run_id, owner_id, checkout, ctx.merchant_ref)
            freshness = self._freshness(connection, vault, run_id, owner_id, checkout, terms)
            hp = self._hp_facts(
                connection,
                vault,
                run_id=run_id,
                owner_id=owner_id,
                owner_ref=ctx.owner_ref,
                expected_fingerprint=expected_fingerprint,
                current=current,
                clicked_at=clocks.authority_now,
                valid_until=terms["commercial_expires_at"],
            )
            profile_ref = vault.register_authorization_source(
                run_id,
                owner_id,
                "authorization_profile",
                canonical_bytes(
                    {
                        "schema_version": "commerce-hp-authorization-profile/1",
                        "profile": self._profile,
                        "approval": "checkout fingerprint hmac-sha256/v1",
                        "mandates": ["checkout_mandate", "payment_mandate"]
                        if self._profile == "hp-ap2/v1"
                        else [],
                    }
                ),
            )
            decision_id = "decision:" + secrets.token_hex(16)
            payload = {
                "schema_version": SCHEMA,
                "decision_id": decision_id,
                "run_id": run_id,
                "trace_id": ctx.trace_id,
                "owner_ref": ctx.owner_ref,
                "purchase_mode": "HP",
                "governance_mode": "enforce",
                "phase": "D2",
                "action": "complete_purchase",
                "context_version": context.ref.version,
                "expected_local_state_version": observed.expected_local_state_version,
                "expected_policy": ctx.expected_policy.model_dump(mode="json"),
                **clocks.model_dump(mode="json", exclude={"schema_version"}),
                "clock_profile_ref": ctx.clock_profile_ref.model_dump(mode="json"),
                "experiment_run_id": ctx.experiment_run_id,
                "conversation_id": ctx.conversation_id,
                "principal": {
                    "schema_version": SCHEMA,
                    "principal_ref": "owner:" + pseudonym(self._keys, "owner", actor_id.encode()),
                    # The API verified the buyer before the approval reached this gate.
                    "authentication_basis": "user_jwt",
                    "authentication_verified": True,
                    "owns_run": hmac.compare_digest(owner_id.encode(), actor_id.encode()),
                    "permitted_actions": ["complete_purchase"],
                },
                "intent": d1_input.intent.model_dump(mode="json"),
                "selected_variant_id": selected_variant_id,
                "terms": terms,
                "freshness": freshness,
                "constraint_matches": matches,
                "d1": {
                    "schema_version": SCHEMA,
                    "decision_id": d1.decision.decision_id,
                    "evidence_id": d1.evidence_id,
                    "evidence_digest": sha256_digest(canonical_bytes(d1)),
                    "outcome": d1.decision.outcome,
                    "run_id": d1_input.run_id,
                    "owner_ref": d1_input.owner_ref,
                    "purchase_mode": d1_input.purchase_mode,
                    "governance_mode": d1_input.governance_mode,
                    "intent_ref": d1_input.intent.intent_ref.model_dump(mode="json"),
                    "feed_snapshot_ref": d1_input.feed_snapshot_ref.model_dump(mode="json"),
                    "selected_variant_id": d1_input.selected_variant_id,
                    "merchant_ref": d1_input.merchant_ref,
                    "policy_binding": d1.decision.policy_binding.model_dump(mode="json"),
                    "context_version": d1_input.context_version,
                    "input_digest": d1.decision.input_digest,
                },
                "authorization_id": "authorization:"
                + pseudonym(
                    self._keys,
                    "authorization",
                    canonical_bytes([run_id, hp["approval_attempt_id"], decision_id]),
                ),
                "operation_id": d1.application.transition_ref,
                "authorization_profile": {
                    "schema_version": SCHEMA,
                    "status": "validated",
                    "profile_ref": profile_ref.model_dump(mode="json"),
                },
                "hp": hp,
            }
            data = parse_model(AuthorizationPolicyInput, canonical_bytes(payload))
            refs = _unique(
                (
                    data.intent.intent_ref,
                    data.d1.feed_snapshot_ref,
                    data.clock_profile_ref,
                    context.ref,
                    data.terms.snapshot_ref,
                    data.terms.material_contract_ref,
                    data.freshness.contract_ref,
                    data.freshness.source_offer_ref,
                    data.hp.presented_snapshot_ref,
                    data.hp.consent_binding_ref,
                    profile_ref,
                )
            )
            scope = EvidenceScope(
                schema_version=SCHEMA,
                run_id=run_id,
                trace_id=data.trace_id,
                owner_ref=data.owner_ref,
                purchase_mode="HP",
                governance_mode="enforce",
                phase="D2",
                context_version=data.context_version,
                policy_binding=data.expected_policy,
                source_refs=refs,
                policy_artifact_ref=ctx.policy_artifact_ref,
                engine_artifact_ref=self._engine_ref,
            )
            policy = vault.public_source(run_id, owner_id, ctx.policy_artifact_ref, "policy")
            ledger.register_decision_scope(
                decision_id, scope, policy_bytes=policy, engine_bytes=self._engine_bytes
            )
        return data, scope, len(history) + 1, policy

    @staticmethod
    def _constraint_match(
        vault: SqliteD1ProvenanceStore,
        run_id: str,
        owner_id: str,
        ref: str,
        candidate: ProductCandidate,
        feed_ref: ObjectRef,
    ) -> dict[str, Any]:
        """One hard constraint of the intent against the variant in D1's frozen feed.

        The ACP checkout carries no category, condition or options, so the variant D1
        reviewed is the source; availability is the freshness check's job, not this one.
        """
        constraint = vault.recover_constraint(run_id, owner_id, ref)
        kind = constraint["kind"]
        hard = HardConstraints.model_validate_json(
            json.dumps({_CONSTRAINT_FIELDS[kind]: [constraint["value"]]})
        )
        reason = rejection_reason(
            candidate.model_copy(update={"available": True}), PurchaseIntent(hard=hard)
        )
        return {
            "schema_version": SCHEMA,
            "constraint_ref": ref,
            "kind": kind,
            "matches": reason is None,
            "source_snapshot_ref": feed_ref.model_dump(mode="json"),
        }

    def _material_terms(
        self,
        vault: SqliteD1ProvenanceStore,
        run_id: str,
        owner_id: str,
        checkout: CheckoutSession,
        merchant_ref: str,
    ) -> dict[str, Any]:
        """Typed projection of the authoritative checkout; buyer data stays in the vault."""
        snapshot = checkout.model_dump(mode="json")
        [line] = checkout.line_items
        item = line["item"]
        shipping = [row["amount"] for row in checkout.totals if row.get("type") == "fulfillment"]
        if len(shipping) > 1:
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        unit, quantity = item["unit_amount"], line["quantity"]
        shipping_minor = shipping[0] if shipping else 0
        if any(type(value) is not int for value in (unit, quantity, shipping_minor)):
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        values = {
            "merchant_ref": merchant_ref,
            "checkout_id": checkout.id,
            "variant_id": item["id"],
            "checkout_status": checkout.status,
            "quantity": quantity,
            "unit_minor": unit,
            "shipping_minor": shipping_minor,
            # A negative remainder is not representable and stops the approval.
            "other_minor": checkout.total_minor - unit * quantity - shipping_minor,
            "total_minor": checkout.total_minor,
            "currency": checkout.currency.upper(),
            "fulfillment_service_ref": "fulfillment:"
            + pseudonym(
                self._keys,
                "fulfillment-service",
                canonical_bytes(snapshot.get("selected_fulfillment_options")),
            ),
            "destination_binding": {
                "schema_version": SCHEMA,
                "binding_id": "destination:"
                + pseudonym(self._keys, "destination-id", canonical_bytes([run_id, checkout.id])),
                "scheme": "hmac-sha256/v1",
                "key_id": self._keys.key_id,
                "value": pseudonym(
                    self._keys, "destination", canonical_bytes(snapshot.get("fulfillment_details"))
                ),
            },
            "commercial_expires_at": _utc(checkout.expires_at),
        }
        snapshot_ref = vault.register_authorization_source(
            run_id, owner_id, "checkout_terms", canonical_bytes(snapshot)
        )
        contract_ref = vault.register_authorization_source(
            run_id, owner_id, "material_contract", canonical_bytes(MATERIAL_CONTRACT)
        )
        return {
            "schema_version": SCHEMA,
            "snapshot_ref": snapshot_ref.model_dump(mode="json"),
            **values,
            "material_digest": sha256_digest(canonical_bytes(values)),
            "material_contract_ref": contract_ref.model_dump(mode="json"),
        }

    @staticmethod
    def _freshness(
        connection: sqlite3.Connection,
        vault: SqliteD1ProvenanceStore,
        run_id: str,
        owner_id: str,
        checkout: CheckoutSession,
        terms: dict[str, Any],
    ) -> dict[str, Any]:
        """The last `approve` freshness check of this checkout, from the verified HP chain."""
        records = read_run(connection, run_id)
        if not chain_is_valid(run_id, records):
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        checks = [
            record
            for record in records
            if record.record_kind == "freshness_check"
            and record.body.get("stage") == "approve"
            and record.body.get("checkout_id") == checkout.id
        ]
        if not checks:
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        check = checks[-1]
        body = check.body
        outcome = body.get("outcome")
        status = (
            "current"
            if outcome == "fresh" and body.get("decision") == "allow"
            else "stale"
            if outcome == "changed"
            else "unresolved"
        )
        offer_expiry = (body.get("compared") or {}).get("expires_at", {}).get("offer")
        contract_ref = vault.register_authorization_source(
            run_id, owner_id, "freshness_contract", canonical_bytes(FRESHNESS_CONTRACT)
        )
        return {
            "schema_version": SCHEMA,
            "status": status,
            "observed_at": _utc(str(body["checked_at"])),
            "valid_until": _utc(offer_expiry)
            if isinstance(offer_expiry, str)
            else terms["commercial_expires_at"],
            "contract_ref": contract_ref.model_dump(mode="json"),
            # The `hp_evidence` link itself: its chain digest, which the verifier recomputes.
            "source_offer_ref": {
                "schema_version": SCHEMA,
                "id": "hp-evidence:freshness_check",
                "version": check.sequence,
                "digest": check.digest,
            },
        }

    def _hp_facts(
        self,
        connection: sqlite3.Connection,
        vault: SqliteD1ProvenanceStore,
        *,
        run_id: str,
        owner_id: str,
        owner_ref: str,
        expected_fingerprint: str,
        current: ApprovalTerms,
        clicked_at: str,
        valid_until: str,
    ) -> dict[str, Any]:
        """The terms the server presented last, and whether the click names them."""
        row = connection.execute(
            "SELECT terms, updated_at FROM approval_terms WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        presented = json.loads(row[0])
        presented_fingerprint = presented.get("fingerprint")
        if not isinstance(presented_fingerprint, str):
            raise GovernanceError("GOV_FACTS_UNAVAILABLE")
        presented_ref = vault.register_authorization_source(
            run_id, owner_id, "presented_terms", canonical_bytes(presented)
        )
        attempt_id = "attempt:" + pseudonym(
            self._keys, "approval-attempt", canonical_bytes([run_id, presented_fingerprint])
        )
        consent_ref = vault.register_authorization_source(
            run_id,
            owner_id,
            "consent_binding",
            canonical_bytes(
                {
                    "schema_version": "commerce-hp-consent/1",
                    "approval_attempt_id": attempt_id,
                    "checkout_id": current.checkout_id,
                    "fingerprint_version": current.fingerprint_version,
                    "clicked_fingerprint": expected_fingerprint,
                    "presented_fingerprint": presented_fingerprint,
                }
            ),
        )
        return {
            "schema_version": SCHEMA,
            "approval_attempt_id": attempt_id,
            "attempt_version": presented_ref.version,
            # Only the run's owner is shown its terms (`owned_purchase`).
            "attempt_owner_ref": owner_ref,
            "presented_snapshot_ref": presented_ref.model_dump(mode="json"),
            "presented_at": _utc(str(row[1])),
            "attempt_valid_until": valid_until,
            "click_received": True,
            # The approval request is the click: the server's time when it arrived.
            "clicked_at": clicked_at,
            "material_match": hmac.compare_digest(expected_fingerprint, presented_fingerprint)
            and hmac.compare_digest(presented_fingerprint, current.fingerprint)
            and presented.get("checkout_id") == current.checkout_id,
            "consent_binding_ref": consent_ref.model_dump(mode="json"),
        }

    def _reject(self, run_id: str) -> None:
        """Best effort: name an approval whose facts could not be assembled (`input_rejected`)."""
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                ledger = SqliteGovernanceEvidenceStore(connection)
                ledger.initialize_decision_scopes()
                row = connection.execute(
                    "SELECT scope FROM governance_episodes WHERE run_id=?", (run_id,)
                ).fetchone()
                version = connection.execute(
                    "SELECT state_version FROM purchases WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is None or version is None:
                    raise GovernanceError("GOV_FACTS_UNAVAILABLE")
                base = parse_model(EvidenceScope, row[0].encode())
                decision_id = "decision:" + secrets.token_hex(16)
                ledger.register_decision_scope(
                    decision_id,
                    base.model_copy(
                        update={
                            "phase": "D2",
                            "source_refs": (),
                            "policy_artifact_ref": None,
                            "engine_artifact_ref": None,
                        }
                    ),
                    policy_bytes=None,
                    engine_bytes=None,
                )
                ledger.record_decision(
                    InputRejected(
                        schema_version=SCHEMA,
                        record_kind="input_rejected",
                        evidence_id="evidence:" + secrets.token_hex(16),
                        sequence=len(ledger.for_run(run_id)) + 1,
                        recorded_at=utc_milliseconds(datetime.now(UTC)),
                        run_id=run_id,
                        trace_id=base.trace_id,
                        decision_id=decision_id,
                        reason_code="GOV_FACTS_UNAVAILABLE",
                        safe_field_paths=(),
                        replayability="not_evaluable",
                    ),
                    expected_state_version=int(version[0]),
                )
        except Exception:
            self._lost()

    def _lost(self) -> None:
        self.lost_diagnostics += 1
        logger.warning("D2 diagnostic unavailable: GOV_EVIDENCE_UNAVAILABLE")


class _ApprovalTransition:
    """Verifies, on the ledger's own connection, the approval the D2 allow claims."""

    def __init__(self, evidence_id: str):
        self._evidence_id = evidence_id

    def verify(
        self, connection: sqlite3.Connection, run_id: str, application: EvidenceApplication
    ) -> bool:
        purchase = connection.execute(
            "SELECT stage, state_version FROM purchases WHERE run_id=?", (run_id,)
        ).fetchone()
        approval = connection.execute(
            "SELECT authorization_evidence_id, consumed_at FROM approvals WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return (
            purchase is not None
            and tuple(purchase) == ("approved", application.resulting_state_version)
            and approval is not None
            and tuple(approval) == (self._evidence_id, None)
        )


class D2Attempt:
    """One approval request. `authorize` runs before signing; `commit` replaces the save."""

    def __init__(
        self,
        gate: SqliteD2AuthorizationGate,
        run_id: str,
        actor_id: str,
        expected_fingerprint: str,
        checkout: CheckoutSession,
    ):
        self._gate = gate
        self._run_id = run_id
        self._actor_id = actor_id
        self._fingerprint = expected_fingerprint
        self._checkout = checkout
        self._connection: sqlite3.Connection | None = None
        self._ledger: SqliteGovernanceEvidenceStore | None = None
        self._record: DecisionRecord | None = None

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def authorize(self, current: ApprovalTerms) -> str:
        """Evaluate POL-03..06; the evidence ID of the allow, or a refusal with no signature."""
        gate = self._gate
        try:
            self._connection = connection = gate._connect()
            ledger = self._ledger = SqliteGovernanceEvidenceStore(connection)
            data, scope, sequence, policy = gate._assemble(
                connection,
                run_id=self._run_id,
                actor_id=self._actor_id,
                expected_fingerprint=self._fingerprint,
                checkout=self._checkout,
                current=current,
            )
        except Exception:
            gate._reject(self._run_id)
            raise PurchaseServiceError(
                "D2 authorization unavailable", code="d2_unavailable"
            ) from None
        metadata = EvaluationMetadata(
            schema_version=SCHEMA,
            decided_at=utc_milliseconds(datetime.now(UTC)),
            latency_ms=0,
        )

        def factory() -> AuthorizationEvaluator:
            if gate._factory is not None:
                return gate._factory(policy, data.expected_policy, metadata)
            return ReferenceAuthorizationEvaluator(
                artifact_bytes=policy,
                binding=data.expected_policy,
                metadata=metadata,
                identity=gate._identity,
            )

        started = perf_counter_ns()
        result = D2PolicyBoundary(
            binding=data.expected_policy,
            artifact_bytes=policy,
            engine_identity=gate._identity,
            metadata=metadata,
            evaluator_factory=factory,
        ).evaluate(data)
        result = result.model_copy(
            update={
                "decided_at": utc_milliseconds(datetime.now(UTC)),
                "latency_ms": (perf_counter_ns() - started) // 1_000_000,
            }
        )
        allow = result.outcome == "allow"
        expected = data.expected_local_state_version
        record = DecisionRecord(
            schema_version=SCHEMA,
            record_kind="decision_record",
            evidence_id="evidence:" + secrets.token_hex(16),
            sequence=sequence,
            recorded_at=result.decided_at,
            canonicalization_profile="commerce-canonical-json/1",
            canonical_input_bytes=canonical_bytes(data).decode(),
            decision=result,
            policy_artifact_ref=scope.policy_artifact_ref,
            engine_artifact_ref=scope.engine_artifact_ref if result.origin == "policy" else None,
            source_refs=scope.source_refs,
            application=EvidenceApplication(
                schema_version=SCHEMA,
                disposition="transition_committed" if allow else "blocked",
                transition_ref=data.authorization_id if allow else None,
                expected_state_version=expected,
                resulting_state_version=expected + 1 if allow else None,
            ),
            replayability="full" if result.origin == "policy" else "policy_not_evaluated",
        )
        try:
            if result.origin == "policy":
                ledger.prepare_decision(record)
        except Exception:
            gate._lost()
            raise PurchaseServiceError(
                "D2 authorization unavailable", code="d2_unavailable"
            ) from None
        if allow:
            self._record = record
            return record.evidence_id
        try:
            connection.execute("BEGIN IMMEDIATE")
            ledger.record_decision(record, expected_state_version=expected)
            connection.commit()
        except Exception:
            connection.rollback()
            gate._lost()
        if result.origin == "policy":
            raise PurchaseServiceError("D2 denied the approval", code="d2_denied")
        raise PurchaseServiceError("D2 authorization unavailable", code="d2_unavailable")

    def commit(self, approval: ApprovalRecord) -> None:
        """Purchase transition, approval and D2 evidence in one transaction, or none of them."""
        record, connection, ledger = self._record, self._connection, self._ledger
        if record is None or connection is None or ledger is None:
            raise PurchaseServiceError("D2 authorization unavailable", code="d2_unavailable")
        expected = record.application.expected_state_version
        try:
            if (
                approval.run_id != self._run_id
                or approval.authorization_evidence_id != record.evidence_id
            ):
                raise GovernanceError("GOV_STATE_CONFLICT")
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE purchases SET stage='approved', updated_at=?, "
                "state_version=state_version+1 "
                "WHERE run_id=? AND state_version=? AND stage='awaiting_confirmation'",
                (_now(), self._run_id, expected),
            )
            if cursor.rowcount != 1:
                raise GovernanceError("GOV_STATE_CONFLICT")
            write_approval(connection, approval)
            ledger.record_decision(
                record,
                expected_state_version=expected,
                transition_verifier=_ApprovalTransition(record.evidence_id),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            self._diagnose(record)
            # The mandates signed for this approval are discarded with it.
            raise PurchaseServiceError(
                "D2 allow could not be applied", code="d2_allow_unapplied"
            ) from None

    def _diagnose(self, record: DecisionRecord) -> None:
        """Keep the allow as `blocked` plus the application failure; never a fake transition."""
        connection, ledger = self._connection, self._ledger
        try:
            assert connection is not None and ledger is not None
            data = record.authorization_input()
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
            ledger.prepare_decision(blocked)
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
            connection.execute("BEGIN IMMEDIATE")
            try:
                ledger.record_decision(
                    blocked, expected_state_version=data.expected_local_state_version
                )
                ledger.record_decision(
                    failure, expected_state_version=data.expected_local_state_version
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        except Exception:
            self._gate._lost()
