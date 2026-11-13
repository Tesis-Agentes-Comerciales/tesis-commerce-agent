"""The HNP executor as the runtime assembles it (docs/design/hnp-governed.md §3.3, §6).

With AGENT_HNP_ENABLED off nothing changes. On, `off` runs B0-HNP and `enforce` runs B2-HNP with
D1-HNP, D2/D3-HNP and the `close` freshness guard, both through the dispatch (HNP-09b). Real keys,
real SQLite and the AP2 merchant and PSP doubles; the `llm` selector and the `llm` intent reviewer
are only assembled here, never called.
"""

import json
import logging
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from commerce_agent.ap2_hnp import MERCHANT_ID
from commerce_agent.api import create_app
from commerce_agent.application.hnp_dispatch import HnpDispatcher
from commerce_agent.application.hnp_execution import HnpExecutionService, NullHnpPolicyGate
from commerce_agent.application.hnp_intent_review import IntentReview
from commerce_agent.application.hnp_selection import DeterministicHnpSelector, ModelHnpSelector
from commerce_agent.config import Settings
from commerce_agent.delegation.service import DelegationConsentRequest, DelegationProposalRequest
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.hnp_gate import SqliteHnpPolicyGate
from commerce_agent.llm.delegated_choice import ClaudeDelegatedChooser
from commerce_agent.llm.intent_review import ModelHnpIntentReviewer
from commerce_agent.persistence import delegation_sqlite
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.runtime import AgentRuntime, build_runtime
from tests.governance.test_shadow_integration import configuration
from tests.governance.test_stale_terms_pending import Ap2Merchant
from tests.support.hnp_dispatch import HNP_ORDER_ID, HnpMerchant, Psp
from tests.test_delegation_api import (
    AGENT_PEM,
    FEED,
    MERCHANT,
    OPERATOR_TOKEN,
    PROVIDER_PEM,
    SERVICE_TOKEN,
    hnp_settings,
    proposal_body,
)

OWNER = "buyer-a"
HP_COLUMNS = ["sequence", "stage", "started_at", "elapsed_ms", "outcome"]


def governed_hnp(tmp_path: Path, **changes: Any) -> Settings:
    """B2-HNP: enforce with the governed HP flow, plus what HNP startup requires (§6)."""
    return configuration(
        tmp_path / "agent.db",
        mode="enforce",
        agent_hp_flow="governed",
        agent_auth_mode="jwt",
        agent_auth_issuer="http://127.0.0.1:5173",
        agent_ap2_required=True,
        agent_ap2_private_key_pem=SecretStr(PROVIDER_PEM),
        agent_hnp_enabled=True,
        agent_hnp_agent_key_pem=SecretStr(AGENT_PEM),
        agent_hnp_service_token=SecretStr(SERVICE_TOKEN),
        agent_hnp_operator_token=SecretStr(OPERATOR_TOKEN),
        **changes,
    )


def runtime(settings: Settings, merchant: Any, psp: Psp | None = None) -> AgentRuntime:
    return build_runtime(
        settings,
        transport=httpx.MockTransport(merchant),
        payment_transport=httpx.MockTransport(psp or merchant),
    )


def consented(built: AgentRuntime) -> str:
    assert built.delegations is not None
    proposal = built.delegations.propose(
        OWNER, DelegationProposalRequest.model_validate_json(json.dumps(proposal_body()))
    )
    request = DelegationConsentRequest(
        proposal_id=proposal.proposal_id, scope_digest=proposal.scope_digest
    )
    view, created = built.delegations.consent(OWNER, request)
    assert created
    return view.task.task_id


def columns(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        return [row[1] for row in connection.execute("PRAGMA table_info(stage_timings)")]


def committed(path: Path, task_id: str) -> tuple[str, str, str | None, str | None]:
    """Task and purchase, the outbox's order and the order the purchase completed with."""
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT t.state, p.stage, (SELECT order_id FROM dispatch_outbox), "
            "(SELECT order_id FROM completion_results WHERE run_id = t.run_id) FROM hnp_tasks t "
            "JOIN purchases p ON p.run_id = t.run_id WHERE t.task_id = ?",
            (task_id,),
        ).fetchone()


def decisions(path: Path, run_id: str) -> list[tuple[str, str, str]]:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        return [
            (item.decision.phase, item.decision.purchase_mode, item.decision.outcome)
            for item in SqliteGovernanceEvidenceStore(connection).for_run(run_id)
            if isinstance(item, DecisionRecord)
        ]


def test_without_the_flag_nothing_is_assembled(tmp_path: Path) -> None:
    built = runtime(hnp_settings(tmp_path, agent_hnp_enabled=False), Ap2Merchant(MERCHANT))
    try:
        assert (built.delegations, built.hnp_executor, built.owned_hnp_chooser) == (None,) * 3
        assert built.purchases._hnp_d1_gate is None
    finally:
        built.close()
    assert columns(tmp_path / "agent.db") == HP_COLUMNS


def test_b0_hnp_runs_a_consented_task_to_its_purchase(tmp_path: Path) -> None:
    merchant, psp = HnpMerchant(MERCHANT), Psp()
    built = runtime(hnp_settings(tmp_path), merchant, psp)
    try:
        executor = built.hnp_executor
        assert isinstance(executor, HnpExecutionService)
        assert isinstance(executor._dispatcher, HnpDispatcher)
        assert isinstance(executor._gate, NullHnpPolicyGate)
        assert isinstance(executor._selector, DeterministicHnpSelector)
        assert built.owned_hnp_chooser is None and built.purchases._hnp_d1_gate is None
        step = executor.run(consented(built))
    finally:
        built.close()

    assert step.status == "closed", step
    assert step.execution is not None
    assert committed(tmp_path / "agent.db", step.execution.task.task_id) == (
        "purchased",
        "completed",
        HNP_ORDER_ID,
        HNP_ORDER_ID,
    )
    assert merchant.creations() == 1 and len(psp.delegations) == len(merchant.completions) == 1
    # The PSP client the runtime builds names the merchant `payment_request_digest` fixes.
    [(_, body)] = psp.delegations
    assert body["allowance"]["merchant_id"] == MERCHANT_ID
    assert columns(tmp_path / "agent.db") == [*HP_COLUMNS, "input_tokens", "output_tokens"]


def test_b2_hnp_runs_a_consented_task_to_its_purchase(tmp_path: Path) -> None:
    merchant, psp = HnpMerchant(MERCHANT), Psp()
    built = runtime(governed_hnp(tmp_path), merchant, psp)
    try:
        executor = built.hnp_executor
        assert isinstance(executor, HnpExecutionService)
        assert isinstance(executor._gate, SqliteHnpPolicyGate)
        assert isinstance(built.purchases._hnp_d1_gate, SqliteD1EnforceGate)
        assert executor._freshness is not None and executor._d1_denials is not None
        step = executor.run(consented(built))
    finally:
        built.close()

    assert step.status == "closed", step
    assert step.execution is not None and step.execution.task.run_id is not None
    assert committed(tmp_path / "agent.db", step.execution.task.task_id) == (
        "purchased",
        "completed",
        HNP_ORDER_ID,
        HNP_ORDER_ID,
    )
    assert merchant.creations() == 1 and len(psp.delegations) == len(merchant.completions) == 1
    assert decisions(tmp_path / "agent.db", step.execution.task.run_id) == [
        ("D1", "HNP", "allow"),
        ("D2", "HNP", "allow"),
        ("D3", "HNP", "allow"),
    ]


def test_the_llm_selector_needs_the_model_settings(tmp_path: Path) -> None:
    unset = hnp_settings(tmp_path, agent_hnp_selector="llm", anthropic_api_key=None)
    with pytest.raises(RuntimeError, match="AGENT_HNP_SELECTOR=llm"):
        runtime(unset, Ap2Merchant(MERCHANT))

    settings = hnp_settings(
        tmp_path,
        agent_hnp_selector="llm",
        anthropic_api_key=SecretStr("test-key-never-sent"),
        agent_model="test-model",
    )
    built = runtime(settings, Ap2Merchant(MERCHANT))
    try:
        assert built.hnp_executor is not None
        assert isinstance(built.hnp_executor._selector, ModelHnpSelector)
        assert isinstance(built.owned_hnp_chooser, ClaudeDelegatedChooser)
    finally:
        built.close()


# K12 by condition (§3.3): the grant's shortlist in B0-HNP, D1-HNP's POL-01 in B2-HNP


def feed_with_another_candidate(tmp_path: Path) -> Path:
    """The fixture feed plus SON-02, a second candidate that the grant (SON-01 only) leaves out."""
    feed = tmp_path / "feed"
    shutil.copytree(FEED, feed)
    line = (FEED / "products.jsonl").read_text().splitlines()[0]
    with (feed / "products.jsonl").open("a") as products:
        products.write(line.replace("SON-01", "SON-02").replace("son-01", "son-02") + "\n")
    return feed


class OutsideTheGrant:
    """A selector that answers the candidate the grant left out of its shortlist."""

    def choose(self, shortlist, intent, preference_text) -> str:
        assert [item.candidate.variant_id for item in shortlist] == ["SON-01"]
        return "SON-02"


def d1_records(path: Path, run_id: str) -> list[DecisionRecord]:
    with closing(sqlite3.connect(path)) as connection:
        table = "SELECT 1 FROM sqlite_master WHERE name = 'governance_evidence'"
        if connection.execute(table).fetchone() is None:
            return []  # B0-HNP never creates the governance evidence store
        connection.execute("PRAGMA foreign_keys=ON")
        return [
            item
            for item in SqliteGovernanceEvidenceStore(connection).for_run(run_id)
            if isinstance(item, DecisionRecord)
        ]


def purchase_of(path: Path, run_id: str) -> tuple[str, str | None] | None:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT stage, failure_code FROM purchases WHERE run_id = ?", (run_id,)
        ).fetchone()


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_a_choice_outside_the_grant_is_never_bought(tmp_path: Path, mode: str) -> None:
    base = governed_hnp(tmp_path) if mode == "enforce" else hnp_settings(tmp_path)
    settings = base.model_copy(update={"agent_feed_path": feed_with_another_candidate(tmp_path)})
    merchant = Ap2Merchant(MERCHANT)
    built = runtime(settings, merchant)
    try:
        executor = built.hnp_executor
        assert executor is not None
        executor._selector = OutsideTheGrant()
        step = executor.run(consented(built))
    finally:
        built.close()

    assert step.status == "closed" and step.execution is not None
    task, path = step.execution.task, settings.agent_database_path
    assert (task.state, task.outcome_code) == ("failed", "selection_not_eligible")
    assert task.run_id is not None
    assert merchant.creations() == 0
    if mode == "off":
        # B0-HNP: the executor's guard, before `start`: no purchase and no decision.
        assert purchase_of(path, task.run_id) is None
        assert d1_records(path, task.run_id) == []
        return
    # B2-HNP: D1-HNP denies the choice with POL-01 and keeps its evidence, before any POST.
    [record] = d1_records(path, task.run_id)
    assert (record.decision.phase, record.decision.outcome) == ("D1", "deny")
    assert record.decision.reason_codes == ("SELECTION_NOT_ELIGIBLE",)
    assert record.policy_input().eligible_variant_ids == ("SON-01",)
    assert purchase_of(path, task.run_id) == ("failed", "selection_not_eligible")


# The intent reviewer (docs/design/hnp-intent-review.md §9)

MODEL = {"anthropic_api_key": SecretStr("test-key-never-sent"), "agent_model": "test-model"}
NO_MODEL = {"anthropic_api_key": None}


def test_the_reviewer_is_off_by_default(tmp_path: Path) -> None:
    built = runtime(governed_hnp(tmp_path), Ap2Merchant(MERCHANT))
    try:
        assert built.hnp_executor is not None and built.hnp_executor._reviewer is None
        assert built.owned_hnp_reviewer is None
    finally:
        built.close()


def test_the_llm_reviewer_is_assembled_in_b2_hnp(tmp_path: Path) -> None:
    built = runtime(
        governed_hnp(tmp_path, agent_hnp_reviewer="llm", **MODEL), Ap2Merchant(MERCHANT)
    )
    try:
        assert built.hnp_executor is not None
        reviewer = built.hnp_executor._reviewer
        assert isinstance(reviewer, ModelHnpIntentReviewer)
        assert built.owned_hnp_reviewer is reviewer
        assert (reviewer.prompt_id, reviewer.model) == ("hnp-intent-review-v1", "test-model")
    finally:
        built.close()


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (
            lambda path: governed_hnp(path, agent_hnp_reviewer="llm", **MODEL).model_copy(
                update={"agent_hnp_enabled": False}
            ),
            "AGENT_HNP_REVIEWER=llm requires AGENT_HNP_ENABLED=true",
        ),
        (
            lambda path: hnp_settings(path, agent_hnp_reviewer="llm", **MODEL),
            "AGENT_HNP_REVIEWER=llm requires AGENT_GOVERNANCE_MODE=enforce",
        ),
        (
            lambda path: governed_hnp(path, agent_hnp_reviewer="llm", **NO_MODEL),
            "AGENT_HNP_REVIEWER=llm requires ANTHROPIC_API_KEY",
        ),
    ],
    ids=["hnp-off", "b0-hnp", "no-model"],
)
def test_the_llm_reviewer_needs_b2_hnp_and_the_model(
    tmp_path: Path, settings: Any, message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        runtime(settings(tmp_path), Ap2Merchant(MERCHANT))
    # Startup stopped before any table or client existed.
    assert not (tmp_path / "agent.db").exists()


REVIEW_CODES = ("'choice_reviewed'", "'choice_not_confirmed'")


def legacy_ledger(path: Path, codes: tuple[str, ...] = REVIEW_CODES) -> None:
    """A ledger created before some codes: its CHECKs lack them (by default, the reviewer's event
    and escalation)."""
    with closing(sqlite3.connect(path)) as connection, connection:
        for statement in delegation_sqlite._SCHEMA:
            for code in codes:
                statement = statement.replace(f",{code}", "").replace(f"{code},", "")
            connection.execute(statement)


def test_a_ledger_without_the_review_codes_is_refused_with_the_reviewer(tmp_path: Path) -> None:
    path = tmp_path / "agent.db"
    legacy_ledger(path)
    assert not SqliteDelegationLedger(path).admits_choice_review()

    with pytest.raises(RuntimeError, match="AGENT_DATABASE_PATH"):
        runtime(governed_hnp(tmp_path, agent_hnp_reviewer="llm", **MODEL), Ap2Merchant(MERCHANT))
    # Off, the same database still starts: nothing writes the new codes.
    runtime(governed_hnp(tmp_path), Ap2Merchant(MERCHANT)).close()


def test_a_new_ledger_admits_the_review(tmp_path: Path) -> None:
    ledger = SqliteDelegationLedger(tmp_path / "agent.db")
    ledger.initialize()
    assert ledger.admits_choice_review()


# A selector that makes no choice (§3.3, §8.1)


def test_a_ledger_without_selection_unavailable_is_refused_with_the_llm_selector(
    tmp_path: Path,
) -> None:
    """Only the model selector can fail in practice: with it, startup refuses a ledger whose CHECK
    would reject the terminal halfway through a run."""
    path = tmp_path / "agent.db"
    legacy_ledger(path, ("'selection_unavailable'",))
    assert not SqliteDelegationLedger(path).admits_selection_unavailable()

    with pytest.raises(RuntimeError, match="AGENT_DATABASE_PATH"):
        runtime(hnp_settings(tmp_path, agent_hnp_selector="llm", **MODEL), Ap2Merchant(MERCHANT))
    # The deterministic selector always chooses: the same database still starts.
    runtime(hnp_settings(tmp_path), Ap2Merchant(MERCHANT)).close()


def test_a_new_ledger_admits_selection_unavailable(tmp_path: Path) -> None:
    ledger = SqliteDelegationLedger(tmp_path / "agent.db")
    ledger.initialize()
    assert ledger.admits_selection_unavailable()


class Unreachable:
    """A selector whose model cannot be reached."""

    def choose(self, shortlist, intent, preference_text) -> str:
        raise ConnectionError("the model is unreachable")


def test_b2_hnp_escalates_a_selector_outage_without_effects(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    merchant = Ap2Merchant(MERCHANT)
    settings = governed_hnp(tmp_path)
    built = runtime(settings, merchant)
    try:
        executor = built.hnp_executor
        assert executor is not None
        executor._selector = Unreachable()
        with caplog.at_level(logging.ERROR, logger="commerce_agent.hnp.alerts"):
            step = executor.run(consented(built))
    finally:
        built.close()

    assert step.status == "closed" and step.execution is not None
    task, grant, path = step.execution.task, step.execution.grant, settings.agent_database_path
    assert (task.state, task.outcome_code) == ("escalated", "selection_unavailable")
    assert (grant.state, grant.revoked_reason) == ("revoked", "escalated")
    assert task.run_id is not None
    assert merchant.creations() == 0
    assert d1_records(path, task.run_id) == []
    assert purchase_of(path, task.run_id) is None
    assert [r for r in caplog.records if r.name == "commerce_agent.hnp.alerts"] == []


class NotConfirming:
    prompt_id, model = "hnp-intent-review-v1", "test-model"

    def review(self, shortlist, intent, preference_text, chosen_variant_id) -> IntentReview:
        return IntentReview("not_confirmed", "outside_request")


def test_b2_hnp_escalates_a_choice_not_confirmed_before_d1_and_any_post(tmp_path: Path) -> None:
    merchant = Ap2Merchant(MERCHANT)
    settings = governed_hnp(tmp_path, agent_hnp_reviewer="llm", **MODEL)
    built = runtime(settings, merchant)
    try:
        executor = built.hnp_executor
        assert executor is not None
        executor._reviewer = NotConfirming()  # the assembled model reviewer is never called
        step = executor.run(consented(built))
    finally:
        built.close()

    assert step.status == "closed" and step.execution is not None
    task, path = step.execution.task, settings.agent_database_path
    assert (task.state, task.outcome_code) == ("escalated", "choice_not_confirmed")
    assert task.run_id is not None
    assert merchant.creations() == 0
    assert d1_records(path, task.run_id) == []
    assert purchase_of(path, task.run_id) == ("canceled", None)


def test_health_reports_the_intent_reviewer(tmp_path: Path) -> None:
    app = create_app(
        settings=governed_hnp(tmp_path, agent_hnp_reviewer="llm", **MODEL),
        transport=httpx.MockTransport(Ap2Merchant(MERCHANT)),
    )
    with TestClient(app) as client:
        health = client.get("/health").json()

    assert (health["governance_mode"], health["hnp_enabled"], health["hnp_reviewer"]) == (
        "enforce",
        True,
        "llm",
    )
