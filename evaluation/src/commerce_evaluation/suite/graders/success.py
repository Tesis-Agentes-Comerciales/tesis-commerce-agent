"""Strict success: a trial passes only if every grader of its reward basis passes (§2).

Any `fail` fails it; otherwise any `unknown` leaves it unknown, which never counts as a pass.
"""

from collections.abc import Callable, Iterable

from commerce_evaluation.suite.cases import Case, Grader
from commerce_evaluation.suite.graders.claims import claims
from commerce_evaluation.suite.graders.effects import effects
from commerce_evaluation.suite.graders.evidence import evidence
from commerce_evaluation.suite.graders.grade import Grade, Verdict
from commerce_evaluation.suite.graders.outcome import outcome
from commerce_evaluation.suite.trial import TrialRecord

GRADERS: dict[Grader, Callable[[Case, TrialRecord], Grade]] = {
    "effects": effects,
    "outcome": outcome,
    "evidence": evidence,
    "claims": claims,
}


def grade(case: Case, record: TrialRecord) -> tuple[Grade, ...]:
    """The grades of the case's reward basis, in its order."""
    if record.case_id != case.id:
        raise ValueError(f"The record of {record.case_id} was paired with {case.id}")
    return tuple(GRADERS[name](case, record) for name in case.reward_basis)


def strict_success(case: Case, grades: Iterable[Grade]) -> Verdict:
    """One grade per grader of the reward basis; grades of other graders do not count."""
    verdicts: dict[Grader, Verdict] = {}
    for given in grades:
        if given.grader in verdicts:
            raise ValueError(f"Two {given.grader} grades for one trial")
        verdicts[given.grader] = given.verdict
    if missing := [name for name in case.reward_basis if name not in verdicts]:
        raise ValueError(f"No grade from {', '.join(missing)}")
    counted = {verdicts[name] for name in case.reward_basis}
    if "fail" in counted:
        return "fail"
    return "unknown" if "unknown" in counted else "pass"
