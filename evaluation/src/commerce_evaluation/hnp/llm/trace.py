"""JSONL episode traces for the model matrix, one line per episode, written as it runs.

Traces hold identifiers, configuration, usage, latency and outcomes. They never hold API keys,
raw model output that failed validation, or exception messages: errors are recorded by class
name only. Bounded model reasons are kept for qualitative analysis of parsed answers.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models import Condition, ExecutionKind

TRACE_SCHEMA = "hnp-llm-trace/1"
Role = Literal["buyer", "validator"]
TransportId = Literal["none", "fake", "openai-responses"]
CallStatus = Literal[
    "ok",
    "refusal",
    "incomplete",
    "schema_invalid",
    "inconsistent",
    "timeout",
    "transport_error",
]


class TraceModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class TokenUsage(TraceModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)


class Actor(TraceModel):
    role: Role
    kind: Literal["scripted", "llm"]
    adapter_id: str
    transport: TransportId
    model: str | None = None
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    seed: int | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float | None = None
    input_usd_per_mtok: str | None = None
    output_usd_per_mtok: str | None = None


class ReviewOutput(TraceModel):
    """A parsed validator answer, kept for analysis; never authority."""

    decision: str
    checks: dict[str, str]
    reasons: tuple[str, ...]


class CallRecord(TraceModel):
    call_id: str
    role: Role
    model: str
    transport: TransportId
    latency_ms: float = Field(ge=0)
    status: CallStatus
    usage: TokenUsage | None
    cost_usd: float | None
    error_kind: str | None = None
    review: ReviewOutput | None = None


class EpisodeResult(TraceModel):
    admission_code: str
    effect: bool
    delivery_code: str
    assessment_status: str
    validator_verdict: str | None
    backend_failures: tuple[str, ...]
    outcome_class: Literal["experimental", "infrastructure"]
    record_digest: str


class EpisodeTrace(TraceModel):
    schema_version: Literal["hnp-llm-trace/1"] = TRACE_SCHEMA
    experiment_id: str
    execution_kind: ExecutionKind
    episode_id: str
    pair_id: str
    order_index: int = Field(ge=0)
    arm: Condition
    case_id: str
    repetition: int = Field(ge=1)
    actors: tuple[Actor, ...]
    calls: tuple[CallRecord, ...]
    # Calls abandoned at the deadline that had not ended after the drain grace.
    pending_calls: int = Field(ge=0)
    latency_ms: dict[str, float]
    tokens: TokenUsage
    cost_usd: float = Field(ge=0)
    result: EpisodeResult


def total_usage(calls: tuple[CallRecord, ...]) -> TokenUsage:
    known = [c.usage for c in calls if c.usage is not None]
    return TokenUsage(
        input_tokens=sum(u.input_tokens for u in known),
        output_tokens=sum(u.output_tokens for u in known),
        cached_input_tokens=sum(u.cached_input_tokens for u in known),
        reasoning_tokens=sum(u.reasoning_tokens for u in known),
    )


class TraceWriter:
    """Append-only JSONL file; never overwrites another experiment's traces."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8"):
            pass
        self.path = path

    def write(self, trace: EpisodeTrace) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(trace.model_dump_json() + "\n")
            stream.flush()


def load_traces(path: Path) -> tuple[EpisodeTrace, ...]:
    return tuple(
        EpisodeTrace.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    )
