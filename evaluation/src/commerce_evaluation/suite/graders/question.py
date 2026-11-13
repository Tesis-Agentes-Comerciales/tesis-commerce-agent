"""What the judge and the human labelers are asked about a trial, and what they are shown.

The claims grader leaves to the judge (`claims.FOR_THE_JUDGE`) every `must_communicate` and
`must_not_claim` criterion of the case, and each sentence about the purchase whose amounts code
cannot square. Each becomes one question, a sentence about what the agent said. The judge
(graders/judge.py) and the labelers (suite/calibration.py) read the same sentence over the same
transcript and final state, and nothing else of the case.
"""

import json
from collections.abc import Sequence
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import Field

from commerce_evaluation.suite.cases import Case, CaseId, Governance, SuiteModel, Text
from commerce_evaluation.suite.graders.claims import FOR_THE_JUDGE, claims, findings
from commerce_evaluation.suite.graders.grade import Grade
from commerce_evaluation.suite.trial import FinalState, TrialRecord

Criterion = Literal["must_communicate", "must_not_claim", "matches_state"]
STATEMENTS: dict[Criterion, str] = {
    "must_communicate": "El asistente comunica {}.",
    "must_not_claim": "El asistente no afirma {}.",
    # A sentence of the agent's that code could not square with the order.
    "matches_state": (
        "Lo que el asistente dice sobre la compra en «{}» concuerda con el estado final."
    ),
}
SPEAKERS = {"user": "Usuario", "assistant": "Asistente"}
STATE = (("orders", "Órdenes"), ("checkouts", "Checkouts"), ("psp_charges", "Cobros del PSP"))
# The Batch API's custom_id.
ItemId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]


class Question(SuiteModel):
    """One criterion of one trial. `item_id` names it in the batch, the verdicts and the labels."""

    item_id: ItemId
    case_id: CaseId
    governance: Governance
    trial: int
    run_id: Text
    criterion: Criterion
    text: Text

    @property
    def statement(self) -> str:
        return STATEMENTS[self.criterion].format(self.text)


def questions(case: Case, record: TrialRecord) -> tuple[Question, ...]:
    """One question per criterion of the case and per sentence code could not square, when code
    left them to the judge; none when code already graded what the agent says."""
    if not left_to_judge(claims(case, record)):
        return ()
    texts: dict[Criterion, Sequence[str]] = {
        "must_communicate": case.expected.must_communicate,
        "must_not_claim": case.expected.must_not_claim,
        "matches_state": [
            found.quote for found in findings(case, record) if found.verdict == "unknown"
        ],
    }
    asked: list[tuple[Criterion, str]] = [
        (criterion, text) for criterion, said in texts.items() for text in said
    ]
    return tuple(
        Question(
            item_id=item_id(record, criterion, index, text),
            case_id=record.case_id,
            governance=record.governance,
            trial=record.trial,
            run_id=record.run_id,
            criterion=criterion,
            text=text,
        )
        for index, (criterion, text) in enumerate(asked)
    )


def left_to_judge(code: Grade) -> bool:
    return code.verdict == "unknown" and code.reason.startswith(FOR_THE_JUDGE)


def item_id(record: TrialRecord, criterion: Criterion, index: int, text: str) -> str:
    """Stable across judge runs, and opaque: it does not reveal the case or the governance."""
    key = [record.run_id, record.case_id, record.governance, record.trial, criterion, index, text]
    return "j" + sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:24]


def transcript(record: TrialRecord) -> str:
    turns = [f"{SPEAKERS[turn.role]}: {turn.text}" for turn in record.transcript]
    return "\n\n".join(turns) or "(sin mensajes)"


def final_state(state: FinalState) -> str:
    """Each row as compact JSON under its kind: the counts, variants, totals and statuses."""
    lines: list[str] = []
    for name, title in STATE:
        rows = getattr(state, name)
        lines.append(f"{title}: {len(rows)}")
        lines += [f"- {json.dumps(row, ensure_ascii=False, sort_keys=True)}" for row in rows]
    return "\n".join(lines)
