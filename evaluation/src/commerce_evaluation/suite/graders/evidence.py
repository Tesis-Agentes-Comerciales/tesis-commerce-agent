"""Evidence: can we rebuild why the agent acted? (eval-suite.md §2).

`verify_evidence.verify_database` checks every purchase (`presente`) or task (`delegado`) of the
trial's agent database, and the runner keeps its documents in the record (`verified`): this
grader reads them and opens no database. Every chain must be complete, and one of them must
have each link the case expects `ok`.

Under B0 there is no governance, so the governance links (`GOVERNANCE_LINKS`) go missing by
construction: there a chain is complete when no other link is `missing` or `mismatch` (and, in
`delegado`, its ledger chain is `ok`). B2, the system under test, keeps the verifier's rule.
"""

import json
from pathlib import Path

from pydantic import JsonValue

from commerce_evaluation.suite.cases import Case
from commerce_evaluation.suite.graders.grade import Grade, Verdict
from commerce_evaluation.suite.trial import Row, TrialRecord
from commerce_evaluation.verify_evidence import verify_database

BROKEN = ("missing", "mismatch")
# The links B0 never records: without governance there is no intent, D1, D2 or D3 evidence, no
# AP2 approval, mandates or receipts in presente, and the links that chain onto them (the
# checkout's creation and freshness) go missing too.
GOVERNANCE_LINKS = frozenset(
    {
        "intent",
        "d1_decision",
        "checkout_created",
        "freshness",
        "d2_decision",
        "d3_decision",
        "approval",
        "mandates",
        "receipts",
    }
)


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
    ungoverned = record.governance == "B0"
    broken = [
        _broken(document, chain, ungoverned)
        for document, chain in zip(record.evidence, statuses, strict=True)
        if not _complete(document, chain, ungoverned)
    ]
    # What a B0 pass rests on, in its reason.
    scope = " sin los eslabones de gobernanza (B0)" if ungoverned else ""
    if broken:
        return _grade("fail", "; ".join(broken))
    expected = case.expected.evidence
    if not expected:
        return _grade(
            "pass", f"{len(statuses)} cadenas completas{scope}, y el caso no espera eslabones"
        )
    if not statuses:
        return _grade("fail", f"No quedó ninguna cadena; se esperaba ok en {', '.join(expected)}")
    short = min(
        ([name for name in expected if chain.get(name) != "ok"] for chain in statuses), key=len
    )
    if short:
        return _grade("fail", f"Ninguna cadena tiene ok en {', '.join(short)}")
    return _grade("pass", f"Cadena completa{scope}, con ok en {', '.join(expected)}")


def _complete(document: Row, statuses: dict[str, JsonValue], ungoverned: bool) -> bool:
    """The verifier's `complete`; under B0, no link outside the governance links broken."""
    if document["complete"] is True:
        return True
    if not ungoverned:
        return False
    ledger = document.get("ledger_chain")
    if isinstance(ledger, dict) and ledger.get("status") != "ok":
        return False
    return not any(
        status in BROKEN for name, status in statuses.items() if name not in GOVERNANCE_LINKS
    )


def _statuses(document: Row) -> dict[str, JsonValue] | None:
    """Each link's status (None for a link the document lacks), or None when the document is
    not one of verify_database's."""
    links = document.get("links")
    if not isinstance(links, dict) or not isinstance(document.get("complete"), bool):
        return None
    if not all(isinstance(link, dict) for link in links.values()):
        return None
    return {name: link.get("status") for name, link in links.items() if isinstance(link, dict)}


def _broken(document: Row, statuses: dict[str, JsonValue], ungoverned: bool) -> str:
    # What broke it: a link missing or changed, else an effect whose links did not all apply.
    # Under B0 the governance links are set aside, so they never name the break.
    named = document.get("task_id") or document.get("run_id")
    counted = {
        name: status
        for name, status in statuses.items()
        if not (ungoverned and name in GOVERNANCE_LINKS)
    }
    bad = [f"{name} {status}" for name, status in counted.items() if status in BROKEN] or [
        f"{name} {status}" for name, status in counted.items() if status != "ok"
    ]
    ledger = document.get("ledger_chain")
    if not bad and isinstance(ledger, dict):
        # Every link held: what broke is the delegado's ledger chain.
        bad = [f"ledger_chain {ledger.get('status')}"]
    scope = " sin los eslabones de gobernanza (B0)" if ungoverned else ""
    return f"La cadena de {named} no está completa{scope} ({', '.join(bad)})"


def _grade(verdict: Verdict, reason: str) -> Grade:
    return Grade(grader="evidence", verdict=verdict, reason=reason)
