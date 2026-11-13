"""Every request the agent sends to the model leaves a row in `model_requests` (run-traceability.md
§4.4, T2), and recording it never changes the request.

The router and the chooser build their real SDK client; only its transport is a scripted
`httpx2.MockTransport` (tests/support/model_server.py). No request leaves the process.
"""

import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from commerce_agent.application.hnp_selection import MODEL_STAGE
from commerce_agent.application.stage_timing import TimedConversationRouter
from commerce_agent.domain import HardConstraints, PurchaseIntent, rank_candidates
from commerce_agent.llm import (
    CandidateDecision,
    ClaudeConversationRouter,
    CommerceDecision,
    ConversationMessage,
    IntentInterpretationError,
)
from commerce_agent.llm import model_requests as recorder_module
from commerce_agent.llm.delegated_choice import ClaudeDelegatedChooser, DelegatedChoice
from commerce_agent.llm.model_requests import model_request_scope
from commerce_agent.llm.reasoning import reasoning_listener
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.model_requests_sqlite import ModelRequestRow, SqliteModelRequests
from commerce_agent.persistence.stage_timings_sqlite import SqliteStageTimings
from tests.support.model_server import (
    GRAMMAR_TOO_LARGE,
    MODEL,
    SAID_WITHOUT_LOOKUP,
    ModelServer,
    SentRequest,
    disconnect,
    failure,
    function_call,
    no_tool_call,
    routed,
    shortlisted_choice,
    structured,
    text,
)
from tests.test_conversation_router import FEED

API_KEY = "sk-ant-test-model-requests-0123456789abcdef"
RUN_ID = "run_" + "a" * 32
INTENT = PurchaseIntent(
    hard=HardConstraints(currency="COP", categories=("headphones",), max_total_amount_minor=150_000)
)
SHORTLIST = rank_candidates(FEED, INTENT).ranked
MESSAGES = (ConversationMessage(role="user", content="Quiero audífonos por máximo 150.000 COP"),)
ROUTED = CommerceDecision(
    action="purchase",
    assistant_message="Busco audífonos dentro de tu presupuesto.",
    purchase_request="audífonos por máximo 150.000 COP",
    intent=INTENT,
)
COLUMNS = [
    "sequence",
    "run_id",
    "stage",
    "request_index",
    "started_at",
    "latency_ms",
    "outcome",
    "error_kind",
    "model",
    "response_model",
    "reasoning_effort",
    "text_format",
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
    "retries",
    "request_sha256",
    "instructions_sha256",
    "catalog_vocabulary",
    "input_json",
    "output_json",
    "reasoning_summary",
    "http_status",
    "provider_error_code",
    "provider_error_message",
]
# The table as it was before http_status and provider_error_code (p0-model-failures.md §3.3).
FIRST_SCHEMA = """CREATE TABLE model_requests (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    stage TEXT,
    request_index INTEGER NOT NULL CHECK(request_index >= 1),
    started_at TEXT NOT NULL,
    latency_ms REAL NOT NULL CHECK(latency_ms >= 0),
    outcome TEXT NOT NULL CHECK(outcome IN ('ok', 'error')),
    error_kind TEXT CHECK((outcome = 'error') = (error_kind IS NOT NULL)),
    model TEXT NOT NULL,
    response_model TEXT,
    reasoning_effort TEXT,
    text_format TEXT,
    input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens >= 0),
    cached_input_tokens INTEGER CHECK(cached_input_tokens IS NULL OR cached_input_tokens >= 0),
    cache_write_tokens INTEGER CHECK(cache_write_tokens IS NULL OR cache_write_tokens >= 0),
    output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens >= 0),
    reasoning_tokens INTEGER CHECK(reasoning_tokens IS NULL OR reasoning_tokens >= 0),
    retries INTEGER CHECK(retries IS NULL OR retries >= 0),
    request_sha256 TEXT,
    instructions_sha256 TEXT,
    catalog_vocabulary TEXT,
    input_json TEXT,
    output_json TEXT,
    reasoning_summary TEXT)"""
# The table as it was before provider_error_message.
SECOND_SCHEMA = (
    FIRST_SCHEMA.removesuffix(")") + ",\n    http_status INTEGER,\n    provider_error_code TEXT)"
)


def ledger(path: Path) -> SqliteModelRequests:
    result = SqliteModelRequests(path)
    result.initialize()
    return result


def rows(path: Path) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute("SELECT * FROM model_requests ORDER BY sequence")
        ]


def timed(router: ClaudeConversationRouter, path: Path) -> TimedConversationRouter:
    timings = SqliteStageTimings(path)
    timings.initialize()
    return TimedConversationRouter(router, timings)


def build_router(
    monkeypatch: pytest.MonkeyPatch,
    server: ModelServer,
    sink: Any = None,
    *,
    summaries: bool = False,
) -> ClaudeConversationRouter:
    server.install(monkeypatch)
    return ClaudeConversationRouter(
        api_key=SecretStr(API_KEY),
        feed=FEED,
        effort="low",
        reasoning_summary=summaries,
        model_requests=sink,
    )


def turn_answers() -> tuple[Any, ...]:
    """The router looks the catalog up once; the choice is throttled once and then answers."""
    return (
        function_call("lookup_catalog", {"query": "audífonos negros"}),
        structured(ROUTED),
        failure(429, error_type="rate_limit_error"),
        structured(shortlisted_choice),
    )


def run_turn(
    monkeypatch: pytest.MonkeyPatch, path: Path, sink: Any, *, summaries: bool = False
) -> tuple[list[SentRequest], CommerceDecision, CandidateDecision]:
    server = ModelServer(*turn_answers())
    router = timed(build_router(monkeypatch, server, sink, summaries=summaries), path)
    listener = (lambda delta: None) if summaries else None
    with reasoning_listener(listener), model_request_scope(run_id=RUN_ID):
        decision = router.route(MESSAGES)
        assert decision.intent is not None
        choice = router.choose_candidate(MESSAGES, decision.intent, SHORTLIST)
    router.close()
    return server.sent(), decision, choice


# The table ------------------------------------------------------------------------------------


def test_the_table_is_added_once_to_an_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "agent.db"
    SqliteAgentStore(path).initialize()
    timings = SqliteStageTimings(path)
    timings.initialize()
    with timings.measure("model.route"):
        pass

    ledger(path)
    ledger(path)  # idempotent, on a database that already has data

    with closing(sqlite3.connect(path)) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(model_requests)")]
        kept = connection.execute("SELECT stage FROM stage_timings").fetchall()
    assert columns == COLUMNS
    assert kept == [("model.route",)]
    assert rows(path) == []


ROW = ModelRequestRow(
    run_id=None,
    stage="model.route",
    request_index=1,
    started_at="2026-10-21T00:00:00+00:00",
    latency_ms=1.0,
    outcome="ok",
    error_kind=None,
    model="test-model",
    response_model=None,
    reasoning_effort=None,
    text_format=None,
    input_tokens=None,
    cached_input_tokens=None,
    cache_write_tokens=None,
    output_tokens=None,
    reasoning_tokens=None,
    retries=None,
    request_sha256=None,
    instructions_sha256=None,
    catalog_vocabulary=None,
    input_json=None,
    output_json=None,
)


@pytest.mark.parametrize("schema", [FIRST_SCHEMA, SECOND_SCHEMA], ids=["first", "second"])
def test_an_older_table_gains_the_failure_columns_and_keeps_its_rows(
    tmp_path: Path, schema: str
) -> None:
    path = tmp_path / "agent.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(schema)
        connection.execute(
            "INSERT INTO model_requests (run_id, stage, request_index, started_at, latency_ms, "
            "outcome, error_kind, model, retries) VALUES (?, 'model.route', 1, "
            "'2026-10-21T00:00:00+00:00', 12.5, 'error', 'RateLimitError', 'test-model', 1)",
            (RUN_ID,),
        )

    sink = ledger(path)
    ledger(path)  # idempotent once migrated
    sink.record(
        replace(
            ROW,
            outcome="error",
            error_kind="RateLimitError",
            http_status=429,
            provider_error_code="enforced_spend_limit_reached",
            provider_error_message="You have reached your monthly spend limit for this tier.",
        )
    )

    with closing(sqlite3.connect(path)) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(model_requests)")]
    assert columns == COLUMNS
    kept, recorded = rows(path)
    # The older row is intact; it never knew its status, code or message.
    assert (kept["sequence"], kept["run_id"], kept["error_kind"], kept["retries"]) == (
        1,
        RUN_ID,
        "RateLimitError",
        1,
    )
    assert (
        kept["latency_ms"],
        kept["http_status"],
        kept["provider_error_code"],
        kept["provider_error_message"],
    ) == (12.5, None, None, None)
    assert (
        recorded["sequence"],
        recorded["http_status"],
        recorded["provider_error_code"],
        recorded["provider_error_message"],
    ) == (
        2,
        429,
        "enforced_spend_limit_reached",
        "You have reached your monthly spend limit for this tier.",
    )


def test_a_row_that_cannot_be_written_is_logged_and_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sink = SqliteModelRequests(tmp_path / "agent.db")  # never initialized: no table

    sink.record(ROW)

    assert "Model request not recorded: model.route" in caplog.text


# The conversation router -----------------------------------------------------------------------


@pytest.mark.parametrize("summaries", [False, True], ids=["plain", "summaries"])
def test_each_request_of_a_turn_is_one_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, summaries: bool
) -> None:
    path = tmp_path / "agent.db"

    sent, decision, choice = run_turn(monkeypatch, path, ledger(path), summaries=summaries)

    recorded = rows(path)
    lookup, routed, chosen = recorded
    assert [(r["run_id"], r["stage"], r["request_index"]) for r in recorded] == [
        (RUN_ID, "model.route", 1),
        (RUN_ID, "model.route", 2),
        (RUN_ID, "model.choose_candidate", 1),
    ]
    assert {
        (r["outcome"], r["error_kind"], r["model"], r["response_model"], r["reasoning_effort"])
        for r in recorded
    } == {("ok", None, "claude-haiku-5-5", MODEL, "low")}
    # The router's first request asks for no schema: it may call lookup_catalog (llm/claude.py).
    assert [r["text_format"] for r in recorded] == [None, "CommerceDecision", "CandidateChoice"]
    # The counters of each request: all its input (801 uncached and 1024 read from the cache),
    # the read and the written parts, the output and the thinking within it.
    assert {
        (
            r["input_tokens"],
            r["cached_input_tokens"],
            r["cache_write_tokens"],
            r["output_tokens"],
            r["reasoning_tokens"],
        )
        for r in recorded
    } == {(1825, 1024, 0, 786, 640)}
    # The SDK retried the throttled choice once; the other requests went out once.
    assert [r["retries"] for r in recorded] == [0, 0, 1]
    assert len(sent) == 4
    # The router's instructions carry the feed's vocabulary, which a scenario may mutate.
    route_body = json.loads(sent[0][3])
    assert route_body["system"][0]["text"].endswith(lookup["catalog_vocabulary"])
    assert json.loads(lookup["catalog_vocabulary"])["product_count"] == 1
    assert lookup["instructions_sha256"] == routed["instructions_sha256"]
    assert chosen["catalog_vocabulary"] is None
    assert chosen["instructions_sha256"] != routed["instructions_sha256"]
    assert all(r["latency_ms"] >= 0 and r["started_at"] for r in recorded)
    assert all(r["reasoning_summary"] is None for r in recorded)
    # A request that ended well, even after a throttled attempt, has no failure to describe.
    assert {
        (r["http_status"], r["provider_error_code"], r["provider_error_message"]) for r in recorded
    } == {(None, None, None)}

    # lookup_catalog, with its query: the first request's output and the second one's input.
    assert json.loads(lookup["output_json"]) == {
        "structured": None,
        "function_calls": [
            {"name": "lookup_catalog", "arguments": '{"query": "audífonos negros"}'}
        ],
        "stop_reason": "tool_use",
        "text": None,
    }
    assert json.loads(lookup["input_json"]) == [m.model_dump(mode="json") for m in MESSAGES]
    asked, answered = json.loads(routed["input_json"])[-2:]
    assert (asked["role"], asked["content"][0]["type"]) == ("assistant", "tool_use")
    assert (answered["role"], answered["content"][0]["type"]) == ("user", "tool_result")
    assert "SON-01" in answered["content"][0]["content"]
    # The model's own intent, before the router aligns it with the catalog.
    assert json.loads(routed["output_json"]) == {
        "structured": ROUTED.model_dump(mode="json"),
        "function_calls": [],
    }
    assert decision.intent is not None
    # The choice: what the model saw and why it chose.
    payload = json.loads(chosen["input_json"])
    assert [item["variant_id"] for item in payload["candidates"]] == ["SON-01"]
    assert json.loads(chosen["output_json"])["structured"] == choice.model_dump(mode="json")
    assert choice.assistant_message == "Elijo SON-01 porque es el más barato."


def test_the_request_digest_covers_the_instructions_and_the_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "agent.db"
    server = ModelServer(structured(shortlisted_choice))
    router = build_router(monkeypatch, server, ledger(path))

    router.choose_candidate(MESSAGES, INTENT, SHORTLIST)

    (row,) = rows(path)
    (body,) = server.bodies()
    (message,) = body["messages"]
    canonical = json.dumps(
        {"instructions": body["system"][0]["text"], "input": message["content"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert row["request_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    # input_json is the variable part; the instructions are versioned in the code.
    assert row["input_json"] == message["content"]
    # Outside a timed stage and a turn there is no stage or run to name.
    assert (row["run_id"], row["stage"], row["request_index"]) == (None, None, 1)


@pytest.mark.parametrize(
    ("answers", "error_kind", "retries", "http_status", "provider_error_code"),
    [
        ((failure(500), failure(500)), "InternalServerError", 1, 500, "api_error"),
        (
            (failure(529, error_type="overloaded_error"),) * 2,
            "OverloadedError",
            1,
            529,
            "overloaded_error",
        ),
        (
            (failure(400, error_type="invalid_request_error", message=GRAMMAR_TOO_LARGE),),
            "BadRequestError",
            0,
            400,
            "invalid_request_error",
        ),
        # Both are 429s; the code tells them apart (§3.3).
        (
            (failure(429, error_type="rate_limit_error"),) * 2,
            "RateLimitError",
            1,
            429,
            "rate_limit_error",
        ),
        (
            (failure(429, code="enforced_spend_limit_reached", error_type="rate_limit_error"),),
            "EnforcedSpendLimitReachedError",
            0,
            429,
            "enforced_spend_limit_reached",
        ),
        ((failure(402, error_type="billing_error"),), "APIStatusError", 0, 402, "billing_error"),
        # No response came back.
        ((disconnect(), disconnect()), "APIConnectionError", 1, None, None),
    ],
    ids=[
        "retried",
        "overloaded",
        "refused",
        "rate-limited",
        "spend-cap",
        "billing",
        "unreachable",
    ],
)
def test_a_failed_request_is_an_error_row_and_the_same_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    answers: tuple[Any, ...],
    error_kind: str,
    retries: int,
    http_status: int | None,
    provider_error_code: str | None,
) -> None:
    path = tmp_path / "agent.db"
    router = timed(build_router(monkeypatch, ModelServer(*answers), ledger(path)), path)

    with pytest.raises(IntentInterpretationError, match="The conversation model request failed"):
        router.route(MESSAGES)

    # The router's first request failed, so it never sent the second.
    (row,) = rows(path)
    assert (row["stage"], row["outcome"], row["error_kind"], row["retries"]) == (
        "model.route",
        "error",
        error_kind,
        retries,
    )
    assert (row["http_status"], row["provider_error_code"]) == (http_status, provider_error_code)
    # What the provider said is kept for diagnosis; a dropped connection said nothing.
    said = answers[-1]({}).json()["error"]["message"] if http_status else None
    assert row["provider_error_message"] == said
    # No answer came back: nothing billed to count, nothing said to keep.
    assert (row["input_tokens"], row["output_json"]) == (None, None)
    assert json.loads(row["input_json"]) == [m.model_dump(mode="json") for m in MESSAGES]


CUT = '{"action": "respond", "assistant_mess'
REFUSAL = "No puedo ayudar con eso."


@pytest.mark.parametrize(
    ("answer", "stop_reason", "said"),
    [
        (structured(lambda body: {"action": "buy"}), "end_turn", '{"action": "buy"}'),
        (text(CUT, stop_reason="max_tokens"), "max_tokens", CUT),
        (text(REFUSAL, stop_reason="refusal"), "refusal", REFUSAL),
    ],
    ids=["outside-the-schema", "cut", "refused"],
)
def test_an_invalid_answer_keeps_its_usage_and_why_it_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answer: Any, stop_reason: str, said: str
) -> None:
    path = tmp_path / "agent.db"
    server = ModelServer(no_tool_call(), answer)
    router = timed(build_router(monkeypatch, server, ledger(path)), path)

    with pytest.raises(IntentInterpretationError, match="The conversation model request failed"):
        router.route(MESSAGES)

    # The decision request failed; the first one, which no schema bound, did not.
    first, row = rows(path)
    assert (first["outcome"], first["request_index"], row["request_index"]) == ("ok", 1, 2)
    assert (row["outcome"], row["error_kind"]) == ("error", "ValidationError")
    assert (row["http_status"], row["provider_error_code"]) == (None, None)
    assert row["provider_error_message"] is None
    # The answer was billed: its tokens count like an ok row's.
    assert (row["input_tokens"], row["output_tokens"], row["response_model"]) == (1825, 786, MODEL)
    # What the model said and why it stopped: a refusal reads apart from a cut answer.
    assert json.loads(row["output_json"]) == {
        "structured": None,
        "function_calls": [],
        "stop_reason": stop_reason,
        "text": said,
    }


def test_a_turn_without_a_lookup_keeps_the_discarded_answer_and_the_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "agent.db"
    router = timed(build_router(monkeypatch, ModelServer(*routed(ROUTED)), ledger(path)), path)

    with model_request_scope(run_id=RUN_ID):
        router.route(MESSAGES)

    offered, decided = rows(path)
    assert [(r["stage"], r["request_index"], r["outcome"]) for r in (offered, decided)] == [
        ("model.route", 1, "ok"),
        ("model.route", 2, "ok"),
    ]
    # The first answer is discarded by the router, never by the ledger: what it said is kept.
    assert offered["text_format"] is None
    assert json.loads(offered["output_json"]) == {
        "structured": None,
        "function_calls": [],
        "stop_reason": "end_turn",
        "text": SAID_WITHOUT_LOOKUP,
    }
    assert decided["text_format"] == "CommerceDecision"
    assert json.loads(decided["output_json"])["structured"] == ROUTED.model_dump(mode="json")
    # The same instructions and input: the second asks again for the decision.
    assert offered["input_json"] == decided["input_json"]
    assert offered["request_sha256"] == decided["request_sha256"]
    assert offered["catalog_vocabulary"] == decided["catalog_vocabulary"]


def test_the_provider_message_is_cut_short_and_never_keeps_a_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "agent.db"
    message = f"invalid x-api-key {API_KEY}: " + "x" * 600
    server = ModelServer(failure(401, error_type="authentication_error", message=message))
    router = timed(build_router(monkeypatch, server, ledger(path)), path)

    with pytest.raises(IntentInterpretationError) as raised:
        router.route(MESSAGES)

    assert raised.value.failure_kind == "rejected"
    (row,) = rows(path)
    kept = row["provider_error_message"]
    assert len(kept) == 500
    assert kept.startswith("invalid x-api-key [redacted]: xxx")
    assert API_KEY.encode() not in path.read_bytes()


# Recording never changes the request -----------------------------------------------------------


@pytest.mark.parametrize("summaries", [False, True], ids=["plain", "summaries"])
def test_the_wire_is_identical_with_and_without_the_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, summaries: bool
) -> None:
    plain = run_turn(monkeypatch, tmp_path / "plain.db", None, summaries=summaries)
    path = tmp_path / "agent.db"
    recorded = run_turn(monkeypatch, path, ledger(path), summaries=summaries)

    # Method, URL, every header (the SDK's retry count included) and the body, byte for byte.
    assert recorded == plain
    assert len(rows(path)) == 3


class BrokenSink:
    def record(self, row: ModelRequestRow) -> None:
        raise RuntimeError("disk full")


def _raise(*args: object) -> None:
    raise ValueError("recorder bug")


@pytest.mark.parametrize("failing", ["sink", "table", "attempt-hook", "output"])
def test_a_recording_failure_never_breaks_or_changes_the_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failing: str,
) -> None:
    plain = run_turn(monkeypatch, tmp_path / "plain.db", None)
    path = tmp_path / "agent.db"
    sink: Any = ledger(path)
    if failing == "sink":
        sink = BrokenSink()
    elif failing == "table":
        sink = SqliteModelRequests(tmp_path / "uninitialized.db")
    elif failing == "attempt-hook":
        # An exception inside an httpx hook would fail the attempt and make the SDK retry it.
        monkeypatch.setattr(recorder_module, "_retry_count", _raise)
    else:
        monkeypatch.setattr(recorder_module, "_output_json", _raise)

    with caplog.at_level(logging.WARNING):
        recorded = run_turn(monkeypatch, path, sink)

    assert recorded == plain
    assert "Model request" in caplog.text
    assert API_KEY not in caplog.text


def test_no_secret_or_header_reaches_the_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "agent.db"

    sent, _, _ = run_turn(monkeypatch, path, ledger(path))

    # The key did travel, in the Authorization header of every attempt (the SDK sends it so).
    assert all(("authorization", f"Bearer {API_KEY}") in headers for _, _, headers, _ in sent)
    assert API_KEY.encode() not in path.read_bytes()
    stored = json.dumps(rows(path), ensure_ascii=False).casefold()
    assert API_KEY.casefold() not in stored
    for header in ("x-api-key", "authorization", "bearer", "x-stainless", "anthropic-version"):
        assert header not in stored


# The delegated choice (HNP) --------------------------------------------------------------------


def test_the_delegated_choice_records_usage_reasoning_and_its_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "agent.db"
    reason = "Elijo los SON-01 porque son negros, como pediste."
    # A first request: 300 input tokens plus the 1525 of the prefix it writes to the cache.
    usage = {
        "input_tokens": 300,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1525,
        "output_tokens": 786,
        "output_tokens_details": {"thinking_tokens": 640},
    }
    answer = {"action": "purchase", "assistant_message": reason, "selected_variant_id": "SON-01"}
    server = ModelServer(structured(lambda body: answer, usage=usage))
    server.install(monkeypatch)
    chooser = ClaudeDelegatedChooser(api_key=SecretStr(API_KEY), model_requests=ledger(path))

    with model_request_scope(run_id=RUN_ID, stage=MODEL_STAGE):
        choice = chooser.choose(INTENT, SHORTLIST, "Prefiero los negros.")

    # The decision is unchanged: the id and the two counters the executor already read.
    assert choice == DelegatedChoice("SON-01", 1825, 786)
    (row,) = rows(path)
    assert (row["run_id"], row["stage"], row["request_index"], row["outcome"]) == (
        RUN_ID,
        MODEL_STAGE,
        1,
        "ok",
    )
    assert (row["input_tokens"], row["cached_input_tokens"], row["cache_write_tokens"]) == (
        1825,
        0,
        1525,
    )
    assert (row["output_tokens"], row["reasoning_tokens"], row["retries"]) == (786, 640, 0)
    assert row["response_model"] == MODEL
    # The reason the chooser itself discards.
    assert json.loads(row["output_json"])["structured"]["assistant_message"] == reason
    assert json.loads(row["input_json"])["preference_text"] == "Prefiero los negros."


def test_a_usage_without_cache_counters_is_its_uncached_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "agent.db"
    server = ModelServer(
        structured(shortlisted_choice, usage={"input_tokens": 900, "output_tokens": 120})
    )
    server.install(monkeypatch)
    chooser = ClaudeDelegatedChooser(api_key=SecretStr(API_KEY), model_requests=ledger(path))

    assert chooser.choose(INTENT, SHORTLIST, None) == DelegatedChoice("SON-01", 900, 120)
    (row,) = rows(path)
    assert (row["input_tokens"], row["cached_input_tokens"], row["cache_write_tokens"]) == (
        900,
        None,
        None,
    )
    assert (row["output_tokens"], row["reasoning_tokens"]) == (120, None)


def test_the_delegated_choice_counts_the_sdk_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "agent.db"
    server = ModelServer(
        failure(429, error_type="rate_limit_error"), structured(shortlisted_choice)
    )
    server.install(monkeypatch)
    chooser = ClaudeDelegatedChooser(api_key=SecretStr(API_KEY), model_requests=ledger(path))

    with model_request_scope(run_id=RUN_ID, stage=MODEL_STAGE):
        choice = chooser.choose(INTENT, SHORTLIST, None)
    chooser.close()

    assert choice == DelegatedChoice("SON-01", 1825, 786)
    (row,) = rows(path)
    assert (row["retries"], row["outcome"]) == (1, "ok")
    assert [dict(headers)["x-stainless-retry-count"] for _, _, headers, _ in server.sent()] == [
        "0",
        "1",
    ]
