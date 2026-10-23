"""The intent reviewer in the executor (docs/design/hnp-intent-review.md §5 to §7).

Before D1 and any POST, a second model checks the selector's choice against the buyer's
`preference_text`: a confirmation goes on as before, anything else escalates
`choice_not_confirmed` with nothing created, signed or charged. Every review is a `choice_reviewed`
ledger event. The reviewers here are fakes; no model is called.
"""

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest

from commerce_agent.application.hnp_execution import HnpExecutionService, NullHnpPolicyGate
from commerce_agent.application.hnp_intent_review import IntentReview
from commerce_agent.domain import PurchaseIntent, RankedCandidate
from tests.support.hnp_executor import Bench, Effects, Gate

CONFIRMED = IntentReview("confirmed", "matches_request")
CONTRADICTS = IntentReview("not_confirmed", "contradicts_preference")
NOTHING = Effects(creates=0, signatures=0, commits=0, cancels=0)


@dataclass
class FakeReviewer:
    """Answers `result` (or raises it) and keeps what it was given."""

    result: object = CONFIRMED
    prompt_id: str = "hnp-intent-review-v1"
    model: str = "test-model"
    calls: list[dict[str, Any]] = field(default_factory=list)
    before: Any = None

    def review(
        self,
        shortlist: tuple[RankedCandidate, ...],
        intent: PurchaseIntent,
        preference_text: str | None,
        chosen_variant_id: str,
    ) -> IntentReview:
        self.calls.append(
            {
                "shortlist": shortlist,
                "intent": intent,
                "preference_text": preference_text,
                "chosen_variant_id": chosen_variant_id,
            }
        )
        if self.before is not None:
            self.before()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result  # type: ignore[return-value]


class Choosing:
    """A selector that answers `variant_id` and keeps what it was given."""

    def __init__(self, variant_id: str = "SON-01") -> None:
        self.variant_id = variant_id
        self.calls: list[tuple[Any, ...]] = []

    def choose(self, shortlist, intent, preference_text) -> str:
        self.calls.append((shortlist, intent, preference_text))
        return self.variant_id


def reviews(bench: Bench) -> list[Mapping[str, Any]]:
    return [
        event.body
        for event in bench.ledger.events(bench.grant_id)
        if event.event_type == "choice_reviewed"
    ]


def purchases(path: Path) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]


# Off: nothing changes


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_without_a_reviewer_no_review_is_recorded(
    tmp_path: Path, mode: Literal["off", "enforce"]
) -> None:
    bench = Bench(tmp_path, mode=mode)

    assert bench.run().status == "applied"

    assert reviews(bench) == []
    assert "choice_reviewed" not in bench.events()
    assert bench.effects().creates == 1


def test_the_reviewer_runs_only_in_b2_hnp(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    with pytest.raises(ValueError, match="enforce"):
        HnpExecutionService(
            mode="off",
            ledger=bench.ledger,
            store=bench.store,
            purchases=bench.service._purchases,
            checkout=bench.merchant,
            closer=bench.closer,
            gate=NullHnpPolicyGate(),
            selector=Choosing(),
            feed=bench.service._feed,
            clock=bench.clock,
            id_factory=bench.service._new_id,
            reviewer=FakeReviewer(),
        )


# Confirmed: the purchase goes on


def test_a_confirmed_choice_goes_on_and_is_recorded_before_the_checkout(tmp_path: Path) -> None:
    reviewer = FakeReviewer(CONFIRMED)
    bench = Bench(tmp_path, mode="enforce", reviewer=reviewer)

    assert bench.run().status == "applied"

    assert len(reviewer.calls) == 1
    assert reviews(bench) == [
        {
            "decision": "confirmed",
            "reason": "matches_request",
            "prompt_id": "hnp-intent-review-v1",
            "model": "test-model",
        }
    ]
    events = bench.events()
    assert events.index("task_claimed") < events.index("choice_reviewed")
    assert events.index("choice_reviewed") < events.index("reservation_reserved")
    assert bench.effects().creates == 1
    assert isinstance(bench.gate, Gate) and bench.gate.calls == ["D2", "D3"]
    assert bench.ended() == ("running", None)


# Not confirmed: escalation before any effect


def test_a_choice_not_confirmed_escalates_with_nothing_created(tmp_path: Path) -> None:
    reviewer = FakeReviewer(CONTRADICTS)
    bench = Bench(tmp_path, mode="enforce", reviewer=reviewer)

    step = bench.run()

    assert step.status == "closed"
    assert bench.ended() == ("escalated", "choice_not_confirmed")
    assert bench.effects() == NOTHING
    assert bench.merchant.calls == []  # no POST of any kind to the merchant
    assert isinstance(bench.gate, Gate) and bench.gate.calls == []
    assert [review["reason"] for review in reviews(bench)] == ["contradicts_preference"]
    assert bench.events()[-3:] == ["choice_reviewed", "revoked", "task_finished"]
    grant = bench.execution().grant
    assert (grant.state, grant.revoked_reason) == ("revoked", "escalated")
    # The purchase exists so that its trace shows the review, and the terminal closed it.
    assert bench.purchase() == ("canceled", None)


@pytest.mark.parametrize(
    "result",
    [
        RuntimeError("model unavailable"),
        TimeoutError("model timeout"),
        "confirmed",
        None,
        object.__new__(IntentReview),
    ],
    ids=["raises", "timeout", "text", "none", "unbuilt"],
)
def test_a_review_that_fails_or_is_invalid_fails_closed(tmp_path: Path, result: object) -> None:
    bench = Bench(tmp_path, mode="enforce", reviewer=FakeReviewer(result))

    assert bench.run().status == "closed"

    assert bench.ended() == ("escalated", "choice_not_confirmed")
    assert bench.effects() == NOTHING
    [review] = reviews(bench)
    assert (review["decision"], review["reason"]) == ("not_confirmed", "review_unavailable")


def test_an_inconsistent_review_fails_closed(tmp_path: Path) -> None:
    """A review built around its own check (confirmed for another reason) is not a confirmation."""
    inconsistent = object.__new__(IntentReview)
    object.__setattr__(inconsistent, "decision", "confirmed")
    object.__setattr__(inconsistent, "reason", "unclear")
    bench = Bench(tmp_path, mode="enforce", reviewer=FakeReviewer(inconsistent))

    bench.run()

    assert bench.ended() == ("escalated", "choice_not_confirmed")
    assert reviews(bench)[0]["reason"] == "review_unavailable"


def test_an_intent_review_pairs_confirmed_with_matches_request_only() -> None:
    with pytest.raises(ValueError):
        IntentReview("confirmed", "contradicts_preference")
    with pytest.raises(ValueError):
        IntentReview("not_confirmed", "matches_request")


# Independence: what the reviewer sees


def test_the_reviewer_sees_what_the_selector_saw_and_its_choice_only(tmp_path: Path) -> None:
    selector, reviewer = Choosing(), FakeReviewer(CONFIRMED)
    bench = Bench(tmp_path, mode="enforce", selector=selector, reviewer=reviewer)

    bench.run()

    [(shortlist, intent, preference_text)] = selector.calls
    [call] = reviewer.calls
    # No selector reasoning: exactly the selector's inputs plus the id it answered.
    assert call == {
        "shortlist": shortlist,
        "intent": intent,
        "preference_text": preference_text,
        "chosen_variant_id": "SON-01",
    }
    # The catalog's closed channel: structured observations, never the feed's free descriptions.
    assert all(isinstance(item, RankedCandidate) for item in call["shortlist"])
    assert "Synthetic Sonora headphones" not in repr(call)


def test_a_choice_outside_the_shortlist_is_not_reviewed(tmp_path: Path) -> None:
    """K12 already owns it (POL-01 in B2-HNP): the reviewer only judges a shortlisted choice."""
    reviewer = FakeReviewer(CONFIRMED)
    bench = Bench(tmp_path, mode="enforce", selector=Choosing("SON-99"), reviewer=reviewer)

    bench.run()

    assert reviewer.calls == []
    assert reviews(bench) == []
    assert bench.ended() == ("failed", "selection_not_eligible")


class NoChoice:
    """A selector whose model did not answer."""

    def choose(self, shortlist, intent, preference_text) -> str:
        raise TimeoutError("the model did not answer")


def test_a_failed_selector_is_never_reviewed(tmp_path: Path) -> None:
    """Without a choice there is nothing to review: the task escalates `selection_unavailable`
    before the reviewer, D1 and any POST, and leaves no `choice_reviewed` event."""
    reviewer = FakeReviewer(CONFIRMED)
    bench = Bench(tmp_path, mode="enforce", selector=NoChoice(), reviewer=reviewer)

    bench.run()

    assert reviewer.calls == []
    assert reviews(bench) == []
    assert bench.ended() == ("escalated", "selection_unavailable")
    assert bench.purchase() is None
    assert bench.effects() == NOTHING


def test_a_lease_lost_during_the_review_writes_nothing(tmp_path: Path) -> None:
    """Another claim took the task while the model answered: this run stops without the event."""
    reviewer = FakeReviewer(CONFIRMED)
    bench = Bench(tmp_path, mode="enforce", reviewer=reviewer)

    def reclaimed() -> None:
        with closing(sqlite3.connect(bench.path)) as connection, connection:
            connection.execute("UPDATE hnp_tasks SET lease_id = 'lease_other'")

    reviewer.before = reclaimed

    assert bench.run().status == "lease_lost"

    assert reviews(bench) == []
    assert bench.effects() == NOTHING
    assert bench.execution().task.state == "running"
