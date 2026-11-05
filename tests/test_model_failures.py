"""A failure of the model provider is named once, and a spent quota is not retried
(docs/design/p0-model-failures.md §3.1 and §3.2).

The three model clients (the HP router, the HNP selector and the HNP reviewer) build their real
SDK client; only its transport is the scripted `httpx.MockTransport` of
tests/support/model_server.py. No request leaves the process and no model is called.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from pydantic import SecretStr, ValidationError

from commerce_agent.domain import HardConstraints, PurchaseIntent, rank_candidates
from commerce_agent.llm import (
    ConversationMessage,
    IntentInterpretationError,
    OpenAIConversationRouter,
    UnknownVariantSelection,
)
from commerce_agent.llm.delegated_choice import OpenAIDelegatedChooser
from commerce_agent.llm.intent_review import IntentVerdict, ModelHnpIntentReviewer
from commerce_agent.llm.model_failures import ModelFailure, classify_model_failure
from commerce_agent.llm.reasoning import reasoning_listener
from commerce_agent.persistence.model_requests_sqlite import SqliteModelRequests
from commerce_agent.persistence.stage_timings_sqlite import SqliteStageTimings
from tests.support.model_server import (
    Answer,
    ModelServer,
    disconnect,
    failure,
    function_call,
    shortlisted_choice,
    structured,
)
from tests.test_conversation_router import FEED

API_KEY = "sk-test-model-failures-0123456789abcdef"
INTENT = PurchaseIntent(
    hard=HardConstraints(currency="COP", categories=("headphones",), max_total_amount_minor=150_000)
)
SHORTLIST = rank_candidates(FEED, INTENT).ranked
MESSAGES = (ConversationMessage(role="user", content="Quiero audífonos por máximo 150.000 COP"),)
URL = "https://api.openai.com/v1/responses"

# OpenAI's own error bodies (the `error` object of the response).
SPENT_QUOTA = {
    "message": "You exceeded your current quota, please check your plan and billing details.",
    "type": "insufficient_quota",
    "param": None,
    "code": "insufficient_quota",
}
RATE_LIMIT = {
    "message": "Rate limit reached for requests.",
    "type": "requests",
    "param": None,
    "code": "rate_limit_exceeded",
}
SERVER_ERROR = {"message": "The server had an error.", "type": "server_error", "code": None}
BAD_KEY = {
    "message": "Incorrect API key.",
    "type": "invalid_request_error",
    "code": "invalid_api_key",
}


# Classification -------------------------------------------------------------------------------


def status_error(error: type[openai.APIStatusError], status: int, body: Any) -> Exception:
    """A status error exactly as the SDK builds it: `body` is the `error` object."""
    response = httpx.Response(status, request=httpx.Request("POST", URL), json={"error": body})
    return error("scripted", response=response, body=body)


def validation_error() -> ValidationError:
    try:
        IntentVerdict.model_validate({"decision": "confirmed"})
    except ValidationError as error:
        return error
    raise AssertionError("The verdict should not validate")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            status_error(openai.RateLimitError, 429, RATE_LIMIT),
            ModelFailure("rate_limited", 429, "rate_limit_exceeded"),
        ),
        (
            status_error(openai.RateLimitError, 429, SPENT_QUOTA),
            ModelFailure("quota_exhausted", 429, "insufficient_quota"),
        ),
        # The quota is spent whether the body says so in its code or only in its type.
        (
            status_error(openai.RateLimitError, 429, {**SPENT_QUOTA, "type": "requests"}),
            ModelFailure("quota_exhausted", 429, "insufficient_quota"),
        ),
        (
            status_error(openai.RateLimitError, 429, {**SPENT_QUOTA, "code": None}),
            ModelFailure("quota_exhausted", 429, "insufficient_quota"),
        ),
        (
            openai.APIConnectionError(request=httpx.Request("POST", URL)),
            ModelFailure("unreachable"),
        ),
        (openai.APITimeoutError(request=httpx.Request("POST", URL)), ModelFailure("unreachable")),
        # Without a code, the type names the provider's error.
        (
            status_error(openai.InternalServerError, 500, SERVER_ERROR),
            ModelFailure("provider_error", 500, "server_error"),
        ),
        (
            status_error(openai.InternalServerError, 503, None),
            ModelFailure("provider_error", 503, None),
        ),
        (
            status_error(openai.BadRequestError, 400, {"type": "invalid_request_error"}),
            ModelFailure("rejected", 400, "invalid_request_error"),
        ),
        (
            status_error(openai.AuthenticationError, 401, BAD_KEY),
            ModelFailure("rejected", 401, "invalid_api_key"),
        ),
        (
            status_error(openai.PermissionDeniedError, 403, None),
            ModelFailure("rejected", 403, None),
        ),
        # The SDK retries a 409 (and a 408) once; it is still a refusal.
        (status_error(openai.ConflictError, 409, None), ModelFailure("rejected", 409, None)),
        (validation_error(), ModelFailure("invalid_output")),
        # Any other SDK error, and the stream that ends without `response.completed`.
        (openai.OpenAIError("unexpected"), ModelFailure("provider_error")),
        (
            RuntimeError("Didn't receive a `response.completed` event."),
            ModelFailure("provider_error"),
        ),
    ],
    ids=[
        "rate-limited",
        "quota",
        "quota-code-only",
        "quota-type-only",
        "connection",
        "timeout",
        "server-error",
        "unavailable",
        "bad-request",
        "bad-key",
        "forbidden",
        "conflict",
        "invalid-output",
        "other-sdk-error",
        "incomplete-stream",
    ],
)
def test_each_failure_has_one_kind(error: Exception, expected: ModelFailure) -> None:
    assert classify_model_failure(error) == expected


def test_a_client_error_is_classified_by_its_sdk_cause() -> None:
    cause = status_error(openai.RateLimitError, 429, SPENT_QUOTA)
    try:
        raise IntentInterpretationError("failed", failure_kind="quota_exhausted") from cause
    except IntentInterpretationError as error:
        wrapped = error

    assert classify_model_failure(wrapped) == ModelFailure(
        "quota_exhausted", 429, "insufficient_quota"
    )


def test_a_client_error_without_an_sdk_cause_keeps_its_own_kind() -> None:
    try:
        raise IntentInterpretationError(
            "incoherent", failure_kind="invalid_output"
        ) from ValueError()
    except IntentInterpretationError as error:
        wrapped = error

    assert classify_model_failure(wrapped) == ModelFailure("invalid_output")


def test_the_error_keeps_its_code_and_whoever_builds_it_today() -> None:
    # The conversation graph raises it for causes outside the model: no kind to name.
    plain = IntentInterpretationError("No conversation model is configured")
    unknown = UnknownVariantSelection("SON-99")

    assert (plain.code, plain.failure_kind, str(plain)) == (
        "intent_model_error",
        None,
        "No conversation model is configured",
    )
    assert classify_model_failure(plain) == ModelFailure("provider_error")
    assert isinstance(unknown, IntentInterpretationError)
    assert (unknown.code, unknown.failure_kind, unknown.variant_id) == (
        "intent_model_error",
        "invalid_output",
        "SON-99",
    )
    assert str(unknown) == "The candidate model selected an unknown variant"


# The three clients ----------------------------------------------------------------------------

Call = Callable[[], object]
Build = Callable[[pytest.MonkeyPatch, ModelServer, Path, bool], Call]

VALID = {
    "router": structured(
        lambda body: {
            "action": "respond",
            "assistant_message": "Hola, ¿qué buscas?",
            "purchase_request": None,
            "intent": None,
        }
    ),
    "selector": structured(shortlisted_choice),
    "reviewer": structured(lambda body: {"decision": "confirmed", "reason": "matches_request"}),
}
# A structured output outside each client's schema: `parse` fails after the HTTP exchange.
OUTSIDE_THE_SCHEMA = {
    "router": structured(lambda body: {"action": "buy"}),
    "selector": structured(lambda body: {"action": "purchase"}),
    "reviewer": structured(lambda body: {"decision": "confirmed"}),
}


def ledger(path: Path, recorded: bool) -> SqliteModelRequests | None:
    if not recorded:
        return None
    result = SqliteModelRequests(path)
    result.initialize()
    return result


def build_router(
    monkeypatch: pytest.MonkeyPatch, server: ModelServer, path: Path, recorded: bool
) -> Call:
    server.install(monkeypatch)
    router = OpenAIConversationRouter(
        api_key=SecretStr(API_KEY),
        model="test-model",
        feed=FEED,
        model_requests=ledger(path, recorded),
    )
    return lambda: router.route(MESSAGES)


def build_selector(
    monkeypatch: pytest.MonkeyPatch, server: ModelServer, path: Path, recorded: bool
) -> Call:
    server.install(monkeypatch)
    chooser = OpenAIDelegatedChooser(
        api_key=SecretStr(API_KEY), model="test-model", model_requests=ledger(path, recorded)
    )
    return lambda: chooser.choose(INTENT, SHORTLIST, None)


def build_reviewer(
    monkeypatch: pytest.MonkeyPatch, server: ModelServer, path: Path, recorded: bool
) -> Call:
    server.install(monkeypatch)
    timings = SqliteStageTimings(path)
    timings.initialize()
    timings.initialize_model_tokens()
    reviewer = ModelHnpIntentReviewer(
        api_key=SecretStr(API_KEY),
        model="test-model",
        timer=timings,
        model_requests=ledger(path, recorded),
    )
    return lambda: reviewer.review(
        shortlist=SHORTLIST, intent=INTENT, preference_text=None, chosen_variant_id="SON-01"
    )


CLIENTS: dict[str, Build] = {
    "router": build_router,
    "selector": build_selector,
    "reviewer": build_reviewer,
}

each_client = pytest.mark.parametrize("client", list(CLIENTS))
with_and_without_ledger = pytest.mark.parametrize(
    "recorded", [True, False], ids=["ledger", "no-ledger"]
)


def failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: str,
    recorded: bool,
    *answers: Answer,
) -> tuple[IntentInterpretationError, ModelServer]:
    server = ModelServer(*answers)
    call = CLIENTS[client](monkeypatch, server, tmp_path / "agent.db", recorded)
    with pytest.raises(IntentInterpretationError) as raised:
        call()
    return raised.value, server


@each_client
@with_and_without_ledger
def test_a_spent_quota_is_tried_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: str, recorded: bool
) -> None:
    quota = failure(429, code="insufficient_quota", error_type="insufficient_quota")
    # The provider does not say it: only the agent's hook stops the retry.
    assert "x-should-retry" not in quota({}).headers

    error, server = failed(monkeypatch, tmp_path, client, recorded, quota, VALID[client])

    assert len(server.requests) == 1
    assert error.failure_kind == "quota_exhausted"
    assert classify_model_failure(error) == ModelFailure(
        "quota_exhausted", 429, "insufficient_quota"
    )


@pytest.mark.parametrize(
    "quota",
    [failure(429, code="insufficient_quota"), failure(429, error_type="insufficient_quota")],
    ids=["code-only", "type-only"],
)
@with_and_without_ledger
def test_either_quota_marker_stops_the_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, quota: Answer, recorded: bool
) -> None:
    error, server = failed(monkeypatch, tmp_path, "selector", recorded, quota, VALID["selector"])

    assert (len(server.requests), error.failure_kind) == (1, "quota_exhausted")


@pytest.mark.parametrize("recorded", [True, False], ids=["ledger", "no-ledger"])
def test_the_streamed_router_tries_a_spent_quota_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recorded: bool
) -> None:
    server = ModelServer(failure(429, code="insufficient_quota"), VALID["router"])
    server.install(monkeypatch)
    router = OpenAIConversationRouter(
        api_key=SecretStr(API_KEY),
        model="test-model",
        feed=FEED,
        reasoning_summary=True,
        model_requests=ledger(tmp_path / "agent.db", recorded),
    )

    with reasoning_listener(lambda delta: None), pytest.raises(IntentInterpretationError) as raised:
        router.route(MESSAGES)

    assert server.bodies()[0]["stream"] is True
    assert (len(server.requests), raised.value.failure_kind) == (1, "quota_exhausted")


@each_client
@with_and_without_ledger
def test_a_rate_limit_keeps_the_sdk_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: str, recorded: bool
) -> None:
    limited = failure(429, code="rate_limit_exceeded", error_type="requests")

    error, server = failed(monkeypatch, tmp_path, client, recorded, limited, limited)

    assert len(server.requests) == 2
    assert error.failure_kind == "rate_limited"
    assert classify_model_failure(error) == ModelFailure("rate_limited", 429, "rate_limit_exceeded")


@each_client
@with_and_without_ledger
def test_a_rate_limit_that_clears_still_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: str, recorded: bool
) -> None:
    server = ModelServer(failure(429, code="rate_limit_exceeded"), VALID[client])

    CLIENTS[client](monkeypatch, server, tmp_path / "agent.db", recorded)()

    assert len(server.requests) == 2


@each_client
@with_and_without_ledger
def test_a_dropped_connection_is_retried_and_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: str, recorded: bool
) -> None:
    error, server = failed(monkeypatch, tmp_path, client, recorded, disconnect(), disconnect())

    assert len(server.requests) == 2
    assert error.failure_kind == "unreachable"
    assert classify_model_failure(error) == ModelFailure("unreachable")


@each_client
@with_and_without_ledger
def test_a_structured_output_outside_the_schema_is_invalid_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: str, recorded: bool
) -> None:
    error, server = failed(monkeypatch, tmp_path, client, recorded, OUTSIDE_THE_SCHEMA[client])

    assert len(server.requests) == 1
    assert error.failure_kind == "invalid_output"
    assert classify_model_failure(error) == ModelFailure("invalid_output")


def test_a_server_error_is_a_provider_error_after_the_sdk_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    error, server = failed(
        monkeypatch, tmp_path, "router", True, failure(500), failure(500, error_type="server_error")
    )

    assert len(server.requests) == 2
    assert classify_model_failure(error) == ModelFailure("provider_error", 500, "server_error")


@pytest.mark.parametrize(
    ("status", "attempts"), [(401, 1), (409, 2)], ids=["unauthorized", "conflict"]
)
def test_a_refused_request_is_rejected_and_keeps_the_sdk_retry_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int, attempts: int
) -> None:
    refused = failure(status, code="scripted_refusal", error_type="invalid_request_error")

    error, server = failed(monkeypatch, tmp_path, "selector", False, refused, refused)

    # The SDK does not retry a 401 and retries a 409 once, as before.
    assert len(server.requests) == attempts
    assert classify_model_failure(error) == ModelFailure("rejected", status, "scripted_refusal")


def test_a_429_the_hook_cannot_read_keeps_the_sdk_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unreadable(body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after-ms": "1"}, text="Too Many Requests")

    error, server = failed(monkeypatch, tmp_path, "router", False, unreadable, unreadable)

    assert len(server.requests) == 2
    assert classify_model_failure(error) == ModelFailure("rate_limited", 429, None)


# Every output a client cannot use -------------------------------------------------------------


def _verdict(decision: str, reason: str) -> Answer:
    return structured(lambda body: {"decision": decision, "reason": reason})


def _empty(body: dict[str, Any]) -> httpx.Response:
    """A completed response without any output."""
    return httpx.Response(
        200,
        json={
            "id": "resp_test",
            "object": "response",
            "created_at": 0,
            "model": "test-model-2026-08-07",
            "output": [],
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "status": "completed",
        },
    )


@pytest.mark.parametrize(
    ("client", "answers"),
    [
        ("router", (_empty,)),
        ("router", (function_call("delete_catalog", {}),)),
        ("router", (function_call("lookup_catalog", {"query": 7}),)),
        (
            "router",
            (
                function_call("lookup_catalog", {"query": "audífonos"}),
                function_call("lookup_catalog", {"query": "audífonos"}),
            ),
        ),
        ("selector", (_empty,)),
        ("reviewer", (_empty,)),
        ("reviewer", (_verdict("confirmed", "unclear"),)),
    ],
    ids=[
        "router-no-decision",
        "router-unknown-tool",
        "router-invalid-tool-arguments",
        "router-tool-limit",
        "selector-no-decision",
        "reviewer-no-verdict",
        "reviewer-incoherent",
    ],
)
def test_an_output_the_client_cannot_use_is_invalid_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: str, answers: tuple[Answer, ...]
) -> None:
    error, server = failed(monkeypatch, tmp_path, client, True, *answers)

    assert len(server.requests) == len(answers)
    assert error.failure_kind == "invalid_output"
    assert classify_model_failure(error) == ModelFailure("invalid_output")


def test_a_choice_outside_the_shortlist_is_invalid_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = ModelServer(
        structured(
            lambda body: {
                "action": "purchase",
                "assistant_message": "Elijo otro.",
                "selected_variant_id": "SON-99",
            }
        )
    )
    server.install(monkeypatch)
    router = OpenAIConversationRouter(api_key=SecretStr(API_KEY), model="test-model", feed=FEED)

    with pytest.raises(UnknownVariantSelection) as raised:
        router.choose_candidate(MESSAGES, INTENT, SHORTLIST)

    assert raised.value.variant_id == "SON-99"
    assert raised.value.failure_kind == "invalid_output"
    assert classify_model_failure(raised.value) == ModelFailure("invalid_output")
