"""Who a failure of the agent's model counts against, by the product's own kind
(suite/live/failures.py, docs/design/eval-suite.md §4). No stack and no model: the Agent API's
answers are scripted, and the trial's database is written with the product's own schema."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, get_args

import pytest
from pydantic import ValidationError

from commerce_agent.api import error_response
from commerce_agent.delegation.models import EventType
from commerce_agent.llm.model_failures import IntentInterpretationError, ModelFailureKind
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger, append_event
from commerce_agent.persistence.model_requests_sqlite import ModelRequestRow, SqliteModelRequests
from commerce_evaluation.suite.cases import PresentCase
from commerce_evaluation.suite.executor import RunSettings
from commerce_evaluation.suite.live import failures, observe
from commerce_evaluation.suite.live.failures import Seen
from commerce_evaluation.suite.live.presente import converse
from commerce_evaluation.suite.live_executor import LiveExecutor
from commerce_evaluation.suite.trial import AGENT_FAILURES, Ending, FinalState, ModelFailure
from commerce_evaluation.suite.users import ScriptedUser
from tests.evaluation.suite.runs import REFUSED_REQUEST, make_case

KINDS: tuple[ModelFailureKind, ...] = get_args(ModelFailureKind)
TURN = "POST /conversation/turn 502"
SELECTION = "task_finished selection_unavailable"
FAILED = "The conversation model request failed"
# What the provider said of the smoke run's 400 (#217).
GRAMMAR = "The compiled grammar is too large"


def present() -> PresentCase:
    built = make_case("prioridad-explicita")
    assert isinstance(built, PresentCase)
    return built


CASE_ = present()


def against(kind: str) -> Literal["agent", "infrastructure"]:
    return "agent" if kind in {"rejected", "invalid_output"} else "infrastructure"


def test_only_a_refused_request_and_an_unusable_answer_are_the_agent_s():
    assert {"rejected", "invalid_output"} == AGENT_FAILURES
    assert set(KINDS) - AGENT_FAILURES == {
        "provider_error",
        "unreachable",
        "rate_limited",
        "quota_exhausted",
    }


# Presente: the kind is in the body of the Agent API's 5xx.


class Answer:
    def __init__(self, status_code: int, body: Any):
        self.status_code, self._body = status_code, body

    def json(self) -> Any:
        return self._body


class Api:
    """The Agent API with one scripted answer per turn."""

    def __init__(self, *turns: Answer):
        self.turns = list(turns)

    def get(self, url: str) -> Answer:
        raise AssertionError(f"Nothing to approve: {url}")

    def post(self, url: str, *, json: Any = None) -> Answer:
        return self.turns.pop(0)


def model_failed(kind: object, status: int = 502) -> Answer:
    """`error_response` of api.py for the router's IntentInterpretationError."""
    body = {"code": "intent_model_error", "message": FAILED, "model_failure": kind}
    return Answer(status, body)


@pytest.mark.parametrize("kind", KINDS)
def test_a_presente_5xx_counts_by_the_kind_of_its_model_failure(kind: ModelFailureKind):
    talk = converse(Api(model_failed(kind)), ScriptedUser(CASE_), CASE_, {})

    assert talk.model_failure == Seen(kind, TURN)
    assert talk.api_5xx == ([] if against(kind) == "agent" else [TURN])
    # The trace keeps the error and its kind; the conversation ends on what the chat shows.
    assert talk.errors == [
        {
            "call": "POST /conversation/turn",
            "status": 502,
            "code": "intent_model_error",
            "message": FAILED,
            "model_failure": kind,
        }
    ]
    assert talk.action == "respond" and talk.transcript[-1].text == FAILED


@pytest.mark.parametrize("kind", KINDS)
def test_the_kind_is_read_from_the_product_s_own_error_body(kind: ModelFailureKind):
    """api.error_response builds the body: renaming `model_failure` there breaks this test."""
    response = error_response(IntentInterpretationError(FAILED, failure_kind=kind))
    answer = Answer(response.status_code, json.loads(bytes(response.body)))

    talk = converse(Api(answer), ScriptedUser(CASE_), CASE_, {})

    assert response.status_code == 502 and talk.model_failure == Seen(kind, TURN)
    assert talk.api_5xx == ([] if against(kind) == "agent" else [TURN])


@pytest.mark.parametrize(
    "answer",
    [
        model_failed(None),  # an IntentInterpretationError the model did not cause
        model_failed("no_es_un_tipo"),
        Answer(503, {"code": "merchant_unavailable", "message": "No responde."}),
        Answer(504, "Gateway Timeout"),
    ],
)
def test_a_5xx_without_a_model_failure_stays_infrastructure(answer: Answer):
    talk = converse(Api(answer), ScriptedUser(CASE_), CASE_, {})

    assert talk.api_5xx == [f"POST /conversation/turn {answer.status_code}"]
    assert talk.model_failure is None and "model_failure" not in talk.errors[0]


# Delegado: the kind is in the task's chained events.


def agent_db(tmp_path: Path, *bodies: tuple[EventType, dict[str, Any]]) -> Path:
    """A trial's database with the product's ledger and these events of one grant."""
    path = tmp_path / "agent.db"
    SqliteDelegationLedger(path).initialize()
    SqliteModelRequests(path).initialize()
    with closing(sqlite3.connect(path)) as db, db:
        for index, (event, body) in enumerate(bodies):
            append_event(
                db, "dg_1", event, "service", body, f"t{index}", "ht_1", "run_1", owner_ref="o"
            )
    return path


def finished(outcome: str, kind: str | None = None) -> tuple[EventType, dict[str, Any]]:
    body = {"state": "escalated", "outcome_code": outcome, "run_id": "run_1"}
    return ("task_finished", body | ({"failure_kind": kind} if kind else {}))


def reviewed(kind: str) -> tuple[EventType, dict[str, Any]]:
    body = {"decision": "not_confirmed", "reason": "review_unavailable", "failure_kind": kind}
    return ("choice_reviewed", body | {"prompt_id": "hnp-intent-review-v1", "model": "m"})


@pytest.mark.parametrize("kind", KINDS)
def test_a_delegado_selector_failure_counts_by_its_kind(tmp_path: Path, kind: ModelFailureKind):
    path = agent_db(tmp_path, ("task_claimed", {}), finished("selection_unavailable", kind))

    seen = failures.task_failure(path)
    decided = failures.decided(seen, path)

    assert seen == Seen(kind, SELECTION)
    assert decided is not None and decided.counts_against == against(kind)


@pytest.mark.parametrize("kind", KINDS)
def test_a_delegado_reviewer_failure_counts_by_its_kind(tmp_path: Path, kind: ModelFailureKind):
    path = agent_db(tmp_path, reviewed(kind), finished("choice_not_confirmed"))

    assert failures.task_failure(path) == Seen(kind, "choice_reviewed review_unavailable")


def test_a_ledger_written_before_the_message_column_still_gives_its_evidence(tmp_path: Path):
    path = tmp_path / "agent.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE model_requests (sequence INTEGER PRIMARY KEY, outcome TEXT, stage TEXT, "
            "error_kind TEXT, http_status INTEGER, provider_error_code TEXT, retries INTEGER)"
        )
        db.execute(
            "INSERT INTO model_requests (outcome, stage, error_kind, http_status, "
            "provider_error_code, retries) VALUES "
            "('error', 'model.route', 'BadRequestError', 400, 'invalid_request_error', 0)"
        )

    assert failures.failed_requests(path) == (REFUSED_REQUEST | {"provider_error_message": None},)


def test_a_task_that_ended_without_a_model_failure_names_none(tmp_path: Path):
    # The reference selector chose nothing: the product's own escalation, no model involved.
    path = agent_db(tmp_path, finished("selection_unavailable"))

    assert failures.task_failure(path) is None
    assert failures.task_failure(tmp_path / "sin-base.db") is None
    assert failures.decided(None, path) is None


# The decision, its evidence and the record.


def ledger_row(outcome: Literal["ok", "error"], **failed: Any) -> ModelRequestRow:
    return ModelRequestRow(
        run_id="run_1",
        stage=failed.pop("stage", "model.route"),
        request_index=1,
        started_at="t",
        latency_ms=1.0,
        outcome=outcome,
        error_kind=failed.pop("error_kind", None),
        model="claude-haiku-5-5",
        response_model=None,
        reasoning_effort="low",
        text_format=None,
        input_tokens=None,
        cached_input_tokens=None,
        cache_write_tokens=None,
        output_tokens=None,
        reasoning_tokens=None,
        retries=failed.pop("retries", None),
        request_sha256=None,
        instructions_sha256=None,
        catalog_vocabulary=None,
        input_json=None,
        output_json=None,
        **failed,
    )


def test_the_smoke_run_s_400_is_the_agent_s_failure_with_its_evidence(tmp_path: Path):
    """humo-haiku-1: the router's request got a 400 and the API answered 502. It is graded."""
    path = tmp_path / "agent.db"
    requests = SqliteModelRequests(path)
    requests.initialize()
    requests.record(ledger_row("ok", stage="model.catalog"))
    requests.record(
        ledger_row(
            "error",
            error_kind="BadRequestError",
            http_status=400,
            provider_error_code="invalid_request_error",
            provider_error_message=GRAMMAR,
            retries=0,
        )
    )
    talk = converse(Api(model_failed("rejected")), ScriptedUser(CASE_), CASE_, {})

    decided = failures.decided(talk.model_failure, path)

    evidence = REFUSED_REQUEST | {"provider_error_message": GRAMMAR}
    assert decided == ModelFailure(
        kind="rejected", counts_against="agent", source=TURN, requests=(evidence,)
    )
    assert observe.infra_failure(talk.api_5xx, [], 0, None, decided) is None


@pytest.mark.parametrize("kind", KINDS)
def test_infra_failure_names_the_kind_it_counts_against_the_infrastructure(kind: ModelFailureKind):
    def decided(source: str) -> ModelFailure:
        return ModelFailure(kind=kind, counts_against=against(kind), source=source, requests=())

    presente = observe.infra_failure(
        [TURN] if against(kind) != "agent" else [], [], 0, None, decided(TURN)
    )
    delegado = observe.infra_failure([], [], 0, None, decided(SELECTION))

    if against(kind) == "agent":
        assert presente is None and delegado is None
        return
    assert presente is not None and presente.detail == f"API del agente: {TURN} ({kind})"
    assert delegado is not None and delegado.cause == "api_5xx"
    assert delegado.detail == f"modelo del agente: {kind} en {SELECTION}"


def test_a_record_s_decision_follows_its_kind():
    with pytest.raises(ValidationError, match="does not count against"):
        ModelFailure(kind="rejected", counts_against="infrastructure", source=TURN, requests=())
    with pytest.raises(ValidationError, match="does not count against"):
        ModelFailure(kind="rate_limited", counts_against="agent", source=TURN, requests=())


@pytest.mark.parametrize("kind", ["rejected", "provider_error"])
def test_the_executor_s_record_keeps_the_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: ModelFailureKind
):
    path = tmp_path / "agent.db"
    SqliteModelRequests(path).initialize()
    monkeypatch.setattr(
        observe,
        "final_state",
        lambda lane, episode: FinalState(orders=(), checkouts=(), psp_charges=()),
    )
    monkeypatch.setattr(observe, "evidence", lambda lane, agent: ())
    executor = LiveExecutor(RunSettings("r", tmp_path, "regression", None, "low", None), None)
    played = observe.Played(
        agent=SimpleNamespace(database=path),  # type: ignore[arg-type]
        episode=None,  # type: ignore[arg-type]
        run_id="run_1",
        transcript=(),
        final=Ending(action="respond"),
        trace={},
        api_5xx=() if kind == "rejected" else (TURN,),
        model_failure=Seen(kind, TURN),
    )
    lane = SimpleNamespace(proxy=SimpleNamespace(calls=[]), psp_proxy=None)
    record = executor._record(CASE_, "B2", 1, 7, lane, played, None)  # type: ignore[arg-type]

    assert record.model_failure is not None and record.model_failure.kind == kind
    assert record.model_failure.counts_against == against(kind)
    if kind == "rejected":
        assert record.infra_failure is None
    else:
        assert record.infra_failure is not None
        assert record.infra_failure.detail == f"API del agente: {TURN} ({kind})"
    # The record goes to trials.jsonl and comes back with its decision.
    assert type(record).model_validate_json(record.model_dump_json()) == record
    assert json.loads(record.model_dump_json())["model_failure"]["source"] == TURN
