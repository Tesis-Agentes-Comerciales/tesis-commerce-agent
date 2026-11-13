"""`AGENT_HNP_REVIEWER=llm` (docs/design/hnp-intent-review.md §3, §4 and §7): the model reviewer,
its strict structured output, its timing and its `model_requests` row.

The reviewer builds its real SDK client; only the transport is the scripted
`httpx2.MockTransport` of tests/support/model_server.py. No request leaves the process and no
model is called.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from commerce_agent.application.hnp_intent_review import (
    REVIEW_STAGE,
    HnpIntentReviewer,
    IntentReview,
)
from commerce_agent.domain import HardConstraints, PurchaseIntent, rank_candidates
from commerce_agent.llm.conversation_router import IntentInterpretationError
from commerce_agent.llm.delegated_choice import ClaudeDelegatedChooser
from commerce_agent.llm.intent_review import PROMPT_ID, ModelHnpIntentReviewer
from commerce_agent.llm.model_requests import model_request_scope
from commerce_agent.persistence.model_requests_sqlite import SqliteModelRequests
from commerce_agent.persistence.stage_timings_sqlite import SqliteStageTimings
from tests.support.hnp_executor import Bench
from tests.support.model_server import (
    ModelServer,
    failure,
    output_schema,
    shortlisted_choice,
    structured,
    text,
)
from tests.test_conversation_router import FEED

API_KEY = "sk-ant-test-intent-review-0123456789abcdef"
RUN_ID = "run_" + "b" * 32
INTENT = PurchaseIntent(
    hard=HardConstraints(currency="COP", categories=("headphones",), max_total_amount_minor=150_000)
)
SHORTLIST = rank_candidates(FEED, INTENT).ranked
PREFERENCE = "Quiero los SON-01. No compres otro modelo."


def verdict(decision: str, reason: str) -> Any:
    return structured(lambda body: {"decision": decision, "reason": reason})


def stores(path: Path) -> tuple[SqliteStageTimings, SqliteModelRequests]:
    timings = SqliteStageTimings(path)
    timings.initialize()
    timings.initialize_model_tokens()
    requests = SqliteModelRequests(path)
    requests.initialize()
    return timings, requests


def reviewer(path: Path, monkeypatch: pytest.MonkeyPatch, server: ModelServer | None = None):
    if server is not None:
        server.install(monkeypatch)
    timings, requests = stores(path)
    return ModelHnpIntentReviewer(
        api_key=SecretStr(API_KEY),
        model="test-model",
        timer=timings,
        effort="low",
        model_requests=requests,
    )


def timing_rows(path: Path) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT stage, outcome, input_tokens, output_tokens FROM stage_timings "
            "ORDER BY sequence"
        ).fetchall()


def request_rows(path: Path) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute("SELECT * FROM model_requests ORDER BY sequence")
        ]


def review(model: ModelHnpIntentReviewer, chosen: str = "SON-01") -> IntentReview:
    with model_request_scope(run_id=RUN_ID):
        return model.review(
            shortlist=SHORTLIST, intent=INTENT, preference_text=PREFERENCE, chosen_variant_id=chosen
        )


def test_it_is_an_intent_reviewer_named_by_its_prompt_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model: HnpIntentReviewer = reviewer(tmp_path / "agent.db", monkeypatch)

    assert (model.prompt_id, model.model) == (PROMPT_ID, "test-model")
    assert PROMPT_ID == "hnp-intent-review-v1"


def test_the_request_is_strict_and_answers_one_of_four_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = ModelServer(verdict("confirmed", "matches_request"))
    model = reviewer(tmp_path / "agent.db", monkeypatch, server)

    assert review(model) == IntentReview("confirmed", "matches_request")

    (body,) = server.bodies()
    assert (body["model"], body["output_config"]["effort"]) == ("test-model", "low")
    assert body["output_config"]["format"]["type"] == "json_schema"
    schema = output_schema(body)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"decision", "reason"}
    assert schema["properties"]["decision"]["enum"] == ["confirmed", "not_confirmed"]
    # `review_unavailable` never comes from the model: the executor sets it (§4).
    assert schema["properties"]["reason"]["enum"] == [
        "matches_request",
        "contradicts_preference",
        "outside_request",
        "unclear",
    ]


def test_it_sees_what_the_selector_saw_plus_the_chosen_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3: the same structured candidates, the intent and preference_text; nothing the selector
    reasoned and no free catalog text."""
    server = ModelServer(structured(shortlisted_choice), verdict("confirmed", "matches_request"))
    server.install(monkeypatch)
    chooser = ClaudeDelegatedChooser(api_key=SecretStr(API_KEY), model="test-model")
    chooser.choose(INTENT, SHORTLIST, PREFERENCE)
    review(reviewer(tmp_path / "agent.db", monkeypatch))

    selected, reviewed = (json.loads(body["messages"][0]["content"]) for body in server.bodies())
    assert set(reviewed) == {"intent", "preference_text", "candidates", "chosen_variant_id"}
    assert reviewed["chosen_variant_id"] == "SON-01"
    assert reviewed["preference_text"] == PREFERENCE
    assert (reviewed["intent"], reviewed["candidates"]) == (
        selected["intent"],
        selected["candidates"],
    )
    sent = server.bodies()[1]["messages"][0]["content"]
    assert "description" not in sent
    assert "Elijo" not in sent  # the selector's own sentence never reaches the reviewer


@pytest.mark.parametrize(
    ("decision", "reason"),
    [
        ("not_confirmed", "outside_request"),
        ("not_confirmed", "contradicts_preference"),
        ("not_confirmed", "unclear"),
    ],
)
def test_a_choice_not_confirmed_comes_back_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str, reason: str
) -> None:
    model = reviewer(tmp_path / "agent.db", monkeypatch, ModelServer(verdict(decision, reason)))

    assert review(model, "SON-02") == IntentReview(decision, reason)  # type: ignore[arg-type]


def test_the_call_is_timed_and_recorded_under_the_tasks_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "agent.db"
    model = reviewer(path, monkeypatch, ModelServer(verdict("not_confirmed", "outside_request")))

    review(model, "SON-02")
    model.close()

    assert timing_rows(path) == [(REVIEW_STAGE, "ok", 1825, 786)]
    (row,) = request_rows(path)
    assert (row["run_id"], row["stage"], row["outcome"], row["text_format"]) == (
        RUN_ID,
        REVIEW_STAGE,
        "ok",
        "IntentVerdict",
    )
    assert json.loads(row["output_json"])["structured"] == {
        "decision": "not_confirmed",
        "reason": "outside_request",
    }
    assert json.loads(row["input_json"])["chosen_variant_id"] == "SON-02"
    assert API_KEY not in json.dumps(row)


@pytest.mark.parametrize(
    "answers",
    [
        (failure(500), failure(500)),
        (verdict("confirmed", "unclear"),),  # an incoherent pair
        (verdict("not_confirmed", "matches_request"),),
        (verdict("not_confirmed", "review_unavailable"),),  # outside the schema
        (structured(lambda body: {"decision": "confirmed"}),),
        (
            structured(
                lambda body: {"decision": "confirmed", "reason": "matches_request", "extra": 1}
            ),
        ),
    ],
    ids=[
        "unavailable",
        "confirmed-unclear",
        "not-confirmed-match",
        "unavailable-reason",
        "short",
        "extra",
    ],
)
def test_a_failed_or_invalid_answer_is_an_interpretation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers: tuple[Any, ...]
) -> None:
    path = tmp_path / "agent.db"
    model = reviewer(path, monkeypatch, ModelServer(*answers))

    with pytest.raises(IntentInterpretationError):
        review(model)

    assert [(stage, outcome) for stage, outcome, *_ in timing_rows(path)] == [
        (REVIEW_STAGE, "error")
    ]


def test_an_answer_without_output_is_an_interpretation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A refusal, or any answer with no JSON of the schema.
    model = reviewer(tmp_path / "agent.db", monkeypatch, ModelServer(text("No puedo revisar.")))

    with pytest.raises(IntentInterpretationError):
        review(model)


def test_an_empty_model_name_is_refused(tmp_path: Path) -> None:
    timings, _ = stores(tmp_path / "agent.db")
    with pytest.raises(ValueError):
        ModelHnpIntentReviewer(api_key=SecretStr(API_KEY), model=" ", timer=timings)


# Through the executor


@pytest.mark.parametrize(
    ("answers", "ended", "reason"),
    [
        ((verdict("confirmed", "matches_request"),), ("running", None), "matches_request"),
        (
            (verdict("not_confirmed", "outside_request"),),
            ("escalated", "choice_not_confirmed"),
            "outside_request",
        ),
        ((failure(500), failure(500)), ("escalated", "choice_not_confirmed"), "review_unavailable"),
    ],
    ids=["confirmed", "outside", "unavailable"],
)
def test_the_executor_records_the_models_review_under_the_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answers: tuple[Any, ...],
    ended: tuple[str, str | None],
    reason: str,
) -> None:
    path = tmp_path / "agent.db"
    model = reviewer(path, monkeypatch, ModelServer(*answers))
    bench = Bench(tmp_path, mode="enforce", reviewer=model)

    bench.run()

    assert bench.ended() == ended
    (event,) = [
        e.body for e in bench.ledger.events(bench.grant_id) if e.event_type == "choice_reviewed"
    ]
    assert (event["reason"], event["prompt_id"], event["model"]) == (
        reason,
        PROMPT_ID,
        "test-model",
    )
    assert {(row["run_id"], row["stage"]) for row in request_rows(path)} == {
        (bench.run_id(), REVIEW_STAGE)
    }
