"""D1-HNP through the real gate, vault and ledger (docs/design/hnp-governed.md §3.3, §3.4, §4.6).

The merchant runs behind httpx.MockTransport and SQLite is real. The HP gate comes from
build_runtime exactly as in the demo; the HNP gate is the same class with `purchase_mode="HNP"`,
the wiring HNP-09a gives the executor.
"""

import json
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import SecretStr

from commerce_agent.acp.feed import load_product_feed
from commerce_agent.application import PurchaseService, PurchaseServiceError
from commerce_agent.application.conversation import PENDING_STAGES
from commerce_agent.approval import ApprovalService
from commerce_agent.config import Settings
from commerce_agent.domain import HardConstraints, PurchaseIntent, rank_candidates
from commerce_agent.governance.bootstrap import configured_d1
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.hnp_policy import GRANT_SCOPE_ELIGIBILITY, HnpGrant
from commerce_agent.governance.models import HNP_POLICY_SET_ID
from commerce_agent.governance.provenance import HP_POLICY_SET_ID, D1EnforceConfiguration
from commerce_agent.governance.runtime_dispatch import (
    SingleAttemptCheckoutDispatch,
    SingleAttemptPostCreateDispatch,
)
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.runtime import AgentRuntime
from tests.governance.test_d1_budget_runtime import decision_records, rules
from tests.governance.test_hp_continuation import StoreMerchant
from tests.governance.test_runtime_dispatch import CREATE, governed, runtime
from tests.governance.test_shadow_integration import FIXTURE, OWNER
from tests.test_purchase_service import details

RUN = "run-real"
GRANT = HnpGrant(grant_id="dg_0123456789abcdef", variant_ids=("SON-01",))


def settings_for(tmp_path: Path) -> Settings:
    # The HP policy set is b2-hp-v2, as in the demo: D1-HNP must not read it.
    return governed(tmp_path / "agent.db", agent_d1_policy_set_id=HP_POLICY_SET_ID)


def hnp_gate(settings: Settings) -> SqliteD1EnforceGate:
    configured = configured_d1(settings, None)
    assert isinstance(configured.configuration, D1EnforceConfiguration)
    return SqliteD1EnforceGate(
        settings.agent_database_path,
        configured.configuration,
        configured.keys,
        configured.clocks,
        dispatch=SingleAttemptCheckoutDispatch(),
        post_create_dispatch=SingleAttemptPostCreateDispatch(),
        purchase_mode="HNP",
    )


def hnp_service(settings: Settings, built: AgentRuntime) -> PurchaseService:
    return PurchaseService(
        feed=load_product_feed(FIXTURE),
        checkout=built.client,
        store=built.store,
        approvals=ApprovalService(built.store, SecretStr(secrets.token_urlsafe(40))),
        d1_gate=built.d1_gate,
        hnp_d1_gate=hnp_gate(settings),
    )


def start(service: PurchaseService, *, total: int = 80_000, grant: HnpGrant | None = GRANT):
    return service.start(
        run_id=RUN,
        request="delegated purchase",
        intent=PurchaseIntent(hard=HardConstraints(currency="COP", max_total_amount_minor=total)),
        selected_variant_id="SON-01",
        details=details(),
        owner_id=OWNER,
        grant=grant,
    )


def purchase(path: Path) -> tuple[str, str | None, str, str | None]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT purchase_mode,grant_id,stage,checkout_id FROM purchases WHERE run_id=?",
            (RUN,),
        ).fetchone()


def algorithm_source(path: Path, record: DecisionRecord) -> dict:
    ref = record.policy_input().eligibility_algorithm_ref
    with closing(sqlite3.connect(path)) as connection:
        (body,) = connection.execute(
            "SELECT public_body FROM d1_provenance_sources WHERE source_id=? AND version=?",
            (ref.id, ref.version),
        ).fetchone()
    return json.loads(body)


def governance_rows(path: Path) -> int:
    """Evidence and vault rows of the run; a refused gate leaves neither (nor their tables)."""
    with closing(sqlite3.connect(path)) as connection:
        tables = {name for (name,) in connection.execute("SELECT name FROM sqlite_master")}
        return sum(
            connection.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (RUN,)).fetchone()[0]
            for table in ("governance_evidence", "d1_provenance_sources")
            if table in tables
        )


def test_d1_hnp_allow_leaves_hnp_evidence_and_start_stops_at_checkout_prepared(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    merchant = StoreMerchant()
    built = runtime(settings, merchant)
    try:
        prepared = start(hnp_service(settings, built))
        assert built.store.pending_purchase(PENDING_STAGES, OWNER) is None
        journal = hnp_gate(settings).post_create_flow(RUN, OWNER).inspect()
    finally:
        built.close()

    assert prepared.stage == "checkout_prepared"
    assert prepared.checkout.status == "ready_for_payment"
    assert purchase(settings.agent_database_path) == (
        "HNP",
        GRANT.grant_id,
        "checkout_prepared",
        "cs_api_1",
    )
    # One POST, then the journaled details and read; never a human hand-off nor a cancel.
    assert merchant.creations() == 1
    assert merchant.effects() == []
    assert journal[-1].phase == "observed"
    (record,) = decision_records(settings.agent_database_path)
    decision = record.decision
    assert (decision.phase, decision.purchase_mode, decision.outcome) == ("D1", "HNP", "allow")
    assert decision.policy_binding.policy_set_id == HNP_POLICY_SET_ID
    assert rules(record) == [("POL-01", "pass"), ("POL-02", "pass"), ("POL-11", "pass")]
    assert record.application.disposition == "transition_committed"
    data = record.policy_input()
    assert data.eligible_variant_ids == ("SON-01",)
    source = algorithm_source(settings.agent_database_path, record)
    assert source["eligibility_profile"] == GRANT_SCOPE_ELIGIBILITY
    assert (source["grant_id"], source["variant_ids"]) == (GRANT.grant_id, ["SON-01"])
    with closing(sqlite3.connect(settings.agent_database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        replayed = SqliteGovernanceEvidenceStore(connection).replay(record.evidence_id)
    assert (replayed.outcome, replayed.rule_results) == (decision.outcome, decision.rule_results)


def test_a_selection_outside_the_grant_is_denied_by_pol01_before_any_post(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    merchant = StoreMerchant()
    built = runtime(settings, merchant)
    outside = HnpGrant(grant_id=GRANT.grant_id, variant_ids=("SON-07",))
    try:
        with pytest.raises(PurchaseServiceError) as denied:
            start(hnp_service(settings, built), grant=outside)
    finally:
        built.close()

    assert denied.value.code == "d1_denied"
    assert CREATE not in merchant.requests
    (record,) = decision_records(settings.agent_database_path)
    assert record.decision.purchase_mode == "HNP"
    assert rules(record) == [("POL-01", "fail"), ("POL-02", "pass"), ("POL-11", "pass")]
    assert record.decision.reason_codes == ("SELECTION_NOT_ELIGIBLE",)
    assert record.application.disposition == "blocked"
    assert record.policy_input().eligible_variant_ids == ()
    assert purchase(settings.agent_database_path)[2:] == ("received", None)


def test_a_price_over_the_authorized_total_is_denied_by_pol11(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    merchant = StoreMerchant()
    built = runtime(settings, merchant)
    try:
        with pytest.raises(PurchaseServiceError) as denied:
            start(hnp_service(settings, built), total=71_999)
    finally:
        built.close()

    assert denied.value.code == "d1_denied"
    assert CREATE not in merchant.requests
    (record,) = decision_records(settings.agent_database_path)
    assert record.decision.reason_codes == ("BUDGET_NOT_SATISFIABLE",)
    assert record.decision.policy_binding.policy_set_id == HNP_POLICY_SET_ID


def test_enforce_without_the_hnp_gate_refuses_an_hnp_purchase(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    merchant = StoreMerchant()
    built = runtime(settings, merchant)
    try:
        with pytest.raises(PurchaseServiceError) as refused:
            start(built.purchases)
        assert built.store.purchase(RUN) is None
    finally:
        built.close()

    assert refused.value.code == "d1_unavailable"
    assert merchant.requests == []


@pytest.mark.parametrize(
    ("gate_mode", "grant_id", "grant"),
    [
        pytest.param("HNP", GRANT.grant_id, None, id="hnp-gate-without-grant"),
        pytest.param("HP", None, GRANT, id="hp-gate-with-grant"),
        pytest.param("HNP", None, GRANT, id="hnp-gate-on-an-hp-purchase"),
        pytest.param("HNP", "dg_other", GRANT, id="hnp-gate-on-another-grant"),
    ],
)
def test_a_gate_only_decides_purchases_of_its_own_mode(
    tmp_path: Path, gate_mode: str, grant_id: str | None, grant: HnpGrant | None
) -> None:
    settings = settings_for(tmp_path)
    merchant = StoreMerchant()
    built = runtime(settings, merchant)
    try:
        assert built.d1_gate is not None
        gate = hnp_gate(settings) if gate_mode == "HNP" else built.d1_gate
        built.store.create_purchase(RUN, "request", OWNER, grant_id=grant_id)
        feed, intent = load_product_feed(FIXTURE), PurchaseIntent()
        selection = rank_candidates(feed, intent)
        assert selection.selected is not None
        with pytest.raises(PurchaseServiceError) as refused:
            gate.create_checkout(
                gate.capture(feed, intent, rank_candidates),
                run_id=RUN,
                owner_id=OWNER,
                selection=selection,
                candidate=selection.selected,
                gateway=built.client,
                grant=grant,
            )
    finally:
        built.close()

    assert refused.value.code == "d1_unavailable"
    assert merchant.requests == []
    assert governance_rows(settings.agent_database_path) == 0
