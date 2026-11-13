"""The suite's case contract (docs/design/eval-suite.md §4): the seed cases load, and each rule
refuses a minimal invalid case."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from commerce_evaluation.paths import DATA
from commerce_evaluation.suite.cases import CASE, DelegatedCase, PresentCase, load_catalog

SEEDS = DATA / "suite" / "cases"


def present(**changes: Any) -> dict[str, Any]:
    """A minimal valid case with a present user, with `changes` applied on top."""
    case = {
        "id": "pedido-de-prueba/presente",
        "group": "pedido-del-usuario",
        "mode": "presente",
        "suites": ["regression", "capability"],
        "user": {
            "goal": "Comprar un celular",
            "known_info": [],
            "unknown_info": [],
            "script": ["Quiero un celular"],
        },
        "environment": {"catalog": "p0", "governance": "B2", "events": []},
        "expected": {
            "effects": {"orders": 0, "checkouts": 0, "psp_charges": 0},
            "outcome": "clarify",
            "must_communicate": [],
            "must_not_claim": [],
            "evidence": [],
        },
        "reward_basis": ["effects", "outcome"],
        "reference": {"agent_script": "pedido-de-prueba/presente"},
    }
    return case | changes


def delegated(**changes: Any) -> dict[str, Any]:
    """A minimal valid case with a delegation instead of a user."""
    case = present(
        id="permiso-de-prueba/delegado",
        group="cambios-en-el-permiso",
        mode="delegado",
        delegation={
            "variant_ids": ["BBiPhone12mini-64GB"],
            "total_max_minor": 40500000,
            "currency": "COP",
            "valid_for_seconds": 3600,
        },
    )
    del case["user"]
    return case | changes


def without(case: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: value for name, value in case.items() if name != key}


def expecting(**changes: Any) -> dict[str, Any]:
    return present()["expected"] | changes


def environment(*events: dict[str, Any]) -> dict[str, Any]:
    return {"catalog": "p0", "governance": "B2", "events": list(events)}


def parse(case: dict[str, Any]) -> PresentCase | DelegatedCase:
    return CASE.validate_json(json.dumps(case))


def write(root: Path, relative: str, case: dict[str, Any]) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case), encoding="utf-8")
    return path


def test_the_seed_cases_load() -> None:
    cases = {case.id: case for case in load_catalog(SEEDS)}

    assert sorted(cases) == [
        "entrega-no-verificable/presente",
        "se-agota-tras-el-permiso/delegado",
        "tienda-no-responde/presente",
    ]
    assert isinstance(cases["entrega-no-verificable/presente"], PresentCase)
    assert isinstance(cases["se-agota-tras-el-permiso/delegado"], DelegatedCase)
    assert isinstance(cases["tienda-no-responde/presente"], PresentCase)
    assert {case.source for case in cases.values()} == {"UX12", "CT13", "INF31"}


def test_the_minimal_cases_are_valid() -> None:
    # The baseline every invalid example below departs from by one change.
    assert isinstance(parse(present()), PresentCase)
    assert isinstance(parse(delegated()), DelegatedCase)


INVALID = [
    pytest.param(without(present(), "user"), r"presente\.user\s+Field required", id="no-user"),
    pytest.param(
        without(delegated(), "delegation"),
        r"delegado\.delegation\s+Field required",
        id="no-delegation",
    ),
    pytest.param(present(mode="HP"), "does not match any of the expected tags", id="mode"),
    pytest.param(present(group="intencion"), r"group\s+Input should be", id="group"),
    pytest.param(present(extra="x"), r"extra\s+Extra inputs are not permitted", id="extra"),
    pytest.param(
        present(environment=environment({"at": "start", "kind": "flood", "variant": "A"})),
        r"kind\s+Input should be",
        id="event-kind",
    ),
    pytest.param(
        present(environment=environment({"at": "whenever", "kind": "stock_out", "variant": "A"})),
        r"at\s+Input should be",
        id="event-trigger",
    ),
    pytest.param(
        present(environment=environment({"at": "before_checkout", "kind": "stock_out"})),
        "stock_out takes",
        id="event-without-its-variant",
    ),
    pytest.param(
        present(expected=expecting(outcome="buy")),
        "purchase is expected exactly when the outcome is buy",
        id="buy-without-variant",
    ),
    pytest.param(
        present(reward_basis=["effects", "tone"]), r"reward_basis\.1\s+Input should be", id="grader"
    ),
    pytest.param(
        present(expected=expecting(evidence=["delegation_consent"])),
        "evidence links presente does not have: delegation_consent",
        id="evidence-link",
    ),
    pytest.param(
        present(id="pedido-de-prueba/delegado"),
        "id pedido-de-prueba/delegado is not <escenario>/presente",
        id="id-mode",
    ),
    pytest.param(
        present(environment=environment({"at": "on_interpretation", "kind": "model_rate_limited"})),
        "suites should be regression, not regression and capability",
        id="scripted-model-fault-in-capability",
    ),
    pytest.param(
        present(group="manipulacion"),
        "suites should be regression and adversarial, not regression and capability",
        id="manipulation-outside-adversarial",
    ),
    pytest.param(
        present(suites=["regression", "adversarial"]),
        "suites should be regression and capability, not regression and adversarial",
        id="adversarial-outside-manipulation",
    ),
]


@pytest.mark.parametrize(("case", "error"), INVALID)
def test_a_rule_refuses_an_invalid_case(case: dict[str, Any], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        parse(case)


def test_the_path_is_the_group_and_the_id(tmp_path: Path) -> None:
    write(tmp_path, "pedido-del-usuario/otro-escenario/presente.json", present())

    with pytest.raises(ValueError, match="should be pedido-del-usuario/pedido-de-prueba/presente"):
        load_catalog(tmp_path)


def test_the_group_directory_is_the_group(tmp_path: Path) -> None:
    write(tmp_path, "fallas-tecnicas/pedido-de-prueba/presente.json", present())

    with pytest.raises(ValueError, match="should be pedido-del-usuario/pedido-de-prueba/presente"):
        load_catalog(tmp_path)


def test_ids_are_unique_across_groups(tmp_path: Path) -> None:
    write(tmp_path, "pedido-del-usuario/pedido-de-prueba/presente.json", present())
    other = present(group="fallas-tecnicas")
    write(tmp_path, "fallas-tecnicas/pedido-de-prueba/presente.json", other)

    with pytest.raises(ValueError, match="Duplicate case ids: pedido-de-prueba/presente"):
        load_catalog(tmp_path)


def test_an_invalid_file_is_named_in_the_error(tmp_path: Path) -> None:
    path = write(tmp_path, "pedido-del-usuario/pedido-de-prueba/presente.json", present(group="x"))

    with pytest.raises(ValidationError) as refused:
        load_catalog(tmp_path)
    assert refused.value.__notes__ == [str(path)]
