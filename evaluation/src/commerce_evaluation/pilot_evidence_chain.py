"""H2: whether a completed purchase has its whole evidence chain, as `verify-evidence` reports it.

The verifier is P2's (coordination decision 4). The pilot only invokes it and reads one status per
link: `ok`, `missing`, `mismatch` or `not_applicable`. The command and the report shape live here,
so aligning them with P2's final contract (amendment E1) touches this module only.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from commerce_evaluation.paths import ROOT

STATUSES = frozenset({"ok", "missing", "mismatch", "not_applicable"})
# The link the merchant's order_create webhook completes; it arrives after /complete returns.
ORDER_EVENT_LINK = "order_webhook"

type CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class EvidenceChainError(RuntimeError):
    pass


def verifier_command(database: Path, run_id: str) -> list[str]:
    return ["uv", "run", "verify-evidence", "--database", str(database), "--run-id", run_id]


def link_statuses(report: Mapping[str, Any]) -> dict[str, str]:
    """`{link: status}` from `{"links": {name: status}}` or a list of `{link|name, status}`."""
    links = report.get("links")
    if isinstance(links, Mapping):
        pairs = [(str(name), value) for name, value in links.items()]
    elif isinstance(links, list):
        pairs = [(str(item.get("link", item.get("name"))), item.get("status")) for item in links]
    else:
        raise EvidenceChainError("The verifier report has no links")
    statuses = {name: status for name, status in pairs}
    unknown = {name for name, status in statuses.items() if status not in STATUSES}
    if unknown:
        raise EvidenceChainError(f"Links with an unknown status: {', '.join(sorted(unknown))}")
    return statuses


def chain_complete(statuses: Mapping[str, str] | None, links: Sequence[str]) -> int | None:
    """1 if every pre-registered link is ok; a link the verifier did not report is missing."""
    if statuses is None:
        return None
    return int(all(statuses.get(link) == "ok" for link in links))


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class EvidenceChainCheck:
    """The pre-registered links and how to ask the verifier about one purchase."""

    links: tuple[str, ...]
    run: CommandRunner = field(default=_run, repr=False)
    order_event_timeout_seconds: float = 15.0
    poll_seconds: float = 0.5

    def verify(self, database: Path, run_id: str) -> dict[str, str]:
        """Link statuses of the purchase. A non-zero exit still counts if it printed a report:
        an incomplete chain is a result, not a failure of the verifier."""
        result = self.run(verifier_command(database, run_id))
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            detail = result.stderr.strip()[-500:]
            raise EvidenceChainError(f"verify-evidence printed no report: {detail}") from None
        if not isinstance(report, dict):
            raise EvidenceChainError("verify-evidence printed a non-object report")
        return link_statuses(report)

    def await_order_event(self, database: Path, run_id: str) -> int:
        """Milliseconds until the webhook link stops being missing, bounded by the timeout."""
        started = time.monotonic()
        deadline = started + self.order_event_timeout_seconds
        while True:
            try:
                arrived = self.verify(database, run_id).get(ORDER_EVENT_LINK) != "missing"
            except EvidenceChainError:
                arrived = False
            if arrived or time.monotonic() >= deadline:
                return round((time.monotonic() - started) * 1000)
            time.sleep(self.poll_seconds)


def evidence_chain_check(manifest: Mapping[str, Any]) -> EvidenceChainCheck | None:
    chain = manifest.get("evidence_chain")
    return None if chain is None else EvidenceChainCheck(tuple(chain["links"]))
