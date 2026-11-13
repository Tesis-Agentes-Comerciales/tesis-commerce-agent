"""The judge over a run: what it asks, under its own cap, and how it resumes (eval-suite.md §3).

No real model: a FakeBatchTransport answers."""

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation.llm.batch import FakeBatchTransport
from commerce_evaluation.llm.budget import LEDGER_FILE
from commerce_evaluation.llm.transport import (
    FakeReply,
    FakeTransport,
    FundsExhausted,
    LlmRequest,
    TransportError,
)
from commerce_evaluation.suite import judging
from commerce_evaluation.suite.graders.judge import JUDGE_FILE, Judged, read_judged, write_judged
from commerce_evaluation.suite.run import execute
from commerce_evaluation.suite.rundir import JUDGE_BUDGET_DIR
from tests.evaluation.suite.runs import FakeExecutor, lines, make_case, plan, write_catalog

SAID = ("No hay stock y no compré nada.",)
STOCK = make_case("sin-stock", must_communicate=["que no hay stock"])
TWO = make_case("sin-stock-ni-compra", must_communicate=["que no hay stock", "que no compró"])
SILENT = make_case("pasa-sin-juez")


def judged_run(tmp_path: Path, cases: list[Any], k: int = 1) -> Path:
    done = plan(tmp_path / "run", write_catalog(tmp_path / "cases", cases), cases, k=k)
    script = {
        (case.id, "B2", trial): [{"said": SAID}]
        for case in cases
        if case is not SILENT
        for trial in range(1, k + 1)
    }
    execute(done, FakeExecutor(script=script), say=lambda _: None)
    return done.settings.run_dir


def answering(*replies: FakeReply) -> tuple[Callable[[], FakeBatchTransport], FakeTransport]:
    """A client whose items get `replies` in order, the last one for the rest."""
    left = list(replies)

    def respond(_: LlmRequest) -> FakeReply:
        return left.pop(0) if len(left) > 1 else left[0]

    fake = FakeTransport(respond)
    return (lambda: FakeBatchTransport(fake)), fake


def verdict(said: str) -> FakeReply:
    return FakeReply(output={"verdict": said, "quote": SAID[0], "reason": "Lo dice."})


def never() -> Any:
    raise AssertionError("Nothing is left to ask: no batch is sent")


def test_pending_counts_the_questions_and_their_reservation_and_writes_nothing(
    tmp_path: Path,
) -> None:
    run_dir = judged_run(tmp_path, [STOCK, SILENT], k=2)
    before = sorted(run_dir.rglob("*"))

    todo = judging.pending(run_dir)

    assert (len(todo.trials), todo.questions, todo.answered) == (2, 2, 0)
    # Opus at batch rates, 3,000 output tokens reserved per question.
    assert Decimal("0.03") < todo.worst_usd / 2 < Decimal("0.05")
    assert todo.spent_usd == 0
    assert sorted(run_dir.rglob("*")) == before


def test_the_judge_answers_once_under_its_own_cap(tmp_path: Path) -> None:
    run_dir = judged_run(tmp_path, [STOCK, SILENT], k=2)
    client, fake = answering(verdict("pass"))

    judged = judging.judge(run_dir, client, max_usd=Decimal(1))

    assert [row.verdict for row in judged] == ["pass", "pass"] and len(fake.requests) == 2
    assert len(read_judged(run_dir)) == 2
    ledger = lines(run_dir / JUDGE_BUDGET_DIR / LEDGER_FILE)
    assert [(row["role"], row["mode"]) for row in ledger] == [("judge", "batch")] * 2
    # The run's own ledger is the agent's and the simulated user's.
    assert not (run_dir / LEDGER_FILE).exists()
    assert not (run_dir / JUDGE_BUDGET_DIR / JUDGE_FILE).exists()

    assert judging.judge(run_dir, never, max_usd=None) == []
    assert judging.pending(run_dir).answered == 2


def test_a_run_with_nothing_to_judge_is_marked_judged_without_a_call(tmp_path: Path) -> None:
    run_dir = judged_run(tmp_path, [SILENT])

    assert judging.judge(run_dir, never, max_usd=None) == []
    assert (run_dir / JUDGE_FILE).read_text() == ""


def test_a_lost_answer_is_asked_again_and_one_that_stands_is_kept(tmp_path: Path) -> None:
    run_dir = judged_run(tmp_path, [TWO])
    lost = FakeReply(raises=TransportError("BatchItemExpired"))
    client, _ = answering(verdict("pass"), lost)

    first = judging.judge(run_dir, client, max_usd=Decimal(1))

    assert [(row.verdict, row.failure) for row in first] == [
        ("pass", None),
        ("unknown", "transport"),
    ]
    todo = judging.pending(run_dir)
    assert (len(todo.trials), todo.questions, todo.answered) == (1, 2, 0)

    # The trial is asked again, whole; the first answer stands over the new one.
    client, fake = answering(verdict("fail"))
    judging.judge(run_dir, client, max_usd=Decimal(1))

    assert len(fake.requests) == 2
    answers = list(read_judged(run_dir).values())
    assert [(row.question.text, row.verdict) for row in answers] == [
        ("que no hay stock", "pass"),
        ("que no compró", "fail"),
    ]
    assert judging.pending(run_dir).answered == 1


def test_a_judges_refusal_stands(tmp_path: Path) -> None:
    run_dir = judged_run(tmp_path, [STOCK])
    client, _ = answering(FakeReply(failure="refusal"))

    [row] = judging.judge(run_dir, client, max_usd=Decimal(1))

    assert row.failure == "refusal" and judging.pending(run_dir).trials == []


def test_a_batch_that_does_not_fit_the_cap_sends_nothing(tmp_path: Path) -> None:
    run_dir = judged_run(tmp_path, [STOCK, SILENT], k=2)
    client, fake = answering(verdict("pass"))

    with pytest.raises(FundsExhausted):
        judging.judge(run_dir, client, max_usd=Decimal("0.01"))

    assert fake.requests == [] and not (run_dir / JUDGE_FILE).exists()
    ledger = lines(run_dir / JUDGE_BUDGET_DIR / LEDGER_FILE)
    assert [(row["usd"], row["error"]) for row in ledger] == [(0.0, "BudgetExhausted")] * 2


def test_answers_paid_before_the_balance_ran_out_are_kept(tmp_path: Path) -> None:
    run_dir = judged_run(tmp_path, [STOCK, SILENT], k=2)
    spent = FakeReply(raises=FundsExhausted("CreditBalanceExhausted"))
    client, _ = answering(verdict("pass"), spent)

    with pytest.raises(FundsExhausted):
        judging.judge(run_dir, client, max_usd=Decimal(1))

    assert [row.verdict for row in read_judged(run_dir).values()] == ["pass"]
    todo = judging.pending(run_dir)
    assert (len(todo.trials), todo.answered) == (1, 1) and todo.spent_usd > 0


def test_answers_a_stopped_session_left_behind_are_not_asked_again(tmp_path: Path) -> None:
    run_dir = judged_run(tmp_path, [STOCK])
    client, _ = answering(verdict("pass"))
    [row] = judging.judge(run_dir, client, max_usd=Decimal(1))
    (run_dir / JUDGE_FILE).unlink()
    write_judged(run_dir / JUDGE_BUDGET_DIR, [row])

    assert judging.pending(run_dir).answered == 1
    assert judging.judge(run_dir, never, max_usd=None) == []
    assert list(read_judged(run_dir)) == [row.question.item_id]


def test_tally_counts_verdicts_and_failures() -> None:
    def judged(said: str, failure: Any = None) -> Judged:
        return Judged.model_construct(verdict=said, failure=failure)

    rows = [judged("pass"), judged("fail"), judged("unknown", "transport")]

    assert judging.tally(rows) == {"pass": 1, "fail": 1, "unknown": 1, "failure": 1}
