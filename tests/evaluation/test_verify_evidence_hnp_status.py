"""`verify-evidence`, HNP: a receipt status that is not even a string (§9.2, `receipts`).

The verifier normalizes with the product's table (`commerce_agent.delegation.models`), which raises
outside the profile; the link must still be a plain `mismatch`, not an exception that the step
runner reports as uninterpretable data.
"""

import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from tests.evaluation.test_verify_evidence_hnp import SCENARIOS, only_run, rechain

# An order's receipt (expected `success`) and a PSP rejection's (expected `error`).
BASES = {"completed": "checkout", "psp_rejected": "payment"}
NOT_A_STRING: list[Any] = [None, 1, True, ["Success"], {"status": "Success"}]
CASES = [(name, kind, status) for name, kind in BASES.items() for status in NOT_A_STRING]


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    paths = {name: SCENARIOS[name](tmp_path_factory.mktemp(name)) for name in BASES}
    for path in paths.values():
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return paths


@pytest.fixture
def scenario(built: dict[str, Path], tmp_path: Path) -> Callable[[str], Path]:
    def copy(name: str) -> Path:
        target = tmp_path / f"{name}.db"
        shutil.copyfile(built[name], target)
        return target

    return copy


@pytest.mark.parametrize(
    ("name", "kind", "status"), CASES, ids=[f"{n}-{k}-{s!r}" for n, k, s in CASES]
)
def test_a_receipt_status_that_is_not_a_string_is_a_mismatch(
    scenario: Callable[[str], Path], name: str, kind: str, status: Any
) -> None:
    database = scenario(name)
    rechain(database, "receipt_recorded", where={"kind": kind}, status=status)

    link = only_run(database)["links"]["receipts"]

    assert (link["status"], link["detail"].startswith("El estado del recibo")) == ("mismatch", True)
