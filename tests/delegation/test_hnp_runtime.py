"""The HNP executor as the runtime assembles it (docs/design/hnp-governed.md §3.3, §6).

With AGENT_HNP_ENABLED off nothing changes. On, `off` runs B0-HNP and `enforce` runs B2-HNP with
D1-HNP, D2/D3-HNP and the `close` freshness guard. Real keys, real SQLite and the AP2 merchant
double; the `llm` selector is only assembled here, never called.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from commerce_agent.application.hnp_execution import HnpExecutionService, NullHnpPolicyGate
from commerce_agent.application.hnp_selection import DeterministicHnpSelector, ModelHnpSelector
from commerce_agent.config import Settings
from commerce_agent.delegation.service import DelegationConsentRequest, DelegationProposalRequest
from commerce_agent.governance.enforce import SqliteD1EnforceGate
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.hnp_gate import SqliteHnpPolicyGate
from commerce_agent.llm.delegated_choice import OpenAIDelegatedChooser
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from commerce_agent.runtime import AgentRuntime, build_runtime
from tests.governance.test_shadow_integration import configuration
from tests.governance.test_stale_terms_pending import Ap2Merchant
from tests.test_delegation_api import (
    AGENT_PEM,
    MERCHANT,
    OPERATOR_TOKEN,
    PROVIDER_PEM,
    SERVICE_TOKEN,
    hnp_settings,
    proposal_body,
)

OWNER = "buyer-a"
HP_COLUMNS = ["sequence", "stage", "started_at", "elapsed_ms", "outcome"]


def governed_hnp(tmp_path: Path) -> Settings:
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
    )


def runtime(settings: Settings, merchant: Any) -> AgentRuntime:
    return build_runtime(
        settings,
        transport=httpx.MockTransport(merchant),
        payment_transport=httpx.MockTransport(merchant),
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


def committed(path: Path, task_id: str) -> tuple[str, str, int]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT t.state, p.stage, (SELECT COUNT(*) FROM dispatch_outbox) FROM hnp_tasks t "
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


def test_b0_hnp_runs_a_consented_task_to_the_commitment(tmp_path: Path) -> None:
    merchant = Ap2Merchant(MERCHANT)
    built = runtime(hnp_settings(tmp_path), merchant)
    try:
        executor = built.hnp_executor
        assert isinstance(executor, HnpExecutionService)
        assert isinstance(executor._gate, NullHnpPolicyGate)
        assert isinstance(executor._selector, DeterministicHnpSelector)
        assert built.owned_hnp_chooser is None and built.purchases._hnp_d1_gate is None
        step = executor.run(consented(built))
    finally:
        built.close()

    assert step.status == "applied", step
    assert step.execution is not None
    assert committed(tmp_path / "agent.db", step.execution.task.task_id) == (
        "running",
        "completing",
        1,
    )
    assert merchant.creations() == 1
    assert columns(tmp_path / "agent.db") == [*HP_COLUMNS, "input_tokens", "output_tokens"]


def test_b2_hnp_runs_a_consented_task_to_the_commitment(tmp_path: Path) -> None:
    merchant = Ap2Merchant(MERCHANT)
    built = runtime(governed_hnp(tmp_path), merchant)
    try:
        executor = built.hnp_executor
        assert isinstance(executor, HnpExecutionService)
        assert isinstance(executor._gate, SqliteHnpPolicyGate)
        assert isinstance(built.purchases._hnp_d1_gate, SqliteD1EnforceGate)
        assert executor._freshness is not None and executor._d1_denials is not None
        step = executor.run(consented(built))
    finally:
        built.close()

    assert step.status == "applied", step
    assert step.execution is not None and step.execution.task.run_id is not None
    assert committed(tmp_path / "agent.db", step.execution.task.task_id) == (
        "running",
        "completing",
        1,
    )
    assert merchant.creations() == 1
    assert decisions(tmp_path / "agent.db", step.execution.task.run_id) == [
        ("D1", "HNP", "allow"),
        ("D2", "HNP", "allow"),
        ("D3", "HNP", "allow"),
    ]


def test_the_llm_selector_needs_the_model_settings(tmp_path: Path) -> None:
    unset = hnp_settings(tmp_path, agent_hnp_selector="llm", openai_api_key=None, openai_model=None)
    with pytest.raises(RuntimeError, match="AGENT_HNP_SELECTOR=llm"):
        runtime(unset, Ap2Merchant(MERCHANT))

    settings = hnp_settings(
        tmp_path,
        agent_hnp_selector="llm",
        openai_api_key=SecretStr("test-key-never-sent"),
        openai_model="test-model",
    )
    built = runtime(settings, Ap2Merchant(MERCHANT))
    try:
        assert built.hnp_executor is not None
        assert isinstance(built.hnp_executor._selector, ModelHnpSelector)
        assert isinstance(built.owned_hnp_chooser, OpenAIDelegatedChooser)
    finally:
        built.close()
