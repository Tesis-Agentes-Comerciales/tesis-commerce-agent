"""POL-11 through build_runtime: the D1 input carries the selection's catalog price.

Real SQLite in tmp_path and the merchant behind httpx.MockTransport. The policy set comes from
Settings exactly as in the demo (`AGENT_D1_POLICY_SET_ID=b2-hp-v2`).
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from commerce_agent.application import PurchaseServiceError
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.provenance import HP_POLICY_SET_ID
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from tests.governance.test_hp_continuation import StoreMerchant
from tests.governance.test_runtime_dispatch import CREATE, governed, runtime
from tests.governance.test_shadow_integration import configuration, observer, service, start

# SON-01 in tests/fixtures/feed: 72.000 in the catalog; the merchant adds 2.000 of shipping.
SONORA_MINOR = 72_000


def total_budget(amount: int) -> PurchaseIntent:
    return PurchaseIntent(hard=HardConstraints(currency="COP", max_total_amount_minor=amount))


def decision_records(path: Path, run_id: str = "run-real") -> tuple[DecisionRecord, ...]:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        return tuple(
            item
            for item in SqliteGovernanceEvidenceStore(connection).for_run(run_id)
            if isinstance(item, DecisionRecord)
        )


def rules(record: DecisionRecord) -> list[tuple[str, str]]:
    return [(rule.policy_id, rule.result) for rule in record.decision.rule_results]


def purchase_stage(path: Path) -> list[tuple[str, str | None]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT stage,checkout_id FROM purchases").fetchall()


def test_a_total_budget_below_the_catalog_price_is_denied_before_any_post(tmp_path: Path) -> None:
    settings = governed(tmp_path / "agent.db", agent_d1_policy_set_id=HP_POLICY_SET_ID)
    merchant = StoreMerchant()
    built = runtime(settings, merchant)
    try:
        with pytest.raises(PurchaseServiceError) as denied:
            start(built.purchases, intent=total_budget(SONORA_MINOR - 1))
    finally:
        built.close()

    assert denied.value.code == "d1_denied"
    assert CREATE not in merchant.requests
    (record,) = decision_records(settings.agent_database_path)
    assert rules(record) == [("POL-01", "pass"), ("POL-02", "pass"), ("POL-11", "fail")]
    assert record.decision.reason_codes == ("BUDGET_NOT_SATISFIABLE",)
    assert record.application.disposition == "blocked"
    data = record.policy_input()
    # Facts of the selection and of the validated intent, never of the model.
    assert data.selected_unit_minor == SONORA_MINOR
    assert data.intent.max_total_minor == SONORA_MINOR - 1
    assert data.expected_policy.policy_set_id == HP_POLICY_SET_ID
    assert purchase_stage(settings.agent_database_path) == [("received", None)]


def test_a_reachable_total_budget_lets_the_purchase_continue(tmp_path: Path) -> None:
    settings = governed(tmp_path / "agent.db", agent_d1_policy_set_id=HP_POLICY_SET_ID)
    merchant = StoreMerchant()
    built = runtime(settings, merchant)
    try:
        prepared = start(built.purchases, intent=total_budget(80_000))
    finally:
        built.close()

    assert prepared.stage == "awaiting_confirmation"
    assert merchant.creations() == 1
    (record,) = decision_records(settings.agent_database_path)
    assert rules(record) == [("POL-01", "pass"), ("POL-02", "pass"), ("POL-11", "pass")]


def test_the_authoritative_total_guard_still_cancels_after_d1(tmp_path: Path) -> None:
    # 72.000 fits a 73.000 budget, so POL-11 allows; the merchant's 74.000 with shipping does not.
    settings = governed(tmp_path / "agent.db", agent_d1_policy_set_id=HP_POLICY_SET_ID)
    merchant = StoreMerchant()
    built = runtime(settings, merchant)
    try:
        with pytest.raises(PurchaseServiceError) as exceeded:
            start(built.purchases, intent=total_budget(73_000))
    finally:
        built.close()

    assert exceeded.value.code == "budget_exceeded"
    assert merchant.creations() == 1
    assert merchant.effects() == [("POST", "/checkout_sessions/cs_api_1/cancel")]
    (record,) = decision_records(settings.agent_database_path)
    assert record.decision.outcome == "allow"
    assert rules(record)[-1] == ("POL-11", "pass")


def test_the_pilot_v1_policy_set_still_reaches_the_merchant_and_the_guard(tmp_path: Path) -> None:
    # b2-pilot-v1 has no POL-11: the published S02 path (create, then cancel) is unchanged.
    settings = governed(tmp_path / "agent.db", agent_d1_policy_set_id="b2-pilot-v1")
    merchant = StoreMerchant()
    built = runtime(settings, merchant)
    try:
        with pytest.raises(PurchaseServiceError) as exceeded:
            start(built.purchases, intent=total_budget(SONORA_MINOR - 1))
    finally:
        built.close()

    assert exceeded.value.code == "budget_exceeded"
    assert merchant.creations() == 1
    (record,) = decision_records(settings.agent_database_path)
    assert rules(record) == [("POL-01", "pass"), ("POL-02", "pass")]
    # The fact is still recorded: the input contract is the same for every policy set.
    assert record.policy_input().selected_unit_minor == SONORA_MINOR


def test_shadow_observes_the_budget_denial_and_leaves_the_purchase_to_b0(tmp_path: Path) -> None:
    settings = configuration(
        tmp_path / "agent.db", mode="shadow", agent_d1_policy_set_id=HP_POLICY_SET_ID
    )
    purchases, gateway, _ = service(settings, observer(settings))

    # Shadow only records: the checkout is created and B0's total guard cancels it.
    with pytest.raises(PurchaseServiceError) as exceeded:
        start(purchases, intent=total_budget(SONORA_MINOR - 1))

    assert exceeded.value.code == "budget_exceeded"
    assert next(name for name, _ in gateway.calls) == "create"
    (record,) = decision_records(settings.agent_database_path)
    assert (record.decision.governance_mode, record.decision.outcome) == ("shadow", "deny")
    assert record.application.disposition == "observed_shadow"
    assert rules(record)[-1] == ("POL-11", "fail")
    assert json.loads(canonical_bytes(record.policy_input()))["selected_unit_minor"] == (
        SONORA_MINOR
    )
