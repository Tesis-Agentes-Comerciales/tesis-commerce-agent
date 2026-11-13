"""One grader's verdict on one trial (docs/design/eval-suite.md §4)."""

from typing import Literal

from commerce_evaluation.suite.cases import Grader, SuiteModel, Text

# `unknown` goes to human review and never counts as a pass.
Verdict = Literal["pass", "fail", "unknown"]


class Grade(SuiteModel):
    grader: Grader
    verdict: Verdict
    reason: Text
    # The agent's own words the verdict rests on, when there are any.
    quote: Text | None = None
