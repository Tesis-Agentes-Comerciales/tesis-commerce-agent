"""Why the model failed a delegated purchase, kept in the ledger (p0-model-failures.md §3.5).

The terminals do not change: a selector the model failed ends `selection_unavailable` and a review
it could not give ends `choice_not_confirmed`, both before any effect. The `failure_kind` goes in
the events they already write, with no new event type: `task_finished` for the selector and
`choice_reviewed` for the reviewer. The real SDK over the scripted transport of
tests/support/model_server.py and the executor's bench on real SQLite; no model is called.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from commerce_agent.application.hnp_selection import ModelHnpSelector
from commerce_agent.delegation.models import TaskFinish
from commerce_agent.llm.delegated_choice import OpenAIDelegatedChooser
from commerce_agent.llm.intent_review import PROMPT_ID, ModelHnpIntentReviewer
from commerce_agent.persistence.stage_timings_sqlite import SqliteStageTimings
from tests.delegation.test_hnp_intent_review import FakeReviewer, reviews
from tests.support.hnp_executor import Bench, Effects
from tests.support.model_server import Answer, ModelServer, disconnect, failure, structured

API_KEY = "sk-test-failure-kind-0123456789abcdef"
NOTHING = Effects(creates=0, signatures=0, commits=0, cancels=0)


def read_timeout() -> Answer:
    """The model does not answer in time: the SDK sees `httpx.ReadTimeout`."""

    def respond(body: dict[str, Any]) -> httpx.Response:
        raise httpx.ReadTimeout("scripted timeout")

    return respond


def timings(path: Path) -> SqliteStageTimings:
    timer = SqliteStageTimings(path)
    timer.initialize()
    timer.initialize_model_tokens()
    return timer


def model_selector(path: Path) -> ModelHnpSelector:
    chooser = OpenAIDelegatedChooser(api_key=SecretStr(API_KEY), model="test-model")
    return ModelHnpSelector(chooser, timings(path))


def model_reviewer(path: Path) -> ModelHnpIntentReviewer:
    return ModelHnpIntentReviewer(
        api_key=SecretStr(API_KEY), model="test-model", timer=timings(path)
    )


def finished(bench: Bench) -> Mapping[str, Any]:
    (body,) = [
        event.body
        for event in bench.ledger.events(bench.grant_id)
        if event.event_type == "task_finished"
    ]
    return body


# The selector: `failure_kind` in `task_finished`

SELECTOR_FAILURES: dict[str, tuple[tuple[Answer, ...], int]] = {
    "rate_limited": ((failure(429), failure(429)), 2),
    "quota_exhausted": ((failure(429, code="insufficient_quota"),), 1),
    "unreachable": ((disconnect(), disconnect()), 2),
}


@pytest.mark.parametrize("mode", ["off", "enforce"])
@pytest.mark.parametrize("kind", SELECTOR_FAILURES)
def test_a_selector_the_model_failed_records_why_in_task_finished(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, mode: str
) -> None:
    answers, attempts = SELECTOR_FAILURES[kind]
    server = ModelServer(*answers)
    server.install(monkeypatch)
    bench = Bench(tmp_path, mode=mode, selector=model_selector(tmp_path / "agent.db"))  # type: ignore[arg-type]

    assert bench.run().status == "closed"

    state = "escalated" if mode == "enforce" else "failed"
    assert bench.ended() == (state, "selection_unavailable")
    assert finished(bench) == {
        "state": state,
        "outcome_code": "selection_unavailable",
        "run_id": bench.run_id(),
        "failure_kind": kind,
    }
    assert len(server.requests) == attempts
    assert bench.purchase() is None
    assert bench.effects() == NOTHING


class NoChoice:
    """A selector that makes no choice for a reason outside the model."""

    def __init__(self, error: Exception | None) -> None:
        self.error = error

    def choose(self, shortlist, intent, preference_text) -> str:
        if self.error is not None:
            raise self.error
        return None  # type: ignore[return-value]


@pytest.mark.parametrize(
    "selector",
    [NoChoice(RuntimeError("selector bug")), NoChoice(None)],
    ids=["raises", "answers-none"],
)
def test_a_selector_that_fails_outside_the_model_names_no_kind(
    tmp_path: Path, selector: NoChoice
) -> None:
    bench = Bench(tmp_path, mode="enforce", selector=selector)

    assert bench.run().status == "closed"

    assert finished(bench) == {
        "state": "escalated",
        "outcome_code": "selection_unavailable",
        "run_id": bench.run_id(),
    }


def test_only_selection_unavailable_names_the_models_failure(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    claimed = bench.service.claim(bench.task_id)
    assert claimed.execution is not None and claimed.execution.task.lease_id is not None
    lease = claimed.execution.task.lease_id

    with pytest.raises(ValueError, match="selection_unavailable"):
        bench.ledger.finish_task(
            TaskFinish(
                bench.task_id, lease, "failed", "checkout_failed", failure_kind="unreachable"
            )
        )

    assert bench.ended() == ("running", None)


# The reviewer: `failure_kind` in `choice_reviewed`

REVIEW_FAILURES: dict[str, tuple[tuple[Answer, ...], str]] = {
    "timeout": ((read_timeout(), read_timeout()), "unreachable"),
    "disconnect": ((disconnect(), disconnect()), "unreachable"),
    # JSON outside the contract: `review_unavailable` is the executor's, never the model's.
    "outside-contract": (
        (structured(lambda body: {"decision": "not_confirmed", "reason": "review_unavailable"}),),
        "invalid_output",
    ),
    "quota": ((failure(429, code="insufficient_quota"),), "quota_exhausted"),
}


@pytest.mark.parametrize("case", REVIEW_FAILURES)
def test_a_review_the_model_could_not_give_records_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    answers, kind = REVIEW_FAILURES[case]
    server = ModelServer(*answers)
    server.install(monkeypatch)
    bench = Bench(tmp_path, mode="enforce", reviewer=model_reviewer(tmp_path / "agent.db"))

    assert bench.run().status == "closed"

    assert bench.ended() == ("escalated", "choice_not_confirmed")
    assert reviews(bench) == [
        {
            "decision": "not_confirmed",
            "reason": "review_unavailable",
            "prompt_id": PROMPT_ID,
            "model": "test-model",
            "failure_kind": kind,
        }
    ]
    assert len(server.requests) == len(answers)
    assert bench.effects() == NOTHING
    assert bench.merchant.calls == []
    # The purchase exists so that its trace shows the review, and the terminal closed it.
    assert bench.purchase() == ("canceled", None)
    # The task's terminal is the review's: the selector did not fail.
    assert "failure_kind" not in finished(bench)


@pytest.mark.parametrize(
    "result",
    [RuntimeError("reviewer bug"), "confirmed"],
    ids=["raises", "not-a-review"],
)
def test_a_review_unavailable_outside_the_model_names_no_kind(
    tmp_path: Path, result: object
) -> None:
    bench = Bench(tmp_path, mode="enforce", reviewer=FakeReviewer(result))

    bench.run()

    assert reviews(bench) == [
        {
            "decision": "not_confirmed",
            "reason": "review_unavailable",
            "prompt_id": "hnp-intent-review-v1",
            "model": "test-model",
        }
    ]
