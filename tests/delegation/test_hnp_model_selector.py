"""`AGENT_HNP_SELECTOR=llm` (docs/design/hnp-governed.md §3.1, §3.3 step 2): P5b's bounded
`CandidateChoice` over the grant's shortlist, timed with its tokens. The model is the scripted
Messages API of tests/support/model_server.py; no request leaves the process."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from commerce_agent.application.hnp_selection import MODEL_STAGE, ModelHnpSelector
from commerce_agent.domain import HardConstraints, PurchaseIntent, rank_candidates
from commerce_agent.llm.conversation_router import IntentInterpretationError
from commerce_agent.llm.delegated_choice import ClaudeDelegatedChooser, DelegatedChoice
from commerce_agent.persistence.stage_timings_sqlite import ModelUsage, SqliteStageTimings
from tests.support.hnp_executor import Bench
from tests.support.model_server import (
    Answer,
    ModelServer,
    disconnect,
    output_schema,
    structured,
    text,
)
from tests.test_conversation_router import FEED

INTENT = PurchaseIntent(hard=HardConstraints(currency="COP", max_total_amount_minor=150_000))
SHORTLIST = rank_candidates(FEED, INTENT).ranked
USAGE = {"input_tokens": 321, "output_tokens": 12}


def answer(variant_id: str) -> Answer:
    choice = {
        "action": "purchase",
        "assistant_message": "Elijo esta.",
        "selected_variant_id": variant_id,
    }
    return structured(lambda body: choice, usage=USAGE)


def chooser(
    monkeypatch: pytest.MonkeyPatch, *answers: Answer
) -> tuple[ClaudeDelegatedChooser, ModelServer]:
    server = ModelServer(*answers)
    server.install(monkeypatch)
    return ClaudeDelegatedChooser(api_key=SecretStr("sk-ant-test-key"), model="test-model"), server


def rows(path: Path) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT stage, outcome, input_tokens, output_tokens FROM stage_timings "
            "ORDER BY sequence"
        ).fetchall()


def test_the_model_may_only_name_a_shortlisted_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    model, server = chooser(monkeypatch, answer("SON-01"))

    choice = model.choose(INTENT, SHORTLIST, "Prefiero el negro.")

    assert choice == DelegatedChoice("SON-01", 321, 12)
    (call,) = server.bodies()
    assert output_schema(call)["properties"]["selected_variant_id"]["enum"] == ["SON-01"]
    assert (call["model"], call["output_config"]["effort"]) == ("test-model", "low")
    (message,) = call["messages"]
    payload = json.loads(message["content"])
    assert set(payload) == {"intent", "preference_text", "candidates"}
    assert payload["preference_text"] == "Prefiero el negro."
    assert [item["variant_id"] for item in payload["candidates"]] == ["SON-01"]


def test_an_answer_from_outside_still_reaches_the_executor_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation keeps a plain string: K12 in the executor is what refuses it."""
    model, _ = chooser(monkeypatch, answer("SON-99"))

    assert model.choose(INTENT, SHORTLIST, None) == DelegatedChoice("SON-99", 321, 12)


@pytest.mark.parametrize(
    "answers",
    [(disconnect(), disconnect()), (text("No puedo elegir."),)],
    ids=["unavailable", "no-decision"],
)
def test_a_failed_call_is_an_interpretation_error(
    monkeypatch: pytest.MonkeyPatch, answers: tuple[Answer, ...]
) -> None:
    model, _ = chooser(monkeypatch, *answers)

    with pytest.raises(IntentInterpretationError):
        model.choose(INTENT, SHORTLIST, None)


class Scripted:
    def __init__(self, variant_id: str | None) -> None:
        self.variant_id = variant_id

    def choose(self, intent, candidates, preference_text) -> DelegatedChoice:
        if self.variant_id is None:
            raise IntentInterpretationError("no answer")
        return DelegatedChoice(self.variant_id, 321, 12)


def timings(path: Path) -> SqliteStageTimings:
    result = SqliteStageTimings(path)
    result.initialize_model_tokens()
    result.initialize_model_tokens()  # idempotent
    return result


@pytest.mark.parametrize(
    ("variant_id", "status", "ended", "row"),
    [
        ("SON-01", "applied", ("running", None), (MODEL_STAGE, "ok", 321, 12)),
        ("SON-07", "closed", ("failed", "selection_not_eligible"), (MODEL_STAGE, "ok", 321, 12)),
        (None, "closed", ("failed", "selection_unavailable"), (MODEL_STAGE, "error", None, None)),
    ],
    ids=["shortlisted", "outside", "unavailable"],
)
def test_the_executor_times_the_model_and_still_guards_its_choice(
    tmp_path: Path,
    variant_id: str | None,
    status: str,
    ended: tuple[str, str | None],
    row: tuple[Any, ...],
) -> None:
    selector = ModelHnpSelector(Scripted(variant_id), timings(tmp_path / "agent.db"))
    bench = Bench(tmp_path, selector=selector)

    assert bench.run().status == status

    assert bench.ended() == ended
    assert rows(tmp_path / "agent.db") == [row]


def test_a_human_present_timing_keeps_its_shape(tmp_path: Path) -> None:
    path = tmp_path / "agent.db"
    plain = SqliteStageTimings(path)
    plain.initialize()
    with plain.measure("model.route"):
        pass
    with plain.measure(MODEL_STAGE, ModelUsage(5, 1)):
        pass  # without the token columns the row is dropped, never the call

    with closing(sqlite3.connect(path)) as connection:
        columns = [r[1] for r in connection.execute("PRAGMA table_info(stage_timings)")]
        stages = connection.execute("SELECT stage FROM stage_timings").fetchall()
    assert columns == ["sequence", "stage", "started_at", "elapsed_ms", "outcome"]
    assert stages == [("model.route",)]
