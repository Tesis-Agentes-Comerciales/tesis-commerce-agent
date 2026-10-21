"""D2-HP through the API: POL-03..06 when the buyer approves, on a real SQLite store.

Mock merchant and PSP, no model. A policy with D2 rules runs the gate between the
`approve` freshness check and the AP2/HMAC signature; without them approval is unchanged.
"""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretBytes

from commerce_agent.assistant_transport import error_message
from commerce_agent.config import Settings
from commerce_agent.governance import authorization as authorization_module
from commerce_agent.governance.canonical import canonical_bytes, sha256_digest
from commerce_agent.governance.evaluator import ReferenceAuthorizationEvaluator
from commerce_agent.governance.evidence import (
    ApplicationFailure,
    DecisionEvidence,
    DecisionRecord,
    EvidenceError,
    EvidenceScope,
    InputRejected,
)
from commerce_agent.governance.models import (
    SCHEMA,
    AuthorizationRules,
    D1PolicyArtifact,
    PolicyDecision,
)
from commerce_agent.governance.provenance import ProvenanceKeys, merchant_pseudonym
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.runtime import build_runtime
from tests.governance.test_hp_continuation import StoreMerchant, app_client, approve, ask
from tests.governance.test_runtime_dispatch import governed, runtime
from tests.governance.test_stale_terms_pending import Ap2Merchant, ap2, presented
from tests.governance.test_stale_terms_pending import app_client as ap2_client

AUTHORIZATION = AuthorizationRules(
    schema_version=SCHEMA,
    policy_ids=("POL-03", "POL-04", "POL-05", "POL-06"),
    allowed_actions=("complete_purchase",),
    authentication_bases=("user_jwt",),
)


def keys_of(settings: Settings) -> ProvenanceKeys:
    assert settings.agent_d1_key_id is not None
    assert settings.agent_d1_hmac_key_hex is not None and settings.agent_d1_aes_key_hex is not None
    return ProvenanceKeys(
        key_id=settings.agent_d1_key_id,
        pseudonym_key=SecretBytes(bytes.fromhex(settings.agent_d1_hmac_key_hex.get_secret_value())),
        encryption_key=SecretBytes(bytes.fromhex(settings.agent_d1_aes_key_hex.get_secret_value())),
    )


def with_policy(
    settings: Settings,
    *,
    revision: int = 1,
    authorization: AuthorizationRules | None = AUTHORIZATION,
) -> Settings:
    """The configured policy file: D1 for this merchant, plus D2 rules unless left out."""
    assert settings.agent_d1_policy_set_id is not None
    artifact = D1PolicyArtifact(
        schema_version=SCHEMA,
        policy_set_id=settings.agent_d1_policy_set_id,
        version=revision,
        policy_ids=("POL-01", "POL-02"),
        allowed_actions=("prepare_checkout",),
        allowed_merchant_refs=(merchant_pseudonym(keys_of(settings), settings.merchant_base_url),),
        currency="COP",
        quantity=1,
        authorization=authorization,
    )
    payload = canonical_bytes(artifact)
    path = settings.agent_database_path.with_name(f"policy-v{revision}.json")
    path.write_bytes(payload)
    return settings.model_copy(
        update={
            "agent_d1_policy_path": path,
            "agent_d1_policy_digest": sha256_digest(payload),
            "agent_d1_policy_revision": str(revision),
        }
    )


def d2_settings(path: Path, **changes: Any) -> Settings:
    return with_policy(governed(path, **changes))


def evidence(path: Path) -> list[DecisionEvidence]:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        ledger = SqliteGovernanceEvidenceStore(connection)
        return [
            item
            for (run_id,) in connection.execute("SELECT run_id FROM purchases").fetchall()
            for item in ledger.for_run(run_id)
        ]


def d2_records(path: Path) -> list[DecisionRecord]:
    return [
        item
        for item in evidence(path)
        if isinstance(item, DecisionRecord) and item.decision.phase == "D2"
    ]


def approval_row(path: Path, run_id: str) -> tuple[Any, ...] | None:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT authorization_evidence_id, checkout_mandate FROM approvals WHERE run_id=?",
            (run_id,),
        ).fetchone()


def stage(client: TestClient, run_id: str) -> str:
    return client.get(f"/purchases/{run_id}").json()["stage"]


def start_purchase(client: TestClient, turn_id: str) -> str:
    turn = ask(client, turn_id)
    assert turn.status_code == 200, turn.text
    assert turn.json()["purchase"]["stage"] == "awaiting_confirmation"
    return turn.json()["purchase"]["run_id"]


def assert_refused(response: Any, code: str) -> None:
    assert response.status_code == 409, response.text
    assert response.json() == {"code": code, "message": error_message(code)}


def test_allow_writes_approval_stage_and_evidence_together(tmp_path: Path) -> None:
    settings = d2_settings(tmp_path / "agent.db")
    merchant = StoreMerchant()
    with app_client(settings, merchant) as client:
        run_id = start_purchase(client, "turn-allow")
        approved = approve(client, run_id)
        assert approved.status_code == 200, approved.text
        assert stage(client, run_id) == "approved"
        # The trace still reads D1 and the purchase: D2 never breaks it.
        assert client.get(f"/purchases/{run_id}/trace").status_code == 200
        completed = client.post(f"/purchases/{run_id}/complete")
        assert completed.status_code == 200, completed.text
    path = settings.agent_database_path
    d1, d2 = (item for item in evidence(path) if isinstance(item, DecisionRecord))
    assert (d1.decision.phase, d1.sequence, d2.decision.phase, d2.sequence) == ("D1", 1, "D2", 2)
    assert d2.decision.outcome == "allow" and d2.decision.origin == "policy"
    assert [rule.policy_id for rule in d2.decision.rule_results] == [
        "POL-03",
        "POL-04",
        "POL-05",
        "POL-06",
    ]
    data = d2.authorization_input()
    assert d2.application.disposition == "transition_committed"
    assert d2.application.transition_ref == data.authorization_id
    assert d2.application.resulting_state_version == data.expected_local_state_version + 1
    # Linked to D1 by identity and digest, and to the approval that followed.
    assert (data.d1.decision_id, data.d1.evidence_id) == (d1.decision.decision_id, d1.evidence_id)
    assert data.d1.evidence_digest == sha256_digest(canonical_bytes(d1))
    assert data.operation_id == d1.application.transition_ref
    assert data.terms.total_minor == 74_000 and data.freshness.status == "current"
    assert data.hp.material_match and data.principal.owns_run
    assert approval_row(path, run_id) == (d2.evidence_id, None)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        ledger = SqliteGovernanceEvidenceStore(connection)
        replayed = ledger.replay(d2.evidence_id)
        # The freshness link is the `hp_evidence` row this approval just appended.
        (digest,) = connection.execute(
            "SELECT digest FROM hp_evidence WHERE run_id=? AND sequence=?",
            (run_id, data.freshness.source_offer_ref.version),
        ).fetchone()
        (phase,) = connection.execute(
            "SELECT phase FROM governance_evidence WHERE evidence_id=?", (d2.evidence_id,)
        ).fetchone()
    assert (replayed.outcome, replayed.reason_codes) == ("allow", d2.decision.reason_codes)
    assert digest == data.freshness.source_offer_ref.digest and phase == "D2"
    # The canonical input names facts, never buyer data or the click's fingerprint.
    assert "buyer@example.test" not in d2.canonical_input_bytes
    assert "Calle 100" not in d2.canonical_input_bytes


def test_presented_terms_that_differ_from_the_click_deny_before_signing(tmp_path: Path) -> None:
    settings = d2_settings(tmp_path / "agent.db")
    merchant = StoreMerchant()
    with app_client(settings, merchant) as client:
        run_id = start_purchase(client, "turn-pol04")
        terms = client.get(f"/purchases/{run_id}/approval").json()
        # The server presented other terms last: the click does not name them (POL-04).
        SqliteAgentStore(settings.agent_database_path).save_approval_terms(
            run_id, {**terms, "fingerprint": "0" * 64}
        )
        denied = client.post(
            f"/purchases/{run_id}/approval", json={"expected_fingerprint": terms["fingerprint"]}
        )
        assert_refused(denied, "d2_denied")
        assert stage(client, run_id) == "awaiting_confirmation"
        assert client.post(f"/purchases/{run_id}/complete").is_client_error
    (record,) = d2_records(settings.agent_database_path)
    assert record.decision.outcome == "deny"
    assert record.decision.reason_codes == ("EPISODE_BINDING_MISMATCH",)
    assert record.application.disposition == "blocked"
    assert approval_row(settings.agent_database_path, run_id) is None
    assert merchant.effects() == []


def test_a_policy_change_between_d1_and_d2_denies_with_pol06(tmp_path: Path) -> None:
    first = d2_settings(tmp_path / "agent.db")
    merchant = StoreMerchant()
    with app_client(first, merchant) as client:
        run_id = start_purchase(client, "turn-pol06")
    # The operator publishes revision 2 while the purchase awaits the buyer.
    second = with_policy(first, revision=2)
    with app_client(second, merchant) as client:
        assert_refused(approve(client, run_id), "d2_denied")
    (record,) = d2_records(first.agent_database_path)
    assert record.decision.reason_codes == ("POLICY_CONTEXT_MISMATCH",)
    data = record.authorization_input()
    assert data.d1.policy_binding.version == 1 and data.expected_policy.version == 2
    assert approval_row(first.agent_database_path, run_id) is None


def test_an_engine_failure_is_a_denial_without_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: ReferenceAuthorizationEvaluator, policy_input: Any) -> PolicyDecision:
        raise RuntimeError("engine down")

    monkeypatch.setattr(ReferenceAuthorizationEvaluator, "evaluate", broken)
    settings = d2_settings(tmp_path / "agent.db")
    with app_client(settings, StoreMerchant()) as client:
        run_id = start_purchase(client, "turn-engine")
        assert_refused(approve(client, run_id), "d2_unavailable")
        assert stage(client, run_id) == "awaiting_confirmation"
    (record,) = d2_records(settings.agent_database_path)
    assert (record.decision.origin, record.decision.reason_codes) == (
        "runtime_guard",
        ("GOV_ENGINE_UNAVAILABLE",),
    )
    assert record.replayability == "policy_not_evaluated"
    assert approval_row(settings.agent_database_path, run_id) is None


def test_missing_presented_terms_reject_the_input(tmp_path: Path) -> None:
    settings = d2_settings(tmp_path / "agent.db")
    with app_client(settings, StoreMerchant()) as client:
        run_id = start_purchase(client, "turn-facts")
        fingerprint = client.get(f"/purchases/{run_id}/approval").json()["fingerprint"]
        with closing(sqlite3.connect(settings.agent_database_path)) as connection, connection:
            connection.execute("DELETE FROM approval_terms WHERE run_id=?", (run_id,))
        refused = client.post(
            f"/purchases/{run_id}/approval", json={"expected_fingerprint": fingerprint}
        )
        assert_refused(refused, "d2_unavailable")
    rejected = [
        item
        for item in evidence(settings.agent_database_path)
        if not isinstance(item, DecisionRecord)
    ]
    assert [type(item) for item in rejected] == [InputRejected]
    assert rejected[0].reason_code == "GOV_FACTS_UNAVAILABLE"
    assert approval_row(settings.agent_database_path, run_id) is None


def test_an_allow_that_cannot_be_applied_rolls_everything_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(connection: sqlite3.Connection, record: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(authorization_module, "write_approval", failing)
    settings = d2_settings(tmp_path / "agent.db")
    with app_client(settings, StoreMerchant()) as client:
        run_id = start_purchase(client, "turn-unapplied")
        assert_refused(approve(client, run_id), "d2_allow_unapplied")
        assert stage(client, run_id) == "awaiting_confirmation"
    path = settings.agent_database_path
    (record,) = d2_records(path)
    # The allow stays as evidence, `blocked`, with the failure that stopped it.
    assert record.decision.outcome == "allow" and record.application.disposition == "blocked"
    failures = [item for item in evidence(path) if isinstance(item, ApplicationFailure)]
    assert [item.decision_id for item in failures] == [record.decision.decision_id]
    assert approval_row(path, run_id) is None


def test_ap2_signs_only_after_the_d2_allow(tmp_path: Path) -> None:
    settings = with_policy(ap2(tmp_path / "agent.db"))
    merchant = Ap2Merchant(settings.merchant_base_url)
    with ap2_client(settings, merchant) as client:
        run_id, terms = presented(client, "turn-ap2")
        approved = client.post(
            f"/purchases/{run_id}/approval",
            json={
                "expected_fingerprint": terms["fingerprint"],
                "checkout_jwt": terms["checkout_jwt"],
            },
        )
        assert approved.status_code == 200, approved.text
    (record,) = d2_records(settings.agent_database_path)
    assert record.decision.outcome == "allow"
    profile = record.authorization_input().authorization_profile
    assert profile.status == "validated" and profile.profile_ref is not None
    row = approval_row(settings.agent_database_path, run_id)
    # The mandates exist only on the approval the allow committed.
    assert row is not None and row[0] == record.evidence_id and row[1] is not None


def test_without_d2_rules_approval_is_unchanged(tmp_path: Path) -> None:
    settings = with_policy(governed(tmp_path / "agent.db"), authorization=None)
    merchant = StoreMerchant()
    built = runtime(settings, merchant)
    try:
        assert built.d2_gate is None
    finally:
        built.close()
    with app_client(settings, merchant) as client:
        run_id = start_purchase(client, "turn-v1")
        assert approve(client, run_id).status_code == 200
    assert d2_records(settings.agent_database_path) == []
    assert approval_row(settings.agent_database_path, run_id) == (None, None)


def test_off_never_builds_the_gate(tmp_path: Path) -> None:
    settings = governed(tmp_path / "agent.db").model_copy(
        update={"agent_governance_mode": "off", "agent_hp_flow": "blocked"}
    )
    built = runtime(settings, StoreMerchant())
    try:
        assert built.d1_gate is None and built.d2_gate is None
    finally:
        built.close()


def test_a_ledger_created_before_d2_refuses_to_start(tmp_path: Path) -> None:
    settings = d2_settings(tmp_path / "agent.db")
    with closing(sqlite3.connect(settings.agent_database_path)) as connection, connection:
        connection.execute(
            "CREATE TABLE governance_evidence (evidence_id TEXT PRIMARY KEY, "
            "phase TEXT NOT NULL CHECK(phase = 'D1'))"
        )
    with pytest.raises(RuntimeError, match="valid D2 settings"):
        build_runtime(settings)


def test_decision_scopes_are_append_only_and_bound_to_the_episode(tmp_path: Path) -> None:
    settings = d2_settings(tmp_path / "agent.db")
    with app_client(settings, StoreMerchant()) as client:
        run_id = start_purchase(client, "turn-scope")
        assert approve(client, run_id).status_code == 200
    (record,) = d2_records(settings.agent_database_path)
    with closing(sqlite3.connect(settings.agent_database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        ledger = SqliteGovernanceEvidenceStore(connection)
        assert ledger.admits_phase_d2()
        (body,) = connection.execute(
            "SELECT scope FROM governance_decision_scopes WHERE decision_id=?",
            (record.decision.decision_id,),
        ).fetchone()
        scope = EvidenceScope.model_validate_json(body).model_copy(
            update={"policy_artifact_ref": None, "engine_artifact_ref": None}
        )
        d1 = record.authorization_input().d1
        connection.execute("BEGIN IMMEDIATE")
        ledger.register_decision_scope("decision:new", scope, policy_bytes=None, engine_bytes=None)
        # A scope never attaches to a decision whose evidence already exists (D1's here).
        cases = [(d1.decision_id, {}), ("decision:new", {"context_version": 9})]
        cases += [("decision:other", {"phase": "D1"}), ("decision:other", {"trace_id": "t:2"})]
        for decision_id, change in cases:
            with pytest.raises(EvidenceError, match="GOV_INPUT_INVALID"):
                ledger.register_decision_scope(
                    decision_id,
                    scope.model_copy(update=change),
                    policy_bytes=None,
                    engine_bytes=None,
                )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM governance_decision_scopes")
        connection.rollback()


def test_a_legacy_ledger_is_detected(tmp_path: Path) -> None:
    with closing(sqlite3.connect(tmp_path / "legacy.db")) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        ledger = SqliteGovernanceEvidenceStore(connection)
        assert ledger.admits_phase_d2()
        connection.execute(
            "CREATE TABLE governance_evidence (evidence_id TEXT PRIMARY KEY, "
            "phase TEXT NOT NULL CHECK(phase = 'D1'))"
        )
        assert not ledger.admits_phase_d2()
