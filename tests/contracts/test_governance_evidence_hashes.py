"""Pin the historical HP/HNP evidence in docs/governance-monitor/sources by SHA-256.

Keeps every source verifiable from provenance.json alone, without the monitor's exporter and
without changing provenance.json or sources/.

provenance.json is itself pinned by ``PROVENANCE_SHA256``, so a source cannot be edited by also
rewriting its hash there.

Covered byte for byte by a SHA-256 in provenance.json:

- agent-received.json, agent-lost.json and http-deny.json: ``sourcePins`` under the same name.
- hnp-evidence.jsonl: ``sourcePins["evidence.jsonl"]``, the 65-entry HNP-01 ledger copied under
  another name.
- hnp02/<run>/summary.json and traces.jsonl: ``hnp02Runs[].sha256``.

hnp-false-accept-selected.json has no file hash: it is one result extracted from the HNP-01
results.json, and only the original results.json is pinned (that file and the HNP-01
summary.json were never versioned). It is covered through the pinned ledger instead:

- the file is the canonical JSON of the result and has exactly its seven keys;
- ``record`` is a payload of the ledger and digests to ``record_digest``;
- ``effective_admission`` equals the record's admission;
- ``effect_occurred`` is false because no ``simulated_effect`` receipt points at the record;
- ``delivery_code`` is NONE, the only code the HNP-01 DemoResult allows without an effect and
  with the record's own admission.

Not covered: the top-level ``schema_version`` and ``timings`` (that run's per-component
timings), which no pinned file records.

Any other file under sources/, except Finder's .DS_Store, fails the test until provenance.json
pins it.
"""

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "governance-monitor"
PROVENANCE_SHA256 = "5a5703c025592fd82958f96321d9415b9c6c52a25f296207a28a6c8b59fd8dc0"
SELECTED = "hnp-false-accept-selected.json"
SELECTED_KEYS = {
    "delivery_code",
    "effect_occurred",
    "effective_admission",
    "record",
    "record_digest",
    "schema_version",
    "timings",
}
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


def source_files(sources: Path) -> set[str]:
    # macOS Finder drops .DS_Store into folders it opens; it is never evidence.
    return {
        path.relative_to(sources).as_posix()
        for path in sources.rglob("*")
        if path.is_file() and path.name != ".DS_Store"
    }


def pinned_hashes(provenance: dict[str, Any]) -> dict[str, str]:
    pins = {copy: provenance["sourcePins"][name] for name, copy in PINNED_COPIES.items()}
    for run in provenance["hnp02Runs"]:
        pins |= {f"hnp02/{path}": sha256 for path, sha256 in run["sha256"].items()}
    return pins


def assert_selected_result_in_ledger(sources: Path) -> None:
    raw = (sources / SELECTED).read_bytes()
    result = json.loads(raw)
    assert raw == canonical(result) + b"\n", f"{SELECTED} is not the canonical JSON it was cut as"
    assert result.keys() == SELECTED_KEYS, f"{SELECTED} has unexpected keys"
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
    effect = bound[0] in receipts
    assert result["effect_occurred"] == effect, f"{SELECTED} effect does not match the ledger"
    # DemoResult: only EMITTED and RECEIPT_UNAVAILABLE carry an effect, and the other two failure
    # codes replace the admission checked above, so a result without an effect says NONE.
    delivery = {"EMITTED", "RECEIPT_UNAVAILABLE"} if effect else {"NONE"}
    assert result["delivery_code"] in delivery, f"{SELECTED} delivery_code contradicts the effect"


def assert_sources_match_provenance(evidence: Path) -> None:
    raw = (evidence / "provenance.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PROVENANCE_SHA256, "provenance.json changed"
    pins = pinned_hashes(json.loads(raw))
    sources = evidence / "sources"
    files = source_files(sources)
    assert files == pins.keys() | {SELECTED}, "sources/ and provenance.json list different files"
    for name, expected in pins.items():
        actual = hashlib.sha256((sources / name).read_bytes()).hexdigest()
        assert actual == expected, f"{name} does not match its SHA-256 in provenance.json"
    assert_selected_result_in_ledger(sources)


def copy_evidence(target: Path) -> Path:
    shutil.copy2(EVIDENCE / "provenance.json", target / "provenance.json")
    shutil.copytree(EVIDENCE / "sources", target / "sources")
    return target


SOURCE_FILES = sorted(source_files(EVIDENCE / "sources"))


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


def test_finder_metadata_is_not_a_source(tmp_path: Path) -> None:
    sources = copy_evidence(tmp_path) / "sources"
    (sources / ".DS_Store").write_bytes(b"\0\0\0\1Bud1")
    (sources / "hnp02" / ".DS_Store").write_bytes(b"\0\0\0\1Bud1")

    assert_sources_match_provenance(tmp_path)


def test_a_source_rehashed_in_provenance_is_detected(tmp_path: Path) -> None:
    evidence = copy_evidence(tmp_path)
    source = evidence / "sources" / "agent-received.json"
    source.write_bytes(source.read_bytes().replace(b'"scenario"', b'"scenari0"', 1))
    provenance = json.loads((evidence / "provenance.json").read_bytes())
    provenance["sourcePins"]["agent-received.json"] = hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    (evidence / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")

    with pytest.raises(AssertionError, match=r"provenance\.json changed"):
        assert_sources_match_provenance(evidence)


def rewrite(change: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], bytes]:
    def rewritten(result: dict[str, Any]) -> bytes:
        change(result)
        return canonical(result) + b"\n"

    return rewritten


def reject_keeping_digest(result: dict[str, Any]) -> None:
    result["record"]["assessment"]["verdict"] = "reject"


def reject_with_new_digest(result: dict[str, Any]) -> None:
    reject_keeping_digest(result)
    result["record_digest"] = domain_digest("hnp-review-record/v1", result["record"])


def indented(result: dict[str, Any]) -> bytes:
    return json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2).encode() + b"\n"


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        pytest.param(indented, "not the canonical JSON", id="indented"),
        pytest.param(rewrite(lambda r: r.update(approved=True)), "unexpected keys", id="key-added"),
        pytest.param(
            rewrite(lambda r: r.update(timing=r.pop("timings"))),
            "unexpected keys",
            id="key-renamed",
        ),
        pytest.param(
            rewrite(reject_keeping_digest), "does not match its digest", id="verdict-old-digest"
        ),
        pytest.param(
            rewrite(lambda r: r["effective_admission"].update(code="ADMITTED")),
            "admission changed",
            id="admission",
        ),
        pytest.param(
            rewrite(reject_with_new_digest),
            "not a record of the pinned ledger",
            id="verdict-new-digest",
        ),
        pytest.param(
            rewrite(lambda r: r.update(effect_occurred=True)),
            "effect does not match the ledger",
            id="effect",
        ),
        pytest.param(
            rewrite(lambda r: r.update(delivery_code="EMITTED")),
            "delivery_code contradicts the effect",
            id="delivery",
        ),
    ],
)
def test_a_changed_selected_result_is_detected(
    tmp_path: Path, tamper: Callable[[dict[str, Any]], bytes], message: str
) -> None:
    selected = copy_evidence(tmp_path) / "sources" / SELECTED
    selected.write_bytes(tamper(json.loads(selected.read_bytes())))

    with pytest.raises(AssertionError, match=message):
        assert_selected_result_in_ledger(selected.parent)
