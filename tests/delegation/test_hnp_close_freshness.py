"""Freshness at `close` (docs/design/hnp-governed.md §3.3, step 5): the guard HP uses, real HTTP
to the lab merchant double and real evidence; the executor reads only its raise."""

import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from commerce_agent.acp import ACPClient, CheckoutSession
from commerce_agent.application.offer_freshness import OfferFreshnessGuard
from commerce_agent.approval import ApprovalError
from commerce_agent.persistence.hp_evidence_sqlite import SqliteHpEvidenceStore
from tests.governance.test_d1_budget_runtime import decision_records
from tests.support.hnp_chain import hnp_chain
from tests.test_offer_freshness import MERCHANT, NOW, LabMerchant

RUN = "run_hnp_close"


def guard(
    tmp_path: Path, merchant: LabMerchant
) -> tuple[OfferFreshnessGuard, SqliteHpEvidenceStore]:
    evidence = SqliteHpEvidenceStore(tmp_path / "agent.db")
    evidence.initialize()
    client = ACPClient(
        MERCHANT, SecretStr("merchant-token"), transport=httpx.MockTransport(merchant)
    )
    return OfferFreshnessGuard(client, evidence, mode="enforce", clock=lambda: NOW), evidence


@pytest.mark.parametrize("moved", [False, True])
def test_close_reads_the_live_offer_and_records_its_stage(tmp_path: Path, moved: bool) -> None:
    merchant = LabMerchant()
    checkout = CheckoutSession.model_validate(merchant.checkout)
    check, evidence = guard(tmp_path, merchant)
    if moved:
        merchant.mutate(price=180_000_000)
        with pytest.raises(ApprovalError) as changed:
            check.check(run_id=RUN, checkout=checkout, stage="close")
        assert changed.value.code == "checkout_terms_changed"
    else:
        check.check(run_id=RUN, checkout=checkout, stage="close")

    (record,) = [item.body for item in evidence.for_run(RUN)]
    assert (record["stage"], record["outcome"], record["decision"]) == (
        "close",
        "changed" if moved else "fresh",
        "block" if moved else "allow",
    )
    assert merchant.offer_reads == 1


def test_an_offer_of_another_id_at_close_fails_the_task_before_d2_and_any_payment(
    tmp_path: Path,
) -> None:
    """P3-V01 at B2-HNP: the whole executor, the real guard and client, the merchant double."""
    with hnp_chain(tmp_path) as chain:
        # The merchant answers the `close` read with another, well-formed offer.
        chain.merchant.offer = {**chain.merchant.offer, "id": "SON-02"}

        assert chain.run().status == "closed"

        execution = chain.ledger.execution(chain.task_id)
        assert execution is not None
        run_id = execution.task.run_id
        assert run_id is not None
        assert (execution.task.state, execution.task.outcome_code) == (
            "failed",
            "governance_unavailable",
        )
        (finished,) = [
            event
            for event in chain.ledger.events(chain.grant_id)
            if event.event_type == "task_finished"
        ]
        assert finished.body["outcome_code"] == "governance_unavailable"
        # No D2/D3-HNP, no closed mandate, no dispatch, no completion and no payment.
        assert [record.decision.phase for record in decision_records(chain.path, run_id)] == ["D1"]
        assert chain.closed_links() == {}
        assert chain.merchant.effects() == []
        with closing(sqlite3.connect(chain.path)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM dispatch_outbox").fetchone() == (0,)
        # The cause is where any unread offer leaves it: the run's `freshness_check`.
        evidence = SqliteHpEvidenceStore(chain.path)
        (record,) = [item.body for item in evidence.for_run(run_id)]
        assert (record["stage"], record["outcome"], record["reason"], record["decision"]) == (
            "close",
            "unavailable",
            "offer_mismatch",
            "block",
        )
        assert evidence.verify_chain(run_id)
