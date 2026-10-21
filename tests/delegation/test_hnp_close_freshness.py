"""Freshness at `close` (docs/design/hnp-governed.md §3.3, step 5): the guard HP uses, real HTTP
to the lab merchant double and real evidence; the executor reads only its raise."""

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from commerce_agent.acp import ACPClient, CheckoutSession
from commerce_agent.application.offer_freshness import OfferFreshnessGuard
from commerce_agent.approval import ApprovalError
from commerce_agent.persistence.hp_evidence_sqlite import SqliteHpEvidenceStore
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
