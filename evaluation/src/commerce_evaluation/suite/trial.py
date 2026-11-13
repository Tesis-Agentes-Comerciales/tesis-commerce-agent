"""The record of one trial: what the graders judge (docs/design/eval-suite.md §4).

The runner that fills it, `run_trial`, comes with PR 6.
"""

from typing import Annotated, Literal

from pydantic import Field, JsonValue

from commerce_agent.governance.models import Minor
from commerce_evaluation.suite.cases import CaseId, Suite, SuiteModel, Text

Row = dict[str, JsonValue]


class Agent(SuiteModel):
    """The agent under test, and the `model` each of its responses reported (§9)."""

    model: Text
    effort: Text | None
    response_models: tuple[Text, ...]


class Turn(SuiteModel):
    role: Literal["user", "assistant"]
    text: str


class FinalState(SuiteModel):
    """Rows read from the Merchant and the PSP after the trial; the effects grader counts them."""

    orders: tuple[Row, ...]
    checkouts: tuple[Row, ...]
    psp_charges: tuple[Row, ...]


class Usage(SuiteModel):
    """Tokens per role, as each response reported them, and what the trial cost."""

    agent: dict[str, Minor]
    user: dict[str, Minor]
    judge: dict[str, Minor]
    usd: Annotated[float, Field(ge=0)]


class TrialRecord(SuiteModel):
    case_id: CaseId
    suite: Suite
    # 1 to k: the trial's number among the case's k trials.
    trial: Annotated[int, Field(ge=1)]
    seed: int
    agent: Agent
    transcript: tuple[Turn, ...]
    # build_flow_trace's document.
    trace: Row
    final_state: FinalState
    usage: Usage
    # Why the stack failed (it did not start, or the API answered 5xx three times); such a trial
    # is repeated and never counts against the agent.
    infra_failure: Text | None
