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


# B2-PILOT-v1 has no SHA256SUMS: b2-pilot-v1-report.md publishes these digests for its frozen files.
B2_PILOT_V1 = {
    "b0prime-b2-20261020T201055Z.jsonl": (
        "bdcffae3a9bb82586d0a4be12edb5784e8508853e3f5fc107be3a3015169a21c"
    ),
    "b0prime-b2-20261020T201055Z.summary.json": (
        "d2a4b0f4d3214530be799ea8870be7204542ed59c65d1110fd2d4a678bc7db18"
    ),
}


def test_the_b2_pilot_v1_files_keep_their_published_digests() -> None:
    digests = {
        name: hashlib.sha256((RESULTS / name).read_bytes()).hexdigest() for name in B2_PILOT_V1
    }

    assert digests == B2_PILOT_V1
