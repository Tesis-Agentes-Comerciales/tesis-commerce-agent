"""The judge over a run, under its own cap, for `eval-suite judge` (docs/design/eval-suite.md §3,
§8).

Every question of the run's kept trials (graders/question.py) goes to the judge in one Message
Batch (graders/judge.py), through a `RunBudget` of its own in `judge/`: the run's cap is spent on
the agent and the simulated user, and the judge's `--max-usd` sums every session of the judge.
The answers go to `judge.jsonl`, which the report folds into the `claims` grade.

It resumes. A trial whose every question has an answer is not asked again. An answer the
transport lost (`failure` transport or missing) is no answer, so its trial is asked again, whole;
an answer that stands is never replaced, even when its trial is asked again. The judge's own
refusals, truncations and off-schema answers stand: asking until a convenient answer came would
bias the grade. When the balance runs out mid-batch, the answers paid for are kept. A batch is
reserved whole: if its worst case does not fit in what is left of the cap, nothing is sent.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from commerce_evaluation.llm.batch import BudgetedBatchTransport, StructuredBatchLLM
from commerce_evaluation.llm.budget import BATCHES_FILE, LEDGER_FILE, Call, RunBudget, worst_usage
from commerce_evaluation.llm.cost import CLAUDE_PRICES
from commerce_evaluation.suite.cases import Case
from commerce_evaluation.suite.graders.judge import (
    JUDGE_FILE,
    MODEL,
    Judged,
    JudgeReply,
    judge_run,
    read_judged,
    request,
    write_judged,
)
from commerce_evaluation.suite.graders.question import questions
from commerce_evaluation.suite.reduce import is_infra_failure
from commerce_evaluation.suite.rundir import (
    JUDGE_BUDGET_DIR,
    read_cases,
    read_jsonl,
    read_trials,
    repair_jsonl,
)
from commerce_evaluation.suite.trial import TrialRecord

Trial = tuple[Case, TrialRecord]
# Answers the transport lost: their trial is asked again.
LOST = frozenset({"transport", "missing"})


@dataclass(frozen=True)
class Pending:
    # The trials to ask, with every one of their questions.
    trials: list[Trial]
    questions: int
    # Trials whose every question already has an answer.
    answered: int
    # What the batch reserves at once: every question's worst case at the judge's batch price.
    worst_usd: Decimal
    # What earlier sessions of the judge spent, against the same cap.
    spent_usd: Decimal


def batch_client() -> StructuredBatchLLM:
    """Claude's Message Batches, imported only when the judge runs."""
    from anthropic import Anthropic

    from commerce_evaluation.llm.claude import AnthropicBatchTransport

    return AnthropicBatchTransport(Anthropic())


def pending(run_dir: Path) -> Pending:
    """What a judge session would ask now. Reads the run directory and writes nothing."""
    cases, answered = read_cases(run_dir), _answers(run_dir)
    prices = CLAUDE_PRICES[MODEL].batch()
    trials: list[Trial] = []
    asked, done, worst = 0, 0, Decimal(0)
    for record in read_trials(run_dir).kept:
        case = cases[record.case_id]
        if is_infra_failure(case, record) or not (wanted := questions(case, record)):
            continue
        if all(_stands(answered.get(question.item_id)) for question in wanted):
            done += 1
            continue
        trials.append((case, record))
        asked += len(wanted)
        for question in wanted:
            call = Call("judge", "anthropic", "batch", request(case, record, question), JudgeReply)
            worst += prices.cost(worst_usage(call))
    ledger = read_jsonl(run_dir / JUDGE_BUDGET_DIR / LEDGER_FILE)
    spent = sum((Decimal(str(row["usd"])) for row in ledger), Decimal(0))
    return Pending(trials, asked, done, worst, spent)


def judge(
    run_dir: Path, client: Callable[[], StructuredBatchLLM], *, max_usd: Decimal | None
) -> list[Judged]:
    """Asks what is pending under the judge's cap and adds the answers to `judge.jsonl`; the
    client is built only when there is something to ask, and then the cap is required.

    Raises `FundsExhausted` when the cap or the balance refuses the batch or one of its items,
    after keeping what was paid for."""
    _merge(run_dir)
    todo = pending(run_dir)
    if not todo.trials:
        if not (run_dir / JUDGE_FILE).exists():
            write_judged(run_dir, [])
        return []
    if max_usd is None:
        raise ValueError("The judge calls a real LLM: it needs a cap")
    repair_jsonl(run_dir / JUDGE_BUDGET_DIR / LEDGER_FILE)
    budget = RunBudget(max_usd=max_usd, run_dir=run_dir / JUDGE_BUDGET_DIR)
    capped = BudgetedBatchTransport(client(), budget=budget, role="judge")
    try:
        return judge_run(todo.trials, capped, run_dir=run_dir / JUDGE_BUDGET_DIR)
    finally:
        _merge(run_dir)


def last_batch(run_dir: Path) -> str | None:
    """The id of the judge's last batch, which keeps running after an interrupted wait."""
    rows = read_jsonl(run_dir / JUDGE_BUDGET_DIR / BATCHES_FILE)
    return rows[-1]["batch_id"] if rows else None


def tally(judged: Iterable[Judged]) -> dict[str, int]:
    counts = {"pass": 0, "fail": 0, "unknown": 0, "failure": 0}
    for row in judged:
        counts[row.verdict] += 1
        counts["failure"] += row.failure is not None
    return counts


def _stands(answer: Judged | None) -> bool:
    return answer is not None and answer.failure not in LOST


def _answers(run_dir: Path) -> dict[str, Judged]:
    """The run's answers, and those of the session in `judge/` where the run had none that
    stands."""
    answers = read_judged(run_dir) if (run_dir / JUDGE_FILE).exists() else {}
    if (run_dir / JUDGE_BUDGET_DIR / JUDGE_FILE).exists():
        for item_id, answer in read_judged(run_dir / JUDGE_BUDGET_DIR).items():
            if not _stands(answers.get(item_id)):
                answers[item_id] = answer
    return answers


def _merge(run_dir: Path) -> None:
    """Moves the session's answers (`judge_run` writes them to `judge/`) into `judge.jsonl`; a
    question answered again keeps its place."""
    session = run_dir / JUDGE_BUDGET_DIR / JUDGE_FILE
    if session.exists():
        write_judged(run_dir, list(_answers(run_dir).values()))
        session.unlink()
