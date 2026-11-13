"""Evidence: can we rebuild why the agent acted? (eval-suite.md §2).

`verify_evidence.verify_database` checks every purchase (`presente`) or task (`delegado`) of the
trial's agent database, and the runner keeps its documents in the record (`verified`): this
grader reads them and opens no database. Every chain must be complete, and one of them must
have each link the case expects `ok`.
"""

import json
from pathlib import Path

from pydantic import JsonValue

from commerce_evaluation.suite.cases import Case
from commerce_evaluation.suite.graders.grade import Grade, Verdict
from commerce_evaluation.suite.trial import Row, TrialRecord
from commerce_evaluation.verify_evidence import verify_database

BROKEN = ("missing", "mismatch")


def verified(agent_db: Path, psp_db: Path | None = None) -> tuple[Row, ...]:
    """What the runner stores in `TrialRecord.evidence`: verify_database's documents, as JSON.
    Every one of the database's, unfiltered: a trial has a database of its own, and in
    `presente` a run_id names one purchase, not the conversation."""
    return tuple(json.loads(json.dumps(verify_database(agent_db, psp_database=psp_db))))


def evidence(case: Case, record: TrialRecord) -> Grade:
    chains = [_statuses(document) for document in record.evidence]
    if any(chain is None for chain in chains):
        return _grade(
            "unknown", "Un documento no es de verify_database: le faltan links o complete"
        )
    statuses = [chain for chain in chains if chain is not None]
    broken = [
        _broken(document, chain)
        for document, chain in zip(record.evidence, statuses, strict=True)
        if document["complete"] is not True
    ]
    if broken:
        return _grade("fail", "; ".join(broken))
    expected = case.expected.evidence
    if not expected:
        return _grade("pass", f"{len(statuses)} cadenas completas, y el caso no espera eslabones")
    if not statuses:
        return _grade("fail", f"No quedó ninguna cadena; se esperaba ok en {', '.join(expected)}")
    short = min(
        ([name for name in expected if chain.get(name) != "ok"] for chain in statuses), key=len
    )
    if short:
        return _grade("fail", f"Ninguna cadena tiene ok en {', '.join(short)}")
    return _grade("pass", f"Cadena completa, con ok en {', '.join(expected)}")


def _statuses(document: Row) -> dict[str, JsonValue] | None:
    """Each link's status (None for a link the document lacks), or None when the document is
    not one of verify_database's."""
    links = document.get("links")
    if not isinstance(links, dict) or not isinstance(document.get("complete"), bool):
        return None
    if not all(isinstance(link, dict) for link in links.values()):
        return None
    return {name: link.get("status") for name, link in links.items() if isinstance(link, dict)}


def _broken(document: Row, statuses: dict[str, JsonValue]) -> str:
    # What broke it: a link missing or changed, else an effect whose links did not all apply.
    bad = [f"{name} {status}" for name, status in statuses.items() if status in BROKEN] or [
        f"{name} {status}" for name, status in statuses.items() if status != "ok"
    ]
    named = document.get("task_id") or document.get("run_id")
    return f"La cadena de {named} no está completa ({', '.join(bad)})"


def _grade(verdict: Verdict, reason: str) -> Grade:
    return Grade(grader="evidence", verdict=verdict, reason=reason)
