"""Pin the historical HP/HNP evidence in docs/governance-monitor/sources by SHA-256.

Keeps every source verifiable from provenance.json alone, without the monitor's exporter and
without changing provenance.json or sources/.

Covered byte for byte by a SHA-256 in provenance.json:

- agent-received.json, agent-lost.json and http-deny.json: ``sourcePins`` under the same name.
- hnp-evidence.jsonl: ``sourcePins["evidence.jsonl"]``, the 65-entry HNP-01 ledger copied under
  another name.
- hnp02/<run>/summary.json and traces.jsonl: ``hnp02Runs[].sha256``.

hnp-false-accept-selected.json has no file hash: it is one result extracted from the HNP-01
results.json, and only the original results.json is pinned (that file and the HNP-01
summary.json were never versioned). It is covered through the pinned ledger instead: the file
is the canonical JSON of the result, its ``record`` is a ledger payload and digests to
``record_digest``, ``effective_admission`` equals the record's admission, and no
``simulated_effect`` receipt points at it, as ``effect_occurred: false`` says. Not covered:
``delivery_code``, ``schema_version`` and ``timings`` outside the record.

Any other file under sources/ fails the test until provenance.json pins it.
"""

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "governance-monitor"
SELECTED = "hnp-false-accept-selected.json"
LEDGER = "hnp-evidence.jsonl"
# sourcePins name -> versioned copy under sources/.
PINNED_COPIES = {
    "agent-received.json": "agent-received.json",
    "agent-lost.json": "agent-lost.json",
    "http-deny.json": "http-deny.json",
    "evidence.jsonl": LEDGER,
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def domain_digest(domain: str, value: Any) -> str:
    return "sha256:" + hashlib.sha256(domain.encode("ascii") + b"\0" + canonical(value)).hexdigest()


def pinned_hashes(evidence: Path) -> dict[str, str]:
    provenance = json.loads((evidence / "provenance.json").read_bytes())
    pins = {copy: provenance["sourcePins"][name] for name, copy in PINNED_COPIES.items()}
    for run in provenance["hnp02Runs"]:
        pins |= {f"hnp02/{path}": sha256 for path, sha256 in run["sha256"].items()}
    return pins


def assert_selected_result_in_ledger(sources: Path) -> None:
    raw = (sources / SELECTED).read_bytes()
    result = json.loads(raw)
    assert raw == canonical(result) + b"\n", f"{SELECTED} is not the canonical JSON it was cut as"
    record = result["record"]
    digest = domain_digest("hnp-review-record/v1", record)
    assert result["record_digest"] == digest, f"{SELECTED} record does not match its digest"
    assert result["effective_admission"] == record["admission"], f"{SELECTED} admission changed"

    entries = [json.loads(line) for line in (sources / LEDGER).read_bytes().splitlines()]
    bound = [entry["entry_digest"] for entry in entries if entry["payload"] == record]
    assert len(bound) == 1, f"{SELECTED} is not a record of the pinned ledger"
    receipts = {
        entry["payload"]["record_digest"]
        for entry in entries
        if entry["payload"].get("event_kind") == "simulated_effect"
    }
    assert result["effect_occurred"] == (bound[0] in receipts), f"{SELECTED} effect changed"


def assert_sources_match_provenance(evidence: Path) -> None:
    sources = evidence / "sources"
    pins = pinned_hashes(evidence)
    files = {path.relative_to(sources).as_posix() for path in sources.rglob("*") if path.is_file()}
    assert files == pins.keys() | {SELECTED}, "sources/ and provenance.json list different files"
    for name, expected in pins.items():
        actual = hashlib.sha256((sources / name).read_bytes()).hexdigest()
        assert actual == expected, f"{name} does not match its SHA-256 in provenance.json"
    assert_selected_result_in_ledger(sources)


def copy_evidence(target: Path) -> Path:
    shutil.copy2(EVIDENCE / "provenance.json", target / "provenance.json")
    shutil.copytree(EVIDENCE / "sources", target / "sources")
    return target


SOURCE_FILES = sorted(
    path.relative_to(EVIDENCE / "sources").as_posix()
    for path in (EVIDENCE / "sources").rglob("*")
    if path.is_file()
)


def test_versioned_sources_match_provenance() -> None:
    assert_sources_match_provenance(EVIDENCE)


@pytest.mark.parametrize("name", SOURCE_FILES)
def test_one_changed_byte_in_a_source_is_detected(tmp_path: Path, name: str) -> None:
    source = copy_evidence(tmp_path) / "sources" / name
    content = bytearray(source.read_bytes())
    content[len(content) // 2] ^= 0x01
    source.write_bytes(bytes(content))

    with pytest.raises((AssertionError, ValueError)):
        assert_sources_match_provenance(tmp_path)


def test_a_source_without_a_pin_is_detected(tmp_path: Path) -> None:
    (copy_evidence(tmp_path) / "sources" / "unpinned.json").write_bytes(b"{}\n")

    with pytest.raises(AssertionError, match="list different files"):
        assert_sources_match_provenance(tmp_path)
