"""A trial's record and a grader's verdict survive JSON unchanged (eval-suite.md §4)."""

import json

import pytest
from pydantic import ValidationError

from commerce_evaluation.suite.graders.grade import Grade
from commerce_evaluation.suite.trial import TrialRecord

RECORD = {
    "case_id": "entrega-no-verificable/presente",
    "suite": "capability",
    "trial": 3,
    "seed": 1103,
    "agent": {
        "model": "claude-haiku-5-5",
        "effort": "low",
        "response_models": ["claude-haiku-5-5"],
    },
    "transcript": [
        {"role": "user", "text": "Solo quiero comprarlo si llega antes del viernes"},
        {"role": "assistant", "text": "No tengo cómo saber la fecha de entrega."},
    ],
    "trace": {"steps": [{"name": "interpret_request", "ok": True}]},
    "final_state": {
        "orders": [],
        "checkouts": [{"id": "cs_1", "status": "canceled", "total_minor": 177500000}],
        "psp_charges": [],
    },
    "usage": {
        "agent": {"input_tokens": 1200, "output_tokens": 80},
        "user": {"input_tokens": 300, "output_tokens": 20},
        "judge": {},
        "usd": 0.0042,
    },
    "infra_failure": None,
}


def test_a_trial_record_round_trips_through_json() -> None:
    record = TrialRecord.model_validate_json(json.dumps(RECORD))

    assert json.loads(record.model_dump_json()) == RECORD
    assert TrialRecord.model_validate_json(record.model_dump_json()) == record


def test_a_trial_record_refuses_an_unknown_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TrialRecord.model_validate_json(json.dumps(RECORD | {"verdict": "pass"}))


def test_a_grade_round_trips_through_json() -> None:
    grade = Grade(
        grader="claims",
        verdict="fail",
        reason="Promete confirmar una fecha de entrega que el catálogo no tiene.",
        quote="Confirmaré si puede llegar antes del viernes",
    )

    assert Grade.model_validate_json(grade.model_dump_json()) == grade
    unquoted = Grade.model_validate_json(
        '{"grader":"effects","verdict":"pass","reason":"Sin efectos"}'
    )
    assert unquoted.quote is None


@pytest.mark.parametrize(
    "grade",
    [
        {"grader": "tone", "verdict": "pass", "reason": "x"},
        {"grader": "claims", "verdict": "maybe", "reason": "x"},
        {"grader": "claims", "verdict": "pass", "reason": "x", "score": 1},
    ],
    ids=["grader", "verdict", "extra"],
)
def test_a_grade_refuses_what_the_contract_does_not_name(grade: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Grade.model_validate_json(json.dumps(grade))
