"""The suite's catalog (docs/design/eval-suite.md §4): the seed cases load, a file's path is its
group and its id, and no id repeats."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from commerce_evaluation.paths import DATA
from commerce_evaluation.suite.cases import DelegatedCase, PresentCase, load_catalog

SEEDS = DATA / "suite" / "cases"
UX12 = "pedido-del-usuario/entrega-no-verificable/presente.json"


def seed(**changes: Any) -> dict[str, Any]:
    return json.loads((SEEDS / UX12).read_text(encoding="utf-8")) | changes


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


def test_the_seeds_load_from_their_own_directory(tmp_path: Path) -> None:
    # The baseline of the tests below: the same file, in its right place, loads.
    write(tmp_path, UX12, seed())

    assert [case.id for case in load_catalog(tmp_path)] == ["entrega-no-verificable/presente"]


def test_the_path_is_the_group_and_the_id(tmp_path: Path) -> None:
    write(tmp_path, "pedido-del-usuario/otro-escenario/presente.json", seed())

    expected = "should be pedido-del-usuario/entrega-no-verificable/presente"
    with pytest.raises(ValueError, match=expected):
        load_catalog(tmp_path)


def test_the_group_directory_is_the_group(tmp_path: Path) -> None:
    write(tmp_path, "fallas-tecnicas/entrega-no-verificable/presente.json", seed())

    expected = "should be pedido-del-usuario/entrega-no-verificable/presente"
    with pytest.raises(ValueError, match=expected):
        load_catalog(tmp_path)


def test_ids_are_unique_across_groups(tmp_path: Path) -> None:
    write(tmp_path, UX12, seed())
    write(
        tmp_path,
        "fallas-tecnicas/entrega-no-verificable/presente.json",
        seed(group="fallas-tecnicas"),
    )

    with pytest.raises(ValueError, match="Duplicate case ids: entrega-no-verificable/presente"):
        load_catalog(tmp_path)


def test_an_invalid_file_is_named_in_the_error(tmp_path: Path) -> None:
    path = write(tmp_path, UX12, seed(group="x"))

    with pytest.raises(ValidationError) as refused:
        load_catalog(tmp_path)
    assert refused.value.__notes__ == [str(path)]
