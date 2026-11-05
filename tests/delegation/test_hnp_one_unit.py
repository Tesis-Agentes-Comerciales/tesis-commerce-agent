"""UX10-HNP-control (docs/design/p0-ux08-ux10.md §4.2).

The positive control of UX10-HNP-c: the same benches, with a merchant that answers one unit. The
task reaches the dispatch commitment, and the checkout the Agent Key closes, the one the outbox
commits, has a single line of one unit. B0-HNP runs on the executor's `Bench`; B2-HNP on the real
D1-HNP harness (`tests/delegation/test_hnp_executor_d1.py`), a `Bench` whose purchases are the
runtime's `PurchaseService` with its `hnp_d1_gate`. No model is called.
"""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from commerce_agent.acp.models import CheckoutSession
from commerce_agent.delegation.models import Step
from tests.delegation.test_hnp_executor_d1 import bench as d1_bench
from tests.governance.test_d1_hnp_gate import hnp_service, settings_for
from tests.governance.test_hp_continuation import StoreMerchant
from tests.governance.test_runtime_dispatch import runtime
from tests.support.hnp_executor import AgentKey, Bench, Effects


class RecordingKey(AgentKey):
    """The Agent Key double, keeping the checkout it closes: the one the dispatch commits."""

    def __init__(self) -> None:
        super().__init__()
        self.closed: list[CheckoutSession] = []

    def close(self, open_checkout, open_payment, *, checkout_jwt, checkout, closed_at):
        self.closed.append(checkout)
        return super().close(
            open_checkout,
            open_payment,
            checkout_jwt=checkout_jwt,
            checkout=checkout,
            closed_at=closed_at,
        )


def committed(path: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT checkout_id, state FROM dispatch_outbox").fetchall()


def lines(checkout: CheckoutSession) -> list[tuple[str, int]]:
    return [(line["item"]["id"], line["quantity"]) for line in checkout.line_items]


def one_unit(step: Step, run: Bench, key: RecordingKey, checkout_id: str) -> None:
    assert step.status == "applied"
    assert run.ended() == ("running", None)
    assert run.purchase() == ("completing", None)
    (closed,) = key.closed
    assert closed.id == checkout_id
    assert lines(closed) == [("SON-01", 1)]
    assert committed(run.path) == [(checkout_id, "committed")]


def test_ux10_hnp_control_in_b0_one_unit_reaches_the_dispatch(tmp_path: Path) -> None:
    key = RecordingKey()
    run = Bench(tmp_path, closer=key)

    step = run.run()

    one_unit(step, run, key, "cs_1")
    assert run.effects() == Effects(creates=1, signatures=1, commits=1, cancels=0)


def test_ux10_hnp_control_in_b2_one_unit_reaches_the_dispatch(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    merchant = StoreMerchant()
    built = runtime(settings, merchant)
    key = RecordingKey()
    try:
        run = d1_bench(tmp_path, built, hnp_service(settings, built), closer=key)
        step = run.run()
    finally:
        built.close()

    one_unit(step, run, key, "cs_api_1")
    current: dict[str, Any] = merchant.current
    assert [line["quantity"] for line in current["line_items"]] == [1]
    assert merchant.creations() == 1
    assert merchant.effects() == []
