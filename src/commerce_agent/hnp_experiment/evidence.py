"""Synthetic allowlisted JSONL evidence, flushed before fictitious effects."""

import json
import os
from pathlib import Path
from typing import Literal

from .models import Digest, ExperimentalModel, Id, Minor, ReviewRecord, digest


class EffectReceipt(ExperimentalModel):
    event_kind: Literal["simulated_effect"] = "simulated_effect"
    proposal_id: Id
    record_digest: Digest
    effect_kind: Literal["fictitious_only"] = "fictitious_only"


class EvidenceEntry(ExperimentalModel):
    sequence: Minor
    previous_digest: Digest | None
    payload: ReviewRecord | EffectReceipt
    entry_digest: Digest


class ExperimentalEvidence:
    def __init__(self, directory: Path | None = None):
        self.entries: list[EvidenceEntry] = []
        self.path = None if directory is None else directory / "evidence.jsonl"
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Never silently overwrite or append to another experiment.
            with self.path.open("x", encoding="utf-8"):
                pass

    def append(self, payload: ReviewRecord | EffectReceipt) -> str:
        body = dict(
            schema_version="hnp-review/0.1",
            sequence=len(self.entries) + 1,
            previous_digest=self.entries[-1].entry_digest if self.entries else None,
            payload=payload.model_dump(mode="json"),
        )
        value = digest("hnp-evidence-entry/v1", body)
        entry = EvidenceEntry.model_validate_json(json.dumps({**body, "entry_digest": value}))
        if self.path is not None:
            from commerce_agent.governance.canonical import canonical_bytes

            with self.path.open("ab") as stream:
                stream.write(canonical_bytes(entry) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        self.entries.append(entry)
        return value


def verify_entries(entries: list[EvidenceEntry]) -> None:
    previous = None
    admitted: dict[str, ReviewRecord] = {}
    receipts: set[str] = set()
    for sequence, entry in enumerate(entries, 1):
        body = entry.model_dump(mode="json", exclude={"entry_digest"})
        if (
            entry.sequence != sequence
            or entry.previous_digest != previous
            or entry.entry_digest != digest("hnp-evidence-entry/v1", body)
        ):
            raise ValueError("Evidence chain mismatch")
        previous = entry.entry_digest
        if isinstance(entry.payload, ReviewRecord):
            if entry.payload.admission.status == "simulated_admitted":
                admitted[entry.entry_digest] = entry.payload
        else:
            receipt = entry.payload
            record = admitted.get(receipt.record_digest)
            if (
                record is None
                or record.proposal is None
                or record.proposal.proposal_id != receipt.proposal_id
                or receipt.record_digest in receipts
            ):
                raise ValueError("Unbound or duplicate simulated effect")
            receipts.add(receipt.record_digest)


def load_entries(path: Path) -> list[EvidenceEntry]:
    from commerce_agent.governance.canonical import parse_model

    entries = [parse_model(EvidenceEntry, line) for line in path.read_bytes().splitlines()]
    verify_entries(entries)
    return entries
