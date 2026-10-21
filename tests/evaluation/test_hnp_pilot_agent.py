"""HN11's agent: the scripted selector injected by code, on the runtime the agent builds.

Real keys, real SQLite and the AP2 merchant double, as tests/delegation/test_hnp_runtime.py.
"""

from pathlib import Path

import pytest

from commerce_agent.delegation import bootstrap
from commerce_agent.domain import PurchaseIntent
from commerce_evaluation.hnp_pilot_agent import ScriptedHnpSelector, scripted_selector
from tests.delegation.test_hnp_runtime import consented, governed_hnp, runtime
from tests.governance.test_stale_terms_pending import Ap2Merchant
from tests.test_delegation_api import MERCHANT, hnp_settings

VARIANT = "BBOnePlus11-256GB"


def test_the_scripted_selector_returns_its_variant_whatever_the_shortlist() -> None:
    selector = ScriptedHnpSelector(VARIANT)
    assert selector.choose((), PurchaseIntent(), None) == VARIANT


@pytest.mark.parametrize("settings", [hnp_settings, governed_hnp], ids=["B0-HNP", "B2-HNP"])
def test_the_injected_selector_reaches_the_executor_and_is_never_bought(
    tmp_path: Path, settings: object
) -> None:
    original = bootstrap.build_hnp_selector
    merchant = Ap2Merchant(MERCHANT)
    with scripted_selector(VARIANT):
        built = runtime(settings(tmp_path), merchant)  # type: ignore[operator]
    assert bootstrap.build_hnp_selector is original
    try:
        assert built.hnp_executor is not None
        assert isinstance(built.hnp_executor._selector, ScriptedHnpSelector)
        assert built.owned_hnp_chooser is None
        step = built.hnp_executor.run(consented(built))
    finally:
        built.close()
    assert step.execution is not None
    assert (step.execution.task.state, step.execution.task.outcome_code) == (
        "failed",
        "selection_not_eligible",
    )
    assert merchant.creations() == 0


def test_outside_the_block_the_runtime_keeps_its_own_selector(tmp_path: Path) -> None:
    with scripted_selector(VARIANT):
        pass
    built = runtime(hnp_settings(tmp_path), Ap2Merchant(MERCHANT))
    try:
        assert not isinstance(
            built.hnp_executor and built.hnp_executor._selector, ScriptedHnpSelector
        )
    finally:
        built.close()
