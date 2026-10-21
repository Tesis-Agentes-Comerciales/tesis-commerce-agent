"""D1-HNP names its principal, as D2-HNP and D3-HNP do (docs/design/hnp-governed.md §7, §9.1).

The pre-registered `attribution_completeness` (evaluation/data/hnp-pilot-v1.json, metrics) counts
the `delegation_events` rows with `actor_kind`, `actor_ref` and `on_behalf_of`, and the
`governance_evidence` rows with `principal_ref` and `owner_ref`. Without the executor in the
D1-HNP input, no B2-HNP chain could reach the registered 100 %. D1-HP keeps its bytes: its input
has no principal, and an HP input that carries one is invalid.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from commerce_agent.application import PurchaseServiceError
from commerce_agent.delegation.models import EXECUTOR_REF
from commerce_agent.governance.canonical import canonical_bytes
from commerce_agent.governance.evaluator import GovernanceError, parse_input
from commerce_agent.governance.evidence import DecisionRecord
from commerce_agent.governance.hnp_policy import GRANT_ACTIONS, HnpGrant, executor_principal
from commerce_agent.persistence.governance_sqlite import SqliteGovernanceEvidenceStore
from tests.governance.test_d1_budget import policy_input
from tests.governance.test_d1_budget_runtime import decision_records
from tests.governance.test_d1_hnp import hnp_input
from tests.governance.test_d1_hnp_gate import GRANT, hnp_service, settings_for, start
from tests.governance.test_hp_continuation import StoreMerchant
from tests.governance.test_runtime_dispatch import runtime
from tests.support.hnp_chain import hnp_chain

EXECUTOR = {
    "schema_version": "commerce-policy/1.0",
    "principal_ref": EXECUTOR_REF,
    "authentication_basis": "service_and_grant",
    "authentication_verified": True,
    "permitted_actions": ["prepare_checkout", "complete_purchase", "commit_dispatch"],
}


def stored_input(record: DecisionRecord) -> dict[str, Any]:
    """The canonical input exactly as the ledger keeps it (what the pilot reads)."""
    return json.loads(record.canonical_input_bytes)


def episode_owner(path: Path, run_id: str) -> str:
    with closing(sqlite3.connect(path)) as connection:
        (scope,) = connection.execute(
            "SELECT scope FROM governance_episodes WHERE run_id=?", (run_id,)
        ).fetchone()
    return json.loads(scope)["owner_ref"]


def test_the_executor_principal_is_the_one_of_d2_and_d3() -> None:
    """One builder for D1, D2 and D3 (§7, «Hechos del executor»)."""
    assert GRANT_ACTIONS == ("prepare_checkout", "complete_purchase", "commit_dispatch")
    assert executor_principal(owns_run=True) == EXECUTOR | {"owns_run": True}
    assert executor_principal(owns_run=False) == EXECUTOR | {"owns_run": False}


@pytest.mark.parametrize(
    ("grant", "total", "outcome", "reasons"),
    [
        pytest.param(GRANT, 80_000, "allow", ("ALL_APPLICABLE_POLICIES_SATISFIED",), id="allow"),
        pytest.param(
            HnpGrant(grant_id=GRANT.grant_id, variant_ids=("SON-07",)),
            80_000,
            "deny",
            ("SELECTION_NOT_ELIGIBLE",),
            id="pol01-deny",
        ),
        pytest.param(GRANT, 71_999, "deny", ("BUDGET_NOT_SATISFIABLE",), id="pol11-deny"),
    ],
)
def test_a_d1_hnp_decision_records_the_executor_and_the_owner(
    tmp_path: Path, grant: HnpGrant, total: int, outcome: str, reasons: tuple[str, ...]
) -> None:
    settings = settings_for(tmp_path)
    built = runtime(settings, StoreMerchant())
    try:
        if outcome == "allow":
            start(hnp_service(settings, built), grant=grant, total=total)
        else:
            with pytest.raises(PurchaseServiceError):
                start(hnp_service(settings, built), grant=grant, total=total)
    finally:
        built.close()

    (record,) = decision_records(settings.agent_database_path)
    assert (record.decision.purchase_mode, record.decision.outcome) == ("HNP", outcome)
    assert record.decision.reason_codes == reasons
    stored = stored_input(record)
    # This bench has no delegation ledger, so nothing shows that the grant's owner owns the run.
    assert stored["principal"] == EXECUTOR | {"owns_run": False}
    assert stored["owner_ref"] == episode_owner(settings.agent_database_path, "run-real")
    assert stored["owner_ref"].startswith("owner:")
    # The principal is part of the input digest, and the replay reproduces the decision.
    with closing(sqlite3.connect(settings.agent_database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        replayed = SqliteGovernanceEvidenceStore(connection).replay(record.evidence_id)
    assert (replayed.outcome, replayed.reason_codes) == (outcome, reasons)


def _run_id(path: Path, task_id: str) -> str:
    with closing(sqlite3.connect(path)) as connection:
        (run_id,) = connection.execute(
            "SELECT run_id FROM hnp_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    return run_id


def attribution_completeness(path: Path, grant_id: str, run_id: str) -> float:
    """The pre-registered metric over one execution, as hnp-pilot-v1.json defines it."""
    with closing(sqlite3.connect(path)) as connection:
        events = connection.execute(
            "SELECT actor_kind, actor_ref, on_behalf_of FROM delegation_events WHERE grant_id=?",
            (grant_id,),
        ).fetchall()
        evidence = connection.execute(
            "SELECT canonical_input FROM governance_evidence WHERE run_id=?", (run_id,)
        ).fetchall()
    attributed = sum(all(value not in (None, "") for value in row) for row in events)
    for (canonical,) in evidence:
        facts = json.loads(canonical) if canonical is not None else {}
        attributed += bool(facts.get("owner_ref")) and bool(
            (facts.get("principal") or {}).get("principal_ref")
        )
    return attributed / (len(events) + len(evidence))


def test_a_b2_hnp_chain_attributes_every_decision_to_the_executor_for_the_owner(
    tmp_path: Path,
) -> None:
    with hnp_chain(tmp_path) as chain:
        assert chain.run().status == "applied"
        run_id = _run_id(chain.path, chain.task_id)
        records = decision_records(chain.path, run_id)
        completeness = attribution_completeness(chain.path, chain.grant_id, run_id)

    assert [(r.decision.phase, r.decision.outcome) for r in records] == [
        ("D1", "allow"),
        ("D2", "allow"),
        ("D3", "allow"),
    ]
    # The executor acts for the grant's owner, who owns the run: the same facts at D1, D2 and D3.
    principals = [stored_input(record)["principal"] for record in records]
    assert principals == [EXECUTOR | {"owns_run": True}] * 3
    owners = {stored_input(record)["owner_ref"] for record in records}
    assert owners == {episode_owner(chain.path, run_id)}
    assert completeness == 1.0


def test_the_hp_input_has_no_principal_and_refuses_one() -> None:
    data, _ = policy_input()
    hp = json.loads(canonical_bytes(data))
    assert data.principal is None and "principal" not in hp

    with pytest.raises(GovernanceError) as refused:
        parse_input(canonical_bytes(hp | {"principal": EXECUTOR | {"owns_run": True}}))

    assert refused.value.reason == "GOV_INPUT_INVALID"


def test_an_hnp_input_recorded_without_its_principal_still_parses() -> None:
    """D1-HNP evidence written before the principal keeps its bytes and stays verifiable."""
    data, _ = hnp_input()
    assert data.principal is None
    assert "principal" not in json.loads(canonical_bytes(data))

    named = parse_input(
        canonical_bytes(
            json.loads(canonical_bytes(data)) | {"principal": EXECUTOR | {"owns_run": True}}
        )
    )
    assert named.principal is not None and named.principal.principal_ref == EXECUTOR_REF
