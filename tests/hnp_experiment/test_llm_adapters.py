"""EC-HNP-02 model adapters, fully offline: FakeTransport or the SDK over httpx.MockTransport."""

import json
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from commerce_agent.hnp_experiment.adapters import ScriptedBuyerAgent
from commerce_agent.hnp_experiment.context import FixtureContextResolver
from commerce_agent.hnp_experiment.coordinator import ReviewCoordinator
from commerce_agent.hnp_experiment.evidence import ExperimentalEvidence
from commerce_agent.hnp_experiment.harness import load_cases
from commerce_agent.hnp_experiment.llm.config import HnpSettings
from commerce_agent.hnp_experiment.llm.cost import TokenPrices
from commerce_agent.hnp_experiment.llm.prompts import PROMPT_DIRECTORY, load_prompt
from commerce_agent.hnp_experiment.llm.schemas import (
    BUSINESS_CHECKS,
    ReviewChecks,
    ValidatorDecision,
)
from commerce_agent.hnp_experiment.llm.trace import TokenUsage
from commerce_agent.hnp_experiment.llm.transport import (
    BudgetedTransport,
    FakeReply,
    FakeTransport,
    LlmRequest,
    OpenAIResponsesTransport,
    TransportError,
    TransportTimeout,
)
from commerce_agent.hnp_experiment.llm.validator import LlmValidatorAgent
from commerce_agent.hnp_experiment.models import CHECKS
from commerce_agent.hnp_experiment.sink import SimulatedEffectSink

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
CASES = {c.case_id: c for c in load_cases(FIXTURES / "cases.json")}
PROMPT = load_prompt("validator-v1")
PRICES = TokenPrices(Decimal("1.5"), Decimal("6"))
ENVIRONMENT = {
    "HNP_VALIDATOR_MODEL": "validator-under-test",
    "HNP_BUYER_MODEL": "buyer-under-test",
    "HNP_TEMPERATURE": "none",
    "HNP_REASONING_EFFORT": "low",
    "HNP_SEED": "11",
    "HNP_TIMEOUT_SECONDS": "5",
    "HNP_MAX_OUTPUT_TOKENS": "256",
    "HNP_REPETITIONS": "2",
    "HNP_VALIDATOR_INPUT_USD_PER_MTOK": "1.5",
    "HNP_VALIDATOR_OUTPUT_USD_PER_MTOK": "6",
    "HNP_BUYER_INPUT_USD_PER_MTOK": "1.5",
    "HNP_BUYER_OUTPUT_USD_PER_MTOK": "6",
    "HNP_MAX_COST_USD": "2",
    "HNP_VALIDATOR_PROMPT": "validator-v1",
    "HNP_BUYER_PROMPT": "buyer-v1",
    "HNP_CASES_PATH": "tests/hnp_experiment/fixtures/cases.json",
    "HNP_ORACLE_PATH": "tests/hnp_experiment/fixtures/oracle.json",
    "HNP_OUTPUT_DIR": ".local/hnp02-test",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No developer HNP_* variable or API key may leak into a test."""
    import os

    for name in list(os.environ):
        if name.startswith("HNP_") or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name)
    return monkeypatch


def accept_all() -> dict[str, Any]:
    return {"checks": {c: "pass" for c in BUSINESS_CHECKS}, "decision": "accept", "reasons": []}


def validator(transport, timeout_seconds: float = 5.0) -> LlmValidatorAgent:
    return LlmValidatorAgent(
        transport=transport,
        model="validator-under-test",
        prompt=PROMPT,
        temperature=None,
        reasoning_effort=None,
        max_output_tokens=256,
        timeout_seconds=timeout_seconds,
        prices=PRICES,
        seed=11,
        call_prefix="test",
    )


def run_b(agent: LlmValidatorAgent, case_id: str = "valid", budget_ns: int = 2_000_000_000):
    case = CASES[case_id]
    sink = SimulatedEffectSink()
    coordinator = ReviewCoordinator(
        buyer=ScriptedBuyerAgent(case.buyer_payload),
        validator=agent,
        resolver=FixtureContextResolver(case.source),
        evidence=ExperimentalEvidence(),
        sink=sink,
        condition="B",
        validator_budget_ns=budget_ns,
        execution_kind="llm_dry_run",
    )
    return coordinator.run(case_id + ":test"), sink


# Configuration --------------------------------------------------------------------------


def test_settings_have_no_code_defaults_for_experiment_parameters(clean_env):
    with pytest.raises(ValidationError) as error:
        HnpSettings.load(None)
    missing = {str(e["loc"][0]) for e in error.value.errors()}
    assert {"validator_model", "temperature", "seed", "timeout_seconds", "repetitions"} <= missing
    assert {"validator_input_usd_per_mtok", "max_cost_usd", "output_dir"} <= missing


def test_documented_example_is_valid_and_cannot_go_live(clean_env):
    settings = HnpSettings.load(ROOT / ".env.example")
    assert settings.temperature is None and settings.repetitions == 5
    assert not settings.live and settings.max_cost_usd == 0
    blockers = settings.live_blockers()
    assert any("HNP_LIVE" in b for b in blockers) and any("budget" in b for b in blockers)


def test_live_gates_and_public_settings_never_expose_the_key(clean_env):
    for name, value in ENVIRONMENT.items():
        clean_env.setenv(name, value)
    clean_env.setenv("HNP_LIVE", "1")
    clean_env.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    settings = HnpSettings.load(None)
    assert settings.live_blockers() == ()
    assert settings.validator_prices == PRICES
    public = json.dumps(settings.public())
    assert "sk-test" not in public and "openai_api_key" not in public
    assert "sk-test" not in repr(settings)


# Prompts and schemas -------------------------------------------------------------------


def test_prompt_is_versioned_and_hashed_over_exact_bytes(tmp_path):
    from commerce_agent.governance.canonical import sha256_digest

    assert (PROMPT.role, PROMPT.version) == ("validator", "v1")
    assert PROMPT.sha256 == sha256_digest((PROMPT_DIRECTORY / "validator-v1.md").read_bytes())
    (tmp_path / "validator-v9.md").write_bytes(b"line\r\nline\r\n")
    with pytest.raises(ValueError, match="LF"):
        load_prompt("validator-v9", tmp_path)
    for name in ("../validator-v1", "validator", "Validator-v1"):
        with pytest.raises(ValueError):
            load_prompt(name)


def test_model_schema_asks_only_for_business_judgements():
    assert tuple(c for c in CHECKS if c != "binding") == BUSINESS_CHECKS
    assert tuple(ReviewChecks.model_fields) == BUSINESS_CHECKS
    assert set(ValidatorDecision.model_fields) == {"checks", "decision", "reasons"}
    schema = json.dumps(ValidatorDecision.model_json_schema())
    for bookkeeping in ("digest", "fact_refs", "coverage", "context_ref", "binding"):
        assert bookkeeping not in schema


# Validator adapter over FakeTransport ---------------------------------------------------


def test_accept_is_assembled_into_a_bound_assessment_and_costed():
    agent = validator(FakeTransport(lambda _: FakeReply(output=accept_all())))
    result, sink = run_b(agent)
    record = result.record
    assert record.assessment_status == "admitted" and record.assessment is not None
    assert record.assessment.validator_adapter_id == "llm-validator:1"
    assert record.assessment.coverage == CHECKS
    assert record.proposal is not None
    assert record.assessment.proposal_digest == record.proposal.proposal_digest
    assert result.effect_occurred and len(sink.events) == 1
    (call,) = agent.calls()
    assert call.status == "ok" and call.usage is not None and call.cost_usd is not None
    assert call.cost_usd == pytest.approx(float(PRICES.cost(call.usage)))
    assert call.review is not None and call.review.decision == "accept"


def test_validator_rejection_blocks_a_valid_proposal_as_false_block():
    answer = accept_all()
    answer["checks"]["budget"], answer["decision"] = "fail", "reject"
    answer["reasons"] = ["x" * 1_000] * 9
    agent = validator(FakeTransport(lambda _: FakeReply(output=answer)))
    result, sink = run_b(agent)
    assert result.record.admission.code == "VALIDATOR_REJECTED" and not sink.events
    review = agent.calls()[0].review
    assert review is not None and len(review.reasons) == 5
    assert all(len(r) == 240 for r in review.reasons)


@pytest.mark.parametrize(
    "reply,status",
    (
        (FakeReply(output={"decision": "accept"}), "schema_invalid"),
        (FakeReply(output={**accept_all(), "approved_by_human": True}), "schema_invalid"),
        (FakeReply(output={**accept_all(), "decision": "maybe"}), "schema_invalid"),
        (
            FakeReply(
                output={**accept_all(), "checks": {**accept_all()["checks"], "scope": "fail"}}
            ),
            "inconsistent",
        ),
        (FakeReply(output={**accept_all(), "decision": "reject"}), "inconsistent"),
        (FakeReply(failure="refusal"), "refusal"),
        (FakeReply(failure="incomplete"), "incomplete"),
    ),
)
def test_malformed_or_inconsistent_output_fails_closed(reply, status):
    agent = validator(FakeTransport(lambda _: reply))
    result, sink = run_b(agent)
    assert result.record.assessment_status == "invalid"
    assert result.record.admission.code == "VALIDATOR_INVALID"
    assert not result.effect_occurred and not sink.events
    assert agent.calls()[0].status == status


def test_binding_is_adapter_bookkeeping_and_never_makes_a_model_answer_inconsistent():
    from commerce_agent.governance.canonical import parse_model
    from commerce_agent.hnp_experiment.context import (
        assemble,
        proposal_envelope,
        proposal_view,
        reviewer_context,
    )
    from commerce_agent.hnp_experiment.llm.validator import assessment_from_decision
    from commerce_agent.hnp_experiment.models import BuyerDraft, wire_bytes

    case = CASES["valid"]
    context = reviewer_context(assemble(case.source))
    draft = parse_model(BuyerDraft, wire_bytes(case.buyer_payload))
    proposal = proposal_view(proposal_envelope(assemble(case.source), draft, "scripted-buyer:1"))
    foreign = proposal.model_copy(
        update={"episode_ref": proposal.episode_ref.model_copy(update={"id": "other-episode"})}
    )
    decision = ValidatorDecision.model_validate(accept_all())
    assert assessment_from_decision(decision, proposal, context, "a").verdict == "accept"
    # The model answered its ten checks consistently; the adapter's binding check rejects.
    bound = assessment_from_decision(decision, foreign, context, "a")
    assert bound.verdict == "reject"
    assert [c.check_id for c in bound.checks if c.result == "fail"] == ["binding"]


def test_transport_error_is_infrastructure_without_message():
    from commerce_agent.hnp_experiment.evaluation import is_infrastructure_failure

    error = TransportError("APIConnectionError")
    agent = validator(FakeTransport(lambda _: FakeReply(raises=error)))
    result, sink = run_b(agent)
    assert result.record.assessment_status == "unavailable"
    assert is_infrastructure_failure(result) and not sink.events
    (call,) = agent.calls()
    assert (call.status, call.error_kind, call.usage) == (
        "transport_error",
        "TransportError",
        None,
    )


def test_slow_model_times_out_at_the_deadline_and_its_call_is_still_counted():
    from time import perf_counter

    agent = validator(
        FakeTransport(lambda _: FakeReply(output=accept_all(), delay_seconds=5.0)),
        timeout_seconds=0.3,
    )
    began = perf_counter()
    result, sink = run_b(agent, budget_ns=50_000_000)
    assert perf_counter() - began < 0.3
    assert result.record.assessment_status == "timeout"
    assert result.record.admission.code == "VALIDATOR_UNAVAILABLE"
    assert not result.effect_occurred and not sink.events
    # The abandoned request ends at its own timeout; the drain waits for its record.
    assert agent.drain(1, 5.0)
    (call,) = agent.calls()
    assert call.status == "timeout" and call.error_kind == "TransportTimeout"


def test_validator_request_carries_no_leaked_fields_or_labels():
    transport = FakeTransport(lambda _: FakeReply(output=accept_all()))
    result, _ = run_b(validator(transport), "over-total")
    (request,) = transport.requests
    text = request.instructions + request.input_text
    record = result.record
    assert record.proposal is not None and record.original_context is not None
    forbidden = (
        "eligible_variant_ids",
        "buyer_adapter_id",
        "scripted-buyer",
        "justification",
        "over-total",
        "expected_",
        "oracle",
        record.proposal.proposal_digest,
        record.original_context.context_ref.digest,
        record.original_context.intent_ref.digest,
    )
    for marker in forbidden:
        assert marker not in text
    document = json.loads(request.input_text)
    assert set(document) == {
        "review_time",
        "purchase_request",
        "catalog_candidates",
        "authorized_merchant",
        "proposal",
        "checkout_terms",
        "delegation",
    }
    assert request.model == "validator-under-test" and request.max_output_tokens == 256


# OpenAI transport over the real SDK and a mock HTTP layer (no network) -----------------


def response_body(text: str | None, *, status: str = "completed") -> dict[str, Any]:
    content = (
        [{"type": "refusal", "refusal": "no"}]
        if text is None
        else [{"type": "output_text", "text": text, "annotations": []}]
    )
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "model": "validator-under-test-2026",
        "status": status,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "output": [
            {
                "type": "message",
                "id": "msg_test",
                "status": "completed",
                "role": "assistant",
                "content": content,
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 1200,
            "input_tokens_details": {"cached_tokens": 200},
            "output_tokens": 300,
            "output_tokens_details": {"reasoning_tokens": 250},
            "total_tokens": 1500,
        },
    }


def openai_transport(handler) -> tuple[OpenAIResponsesTransport, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    return OpenAIResponsesTransport(api_key=SecretStr("sk-test"), http_client=client), seen


def request(**overrides: Any) -> LlmRequest:
    values: dict[str, Any] = {
        "model": "validator-under-test",
        "instructions": PROMPT.text,
        "input_text": "{}",
        "temperature": None,
        "reasoning_effort": None,
        "max_output_tokens": 256,
        "timeout_seconds": 5.0,
    }
    return LlmRequest(**{**values, **overrides})


@pytest.fixture
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


def test_openai_transport_sends_structured_request_and_reads_usage(no_sockets):
    body = json.dumps(accept_all())
    transport, seen = openai_transport(lambda _: httpx.Response(200, json=response_body(body)))
    result = transport.parse(request(reasoning_effort="low"), ValidatorDecision)
    transport.close()
    assert result.parsed is not None and result.parsed.decision == "accept"
    assert result.failure is None and result.response_model == "validator-under-test-2026"
    assert result.usage == TokenUsage(
        input_tokens=1200, output_tokens=300, cached_input_tokens=200, reasoning_tokens=250
    )
    (sent,) = seen
    assert sent["store"] is False and sent["max_output_tokens"] == 256
    assert sent["model"] == "validator-under-test" and sent["reasoning"] == {"effort": "low"}
    assert "temperature" not in sent
    assert sent["text"]["format"]["type"] == "json_schema" and sent["text"]["format"]["strict"]
    assert sent["instructions"] == PROMPT.text


def test_openai_transport_sends_temperature_only_when_configured(no_sockets):
    body = json.dumps(accept_all())
    transport, seen = openai_transport(lambda _: httpx.Response(200, json=response_body(body)))
    transport.parse(request(temperature=0.0), ValidatorDecision)
    assert seen[0]["temperature"] == 0.0 and "reasoning" not in seen[0]


@pytest.mark.parametrize(
    "body,failure",
    (
        (response_body('{"checks":'), "schema_invalid"),
        (response_body(None), "refusal"),
        (response_body(json.dumps(accept_all()), status="incomplete"), "incomplete"),
        (response_body('{"checks":', status="incomplete"), "incomplete"),
    ),
)
def test_openai_transport_reports_answer_failures_with_usage(no_sockets, body, failure):
    transport, _ = openai_transport(lambda _: httpx.Response(200, json=body))
    result = transport.parse(request(), ValidatorDecision)
    assert result.parsed is None and result.failure == failure
    assert result.usage is not None and result.usage.input_tokens == 1200


def test_openai_transport_maps_provider_faults_without_retrying(no_sockets):
    transport, seen = openai_transport(lambda _: httpx.Response(500, json={"error": {}}))
    with pytest.raises(TransportError) as error:
        transport.parse(request(), ValidatorDecision)
    assert str(error.value) == "InternalServerError" and len(seen) == 1

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    transport, seen = openai_transport(slow)
    with pytest.raises(TransportTimeout):
        transport.parse(request(), ValidatorDecision)
    assert len(seen) == 1


def test_budget_guard_refuses_calls_that_could_exceed_the_cap():
    inner = FakeTransport(lambda _: FakeReply(output=accept_all()))
    small = request(input_text="x" * 300, max_output_tokens=1_000)
    worst = PRICES.cost(
        TokenUsage(
            input_tokens=(len(PROMPT.text) + 300 + 2) // 3,
            output_tokens=1_000,
            cached_input_tokens=0,
            reasoning_tokens=0,
        )
    )
    guarded = BudgetedTransport(inner, prices=PRICES, max_usd=worst * 2)
    assert guarded.transport_id == "fake"
    for _ in range(3):
        guarded.parse(small, ValidatorDecision)
    # Actual usage is far below the worst case, so spend is reconciled downwards.
    assert guarded.spent_usd < worst
    exhausted = BudgetedTransport(inner, prices=PRICES, max_usd=worst / 2)
    with pytest.raises(TransportError, match="BudgetExhausted"):
        exhausted.parse(small, ValidatorDecision)


def test_fake_transport_honours_request_timeout():
    released = Event()

    def respond(_: LlmRequest) -> FakeReply:
        released.set()
        return FakeReply(output=accept_all(), delay_seconds=10.0)

    with pytest.raises(TransportTimeout):
        FakeTransport(respond).parse(request(timeout_seconds=0.05), ValidatorDecision)
    assert released.is_set()
