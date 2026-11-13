"""The suite's trials on the live stack (docs/design/eval-suite.md §2, §4 and §5).

`LiveExecutor` starts the live stack once per session, on `__enter__`: a disposable PostgreSQL
and the real Merchant. Each lane starts on the first trial that needs it, once: `b0` for
presente and `hnp` for delegado, each with the real payment provider. `__exit__` stops
everything. A resumed run is a new session in the same run directory, so each session's stack
lives in `stack/<session>/` and each trial's directory names its session.

Every trial is a new Merchant episode and a new Agent with its own database, in its own
directory under the run's. The Agent is production `create_app` in this process; at its model
boundary stands the model under test (`agent_model`) or the reference agent
(suite/live/reference.py). The case's events apply as suite/live/events.py says; a case with an
event the executor does not apply is refused before its trial starts.

Failures:
- A stack or lane that did not start makes every trial that needs it a record with
  `infra_failure.cause = "stack_not_started"` and nothing else observed; it is not retried
  within the run, so the orchestrator's retries end quickly and the run reports it.
- An Agent API answer 502, 503 or 504, a Merchant or PSP one that no fault armed, a model
  request the provider answered 5xx on every attempt, a delegado task still running when the
  wait ends, or a simulated user whose API failed, is `api_5xx` (observe.infra_failure);
  `suite/reduce.py` keeps it when the case injected it. A bare 500 is graded as the agent's.
- A simulated user's unusable answer raises `SimulatorFailure`, and a simulator reservation
  the budget refuses raises `FundsExhausted`.

The agent's usage is charged to the run's budget whatever ended the trial, a raise included,
one request at a time (suite/live/usage.py, role `agent`): it was spent. A trial that completed
returns its record even when its charge passes the cap; the executor then refuses the next trial
with `FundsExhausted`, whether `RunBudget.charge` raised or only recorded it.
"""

import re
import secrets
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import Self, get_args

from anthropic import Anthropic

from commerce_evaluation.llm.budget import BudgetedTransport, RunBudget
from commerce_evaluation.llm.claude import AnthropicTransport
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
from commerce_evaluation.suite.live import events, observe, usage
from commerce_evaluation.suite.live.delegado import play_delegado
from commerce_evaluation.suite.live.presente import play_presente
from commerce_evaluation.suite.live.settings import (
    AGENT_DATABASE,
    Model,
    claude_missing,
    claude_redirected,
)
from commerce_evaluation.suite.trial import (
    Agent,
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
        if settings.simulator_model is not None:
            if budget is None:
                raise ValueError("A simulated user needs the run's budget (--max-usd)")
            if problems := claude_redirected():
                raise RuntimeError("The simulated user cannot run: " + "; ".join(problems))
        # This session's name: a resumed run starts another in the same run directory.
        self.session = secrets.token_hex(4)
        self._stack: LiveStack | None = None
        self._lanes: dict[str, PurchaseLane] = {}
        # Why the stack or a lane did not start, by its name.
        self._unstarted: dict[str, str] = {}
        self._trials = 0
        # The simulated user's client, one per session.
        self._anthropic: Anthropic | None = None
        # A completed trial's charge passed the cap: the next trial is refused.
        self._exhausted = False

    def __enter__(self) -> Self:
        root = self.settings.run_dir / STACK / self.session
        root.mkdir(parents=True)
        if problems := missing_requirements():
            self._unstarted[STACK] = "Falta " + "; ".join(problems)
            return self
        stack = LiveStack(root)
        try:
            stack.start()
        except Exception as error:
            self._unstarted[STACK] = observe.start_failure(error, root)
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
            if self._anthropic is not None:
                self._anthropic.close()
                self._anthropic = None
            if self._stack is not None:
                self._stack.close()
                self._stack = None

    def run(self, case: Case, governance: Governance, trial: int, seed: int) -> TrialRecord:
        if self._exhausted:
            raise FundsExhausted("BudgetExhausted", attempts=0)
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
        folder = f"{_slug(case.id)}-{governance}-{trial}-{self.session}-{name}"
        root = self.settings.run_dir / "trials" / folder
        root.mkdir(parents=True)
        record: TrialRecord | None = None
        try:
            record = self._play(case, governance, trial, seed, lane, root, name)
        finally:
            try:
                self._charge(root / AGENT_DATABASE)
            except FundsExhausted:
                if record is None:
                    raise
                # The trial completed: its record stands, and the next trial is refused.
                self._exhausted = True
        assert record is not None
        return record

    def _play(
        self,
        case: Case,
        governance: Governance,
        trial: int,
        seed: int,
        lane: PurchaseLane,
        root: Path,
        name: str,
    ) -> TrialRecord:
        user = self._user(case) if isinstance(case, PresentCase) else None
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
            self._unstarted[profile] = observe.start_failure(error, lane.root)
            with suppress(Exception):
                lane.close()
            return None
        self._lanes[profile] = lane
        return lane

    def _user(self, case: PresentCase) -> User:
        if self.settings.simulator_model is None:
            return ScriptedUser(case)
        assert self.budget is not None
        if self._anthropic is None:
            self._anthropic = Anthropic()
        transport = BudgetedTransport(
            AnthropicTransport(self._anthropic), budget=self.budget, role="simulator"
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
        spent = usage.agent_usage(played.agent.database)
        upstream = observe.upstream_failures((lane.proxy, lane.psp_proxy))
        user_usage = user.usage if isinstance(user, SimulatedUser) else None
        return TrialRecord(
            case_id=case.id,
            suite=self.settings.suite,
            governance=governance,
            trial=trial,
            seed=seed,
            run_id=played.run_id,
            agent=self._agent(spent.models),
            transcript=played.transcript,
            trace=observe.json_row(played.trace),
            evidence=observe.evidence(lane, played.agent),
            final=played.final,
            final_state=observe.final_state(lane, played.episode),
            usage=Usage(
                agent=spent.total.model_dump() if self.model is not None else {},
                user=user_usage.model_dump() if user_usage is not None else {},
                judge={},
                usd=float(
                    (usage.cost(self.model.name, spent) if self.model is not None else 0)
                    + usage.user_cost(self.settings.simulator_model, user_usage)
                ),
            ),
            infra_failure=observe.infra_failure(
                played.api_5xx, upstream, spent.provider_5xx, played.unfinished
            ),
        )

    def _charge(self, database: Path) -> None:
        """The trial's agent usage, charged to the run's budget request by request."""
        if self.model is None or self.budget is None:
            return
        spent = usage.agent_usage(database)
        usage.charge(self.budget, self.model.name, _effort(self.model.effort), spent)

    def _agent(self, models: tuple[str, ...]) -> Agent:
        if self.model is None:
            return Agent(model=REFERENCE, effort=None, response_models=())
        return Agent(model=self.model.name, effort=self.model.effort, response_models=models)

    def _unobserved(
        self, case: Case, governance: Governance, trial: int, seed: int, failure: InfraFailure
    ) -> TrialRecord:
        return observe.unobserved(
            (case.id, self.settings.suite, governance, trial, seed), self._agent(()), failure
        )


def _effort(value: str) -> Effort | None:
    return next((effort for effort in get_args(Effort) if effort == value), None)


def _slug(case_id: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", case_id.lower()).strip("-")
