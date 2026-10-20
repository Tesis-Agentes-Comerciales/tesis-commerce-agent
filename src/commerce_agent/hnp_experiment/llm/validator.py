"""LLM validator for arm B: the model judges public facts, the adapter does the bookkeeping.

The model returns per-check outcomes, a decision and short reasons (``schemas.py``). The
adapter derives everything else deterministically: coverage, policy links, fact refs, digests,
VAL reason codes and the protocol binding check. An answer whose decision contradicts its own
checks is invalid (fail closed); it is never repaired.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from random import Random
from threading import Condition, Lock
from time import perf_counter_ns

from pydantic import BaseModel

from ..context import check_refs
from ..models import (
    CHECKS,
    REASONS,
    RELATED,
    CheckId,
    CheckResult,
    ProposalView,
    ReviewerContext,
    ValidatorAssessment,
)
from ..ports import RoleOutputError
from .config import ReasoningEffort
from .cost import TokenPrices
from .prompts import PromptSpec
from .schemas import BUSINESS_CHECKS, CheckOutcome, ReviewChecks, ValidatorDecision
from .trace import Actor, CallRecord, CallStatus, ReviewOutput, TokenUsage
from .transport import FakeReply, LlmRequest, StructuredLLM, TransportError

ADAPTER_ID = "llm-validator:1"
# Retention bounds for non-authoritative prose in traces, not experiment parameters.
MAX_TRACE_REASONS = 5
MAX_TRACE_REASON_CHARS = 240


def _public(value: object) -> object:
    """JSON projection without contract bookkeeping that carries no reviewable fact."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items() if k != "schema_version"}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return value


def review_input(proposal: ProposalView, context: ReviewerContext) -> str:
    """The model's whole view: deterministic JSON of public facts, no digests of envelopes.

    Envelope refs and digests (episode, intent, feed, context, proposal) are bookkeeping the
    adapter binds itself; the offer reference stays because comparing it is a business check.
    """
    intent = context.intent
    document = {
        "review_time": context.model_dump(mode="json")["authority_now"],
        "purchase_request": {
            "hard_constraints": _public(intent.hard),
            "preferences": _public(intent.preferences),
            "required_constraint_ids": list(context.required_constraint_ids),
        },
        "catalog_candidates": _public(list(context.candidates)),
        "authorized_merchant": context.authorized_merchant_ref,
        "proposal": _public(proposal.draft),
        "checkout_terms": _public(context.terms),
        "delegation": _public(context.delegation),
    }
    return json.dumps(document, indent=2, ensure_ascii=False)


def assessment_from_decision(
    decision: ValidatorDecision,
    proposal: ProposalView,
    context: ReviewerContext,
    adapter_id: str,
) -> ValidatorAssessment:
    outcomes: dict[CheckId, CheckOutcome] = {**decision.checks.outcomes()}
    # Protocol bookkeeping, not a judgement: the reviewed proposal belongs to this context.
    outcomes["binding"] = (
        "pass"
        if (proposal.episode_ref, proposal.intent_ref) == (context.episode_ref, context.intent_ref)
        else "fail"
    )
    checks = tuple(
        CheckResult(
            check_id=check,
            result=outcomes[check],
            related_policy_ids=RELATED[check],
            fact_refs=check_refs(check, context),
        )
        for check in CHECKS
    )
    results = {c.result for c in checks}
    verdict = (
        "reject" if "fail" in results else ("abstain" if "unresolved" in results else "accept")
    )
    if decision.decision != verdict:
        raise RoleOutputError("VALIDATOR_OUTPUT_INCONSISTENT")
    return ValidatorAssessment(
        proposal_digest=proposal.proposal_digest,
        context_ref=context.context_ref,
        validator_adapter_id=adapter_id,
        verdict=verdict,
        checks=checks,
        coverage=CHECKS,
        reasons=tuple(REASONS[c.check_id] for c in checks if c.result != "pass"),
    )


class LlmValidatorAgent:
    """One instance per episode, so its call log is that episode's calls."""

    adapter_id = ADAPTER_ID

    def __init__(
        self,
        *,
        transport: StructuredLLM,
        model: str,
        prompt: PromptSpec,
        temperature: float | None,
        reasoning_effort: ReasoningEffort | None,
        max_output_tokens: int,
        timeout_seconds: float,
        prices: TokenPrices,
        seed: int,
        call_prefix: str,
    ):
        if prompt.role != "validator":
            raise ValueError("Validator requires a validator prompt")
        self._transport = transport
        self._model = model
        self._prompt = prompt
        self._temperature = temperature
        self._reasoning_effort: ReasoningEffort | None = reasoning_effort
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        self._prices = prices
        self._seed = seed
        self._call_prefix = call_prefix
        self._calls: list[CallRecord] = []
        self._finished = Condition()

    @property
    def actor(self) -> Actor:
        return Actor(
            role="validator",
            kind="llm",
            adapter_id=self.adapter_id,
            transport=self._transport.transport_id,
            model=self._model,
            prompt_version=self._prompt.name,
            prompt_sha256=self._prompt.sha256,
            seed=self._seed,
            temperature=self._temperature,
            reasoning_effort=self._reasoning_effort,
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
            input_usd_per_mtok=str(self._prices.input_usd_per_mtok),
            output_usd_per_mtok=str(self._prices.output_usd_per_mtok),
        )

    def calls(self) -> tuple[CallRecord, ...]:
        with self._finished:
            return tuple(self._calls)

    def drain(self, expected_calls: int, timeout_seconds: float) -> bool:
        """Wait for calls abandoned at the deadline so their usage is still counted."""
        with self._finished:
            return self._finished.wait_for(
                lambda: len(self._calls) >= expected_calls, timeout_seconds
            )

    def request(self, proposal: ProposalView, context: ReviewerContext) -> LlmRequest:
        return LlmRequest(
            model=self._model,
            instructions=self._prompt.text,
            input_text=review_input(proposal, context),
            temperature=self._temperature,
            reasoning_effort=self._reasoning_effort,
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
        )

    def review(self, proposal: ProposalView, context: ReviewerContext) -> ValidatorAssessment:
        started = perf_counter_ns()
        call = _CallOutcome()
        try:
            return self._review(proposal, context, call)
        finally:
            # Every invocation leaves exactly one record, whatever happened.
            self._record(started, call)

    def _review(
        self, proposal: ProposalView, context: ReviewerContext, call: "_CallOutcome"
    ) -> ValidatorAssessment:
        try:
            result = self._transport.parse(self.request(proposal, context), ValidatorDecision)
        except TimeoutError as error:
            call.status, call.error_kind = "timeout", type(error).__name__
            raise
        except Exception as error:
            call.error_kind = type(error).__name__
            raise
        call.usage = result.usage
        if result.parsed is None:
            call.status = result.failure or "schema_invalid"
            raise RoleOutputError("VALIDATOR_OUTPUT_INVALID")
        decision = result.parsed
        call.review = ReviewOutput(
            decision=decision.decision,
            checks={str(k): str(v) for k, v in decision.checks.outcomes().items()},
            reasons=tuple(r[:MAX_TRACE_REASON_CHARS] for r in decision.reasons[:MAX_TRACE_REASONS]),
        )
        try:
            assessment = assessment_from_decision(decision, proposal, context, self.adapter_id)
        except RoleOutputError:
            call.status = "inconsistent"
            raise
        except ValueError:
            call.status = "schema_invalid"
            raise RoleOutputError("VALIDATOR_OUTPUT_INVALID") from None
        call.status = "ok"
        return assessment

    def _record(self, started: int, call: "_CallOutcome") -> None:
        with self._finished:
            self._calls.append(
                CallRecord(
                    call_id=f"{self._call_prefix}:validator:{len(self._calls) + 1}",
                    role="validator",
                    model=self._model,
                    transport=self._transport.transport_id,
                    latency_ms=(perf_counter_ns() - started) / 1e6,
                    status=call.status,
                    usage=call.usage,
                    cost_usd=None if call.usage is None else float(self._prices.cost(call.usage)),
                    error_kind=call.error_kind,
                    review=call.review,
                )
            )
            self._finished.notify_all()


@dataclass
class _CallOutcome:
    """Mutable while one call runs; frozen into a CallRecord when it ends."""

    status: CallStatus = "transport_error"
    usage: TokenUsage | None = None
    error_kind: str | None = None
    review: ReviewOutput | None = None


def sample_decision() -> str:
    """A representative accepted answer, used for the lower bound of cost estimates."""
    checks = ReviewChecks.model_validate({c: "pass" for c in BUSINESS_CHECKS})
    return ValidatorDecision(checks=checks, decision="accept", reasons=[]).model_dump_json()


DRY_RUN_MIX = (
    ("accept", 60),
    ("reject", 20),
    ("abstain", 6),
    ("inconsistent", 5),
    ("refusal", 3),
    ("schema_invalid", 3),
    ("transport_error", 3),
)


def dry_run_responder(seed: int) -> Callable[[LlmRequest], FakeReply]:
    """Seeded pseudo-random answers that exercise every outcome path of the pipeline.

    This is noise, not a model: its "captures" or "false blocks" say nothing about any LLM.
    Answers depend on the seed, the review input and how often that input was seen, so a run
    is reproducible for a given seed and episode order.
    """
    seen: Counter[str] = Counter()
    lock = Lock()
    kinds = [kind for kind, _ in DRY_RUN_MIX]
    weights = [weight for _, weight in DRY_RUN_MIX]

    def respond(request: LlmRequest) -> FakeReply:
        key = hashlib.sha256(request.input_text.encode("utf-8")).hexdigest()
        with lock:
            seen[key] += 1
            occurrence = seen[key]
        rng = Random(f"{seed}:{key}:{occurrence}")
        kind = rng.choices(kinds, weights)[0]
        checks: dict[str, str] = {c: "pass" for c in BUSINESS_CHECKS}
        if kind == "transport_error":
            return FakeReply(raises=TransportError("FakeProviderError"))
        if kind in ("refusal", "schema_invalid"):
            return FakeReply(
                output={"decision": "accept"} if kind == "schema_invalid" else None,
                failure="refusal" if kind == "refusal" else None,
            )
        decision = "accept"
        if kind in ("reject", "abstain", "inconsistent"):
            checks[rng.choice(BUSINESS_CHECKS)] = "unresolved" if kind == "abstain" else "fail"
            decision = {"reject": "reject", "abstain": "abstain", "inconsistent": "accept"}[kind]
        return FakeReply(
            output={
                "checks": checks,
                "decision": decision,
                "reasons": [] if decision == "accept" else ["Dry-run answer, not a model."],
            }
        )

    return respond
