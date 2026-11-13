"""The archived results of the October pilots keep their bytes (results/README.md).

Their runners are reproduced from the `pilotos-octubre` tag; the results stay in main as thesis
evidence, and each directory's SHA256SUMS lists every file but itself with its digest.
"""

import hashlib

import pytest

from commerce_evaluation.paths import RESULTS


@pytest.mark.parametrize("name", ["b2-pilot-v2", "hnp-review-pilot-v1", "exp06-review"])
def test_the_ledger_lists_every_file_but_itself_with_its_digest(name: str) -> None:
    directory = RESULTS / name
    lines = (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    ledger = {file: digest for digest, file in (line.split("  ", 1) for line in lines if line)}
    files = sorted(p.name for p in directory.iterdir() if p.is_file() and p.name != "SHA256SUMS")

    assert sorted(ledger) == files
    for file, digest in ledger.items():
        assert hashlib.sha256((directory / file).read_bytes()).hexdigest() == digest, file
