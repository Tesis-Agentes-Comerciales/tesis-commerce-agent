"""Paired A/B matrix over the business cases: same inputs per pair, seeded order.

A pair is one business case at one repetition. Both arms of a pair get the same fixture
source, the same buyer payload and the same backend controls; only arm B adds the validator.
Pairs run in a seeded random order and the arm order inside each pair is randomized too, so
drift in provider latency or behaviour does not line up with one arm.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from random import Random
from typing import Protocol

from ..adapters import ScriptedBuyerAgent
from ..context import (
    FixtureContextResolver,
    assemble,
    buyer_context,
    proposal_envelope,
    proposal_view,
    reviewer_context,
    source_ref,
)
from ..coordinator import ReviewCoordinator
from ..evaluation import is_infrastructure_failure
from ..evidence import ExperimentalEvidence
from ..harness import CaseSpec
from ..models import Condition, DemoResult, ExecutionKind, ProposalView, ReviewerContext
from ..ports import ValidatorAgent
from ..sink import SimulatedEffectSink
from .trace import Actor, CallRecord, EpisodeResult, EpisodeTrace, TraceWriter, total_usage

# Fixtures whose scripted validator misbehaves on purpose (false accept, timeout, malformed,
# replay...) test the harness, not a reviewer; a real model replaces that behaviour.
BUSINESS_BEHAVIOR = "normal"
SCRIPTED_BUYER = Actor(
    role="buyer", kind="scripted", adapter_id=ScriptedBuyerAgent.adapter_id, transport="none"
)


def business_cases(cases: Sequence[CaseSpec]) -> tuple[tuple[CaseSpec, ...], tuple[str, ...]]:
    selected = tuple(c for c in cases if c.validator_behavior == BUSINESS_BEHAVIOR)
    excluded = tuple(c.case_id for c in cases if c.validator_behavior != BUSINESS_BEHAVIOR)
    return selected, excluded


@dataclass(frozen=True)
class PlannedEpisode:
    order_index: int
    case_id: str
    repetition: int
    arm: Condition

    @property
    def pair_id(self) -> str:
        return f"{self.case_id}:r{self.repetition}"

    @property
    def episode_id(self) -> str:
        return f"{self.pair_id}:{self.arm}"


def plan_episodes(
    case_ids: Sequence[str], repetitions: int, seed: int
) -> tuple[PlannedEpisode, ...]:
    if repetitions < 1 or len(set(case_ids)) != len(case_ids):
        raise ValueError("Invalid matrix plan")
    rng = Random(seed)
    pairs = [(case_id, rep) for rep in range(1, repetitions + 1) for case_id in case_ids]
    rng.shuffle(pairs)
    planned: list[PlannedEpisode] = []
    for case_id, repetition in pairs:
        arms: list[Condition] = ["A", "B"]
        rng.shuffle(arms)
        for arm in arms:
            planned.append(PlannedEpisode(len(planned), case_id, repetition, arm))
    return tuple(planned)


class TracedValidator(ValidatorAgent, Protocol):
    @property
    def actor(self) -> Actor: ...

    def calls(self) -> tuple[CallRecord, ...]: ...

    def drain(self, expected_calls: int, timeout_seconds: float) -> bool: ...


@dataclass(frozen=True)
class EpisodeOutcome:
    episode: PlannedEpisode
    result: DemoResult
    actors: tuple[Actor, ...]
    calls: tuple[CallRecord, ...]
    pending_calls: int


@dataclass(frozen=True)
class MatrixRun:
    experiment_id: str
    execution_kind: ExecutionKind
    seed: int
    repetitions: int
    case_ids: tuple[str, ...]
    excluded_case_ids: tuple[str, ...]
    outcomes: tuple[EpisodeOutcome, ...]


def review_inputs(
    cases: Sequence[CaseSpec],
    render: Callable[[ProposalView, ReviewerContext], str],
    repetitions: int,
) -> list[str]:
    """What the validator would receive in each B episode of the plan, without calling it.

    Cases whose buyer payload is invalid never reach the validator and add no call.
    """
    inputs: list[str] = []
    for case in business_cases(cases)[0]:
        context = assemble(case.source)
        buyer = ScriptedBuyerAgent(case.buyer_payload)
        try:
            draft = buyer.propose(buyer_context(context))
        except ValueError:
            continue
        proposal = proposal_envelope(context, draft, buyer.adapter_id)
        inputs.extend([render(proposal_view(proposal), reviewer_context(context))] * repetitions)
    return inputs


def episode_trace(experiment_id: str, kind: ExecutionKind, outcome: EpisodeOutcome) -> EpisodeTrace:
    record, episode = outcome.result.record, outcome.episode
    latency: dict[str, float] = {}
    for timing in outcome.result.timings:
        latency[timing.component] = latency.get(timing.component, 0.0) + timing.elapsed_ns / 1e6
    return EpisodeTrace(
        experiment_id=experiment_id,
        execution_kind=kind,
        episode_id=episode.episode_id,
        pair_id=episode.pair_id,
        order_index=episode.order_index,
        arm=episode.arm,
        case_id=episode.case_id,
        repetition=episode.repetition,
        actors=outcome.actors,
        calls=outcome.calls,
        pending_calls=outcome.pending_calls,
        latency_ms=latency,
        tokens=total_usage(outcome.calls),
        cost_usd=sum(c.cost_usd or 0.0 for c in outcome.calls),
        result=EpisodeResult(
            admission_code=record.admission.code,
            effect=outcome.result.effect_occurred,
            delivery_code=outcome.result.delivery_code,
            assessment_status=record.assessment_status,
            validator_verdict=None if record.assessment is None else record.assessment.verdict,
            backend_failures=tuple(record.admission.backend_failures),
            outcome_class="infrastructure"
            if is_infrastructure_failure(outcome.result)
            else "experimental",
            record_digest=outcome.result.record_digest,
        ),
    )


def run_matrix(
    *,
    cases: Sequence[CaseSpec],
    repetitions: int,
    seed: int,
    experiment_id: str,
    execution_kind: ExecutionKind,
    validator_factory: Callable[[PlannedEpisode], TracedValidator],
    validator_budget_ns: int,
    drain_seconds: float,
    evidence: ExperimentalEvidence,
    traces: TraceWriter | None = None,
) -> MatrixRun:
    if execution_kind == "scripted_offline":
        raise ValueError("The model matrix never runs as scripted_offline")
    selected, excluded = business_cases(cases)
    by_id = {c.case_id: c for c in selected}
    outcomes: list[EpisodeOutcome] = []
    for episode in plan_episodes(tuple(by_id), repetitions, seed):
        case = by_id[episode.case_id]
        validator = validator_factory(episode) if episode.arm == "B" else None
        sink = SimulatedEffectSink()
        coordinator = ReviewCoordinator(
            # Fresh objects per episode; both arms of a pair read the same case.
            buyer=ScriptedBuyerAgent(case.buyer_payload),
            validator=validator,
            resolver=FixtureContextResolver(case.source, case.next_source),
            evidence=evidence,
            sink=sink,
            condition=episode.arm,
            validator_budget_ns=validator_budget_ns,
            case_configuration_ref=source_ref("case-configuration", 1, case),
            execution_kind=execution_kind,
        )
        result = coordinator.run(episode.episode_id)
        if result.effect_occurred != bool(sink.events):
            raise ValueError("Effect observation mismatch")
        actors: tuple[Actor, ...] = (SCRIPTED_BUYER,)
        calls: tuple[CallRecord, ...] = ()
        pending = 0
        if validator is not None:
            expected = 0 if result.record.assessment_status == "not_called" else 1
            if not validator.drain(expected, drain_seconds):
                pending = expected - len(validator.calls())
            actors, calls = (*actors, validator.actor), validator.calls()
        outcome = EpisodeOutcome(episode, result, actors, calls, pending)
        outcomes.append(outcome)
        if traces is not None:
            traces.write(episode_trace(experiment_id, execution_kind, outcome))
    return MatrixRun(
        experiment_id=experiment_id,
        execution_kind=execution_kind,
        seed=seed,
        repetitions=repetitions,
        case_ids=tuple(by_id),
        excluded_case_ids=excluded,
        outcomes=tuple(outcomes),
    )
