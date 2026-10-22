"""The scripted scenarios' agents (HN11, HR02, HR03): the selector injected by code, on the
runtime the agent builds.

Real keys, real SQLite and the AP2 merchant double, as tests/delegation/test_hnp_runtime.py.
"""

from pathlib import Path

import pytest
from uvicorn.importer import ImportFromStringError, import_from_string

from commerce_agent.delegation import bootstrap
from commerce_agent.domain import PurchaseIntent
from commerce_evaluation import hnp_pilot_agent
from commerce_evaluation.hnp_pilot_agent import (
    ScriptedHnpSelector,
    scenario_factory,
    scripted_selector,
    scripted_variants,
)
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


def test_each_scripted_scenario_of_the_registered_matrices_has_its_variant() -> None:
    assert scripted_variants() == {
        "HN11": VARIANT,
        "HR02": "BBOnePlus11-128GB",
        "HR03": "BBGooglePixel8-128GB",
    }
    twice = {
        "scenarios": [{"id": "HR02", "selector": {"kind": "scripted", "returns_variant_id": "x"}}]
    }
    with pytest.raises(ValueError, match="HR02 is registered twice"):
        scripted_variants(twice, twice)


@pytest.mark.parametrize(
    ("scenario_id", "variant"),
    [
        ("HN11", VARIANT),
        ("HR02", "BBOnePlus11-128GB"),
        ("HR03", "BBGooglePixel8-128GB"),
    ],
)
def test_uvicorn_starts_each_scenario_s_factory_with_its_selector(
    monkeypatch: pytest.MonkeyPatch, scenario_id: str, variant: str
) -> None:
    """The import string the runner passes to uvicorn installs that scenario's variant."""
    monkeypatch.setattr(bootstrap, "build_hnp_selector", bootstrap.build_hnp_selector)
    monkeypatch.setattr(hnp_pilot_agent, "create_app", lambda: "app")
    factory = import_from_string(scenario_factory(scenario_id))
    assert factory() == "app"
    selector, chooser = bootstrap.build_hnp_selector(None, None)  # type: ignore[arg-type]
    assert isinstance(selector, ScriptedHnpSelector) and chooser is None
    assert selector.variant_id == variant


def test_a_scenario_no_matrix_scripts_has_no_factory() -> None:
    with pytest.raises(ImportFromStringError):
        import_from_string(scenario_factory("HR01"))
    with pytest.raises(ImportFromStringError):
        import_from_string(scenario_factory("HN01"))


def test_hn11_s_factory_still_installs_its_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "build_hnp_selector", bootstrap.build_hnp_selector)
    monkeypatch.setattr(hnp_pilot_agent, "create_app", lambda: "app")
    assert import_from_string(hnp_pilot_agent.FACTORY)() == "app"
    selector, _ = bootstrap.build_hnp_selector(None, None)  # type: ignore[arg-type]
    assert isinstance(selector, ScriptedHnpSelector) and selector.variant_id == VARIANT
