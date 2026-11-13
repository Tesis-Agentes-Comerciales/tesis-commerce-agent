"""D2-HNP and D3-HNP through the real gate (docs/design/hnp-governed.md §4.3, §4.5, §12 HNP-06).

Vault, governance ledger, `hp_evidence` and delegation ledger share one SQLite file, as in the
runtime. D1-HNP runs as in test_d1_hnp_gate; the `close` freshness check is the `hp_evidence` link
the executor's guard writes; the open and closed mandates are the product's (issuer and Agent
Key) with keys generated for the test. The ledger units of work are driven as the executor
drives them (§4.5): the gate's hooks go into `reserve`, `commit_dispatch` or `finish_task`.
"""

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

from commerce_agent.acp.models import CheckoutSession
from commerce_agent.ap2 import checkout_hash
from commerce_agent.ap2_hnp import (
    AcceptableItem,
    AgentKeyCloser,
    MandatePair,
    MandateProof,
    MandateTrust,
    OpenMandateIssuer,
)
from commerce_agent.delegation.models import (
    ClosedMandates,
    ConsentRequest,
    DispatchCommit,
    KillSwitchStage,
    OutcomeCode,
    ProposalDraft,
    ReservationRequest,
    SignedMandate,
    Step,
    TaskFinish,
)
from commerce_agent.delegation.ports import TransactionHook
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.governance import hnp_gate
from commerce_agent.governance.bootstrap import build_hnp_policy_gate, configured_d1
from commerce_agent.governance.canonical import canonical_bytes
from commerce_agent.governance.destination import destination_binding
from commerce_agent.governance.evaluator import GovernanceError
from commerce_agent.governance.evidence import (
    ApplicationFailure,
    DecisionEvidence,
    DecisionRecord,
    InputRejected,
)
from commerce_agent.governance.hnp_gate import SqliteHnpPolicyGate
from commerce_agent.governance.hnp_models import parse_consent_scope, scope_digest
from commerce_agent.governance.hnp_policy import HnpGrant
from commerce_agent.governance.hnp_ports import HnpGateDecision, HnpPolicyGate
from commerce_agent.governance.models import SCHEMA
from commerce_agent.governance.provenance import merchant_pseudonym, pseudonym
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger, utc_text
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.persistence.hp_evidence_sqlite import SqliteHpEvidenceStore
from commerce_evaluation.stack.fake_checkout import details
from tests.governance.test_d1_hnp_gate import hnp_service, settings_for
from tests.governance.test_hp_continuation import StoreMerchant
from tests.governance.test_runtime_dispatch import runtime
from tests.governance.test_shadow_integration import OWNER
from tests.support.ap2_hnp_fixtures import checkout_jwt, generate_keys
from tests.support.delegation import AuthorityClock, Ids

RUN, GRANT, RESERVATION, OPERATION = "run_1", "dg_1", "rsv_1", "op_1"
ITEMS = (AcceptableItem("SON-01", "Audifonos Sonora SON-01"),)
TOTAL_MAX = 80_000


@dataclass
class Delegated:
    """One HNP purchase at `checkout_prepared`, its grant claimed by the executor."""

    path: Path
    ledger: SqliteDelegationLedger
    gate: SqliteHnpPolicyGate
    checkout: CheckoutSession
    opens: MandatePair
    closer: AgentKeyCloser
    merchant_pem: str
    merchant_issuer: str
    task_id: str
    lease_id: str

    def decide(self, phase: str) -> HnpGateDecision:
        decide = self.gate.decide_d2 if phase == "D2" else self.gate.decide_d3
        return decide(
            run_id=RUN,
            grant_id=GRANT,
            checkout=self.checkout,
            reservation_id=RESERVATION,
            operation_id=OPERATION,
        )

    def reserve(self, d2: HnpGateDecision, *, evidence_id: str | None = None) -> Step:
        execution = self.ledger.execution(self.task_id)
        assert execution is not None
        request = ReservationRequest(
            task_id=self.task_id,
            lease_id=self.lease_id,
            expected=execution.authority,
            reservation_id=RESERVATION,
            operation_id=OPERATION,
            checkout_id=self.checkout.id,
            amount_minor=self.checkout.total_minor,
            d2_evidence_id=evidence_id or d2.evidence_id,
        )
        return self.ledger.reserve(request, hooks=d2.hooks)

    def close(
        self, *, checkout: dict[str, Any] | None = None, closer: AgentKeyCloser | None = None
    ) -> Step:
        token = checkout_jwt(
            self.merchant_pem, checkout or self.checkout.model_dump(mode="json"), jti="jti_1"
        )
        pair = (closer or self.closer).close(
            self.opens.checkout.token,
            self.opens.payment.token,
            checkout_jwt=token,
            checkout=self.checkout,
            closed_at=datetime.now(UTC),
        )
        return self.ledger.record_closed_mandates(
            ClosedMandates(
                task_id=self.task_id,
                lease_id=self.lease_id,
                reservation_id=RESERVATION,
                checkout_hash=checkout_hash(token),
                closed_checkout=_ledger_mandate(pair.checkout),
                closed_payment=_ledger_mandate(pair.payment),
            )
        )

    def commit(self, d3: HnpGateDecision) -> Step:
        execution = self.ledger.execution(self.task_id)
        assert execution is not None and d3.transition_ref is not None
        return self.ledger.commit_dispatch(
            DispatchCommit(
                task_id=self.task_id,
                lease_id=self.lease_id,
                expected=execution.authority,
                reservation_id=RESERVATION,
                reservation_version=1,
                outbox_id=d3.transition_ref,
                checkout_expires_at=self.checkout.expires_at,
                payment_request_digest="sha256:" + "0" * 64,
                d3_evidence_id=d3.evidence_id,
            ),
            hooks=d3.hooks,
        )

    def finish(
        self,
        state: Literal["escalated", "failed", "canceled"],
        outcome: OutcomeCode,
        hooks: Sequence[TransactionHook],
        stage: KillSwitchStage | None = None,
    ) -> Step:
        request = TaskFinish(self.task_id, self.lease_id, state, outcome, stage)
        return self.ledger.finish_task(request, hooks=hooks)

    def evidence(self) -> tuple[DecisionEvidence, ...]:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            return SqliteGovernanceEvidenceStore(connection).for_run(RUN)

    def count(self, sql: str) -> Any:
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(sql).fetchone()


def _ledger_mandate(mandate: Any) -> SignedMandate:
    return SignedMandate(mandate.token, mandate.link_hash, json.dumps(mandate.public_jwk))


def delegated(
    tmp_path: Path,
    *,
    destination: dict[str, Any] | None = None,
    freshness: str | None = "fresh",
    foreign_issuer: bool = False,
) -> Delegated:
    settings = settings_for(tmp_path)
    path = settings.agent_database_path
    built = runtime(settings, StoreMerchant())
    try:
        prepared = hnp_service(settings, built).start(
            run_id=RUN,
            request="delegated purchase",
            intent=PurchaseIntent(
                hard=HardConstraints(currency="COP", max_total_amount_minor=80_000)
            ),
            selected_variant_id="SON-01",
            details=details(),
            owner_id=OWNER,
            grant=HnpGrant(grant_id=GRANT, variant_ids=("SON-01",)),
        )
    finally:
        built.close()
    keys = configured_d1(settings, None).keys
    now = datetime.now(UTC).replace(microsecond=0)
    clock = AuthorityClock(now - timedelta(seconds=5))
    ledger = SqliteDelegationLedger(path, clock=clock, id_factory=Ids())
    ledger.initialize()
    pems = generate_keys()
    closer = AgentKeyCloser(pems.agent_pem, merchant_issuer=settings.merchant_base_url)
    issuers = [
        OpenMandateIssuer(
            pem, agent_public_jwk=closer.public_jwk, merchant_issuer=settings.merchant_base_url
        )
        for pem in (pems.provider_pem, generate_keys().provider_pem)
    ]
    expires = now + timedelta(hours=23)
    issuer = issuers[1] if foreign_issuer else issuers[0]
    opens = issuer.issue(
        ITEMS, total_max_minor=TOTAL_MAX, consented_at=clock.now, expires_at=expires
    )
    snapshot = prepared.checkout.model_dump(mode="json")
    consent = parse_consent_scope(
        canonical_bytes(
            {
                "schema_version": "commerce-delegation/1.0",
                "proposal_id": "dpr_1",
                "owner_ref": "owner:" + pseudonym(keys, "owner", OWNER.encode()),
                "scope": {
                    "schema_version": SCHEMA,
                    "merchant_refs": [merchant_pseudonym(keys, settings.merchant_base_url)],
                    "variant_ids": ["SON-01"],
                    "currency": "COP",
                    "quantity_max": 1,
                    "total_max_minor": TOTAL_MAX,
                    "destination_binding": destination_binding(
                        keys, "destination:dpr_1", destination or snapshot["fulfillment_details"]
                    ),
                    "instrument_ref": "tesis_sandbox",
                    "uses_max": 1,
                },
                "display": {
                    "merchant_name": "Tesis Commerce Merchant",
                    "variants": [{"variant_id": "SON-01", "title": ITEMS[0].title}],
                    "total_max": {"minor": TOTAL_MAX, "currency": "COP"},
                    "expires_at": utc_text(expires),
                    "destination_label": "Bogota, DC",
                    "instrument_label": "Tarjeta de prueba",
                    "uses": 1,
                    "preference_text": None,
                },
                "presented_at": utc_text(clock.now),
                "expires_at": utc_text(expires),
                "preference_text": None,
            }
        )
    )
    digest = scope_digest(consent)
    ledger.create_proposal(
        ProposalDraft(
            proposal_id="dpr_1",
            owner_id=OWNER,
            scope_json=canonical_bytes(consent).decode(),
            scope_digest=digest,
            checkout_details_json=details().model_dump_json(),
            presented_at=utc_text(clock.now),
            expires_at=utc_text(clock.now + timedelta(minutes=10)),
        )
    )
    consented = ledger.consent(
        ConsentRequest(
            owner_id=OWNER,
            proposal_id="dpr_1",
            scope_digest=digest,
            intent_json='{"hard":{"currency":"COP","max_total_amount_minor":80000}}',
            agent_key_kid=closer.kid,
            issuer_kid=issuer.kid,
            open_checkout=_ledger_mandate(opens.checkout),
            open_payment=_ledger_mandate(opens.payment),
        )
    )
    assert consented.task is not None and consented.grant is not None
    assert consented.grant.grant_id == GRANT
    claimed = ledger.claim(consented.task.task_id)
    assert claimed.execution is not None and claimed.execution.task.run_id == RUN
    lease = claimed.execution.task.lease_id
    assert lease is not None
    if freshness is not None:
        evidence = SqliteHpEvidenceStore(path)
        evidence.initialize()
        evidence.append(
            RUN,
            "freshness_check",
            {
                "run_id": RUN,
                "checkout_id": prepared.checkout.id,
                "stage": "close",
                "observed_revision": 1,
                "compared": {},
                "outcome": freshness,
                "changed_fields": [],
                "reason": None,
                "governance_mode": "enforce",
                "decision": "allow" if freshness == "fresh" else "block",
                "checked_at": utc_text(datetime.now(UTC)),
            },
        )
    trust = MandateTrust(issuers[0].public_jwk, closer.public_jwk, settings.merchant_base_url)
    return Delegated(
        path=path,
        ledger=ledger,
        gate=build_hnp_policy_gate(settings, None, trust=trust),
        checkout=prepared.checkout,
        opens=opens,
        closer=closer,
        merchant_pem=pems.merchant_pem,
        merchant_issuer=settings.merchant_base_url,
        task_id=consented.task.task_id,
        lease_id=lease,
    )


def rules(decision: Any) -> list[tuple[str, str]]:
    return [(rule.policy_id, rule.result) for rule in decision.rule_results]


# P: an allow is evidence of the unit of work that applies it, on the grant's versions.


def test_d2_and_d3_allows_are_recorded_in_uow1_and_uow2_on_the_grant_versions(
    tmp_path: Path,
) -> None:
    flow = delegated(tmp_path)
    gate: HnpPolicyGate = flow.gate

    d2 = gate.decide_d2(
        run_id=RUN,
        grant_id=GRANT,
        checkout=flow.checkout,
        reservation_id=RESERVATION,
        operation_id=OPERATION,
    )
    assert d2.decision is not None and d2.decision.outcome == "allow"
    assert rules(d2.decision) == [(f"POL-0{n}", "pass") for n in range(3, 10)]
    assert (d2.proof, d2.transition_ref) == ("valid", None)
    # Nothing is applied before the unit of work: the hook writes the evidence.
    assert [item.decision.phase for item in flow.evidence()] == ["D1"]  # type: ignore[union-attr]
    assert flow.reserve(d2).status == "applied"
    assert flow.close().status == "applied"
    d3 = flow.decide("D3")
    assert d3.decision is not None and d3.decision.outcome == "allow"
    assert rules(d3.decision) == [("POL-09", "pass"), ("POL-10", "pass")]
    assert d3.proof == "valid" and d3.transition_ref is not None
    assert flow.commit(d3).status == "applied"

    d1, d2_record, d3_record = flow.evidence()
    assert isinstance(d1, DecisionRecord)
    assert isinstance(d2_record, DecisionRecord) and isinstance(d3_record, DecisionRecord)
    assert [record.evidence_id for record in (d2_record, d3_record)] == [
        d2.evidence_id,
        d3.evidence_id,
    ]
    # D1 counts the purchase's version (1, `received`); D2 and D3 the grant's (§4.3).
    assert d1.application.expected_state_version == 1
    applied = [
        (r.application.transition_ref, r.application.expected_state_version)
        for r in (d2_record, d3_record)
    ]
    assert applied == [(RESERVATION, 1), (d3.transition_ref, 2)]
    assert flow.count(f"SELECT state_version FROM delegation_grants WHERE grant_id='{GRANT}'") == (
        3,
    )
    data = d3_record.dispatch_input()
    assert (data.d2.evidence_id, data.d2.application_transition_ref) == (
        d2.evidence_id,
        RESERVATION,
    )
    assert (
        data.closed_proof.authorization_id == d2_record.hnp_authorization_input().authorization_id
    )
    with closing(sqlite3.connect(flow.path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        ledger = SqliteGovernanceEvidenceStore(connection)
        replayed = [ledger.replay(r.evidence_id) for r in (d2_record, d3_record)]
    assert [(r.outcome, r.rule_results) for r in replayed] == [
        (d2_record.decision.outcome, d2_record.decision.rule_results),
        (d3_record.decision.outcome, d3_record.decision.rule_results),
    ]


# N: a deny is recorded with the task's terminal and nothing is reserved or signed.


def test_a_destination_outside_the_grant_denies_by_pol08_with_the_terminal(
    tmp_path: Path,
) -> None:
    other = {**details().fulfillment.model_dump(mode="json"), "name": "Otra Persona"}
    flow = delegated(tmp_path, destination=other)

    d2 = flow.decide("D2")
    assert d2.decision is not None and d2.decision.outcome == "deny"
    assert ("POL-08", "fail") in rules(d2.decision)
    assert d2.conflict_hooks == ()
    assert flow.finish("escalated", "terms_out_of_scope", d2.hooks).status == "closed"

    *_, record = flow.evidence()
    assert isinstance(record, DecisionRecord)
    assert (record.evidence_id, record.application.disposition) == (d2.evidence_id, "blocked")
    assert record.application.expected_state_version == 1
    assert flow.count("SELECT COUNT(*) FROM grant_reservations") == (0,)


def test_an_open_proof_of_another_issuer_is_invalid_and_fails_pol07(tmp_path: Path) -> None:
    flow = delegated(tmp_path, foreign_issuer=True)

    d2 = flow.decide("D2")

    assert d2.proof == "invalid"
    assert d2.decision is not None and d2.decision.outcome == "deny"
    assert ("POL-07", "fail") in rules(d2.decision)


def test_an_unresolved_open_proof_is_profile_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = delegated(tmp_path)
    unresolved = MandateProof("unresolved", "unresolved_constraint")
    monkeypatch.setattr(hnp_gate, "verify_open_proof", lambda *args, **kwargs: unresolved)

    d2 = flow.decide("D2")
    assert flow.finish("escalated", "authorization_profile_unresolved", d2.hooks).status == "closed"

    assert d2.proof == "unresolved" and d2.decision is not None
    assert (d2.decision.origin, d2.decision.reason_codes) == (
        "runtime_guard",
        ("GOV_AUTHORIZATION_PROFILE_UNRESOLVED",),
    )
    *_, record = flow.evidence()
    assert isinstance(record, DecisionRecord)
    assert (record.replayability, record.application.disposition) == (
        "policy_not_evaluated",
        "blocked",
    )


def test_stale_close_freshness_is_a_runtime_guard_deny(tmp_path: Path) -> None:
    flow = delegated(tmp_path, freshness="changed")

    d2 = flow.decide("D2")

    assert d2.decision is not None
    assert d2.decision.reason_codes == ("GOV_FRESHNESS_UNRESOLVED",)


def test_closed_mandates_of_another_agent_key_are_invalid_and_fail_pol10(tmp_path: Path) -> None:
    flow = delegated(tmp_path)
    assert flow.reserve(flow.decide("D2")).status == "applied"
    other = AgentKeyCloser(generate_keys().agent_pem, merchant_issuer=flow.merchant_issuer)
    assert flow.close(closer=other).status == "applied"

    d3 = flow.decide("D3")

    assert d3.proof == "invalid" and d3.transition_ref is None
    assert d3.decision is not None and ("POL-10", "fail") in rules(d3.decision)


def test_closed_mandates_over_another_checkout_fail_pol10(tmp_path: Path) -> None:
    flow = delegated(tmp_path)
    assert flow.reserve(flow.decide("D2")).status == "applied"
    other = {**flow.checkout.model_dump(mode="json"), "updated_at": "2026-09-20T12:00:01Z"}
    assert flow.close(checkout=other).status == "applied"

    d3 = flow.decide("D3")

    assert d3.decision is not None and rules(d3.decision) == [
        ("POL-09", "pass"),
        ("POL-10", "fail"),
    ]


# C11, CAS and facts: the evidence and the transition commit together or not at all.


def test_c11_a_hook_that_cannot_verify_the_reservation_rolls_uow1_back(tmp_path: Path) -> None:
    flow = delegated(tmp_path)
    d2 = flow.decide("D2")

    with pytest.raises(GovernanceError) as failed:
        flow.reserve(d2, evidence_id="evidence:another")

    assert failed.value.reason == "GOV_EVIDENCE_UNAVAILABLE"
    assert flow.count("SELECT COUNT(*) FROM grant_reservations") == (0,)
    assert flow.count(f"SELECT state_version FROM delegation_grants WHERE grant_id='{GRANT}'") == (
        1,
    )
    assert [item.evidence_id for item in flow.evidence()][1:] == []


def test_a_lost_cas_keeps_the_allow_blocked_with_gov_state_conflict(tmp_path: Path) -> None:
    flow = delegated(tmp_path)
    d2 = flow.decide("D2")
    flow.ledger.set_kill_switch(True)

    blocked = flow.reserve(d2)
    assert (blocked.status, blocked.outcome) == ("blocked", "kill_switch")
    closed = flow.finish("canceled", "kill_switch", d2.conflict_hooks, stage="uow1")
    assert closed.status == "closed"

    *_, record, failure = flow.evidence()
    assert isinstance(record, DecisionRecord) and isinstance(failure, ApplicationFailure)
    assert (record.evidence_id, record.decision.outcome, record.application.disposition) == (
        d2.evidence_id,
        "allow",
        "blocked",
    )
    assert (failure.reason_code, failure.expected_state_version) == ("GOV_STATE_CONFLICT", 1)
    assert flow.count("SELECT COUNT(*) FROM grant_reservations") == (0,)


def test_facts_that_cannot_be_assembled_raise_and_leave_input_rejected(tmp_path: Path) -> None:
    flow = delegated(tmp_path, freshness=None)

    with pytest.raises(GovernanceError) as failed:
        flow.decide("D2")

    assert failed.value.reason == "GOV_FACTS_UNAVAILABLE"
    *_, rejected = flow.evidence()
    assert isinstance(rejected, InputRejected)
    assert flow.count(
        f"SELECT phase, expected_state_version FROM governance_evidence "
        f"WHERE evidence_id='{rejected.evidence_id}'"
    ) == ("D2", 1)


def test_a_ledger_created_before_d3_is_refused_at_startup(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with closing(sqlite3.connect(settings.agent_database_path)) as connection, connection:
        # The DDL a ledger keeps from before D3-HNP: CREATE TABLE IF NOT EXISTS never changes it.
        connection.execute(
            "CREATE TABLE governance_evidence (phase TEXT CHECK(phase IN ('D1','D2')))"
        )
    trust = MandateTrust({}, {"kty": "EC"}, settings.merchant_base_url)

    with pytest.raises(RuntimeError):
        build_hnp_policy_gate(settings, None, trust=trust)
