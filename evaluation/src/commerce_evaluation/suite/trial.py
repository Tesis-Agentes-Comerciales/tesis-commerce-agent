"""The record of one trial: what the graders judge (docs/design/eval-suite.md §4).

The runner that fills it, `run_trial`, comes with PR 6.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from commerce_agent.delegation.contract import OutcomeCode, TaskState
from commerce_agent.governance.models import Id, Minor
from commerce_agent.llm.model_failures import ModelFailureKind
from commerce_evaluation.suite.cases import CaseId, Governance, Suite, SuiteModel, Text

Row = dict[str, JsonValue]
# ConversationTurnResponse.action: what a presente turn ends with.
TurnAction = Literal["respond", "clarify", "purchase"]


class Agent(SuiteModel):
    """The agent under test, and the `model` each of its responses reported (§9)."""

    model: Text
    effort: Text | None
    response_models: tuple[Text, ...]


class Turn(SuiteModel):
    role: Literal["user", "assistant"]
    text: str


class Ending(SuiteModel):
    """How the agent ended the trial, for the outcome grader: the last turn's action in presente;
    the task's `hnp_tasks.state` and `outcome_code` in delegado."""

    action: TurnAction | None = None
    task_state: TaskState | None = None
    outcome_code: OutcomeCode | None = None

    @model_validator(mode="after")
    def _one_mode(self) -> Self:
        if self.action is not None and self.task_state is not None:
            raise ValueError("A trial ends with a turn action or a task state, not both")
        if self.outcome_code is not None and self.task_state is None:
            raise ValueError("An outcome code belongs to a task state")
        return self


class FinalState(SuiteModel):
    """Rows read from the Merchant and the PSP after the trial; the effects grader counts them.

    Checkouts and orders name their `variant_id`. An order also carries `total_minor` (COP minor
    units, shipping included) and `items_total_minor` (the products alone). `psp_charges` are
    the PSP's `approved` payments only."""

    orders: tuple[Row, ...]
    checkouts: tuple[Row, ...]
    psp_charges: tuple[Row, ...]


class Usage(SuiteModel):
    """Tokens per role, as each response reported them, and what the trial cost."""

    agent: dict[str, Minor]
    user: dict[str, Minor]
    judge: dict[str, Minor]
    usd: Annotated[float, Field(ge=0)]


class InfraFailure(SuiteModel):
    """Why the stack failed (§2); such a trial is repeated and never counts against the agent.
    The cause tells a stack that never started from an API that answered 5xx three times, which
    in a case that injects a network, PSP or model fault is the case's doing
    (reduce.is_infra_failure)."""

    cause: Literal["stack_not_started", "api_5xx"]
    detail: Text


# The kinds of model failure that count against the agent (§4): the provider refused our own
# request (a 4xx other than 429), or the answer did not fit the contract. The rest are the
# infrastructure's: `provider_error`, `unreachable`, `rate_limited` and `quota_exhausted`.
AGENT_FAILURES: frozenset[ModelFailureKind] = frozenset({"rejected", "invalid_output"})


class ModelFailure(SuiteModel):
    """The failure of the agent's model in this trial, by the product's own kind
    (src/commerce_agent/llm/model_failures.py), and the decision it carries: `rejected` and
    `invalid_output` are the agent's, graded and never repeated; any other kind is the
    infrastructure's, in `infra_failure`."""

    kind: ModelFailureKind
    counts_against: Literal["agent", "infrastructure"]
    # Where the kind was read: the Agent API's answer, or the delegado task's chained event.
    source: Text
    # The evidence: the `model_requests` rows that failed (stage, error class, HTTP status and
    # the provider's code).
    requests: tuple[Row, ...]

    @model_validator(mode="after")
    def _counts_by_its_kind(self) -> Self:
        if (self.counts_against == "agent") != (self.kind in AGENT_FAILURES):
            raise ValueError(
                f"A {self.kind} failure does not count against the {self.counts_against}"
            )
        return self


class TrialRecord(SuiteModel):
    """Everything the graders read: they are pure functions over this record."""

    case_id: CaseId
    suite: Suite
    # The one of the case's governance conditions this trial ran under.
    governance: Governance
    # 1 to k: the trial's number among the case's k trials.
    trial: Annotated[int, Field(ge=1)]
    seed: int
    # The agent's run, as its database and verify_database name it.
    run_id: Id
    agent: Agent
    # The conversation. In delegado, the task's final message as one assistant turn, so the
    # claims grader reads it like any other answer.
    transcript: tuple[Turn, ...]
    # build_hp_flow_trace's document in presente, build_flow_trace's in delegado.
    trace: Row
    # verify_database's documents for the trial's agent database, which holds this trial alone
    # (graders/evidence.verified); not filtered by run_id, which in presente names one purchase.
    evidence: tuple[Row, ...]
    final: Ending
    final_state: FinalState
    usage: Usage
    infra_failure: InfraFailure | None
    # None when the agent's model did not fail, and in records written before the field.
    model_failure: ModelFailure | None = None
