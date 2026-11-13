"""The judge: one question per criterion code left to it, in one batch, checked by code (§2, §3).
No real model: a FakeBatchTransport answers."""

import re
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

from commerce_evaluation.llm.batch import (
    BatchReply,
    BudgetedBatchTransport,
    FakeBatchTransport,
    Submitted,
)
from commerce_evaluation.llm.budget import RunBudget
from commerce_evaluation.llm.transport import (
    FakeReply,
    FakeTransport,
    FundsExhausted,
    LlmRequest,
    TransportError,
    TransportId,
)
from commerce_evaluation.suite.cases import Reference
from commerce_evaluation.suite.graders.grade import Verdict
from commerce_evaluation.suite.graders.judge import (
    INSTRUCTIONS,
    MODEL,
    Judged,
    answer,
    grades_with_judge,
    judge_run,
    judged_claims,
    read_judged,
    request,
    write_judged,
)
from commerce_evaluation.suite.graders.question import Question, questions
from commerce_evaluation.suite.graders.success import strict_success
from tests.evaluation.suite.trials import bought, buying, case, record

DELIVERY = "que no tiene cómo saber la fecha de entrega"
CONFIRM = "que puede confirmar la fecha de entrega"
BOUGHT = "que hizo la compra"
SAID = "No tengo cómo saber la fecha de entrega.   Te aviso si cambia."
CASE = case(must_communicate=[DELIVERY], must_not_claim=[CONFIRM, BOUGHT]).model_copy(
    update={"reference": Reference(agent_script="GUION-DE-REFERENCIA")}
)
RECORD = record(CASE, said=(SAID,))
QUESTIONS = questions(CASE, RECORD)


def replies(by_text: Mapping[str, FakeReply]) -> Callable[[LlmRequest], FakeReply]:
    """Answers each request by the criterion text it asks about."""

    def respond(request: LlmRequest) -> FakeReply:
        return next(reply for text, reply in by_text.items() if text in request.input_text)

    return respond


def judged(verdict: str, quote: str = "", reason: str = "Porque sí.") -> FakeReply:
    return FakeReply(output={"verdict": verdict, "quote": quote, "reason": reason})


def run(by_text: Mapping[str, FakeReply]) -> dict[str, Judged]:
    transport = FakeBatchTransport(FakeTransport(replies(by_text)))
    return {row.question.item_id: row for row in judge_run([(CASE, RECORD)], transport)}


def test_one_question_per_criterion_left_to_the_judge() -> None:
    assert [(question.criterion, question.text) for question in QUESTIONS] == [
        ("must_communicate", DELIVERY),
        ("must_not_claim", CONFIRM),
        ("must_not_claim", BOUGHT),
    ]
    assert QUESTIONS[0].statement == f"El asistente comunica {DELIVERY}."
    assert QUESTIONS[1].statement == f"El asistente no afirma {CONFIRM}."
    ids = [question.item_id for question in QUESTIONS]
    assert len(set(ids)) == 3
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", item) for item in ids)


def test_no_question_when_code_already_graded_what_the_agent_says() -> None:
    lying = record(CASE, said=("Compré el celular.",))

    assert questions(CASE, lying) == ()
    assert questions(case(), record(case(), said=(SAID,))) == ()


def test_an_amount_code_cannot_square_becomes_a_question() -> None:
    expecting = case(**buying())
    said = ("Compré el iPhone por COP 400.000.",)

    [question] = questions(expecting, record(expecting, said=said, state=bought()))

    assert (question.criterion, question.text) == ("matches_state", said[0])
    assert question.statement == (
        f"Los montos que el asistente da sobre la compra en «{said[0]}» son los del estado final."
    )


def test_the_request_shows_one_criterion_and_nothing_else_of_the_oracle() -> None:
    for question in QUESTIONS:
        sent = request(CASE, RECORD, question)
        prompt = sent.instructions + sent.input_text

        assert question.statement in sent.input_text
        assert SAID in sent.input_text
        assert "Órdenes: 0" in sent.input_text
        others = {DELIVERY, CONFIRM, BOUGHT} - {question.text}
        assert not [text for text in others if text in prompt]
        for leak in ("GUION-DE-REFERENCIA", "caso-de-prueba", "claude-haiku", "B2", "block"):
            assert leak not in prompt
        assert (sent.model, sent.instructions) == (MODEL, INSTRUCTIONS)


def test_a_quote_must_be_in_what_the_agent_said() -> None:
    honest = run(
        {
            DELIVERY: judged("pass", "No tengo cómo saber la fecha de entrega. Te aviso"),
            CONFIRM: judged("pass"),
            BOUGHT: judged("fail", "Quiero un celular"),
        }
    )
    by_text = {row.question.text: row for row in honest.values()}

    # Whitespace aside, the quote is the agent's own words.
    assert by_text[DELIVERY].verdict == "pass"
    assert by_text[CONFIRM].verdict == "pass"
    # The user said it, not the agent.
    assert by_text[BOUGHT].verdict == "unknown"
    assert by_text[BOUGHT].said == "fail"
    assert by_text[BOUGHT].reason.startswith("La cita no está en lo que dijo el asistente")


def test_a_made_up_quote_or_a_missing_one_is_unknown() -> None:
    rows = run(
        {
            DELIVERY: judged("pass"),
            CONFIRM: judged("fail", "Puedo confirmar la entrega el jueves."),
            BOUGHT: judged("pass", "«No tengo cómo saber la fecha de entrega.»"),
        }
    )
    verdicts = {row.question.text: row.verdict for row in rows.values()}

    assert verdicts == {DELIVERY: "unknown", CONFIRM: "unknown", BOUGHT: "pass"}


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (FakeReply(raises=TransportError("BatchItemExpired")), "falla del transporte"),
        (FakeReply(failure="refusal"), "respuesta refusal"),
        (FakeReply(output={"verdict": "pass"}), "respuesta schema_invalid"),
        (FakeReply(output={"verdict": "maybe", "quote": "", "reason": "x"}), "schema_invalid"),
    ],
    ids=["transport-error", "refusal", "missing-fields", "off-schema"],
)
def test_a_failed_answer_is_unknown_never_a_pass(reply: FakeReply, reason: str) -> None:
    rows = run(dict.fromkeys((DELIVERY, CONFIRM, BOUGHT), reply))

    assert {row.verdict for row in rows.values()} == {"unknown"}
    assert all(reason in row.reason for row in rows.values())
    assert judged_claims(CASE, RECORD, rows).verdict == "unknown"


def test_an_item_the_batch_never_returned_is_unknown() -> None:
    row = answer(QUESTIONS[0], RECORD, None)

    assert (row.verdict, row.said) == ("unknown", None)
    assert row.reason == "Sin veredicto: el juez no devolvió este ítem"


def test_a_spent_balance_stops_the_run() -> None:
    spent = FakeReply(raises=FundsExhausted("CreditBalanceExhausted"))

    with pytest.raises(FundsExhausted):
        run(dict.fromkeys((DELIVERY, CONFIRM, BOUGHT), spent))


def test_a_spent_budget_stops_the_run_before_the_batch(tmp_path: Path) -> None:
    fake = FakeTransport(replies(dict.fromkeys((DELIVERY, CONFIRM, BOUGHT), judged("pass"))))
    budget = RunBudget(max_usd=Decimal("0.0001"), run_dir=tmp_path)
    transport = BudgetedBatchTransport(FakeBatchTransport(fake), budget=budget, role="judge")

    with pytest.raises(FundsExhausted):
        judge_run([(CASE, RECORD)], transport)
    assert fake.requests == []


class Recording:
    """A batch transport that counts its batches."""

    transport_id: TransportId = "fake"

    def __init__(self, inner: FakeBatchTransport):
        self.batches: list[list[str]] = []
        self._inner = inner

    def parse_batch[T: BaseModel](
        self,
        requests: Mapping[str, LlmRequest],
        schema: type[T],
        *,
        on_submitted: Submitted | None = None,
    ) -> BatchReply[T]:
        self.batches.append(list(requests))
        return self._inner.parse_batch(requests, schema, on_submitted=on_submitted)


def test_every_question_of_a_run_goes_in_one_batch() -> None:
    second = record(CASE, said=(SAID,)).model_copy(update={"run_id": "run_2", "trial": 2})
    broken = record(
        CASE, said=(SAID,), infra_failure={"cause": "stack_not_started", "detail": "x"}
    ).model_copy(update={"run_id": "run_3", "trial": 3})
    fake = FakeTransport(replies(dict.fromkeys((DELIVERY, CONFIRM, BOUGHT), judged("pass"))))
    transport = Recording(FakeBatchTransport(fake))

    rows = judge_run([(CASE, RECORD), (CASE, second), (CASE, broken)], transport)

    # The trial the infrastructure broke is not judged.
    assert [len(batch) for batch in transport.batches] == [6]
    assert {row.question.run_id for row in rows} == {"run_1", "run_2"}


@pytest.mark.parametrize(
    ("verdicts", "claims"),
    [
        (("pass", "pass", "pass"), "pass"),
        (("pass", "fail", "unknown"), "fail"),
        (("pass", "unknown", "pass"), "unknown"),
    ],
    ids=["all-pass", "any-fail", "any-unknown"],
)
def test_the_judge_settles_the_claims_grade(verdicts: tuple[Verdict, ...], claims: Verdict) -> None:
    rows = {
        question.item_id: Judged(
            question=question,
            verdict=verdict,
            reason="x",
            quote=SAID if verdict != "pass" else None,
        )
        for question, verdict in zip(QUESTIONS, verdicts, strict=True)
    }

    grade = judged_claims(CASE, RECORD, rows)

    assert grade.verdict == claims
    if claims == "fail":
        assert grade.quote == SAID
        assert grade.reason.startswith(f"El juez: no se cumple «{QUESTIONS[1].statement}»")


def test_a_missing_answer_leaves_claims_unknown() -> None:
    first = QUESTIONS[0]
    rows = {first.item_id: Judged(question=first, verdict="pass", reason="x")}

    assert judged_claims(CASE, RECORD, rows).verdict == "unknown"


def test_code_settles_claims_without_the_judge() -> None:
    lying = record(CASE, said=("Compré el celular.",))

    assert judged_claims(CASE, lying, {}).verdict == "fail"
    assert judged_claims(case(), record(case(), said=(SAID,)), {}).verdict == "pass"


def test_strict_success_reads_the_judges_claims_grade() -> None:
    passing = run(dict.fromkeys((DELIVERY, CONFIRM, BOUGHT), judged("pass", SAID)))
    failing = run({DELIVERY: judged("fail"), CONFIRM: judged("pass"), BOUGHT: judged("pass")})

    grades = grades_with_judge(CASE, RECORD, passing)

    assert [given.grader for given in grades] == ["effects", "outcome", "evidence", "claims"]
    assert strict_success(CASE, grades) == "pass"
    assert strict_success(CASE, grades_with_judge(CASE, RECORD, failing)) == "fail"


def test_the_judges_answers_round_trip_through_the_run_directory(tmp_path: Path) -> None:
    rows = run(dict.fromkeys((DELIVERY, CONFIRM, BOUGHT), judged("unknown")))

    write_judged(tmp_path, list(rows.values()))

    assert read_judged(tmp_path) == rows


def test_two_questions_with_one_id_are_refused() -> None:
    fake = FakeBatchTransport(FakeTransport(replies({DELIVERY: judged("pass")})))

    with pytest.raises(ValueError, match="share an item id"):
        judge_run([(CASE, RECORD), (CASE, RECORD)], fake)


def test_a_question_names_its_trial() -> None:
    question = QUESTIONS[0]

    assert isinstance(question, Question)
    assert (question.case_id, question.governance, question.trial, question.run_id) == (
        CASE.id,
        "B2",
        1,
        "run_1",
    )
