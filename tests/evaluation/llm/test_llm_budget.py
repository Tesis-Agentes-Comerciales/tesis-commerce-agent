"""The run's spending cap, its usage ledger and the batch interface, over the offline fakes."""

import json
from decimal import Decimal
from math import ceil
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from commerce_evaluation.llm.batch import BudgetedBatchTransport, FakeBatchTransport
from commerce_evaluation.llm.budget import BudgetedTransport, RunBudget
from commerce_evaluation.llm.cost import CLAUDE_PRICES, TokenPrices, TokenUsage
from commerce_evaluation.llm.transport import (
    FakeReply,
    FakeTransport,
    FundsExhausted,
    LlmRequest,
    TransportError,
)

PRICES = TokenPrices(Decimal("1.5"), Decimal("6"), Decimal("0.15"), Decimal("1.875"))
INSTRUCTIONS = "Decide whether the proposal stays within the delegated scope."
SECRET = "sk-ant-api03-never-in-the-ledger"
ACCEPT = {"decision": "accept"}


class Verdict(BaseModel):
    decision: Literal["accept", "reject"]


SCHEMA = json.dumps(Verdict.model_json_schema())


def request(**overrides: Any) -> LlmRequest:
    values: dict[str, Any] = {
        "model": "model-under-test",
        "instructions": INSTRUCTIONS,
        "input_text": "{}",
        "effort": "low",
        "max_output_tokens": 256,
        "timeout_seconds": 5.0,
    }
    return LlmRequest(**{**values, **overrides})


def usage(input_tokens: int, output_tokens: int, read: int = 0, write: int = 0) -> TokenUsage:
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=read,
        cache_write_tokens=write,
        reasoning_tokens=0,
    )


def worst_case(llm_request: LlmRequest, prices: TokenPrices = PRICES) -> Decimal:
    # The schema travels in output_config.format, so it counts as input too.
    sent = llm_request.instructions + llm_request.input_text + SCHEMA
    return prices.cost(usage(ceil(len(sent) / 2), llm_request.max_output_tokens))


def budget(tmp_path: Path, max_usd: Decimal, **prices: TokenPrices) -> RunBudget:
    return RunBudget(
        max_usd=max_usd, run_dir=tmp_path / "run", prices=prices or {"model-under-test": PRICES}
    )


def ledger(run_budget: RunBudget) -> list[dict[str, Any]]:
    lines = run_budget.ledger_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.parametrize(
    ("model", "tokens", "batch", "usd"),
    [
        ("claude-opus-5-5", usage(1_000_000, 1_000_000, 1_000_000, 1_000_000), False, "29.2"),
        ("claude-sonnet-5-5", usage(1_000_000, 1_000_000, 1_000_000, 1_000_000), False, "14.6"),
        # 100K prompt tokens still bill at the short card; one more goes to the long one.
        ("claude-haiku-5-5", usage(50_000, 1_000, 40_000, 10_000), False, "0.00715"),
        ("claude-haiku-5-5", usage(50_000, 1_000, 40_001, 10_000), False, "0.03575005"),
        ("claude-opus-5-5", usage(1_000_000, 1_000_000), True, "12"),
        ("claude-haiku-5-5", usage(150_000, 1_000), True, "0.03875"),
    ],
    ids=["opus", "sonnet", "haiku-short", "haiku-long", "opus-batch", "haiku-long-batch"],
)
def test_claude_prices_follow_the_published_rate_cards(
    model: str, tokens: TokenUsage, batch: bool, usd: str
):
    prices = CLAUDE_PRICES[model].batch() if batch else CLAUDE_PRICES[model]

    assert prices.cost(tokens) == Decimal(usd)


def test_budget_reconciles_spend_and_cuts_off_before_the_cap(tmp_path: Path):
    inner = FakeTransport(lambda _: FakeReply(output=ACCEPT))
    small = request(input_text="x" * 300, max_output_tokens=1_000)
    roomy = budget(tmp_path / "roomy", worst_case(small) * 2)
    guarded = BudgetedTransport(inner, budget=roomy, role="simulator")
    assert guarded.transport_id == "fake"
    for _ in range(3):
        guarded.parse(small, Verdict)
    # Actual usage is far below the worst case, so spend is reconciled downwards.
    assert roomy.spent_usd < worst_case(small)

    tight = budget(tmp_path / "tight", worst_case(small) / 2)
    with pytest.raises(FundsExhausted, match="BudgetExhausted"):
        BudgetedTransport(inner, budget=tight, role="simulator").parse(small, Verdict)

    assert len(inner.requests) == 3
    [refused] = ledger(tight)
    assert (refused["error"], refused["usd"], refused["latency_ms"]) == ("BudgetExhausted", 0, None)


def test_one_cap_holds_across_the_simulator_and_the_judge(tmp_path: Path):
    simulator_request = request(model="claude-sonnet-5-5", max_output_tokens=2_000)
    judge_request = request(model="claude-opus-5-5", max_output_tokens=4_000)
    judge_worst = worst_case(judge_request, CLAUDE_PRICES["claude-opus-5-5"].batch())
    run_budget = RunBudget(max_usd=judge_worst * 2, run_dir=tmp_path)
    fake = FakeTransport(lambda _: FakeReply(output=ACCEPT))
    simulator = BudgetedTransport(fake, budget=run_budget, role="simulator")
    judge = BudgetedBatchTransport(FakeBatchTransport(fake), budget=run_budget, role="judge")

    judge.parse_batch({"c1": judge_request}, Verdict)
    simulator.parse(simulator_request, Verdict)
    with pytest.raises(FundsExhausted, match="BudgetExhausted"):
        judge.parse_batch({"c2": judge_request, "c3": judge_request}, Verdict)

    assert [(row["role"], row["custom_id"], row["error"]) for row in ledger(run_budget)] == [
        ("judge", "c1", None),
        ("simulator", None, None),
        ("judge", "c2", "BudgetExhausted"),
        ("judge", "c3", "BudgetExhausted"),
    ]
    assert run_budget.spent_usd <= run_budget.max_usd


def test_ledger_records_every_call_without_prompts(tmp_path: Path):
    replies = iter(
        [
            FakeReply(output=ACCEPT),
            FakeReply(failure="refusal"),
            FakeReply(raises=TransportError("RateLimitError")),
        ]
    )
    run_budget = budget(tmp_path, Decimal(1))
    guarded = BudgetedTransport(
        FakeTransport(lambda _: next(replies)), budget=run_budget, role="judge"
    )
    secret_request = request(instructions=f"Key {SECRET}", input_text=f"Card {SECRET}")

    accepted = guarded.parse(secret_request, Verdict)
    refused = guarded.parse(secret_request, Verdict)
    with pytest.raises(TransportError, match="RateLimitError"):
        guarded.parse(secret_request, Verdict)

    assert SECRET not in run_budget.ledger_path.read_text(encoding="utf-8")
    success, refusal, error = ledger(run_budget)
    assert accepted.usage is not None and refused.usage is not None
    assert {key: success[key] for key in success if key not in ("at", "latency_ms")} == {
        "role": "judge",
        "transport": "fake",
        "mode": "sync",
        "model_requested": "model-under-test",
        "model_returned": "model-under-test",
        "effort": "low",
        "batch_id": None,
        "custom_id": None,
        "usage": accepted.usage.model_dump(),
        "usd": float(PRICES.cost(accepted.usage)),
        "attempts": 1,
        "failure": None,
        "error": None,
    }
    assert isinstance(success["latency_ms"], int) and success["latency_ms"] >= 0
    assert (refusal["failure"], refusal["usd"]) == ("refusal", float(PRICES.cost(refused.usage)))
    # No usage came back, so the call keeps its worst case: it may have been billed.
    assert (error["error"], error["usage"]) == ("RateLimitError", None)
    assert error["usd"] == float(worst_case(secret_request))
    assert run_budget.spent_usd == (
        PRICES.cost(accepted.usage) + PRICES.cost(refused.usage) + worst_case(secret_request)
    )


def test_an_unpriced_model_is_refused_before_any_call(tmp_path: Path):
    fake = FakeTransport(lambda _: FakeReply(output=ACCEPT))
    guarded = BudgetedTransport(fake, budget=budget(tmp_path, Decimal(1)), role="simulator")

    with pytest.raises(ValueError, match="No price for model 'claude-unknown'"):
        guarded.parse(request(model="claude-unknown"), Verdict)

    assert fake.requests == []


def test_a_batch_is_priced_at_batch_rates_and_recorded_item_by_item(tmp_path: Path):
    def respond(llm_request: LlmRequest) -> FakeReply:
        if llm_request.input_text == "errored":
            return FakeReply(raises=TransportError("BatchItemErrored:overloaded_error"))
        return FakeReply(output=ACCEPT)

    run_budget = budget(tmp_path, Decimal(1))
    judge = BudgetedBatchTransport(
        FakeBatchTransport(FakeTransport(respond)), budget=run_budget, role="judge"
    )
    items = {"c1": request(input_text="fine"), "c2": request(input_text="errored")}

    reply = judge.parse_batch(items, Verdict)

    parsed, errored = reply.results["c1"], reply.results["c2"]
    assert not isinstance(parsed, TransportError) and parsed.parsed == Verdict(decision="accept")
    assert (
        isinstance(errored, TransportError) and str(errored) == "BatchItemErrored:overloaded_error"
    )
    assert parsed.usage is not None
    first, second = ledger(run_budget)
    assert (first["mode"], first["batch_id"], first["custom_id"]) == ("batch", reply.batch_id, "c1")
    assert first["usd"] == float(PRICES.batch().cost(parsed.usage))
    assert first["usd"] == float(PRICES.cost(parsed.usage) / 2)
    assert (second["custom_id"], second["error"]) == ("c2", "BatchItemErrored:overloaded_error")
    assert second["usd"] == float(worst_case(items["c2"], PRICES.batch()))


def test_the_worst_case_counts_the_schema_at_two_characters_per_token(tmp_path: Path):
    fake = FakeTransport(lambda _: FakeReply(output=ACCEPT))
    small = request(max_output_tokens=16)
    exact = budget(tmp_path / "exact", worst_case(small))
    short = budget(tmp_path / "short", worst_case(small) - Decimal("0.000000001"))

    BudgetedTransport(fake, budget=exact, role="judge").parse(small, Verdict)
    with pytest.raises(FundsExhausted, match="BudgetExhausted"):
        BudgetedTransport(fake, budget=short, role="judge").parse(small, Verdict)


def test_an_interrupted_call_is_recorded_and_keeps_its_reservation(tmp_path: Path):
    run_budget = budget(tmp_path, Decimal(1))
    fake = FakeTransport(lambda _: FakeReply(raises=KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        BudgetedTransport(fake, budget=run_budget, role="simulator").parse(request(), Verdict)

    [row] = ledger(run_budget)
    assert (row["error"], row["usd"], row["attempts"]) == (
        "KeyboardInterrupt",
        float(worst_case(request())),
        1,
    )
    assert run_budget.spent_usd == worst_case(request())


def test_an_interrupted_batch_leaves_its_id_in_the_run_directory(tmp_path: Path):
    run_budget = budget(tmp_path, Decimal(1))
    fake = FakeBatchTransport(FakeTransport(lambda _: FakeReply(raises=KeyboardInterrupt())))
    judge = BudgetedBatchTransport(fake, budget=run_budget, role="judge")

    with pytest.raises(KeyboardInterrupt):
        judge.parse_batch({"c1": request(), "c2": request()}, Verdict)

    [submitted] = [json.loads(line) for line in run_budget.batches_path.read_text().splitlines()]
    assert (submitted["batch_id"], submitted["role"]) == ("fake_batch_1", "judge")
    assert submitted["custom_ids"] == ["c1", "c2"]
    rows = ledger(run_budget)
    assert {(row["batch_id"], row["error"]) for row in rows} == {
        ("fake_batch_1", "KeyboardInterrupt")
    }


@pytest.mark.parametrize("custom_id", ["c 1", "c1/2", "x" * 65, ""], ids=repr)
def test_custom_ids_are_checked_before_anything_is_reserved(tmp_path: Path, custom_id: str):
    run_budget = budget(tmp_path, Decimal(1))
    fake = FakeTransport(lambda _: FakeReply(output=ACCEPT))
    judge = BudgetedBatchTransport(FakeBatchTransport(fake), budget=run_budget, role="judge")

    with pytest.raises(ValueError, match="custom_id"):
        judge.parse_batch({"ok_1": request(), custom_id: request()}, Verdict)

    assert fake.requests == [] and not run_budget.ledger_path.exists()


def test_the_agent_spend_is_charged_to_the_same_cap_and_never_raises(tmp_path: Path):
    agent_usage = usage(400_000, 20_000)
    run_budget = budget(tmp_path, PRICES.cost(agent_usage) + worst_case(request()) / 2)
    simulator = BudgetedTransport(
        FakeTransport(lambda _: FakeReply(output=ACCEPT)), budget=run_budget, role="simulator"
    )

    usd = run_budget.charge(
        role="agent", model="model-under-test", usage=agent_usage, model_returned="served"
    )
    assert not run_budget.exhausted
    with pytest.raises(FundsExhausted, match="BudgetExhausted"):
        simulator.parse(request(), Verdict)
    # Past the cap the agent's spend is still recorded, and the runner reads `exhausted`.
    assert run_budget.charge(role="agent", model="model-under-test", usage=agent_usage) == usd
    assert run_budget.exhausted
    with pytest.raises(FundsExhausted, match="BudgetExhausted"):
        simulator.parse(request(), Verdict)

    assert usd == PRICES.cost(agent_usage)
    charged, refused, crossing, refused_again = ledger(run_budget)
    assert (charged["role"], charged["usage"], charged["usd"]) == (
        "agent",
        agent_usage.model_dump(),
        float(usd),
    )
    assert (charged["model_returned"], charged["error"]) == ("served", None)
    assert (refused["role"], refused["error"]) == ("simulator", "BudgetExhausted")
    assert (crossing["role"], crossing["error"]) == ("agent", "BudgetExhausted")
    assert (refused_again["role"], refused_again["usd"]) == ("simulator", 0.0)
    assert run_budget.spent_usd == 2 * usd


def test_a_cap_reached_exactly_is_exhausted(tmp_path: Path):
    agent_usage = usage(400_000, 20_000)
    run_budget = budget(tmp_path, PRICES.cost(agent_usage))

    run_budget.charge(role="agent", model="model-under-test", usage=agent_usage)

    assert run_budget.exhausted
    assert budget(tmp_path, PRICES.cost(agent_usage) * 2).exhausted is False


@pytest.mark.parametrize("max_usd", ["0", "-1", "NaN", "Infinity"])
def test_the_cap_is_a_positive_amount(tmp_path: Path, max_usd: str):
    with pytest.raises(ValueError, match="positive"):
        budget(tmp_path, Decimal(max_usd))


def test_a_resumed_run_keeps_what_it_already_spent(tmp_path: Path):
    first = budget(tmp_path, Decimal(1))
    fake = FakeTransport(lambda _: FakeReply(output=ACCEPT))
    BudgetedTransport(fake, budget=first, role="judge").parse(request(), Verdict)

    resumed = budget(tmp_path, Decimal(1))

    assert first.spent_usd > 0
    assert abs(resumed.spent_usd - first.spent_usd) < Decimal("1e-12")
