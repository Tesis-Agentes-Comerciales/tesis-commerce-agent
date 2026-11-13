"""The suite's trials on the live stack (docs/design/eval-suite.md §2, §4 and §5).

`LiveExecutor` starts the live stack once per run, on `__enter__`: a disposable PostgreSQL and
the real Merchant. Each lane starts on the first trial that needs it, once: `b0` for presente
and `hnp` for delegado, each with the real payment provider. `__exit__` stops everything.

Every trial is a new Merchant episode and a new Agent with its own database, in its own
directory under the run's. The Agent is production `create_app` in this process; at its model
boundary stands the model under test (`agent_model`) or the reference agent
(suite/live/reference.py). The case's events apply as suite/live/events.py says; a case with an
event the executor does not apply is refused before its trial starts.

Failures:
- A stack or lane that did not start makes every trial that needs it a record with
  `infra_failure.cause = "stack_not_started"` and nothing else observed; it is not retried
  within the run, so the orchestrator's retries end quickly and the run reports it.
- An Agent API answer 5xx, a Merchant or PSP 5xx that no fault armed, a model request the
  provider answered 5xx on every attempt, or a simulated user whose API failed, is
  `api_5xx` (observe.infra_failure); `suite/reduce.py` keeps it when the case injected it.
- A simulated user's unusable answer raises `SimulatorFailure`, and a simulator reservation
  the budget refuses raises `FundsExhausted`.

The agent's usage is charged to the run's budget whatever ended the trial, a raise included
(`RunBudget.charge`, role `agent`), from the trial database's `model_requests`: it was spent.
The charge never stops a trial that completed; the run reads `budget.exhausted` after saving
its record.
"""

import re
import secrets
import traceback
from contextlib import suppress
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Self, get_args

from anthropic import Anthropic

from commerce_evaluation.llm.budget import BudgetedTransport, RunBudget
from commerce_evaluation.llm.claude import AnthropicTransport
from commerce_evaluation.llm.cost import CLAUDE_PRICES, NO_USAGE, TokenUsage
from commerce_evaluation.llm.transport import (
    Effort,
    FundsExhausted,
    TransportError,
    TransportTimeout,
)
from commerce_evaluation.stack.environment import missing_requirements
from commerce_evaluation.stack.live_stack import LiveStack
from commerce_evaluation.stack.purchase_lane import (
    PurchaseLane,
    hnp_requirements,
    payment_requirements,
)
from commerce_evaluation.suite.cases import Case, Governance, PresentCase
from commerce_evaluation.suite.executor import RunSettings, SimulatorFailure
from commerce_evaluation.suite.live import events, observe
from commerce_evaluation.suite.live.delegado import play_delegado
from commerce_evaluation.suite.live.presente import play_presente
from commerce_evaluation.suite.live.settings import AGENT_DATABASE, Model, claude_missing
from commerce_evaluation.suite.trial import (
    Agent,
    Ending,
    FinalState,
    InfraFailure,
    TrialRecord,
    Usage,
)
from commerce_evaluation.suite.users import ScriptedUser, SimulatedUser, User

# The record's agent when no model is under test.
REFERENCE = "reference"
STACK = "stack"
LANES = {"presente": "b0", "delegado": "hnp"}


class LiveExecutor:
    def __init__(self, settings: RunSettings, budget: RunBudget | None):
        self.settings = settings
        self.budget = budget
        self.model: Model | None = None
        if settings.agent_model is not None:
            if budget is None:
                raise ValueError("A model under test needs the run's budget (--max-usd)")
            if settings.agent_effort not in get_args(Effort):
                raise ValueError(f"Unknown effort {settings.agent_effort}")
            if problems := claude_missing():
                raise RuntimeError("The agent cannot run on Claude: " + "; ".join(problems))
            self.model = Model(settings.agent_model, settings.agent_effort)
        if settings.simulator_model is not None and budget is None:
            raise ValueError("A simulated user needs the run's budget (--max-usd)")
        self._stack: LiveStack | None = None
        self._lanes: dict[str, PurchaseLane] = {}
        # Why the stack or a lane did not start, by its name.
        self._unstarted: dict[str, str] = {}
        self._trials = 0

    def __enter__(self) -> Self:
        root = self.settings.run_dir / STACK
        root.mkdir(parents=True, exist_ok=True)
        if problems := missing_requirements():
            self._unstarted[STACK] = "Falta " + "; ".join(problems)
            return self
        stack = LiveStack(root)
        try:
            stack.start()
        except Exception as error:
            self._unstarted[STACK] = _cause(error, root)
            with suppress(Exception):
                stack.close()
            return self
        self._stack = stack
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        try:
            for lane in self._lanes.values():
                with suppress(Exception):
                    lane.close()
        finally:
            self._lanes.clear()
            if self._stack is not None:
                self._stack.close()
                self._stack = None

    def run(self, case: Case, governance: Governance, trial: int, seed: int) -> TrialRecord:
        if governance not in case.environment.governance:
            raise ValueError(f"{case.id} does not run under {governance}")
        events.check(case)
        lane = self._lane(LANES[case.mode])
        if lane is None:
            reason = self._unstarted.get(STACK) or self._unstarted[LANES[case.mode]]
            failure = InfraFailure(cause="stack_not_started", detail=reason)
            return self._unobserved(case, governance, trial, seed, failure)
        self._trials += 1
        name = f"t{self._trials}"
        root = self.settings.run_dir / "trials" / f"{_slug(case.id)}-{governance}-{trial}-{name}"
        root.mkdir(parents=True)
        user = self._user(case) if isinstance(case, PresentCase) else None
        try:
            try:
                if isinstance(case, PresentCase):
                    assert user is not None
                    played = play_presente(lane, case, governance, root, self.model, user, name)
                else:
                    played = play_delegado(lane, case, governance, root, self.model, name)
            except FundsExhausted:
                raise
            except (TransportError, TransportTimeout) as error:
                # The simulated user's API failed: infrastructure, never the agent.
                failure = InfraFailure(
                    cause="api_5xx", detail=f"usuario simulado: {type(error).__name__} {error}"
                )
                return self._unobserved(case, governance, trial, seed, failure)
            if isinstance(user, SimulatedUser) and user.failure is not None:
                raise SimulatorFailure(f"{case.id}: {user.failure}")
            return self._record(case, governance, trial, seed, lane, played, user)
        finally:
            self._charge(root / AGENT_DATABASE)

    def _lane(self, profile: str) -> PurchaseLane | None:
        if self._stack is None:
            return None
        if profile in self._lanes:
            return self._lanes[profile]
        if profile in self._unstarted:
            return None
        problems = payment_requirements()
        if profile == "hnp":
            problems += hnp_requirements(self._stack.merchant)
        if problems:
            self._unstarted[profile] = "Falta " + "; ".join(problems)
            return None
        lane = PurchaseLane(self._stack, profile)
        try:
            lane.start()
        except Exception as error:
            self._unstarted[profile] = _cause(error, lane.root)
            with suppress(Exception):
                lane.close()
            return None
        self._lanes[profile] = lane
        return lane

    def _user(self, case: PresentCase) -> User:
        if self.settings.simulator_model is None:
            return ScriptedUser(case)
        assert self.budget is not None
        transport = BudgetedTransport(
            AnthropicTransport(Anthropic()), budget=self.budget, role="simulator"
        )
        return SimulatedUser(case, transport, model=self.settings.simulator_model)

    def _record(
        self,
        case: Case,
        governance: Governance,
        trial: int,
        seed: int,
        lane: PurchaseLane,
        played: observe.Played,
        user: User | None,
    ) -> TrialRecord:
        usage, models, provider_5xx = observe.agent_usage(played.agent.database)
        upstream = observe.upstream_failures((lane.proxy, lane.psp_proxy))
        user_usage = user.usage if isinstance(user, SimulatedUser) else None
        return TrialRecord(
            case_id=case.id,
            suite=self.settings.suite,
            governance=governance,
            trial=trial,
            seed=seed,
            run_id=played.run_id,
            agent=self._agent(models),
            transcript=played.transcript,
            trace=observe.json_row(played.trace),
            evidence=observe.evidence(lane, played.agent),
            final=played.final,
            final_state=observe.final_state(lane, played.episode),
            usage=Usage(
                agent=_tokens(usage) if self.model is not None else {},
                user=_tokens(user_usage) if user_usage is not None else {},
                judge={},
                usd=float(self._cost(usage) + self._user_cost(user_usage)),
            ),
            infra_failure=observe.infra_failure(played.api_5xx, upstream, provider_5xx),
        )

    def _charge(self, database: Path) -> None:
        """The trial's agent usage, charged to the run's budget."""
        if self.model is None or self.budget is None:
            return
        usage, models, _ = observe.agent_usage(database)
        if usage == NO_USAGE:
            return
        self.budget.charge(
            role="agent",
            model=self.model.name,
            usage=usage,
            model_returned=models[0] if len(models) == 1 else None,
            effort=_effort(self.model.effort),
        )

    def _agent(self, models: tuple[str, ...]) -> Agent:
        if self.model is None:
            return Agent(model=REFERENCE, effort=None, response_models=())
        return Agent(model=self.model.name, effort=self.model.effort, response_models=models)

    def _cost(self, usage: TokenUsage) -> Decimal:
        if self.model is None or self.model.name not in CLAUDE_PRICES:
            return Decimal(0)
        return CLAUDE_PRICES[self.model.name].cost(usage)

    def _user_cost(self, usage: TokenUsage | None) -> Decimal:
        model = self.settings.simulator_model
        if usage is None or model is None or model not in CLAUDE_PRICES:
            return Decimal(0)
        return CLAUDE_PRICES[model].cost(usage)

    def _unobserved(
        self, case: Case, governance: Governance, trial: int, seed: int, failure: InfraFailure
    ) -> TrialRecord:
        """The record of a trial whose stack failed before anything could be observed."""
        return TrialRecord(
            case_id=case.id,
            suite=self.settings.suite,
            governance=governance,
            trial=trial,
            seed=seed,
            run_id=f"unobserved-{secrets.token_hex(8)}",
            agent=self._agent(()),
            transcript=(),
            trace={},
            evidence=(),
            final=Ending(),
            final_state=FinalState(orders=(), checkouts=(), psp_charges=()),
            usage=Usage(agent={}, user={}, judge={}, usd=0.0),
            infra_failure=failure,
        )


def _tokens(usage: TokenUsage) -> dict[str, int]:
    return usage.model_dump()


def _effort(value: str) -> Effort | None:
    return next((effort for effort in get_args(Effort) if effort == value), None)


def _slug(case_id: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", case_id.lower()).strip("-")


def _cause(error: Exception, root: Path) -> str:
    """Why the stack did not start: the error's class and the harness's own marker, never a
    value; the logs stay under `root`."""
    text = "".join(traceback.format_exception_only(error))
    markers = re.findall(r"LIVE_STACK_[A-Z_]+(?::[a-z0-9_]+)?", text)
    return " ".join([type(error).__name__, *markers, f"(registros en {root})"])
