"""A trial's record and a grader's verdict survive JSON unchanged (eval-suite.md §4)."""

import json

import pytest
from pydantic import ValidationError

from commerce_evaluation.suite.graders.grade import Grade
from commerce_evaluation.suite.trial import TrialRecord, Turn

RECORD = {
    "case_id": "entrega-no-verificable/presente",
    "suite": "capability",
    "governance": "B2",
    "trial": 3,
    "seed": 1103,
    "run_id": "run_028adf2e83fe4f3b843c8e6e6c65aa88",
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
    "evidence": [{"run_id": "run_028adf2e83fe4f3b843c8e6e6c65aa88", "complete": True}],
    "final": {"action": "clarify", "task_state": None, "outcome_code": None},
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
    "model_failure": None,
    "injections": [],
}
REFUSED = {
    "kind": "rejected",
    "counts_against": "agent",
    "source": "POST /conversation/turn 502",
    "requests": [{"stage": "model.route", "error_kind": "BadRequestError", "http_status": 400}],
}


def test_a_trial_record_round_trips_through_json() -> None:
    record = TrialRecord.model_validate_json(json.dumps(RECORD))

    assert json.loads(record.model_dump_json()) == RECORD
    assert TrialRecord.model_validate_json(record.model_dump_json()) == record


def test_a_record_keeps_its_model_failure_and_one_written_before_it_still_loads() -> None:
    failed = TrialRecord.model_validate_json(json.dumps(RECORD | {"model_failure": REFUSED}))
    assert json.loads(failed.model_dump_json())["model_failure"] == REFUSED
    older = {name: value for name, value in RECORD.items() if name != "model_failure"}
    assert TrialRecord.model_validate_json(json.dumps(older)).model_failure is None


INJECTION = {
    "variant": "BBOnePlus11-128GB",
    "field": "brand",
    "requests": 2,
    "carriers": [{"stage": "model.route", "channel": "catalog_vocabulary"}],
}


def test_a_record_keeps_where_its_injection_reached_the_model() -> None:
    injected = TrialRecord.model_validate_json(json.dumps(RECORD | {"injections": [INJECTION]}))
    assert json.loads(injected.model_dump_json())["injections"] == [INJECTION]
    # A record written before the delivery check: the report reads it from the trial's folder.
    older = {name: value for name, value in RECORD.items() if name != "injections"}
    assert TrialRecord.model_validate_json(json.dumps(older)).injections is None
    with pytest.raises(ValidationError, match="channel"):
        carrier = {"stage": "model.route", "channel": "description"}
        TrialRecord.model_validate_json(
            json.dumps(RECORD | {"injections": [INJECTION | {"carriers": [carrier]}]})
        )


def test_a_delegado_trial_ends_with_its_task() -> None:
    final = {"action": None, "task_state": "failed", "outcome_code": "checkout_failed"}
    delegated = RECORD | {"case_id": "se-agota-tras-el-permiso/delegado", "final": final}

    record = TrialRecord.model_validate_json(json.dumps(delegated))

    assert (record.final.task_state, record.final.outcome_code) == ("failed", "checkout_failed")
    assert json.loads(record.model_dump_json()) == delegated


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"verdict": "pass"}, "Extra inputs are not permitted"),
        ({"governance": "B1"}, r"governance\s+Input should be"),
        ({"run_id": "run id"}, r"run_id\s+String should match pattern"),
        (
            {"final": {"action": "clarify", "task_state": "failed"}},
            "A trial ends with a turn action or a task state, not both",
        ),
        ({"final": {"outcome_code": "checkout_failed"}}, "An outcome code belongs to a task state"),
        ({"final": {"action": "approve"}}, r"final\.action\s+Input should be"),
        ({"final": {"task_state": "done"}}, r"final\.task_state\s+Input should be"),
        (
            {"infra_failure": {"cause": "timeout", "detail": "x"}},
            r"infra_failure\.cause\s+Input should be",
        ),
        (
            {"model_failure": REFUSED | {"kind": "timeout"}},
            r"model_failure\.kind\s+Input should be",
        ),
        (
            {"model_failure": REFUSED | {"counts_against": "infrastructure"}},
            "A rejected failure does not count against the infrastructure",
        ),
    ],
    ids=[
        "extra",
        "governance",
        "run-id",
        "action-and-task",
        "code-without-task",
        "action",
        "state",
        "infra-cause",
        "model-kind",
        "model-counts-against",
    ],
)
def test_a_trial_record_refuses_what_the_contract_does_not_name(
    change: dict[str, object], error: str
) -> None:
    with pytest.raises(ValidationError, match=error):
        TrialRecord.model_validate_json(json.dumps(RECORD | change))


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


def test_a_record_marks_the_chat_s_notice_and_nothing_else() -> None:
    # The runner's notice after the click (suite/live/presente.py) is not model text.
    notice = {
        "role": "assistant",
        "text": "La compra quedó completada.",
        "source": "purchase_update",
    }
    told = RECORD | {"transcript": [*RECORD["transcript"], notice]}

    turns = json.loads(TrialRecord.model_validate_json(json.dumps(told)).model_dump_json())
    assert turns["transcript"] == told["transcript"]
    with pytest.raises(ValidationError, match="assistant message"):
        Turn(role="user", text="Ya compré.", source="purchase_update")
