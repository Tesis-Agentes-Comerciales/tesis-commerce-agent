"""The agent process of a scripted scenario, with its selector injected by code.

Spec §6: the scripted selector is injected by code from `commerce_evaluation`, never by an
environment variable. The HNP pilot starts one of these factories instead of
`commerce_agent.api:app_factory`: `app_factory` for HN11 (the v1 pilot), and `scenario.<id>` for
each scripted scenario of a matrix, one variant per scenario (HR02 and HR03 of the review pilot).
Inside that process only, it replaces the selector the runtime assembles at startup
(`delegation/bootstrap.build_hnp_selector`), so the product gains no switch and every other guard
(the shortlist check of K12, D1 POL-01, the intent reviewer) runs as in any episode.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from commerce_agent import runtime
from commerce_agent.api import create_app
from commerce_agent.config import Settings
from commerce_agent.delegation import bootstrap
from commerce_agent.domain import PurchaseIntent, RankedCandidate
from commerce_agent.persistence.stage_timings_sqlite import SqliteStageTimings
from commerce_evaluation.paths import DATA

FACTORY = "commerce_evaluation.hnp_pilot_agent:app_factory"
SEAM = "commerce_agent.delegation.bootstrap.build_hnp_selector"
SCENARIOS = DATA / "hnp-scenarios.json"
REVIEW_SCENARIOS = DATA / "hnp-review-scenarios.json"
# Every matrix whose scripted scenarios `scenario.<id>` can start: the v1 and the review pilot.
MATRICES = (SCENARIOS, REVIEW_SCENARIOS)
# HN11's adversarial selector (v1) and the review pilot's fixed choices (HR02, HR03).
SCRIPTED_KINDS = frozenset({"scripted_adversarial", "scripted"})


class ScriptedHnpSelector:
    """A scripted selector: always the registered variant, whatever the shortlist says."""

    def __init__(self, variant_id: str) -> None:
        self.variant_id = variant_id

    def choose(
        self,
        shortlist: tuple[RankedCandidate, ...],
        intent: PurchaseIntent,
        preference_text: str | None,
    ) -> str:
        return self.variant_id


def scripted_variant(matrix: Mapping[str, Any] | None = None) -> str:
    """The variant the one scripted scenario of the matrix registers (HN11)."""
    scenarios = (matrix or json.loads(SCENARIOS.read_text(encoding="utf-8")))["scenarios"]
    scripted = [
        scenario["selector"]["returns_variant_id"]
        for scenario in scenarios
        if scenario["selector"]["kind"] == "scripted_adversarial"
    ]
    if len(scripted) != 1:
        raise ValueError(f"Expected one scripted selector in the matrix, found {len(scripted)}")
    return str(scripted[0])


def _matrix(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def scripted_variants(*matrices: Mapping[str, Any]) -> dict[str, str]:
    """The variant each scripted scenario registers, by scenario id, over the given matrices (all
    the registered ones by default). A scenario id registered twice is refused."""
    variants: dict[str, str] = {}
    for matrix in matrices or tuple(_matrix(path) for path in MATRICES):
        for item in matrix["scenarios"]:
            if item["selector"]["kind"] not in SCRIPTED_KINDS:
                continue
            if item["id"] in variants:
                raise ValueError(f"Scenario {item['id']} is registered twice")
            variants[str(item["id"])] = str(item["selector"]["returns_variant_id"])
    return variants


def scenario_factory(scenario_id: str) -> str:
    """The uvicorn import string of a scripted scenario's agent (`--factory`)."""
    return f"{FACTORY.partition(':')[0]}:scenario.{scenario_id}"


def _install(variant_id: str) -> None:
    def build_scripted_selector(
        settings: Settings, timings: SqliteStageTimings
    ) -> tuple[ScriptedHnpSelector, None]:
        return ScriptedHnpSelector(variant_id), None

    bootstrap.build_hnp_selector = build_scripted_selector


@contextmanager
def scripted_selector(variant_id: str) -> Iterator[None]:
    """The scripted selector for the runtimes built inside the block (tests)."""
    original = bootstrap.build_hnp_selector
    _install(variant_id)
    try:
        yield
    finally:
        bootstrap.build_hnp_selector = original


def injection_readiness() -> dict[str, Any]:
    """Ready when `build_runtime` resolves the selector from the bootstrap module at startup: a
    module-level import in runtime.py would keep the original and ignore the injection."""
    code = runtime.build_runtime.__code__
    resolved_at_startup = "build_hnp_selector" in code.co_names
    ready = (
        resolved_at_startup
        and "build_hnp_selector" not in vars(runtime)
        and callable(getattr(bootstrap, "build_hnp_selector", None))
    )
    return {"ready": ready, "factory": FACTORY, "seam": SEAM}


def app_factory() -> FastAPI:
    """HN11's agent (the v1 pilot): the one scripted scenario of hnp-scenarios.json."""
    _install(scripted_variant())
    return create_app()


class _ScenarioFactories:
    """`scenario.<id>`: the agent factory of one scripted scenario, with the variant its matrix
    registers. uvicorn resolves `module:scenario.HR02` attribute by attribute; an id that no
    registered matrix scripts is not an attribute, so the agent never starts with a guess."""

    def __getattr__(self, scenario_id: str) -> Callable[[], FastAPI]:
        if scenario_id.startswith("__"):
            raise AttributeError(scenario_id)
        variant_id = scripted_variants().get(scenario_id)
        if variant_id is None:
            raise AttributeError(f"No registered matrix scripts the selector of {scenario_id}")

        def factory() -> FastAPI:
            _install(variant_id)
            return create_app()

        return factory


scenario = _ScenarioFactories()
