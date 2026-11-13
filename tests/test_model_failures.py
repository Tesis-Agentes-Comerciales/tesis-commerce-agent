"""A failure of the model provider is named once, and a spent balance is not retried
(docs/design/p0-model-failures.md §3.1 and §3.2).

The three model clients (the HP router, the HNP selector and the HNP reviewer) build their real
SDK client; only its transport is the scripted `httpx2.MockTransport` of
tests/support/model_server.py. No request leaves the process and no model is called.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import SecretStr, ValidationError

from commerce_agent.domain import HardConstraints, PurchaseIntent, rank_candidates
from commerce_agent.llm import (
    ClaudeConversationRouter,
    ConversationMessage,
    IntentInterpretationError,
    UnknownVariantSelection,
)
from commerce_agent.llm.delegated_choice import ClaudeDelegatedChooser
from commerce_agent.llm.intent_review import IntentVerdict, ModelHnpIntentReviewer
from commerce_agent.llm.model_failures import (
    ModelFailure,
    classify_model_failure,
    model_failure_kind,
)
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
    text,
)
from tests.test_conversation_router import FEED

API_KEY = "sk-ant-test-model-failures-0123456789abcdef"
INTENT = PurchaseIntent(
    hard=HardConstraints(currency="COP", categories=("headphones",), max_total_amount_minor=150_000)
)
SHORTLIST = rank_candidates(FEED, INTENT).ranked
MESSAGES = (ConversationMessage(role="user", content="Quiero audífonos por máximo 150.000 COP"),)
URL = "https://api.anthropic.com/v1/messages"

# Anthropic's own error objects (the `error` of the response body; docs/api/errors).
SPEND_CAP = {
    "type": "rate_limit_error",
    "message": "You have reached your monthly spend limit for this tier.",
    "details": {"error_code": "enforced_spend_limit_reached"},
}
BILLING = {"type": "billing_error", "message": "Your account has a billing issue."}
LOW_CREDIT = {
    "type": "invalid_request_error",
    "message": "Your credit balance is too low to access the Anthropic API.",
}
SPEND_LIMIT = {
    "type": "invalid_request_error",
    "message": "You have reached your specified API usage limits. You will regain access soon.",
}
RATE_LIMIT = {"type": "rate_limit_error", "message": "Number of request tokens has exceeded."}
RAMP_LIMIT = {
    "type": "rate_limit_error",
    "message": "Your usage grew too fast.",
    "details": {"error_code": "ramp_limit_exceeded"},
}
SERVER_ERROR = {"type": "api_error", "message": "Internal server error."}
OVERLOADED = {"type": "overloaded_error", "message": "Overloaded."}
BAD_KEY = {"type": "authentication_error", "message": "invalid x-api-key"}


# Classification -------------------------------------------------------------------------------


def status_error(error: type[anthropic.APIStatusError], status: int, body: Any) -> Exception:
    """A status error exactly as the SDK builds it: `body` is the `error` object."""
    content = None if body is None else {"type": "error", "error": body}
    response = httpx2.Response(status, request=httpx2.Request("POST", URL), json=content)
    return error("scripted", response=response, body=content)


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
            status_error(anthropic.RateLimitError, 429, RATE_LIMIT),
            ModelFailure("rate_limited", 429, "rate_limit_error"),
        ),
        # A usage that grew too fast clears with time, as any rate limit.
        (
            status_error(anthropic.RateLimitError, 429, RAMP_LIMIT),
            ModelFailure("rate_limited", 429, "ramp_limit_exceeded"),
        ),
        # The balance is spent in four ways: the tier's monthly cap, by its code; a billing
        # error, by its status or its type; and two 400s that say so only in their message.
        (
            status_error(anthropic.RateLimitError, 429, SPEND_CAP),
            ModelFailure("quota_exhausted", 429, "enforced_spend_limit_reached"),
        ),
        (
            status_error(anthropic.APIStatusError, 402, BILLING),
            ModelFailure("quota_exhausted", 402, "billing_error"),
        ),
        (
            status_error(anthropic.APIStatusError, 402, None),
            ModelFailure("quota_exhausted", 402, None),
        ),
        (
            status_error(anthropic.BadRequestError, 400, LOW_CREDIT),
            ModelFailure("quota_exhausted", 400, "invalid_request_error"),
        ),
        (
            status_error(anthropic.BadRequestError, 400, SPEND_LIMIT),
            ModelFailure("quota_exhausted", 400, "invalid_request_error"),
        ),
        (
            anthropic.APIConnectionError(request=httpx2.Request("POST", URL)),
            ModelFailure("unreachable"),
        ),
        (
            anthropic.APITimeoutError(request=httpx2.Request("POST", URL)),
            ModelFailure("unreachable"),
        ),
        # Without a code, the type names the provider's error.
        (
            status_error(anthropic.InternalServerError, 500, SERVER_ERROR),
            ModelFailure("provider_error", 500, "api_error"),
        ),
        (
            status_error(anthropic.InternalServerError, 529, OVERLOADED),
            ModelFailure("provider_error", 529, "overloaded_error"),
        ),
        (
            status_error(anthropic.InternalServerError, 503, None),
            ModelFailure("provider_error", 503, None),
        ),
        (
            status_error(anthropic.BadRequestError, 400, {"type": "invalid_request_error"}),
            ModelFailure("rejected", 400, "invalid_request_error"),
        ),
        (
            status_error(anthropic.AuthenticationError, 401, BAD_KEY),
            ModelFailure("rejected", 401, "authentication_error"),
        ),
        (
            status_error(anthropic.PermissionDeniedError, 403, None),
            ModelFailure("rejected", 403, None),
        ),
        # The SDK retries a 409 (and a 408) once; it is still a refusal.
        (status_error(anthropic.ConflictError, 409, None), ModelFailure("rejected", 409, None)),
        (validation_error(), ModelFailure("invalid_output")),
        # Any other SDK error, and anything else.
        (anthropic.AnthropicError("unexpected"), ModelFailure("provider_error")),
        (RuntimeError("unexpected"), ModelFailure("provider_error")),
    ],
    ids=[
        "rate-limited",
        "ramp-limited",
        "spend-cap",
        "billing",
        "billing-status-only",
        "low-credit",
        "spend-limit",
        "connection",
        "timeout",
        "server-error",
        "overloaded",
        "unavailable",
        "bad-request",
        "bad-key",
        "forbidden",
        "conflict",
        "invalid-output",
        "other-sdk-error",
        "other-error",
    ],
)
def test_each_failure_has_one_kind(error: Exception, expected: ModelFailure) -> None:
    assert classify_model_failure(error) == expected


def test_a_client_error_is_classified_by_its_sdk_cause() -> None:
    cause = status_error(anthropic.RateLimitError, 429, SPEND_CAP)
    try:
        raise IntentInterpretationError("failed", failure_kind="quota_exhausted") from cause
    except IntentInterpretationError as error:
        wrapped = error

    assert classify_model_failure(wrapped) == ModelFailure(
        "quota_exhausted", 429, "enforced_spend_limit_reached"
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


def wrapped(cause: BaseException | None) -> IntentInterpretationError:
    error = IntentInterpretationError("failed")
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (IntentInterpretationError("failed", failure_kind="unreachable"), "unreachable"),
        (UnknownVariantSelection("SON-99"), "invalid_output"),
        # Without its kind, a client error is named by its SDK cause.
        (
            wrapped(anthropic.APIConnectionError(request=httpx2.Request("POST", URL))),
            "unreachable",
        ),
        (status_error(anthropic.RateLimitError, 429, SPEND_CAP), "quota_exhausted"),
        (validation_error(), "invalid_output"),
        # Not the model: the graph's own error, or anything else a selector or reviewer raised.
        (IntentInterpretationError("No conversation model is configured"), None),
        (wrapped(ValueError("bad")), None),
        (RuntimeError("bug"), None),
        (TimeoutError("slow"), None),
    ],
    ids=[
        "client-kind",
        "unknown-variant",
        "sdk-cause",
        "raw-sdk",
        "raw-validation",
        "graph",
        "other-cause",
        "bug",
        "builtin-timeout",
    ],
)
def test_only_a_model_failure_has_a_kind(error: BaseException, kind: str | None) -> None:
    assert model_failure_kind(error) == kind


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
    router = ClaudeConversationRouter(
        api_key=SecretStr(API_KEY), feed=FEED, model_requests=ledger(path, recorded)
    )
    return lambda: router.route(MESSAGES)


def build_selector(
    monkeypatch: pytest.MonkeyPatch, server: ModelServer, path: Path, recorded: bool
) -> Call:
    server.install(monkeypatch)
    chooser = ClaudeDelegatedChooser(
        api_key=SecretStr(API_KEY), model_requests=ledger(path, recorded)
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
        api_key=SecretStr(API_KEY), timer=timings, model_requests=ledger(path, recorded)
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


SPENT = {
    "spend-cap": (
        failure(429, code="enforced_spend_limit_reached", error_type="rate_limit_error"),
        ModelFailure("quota_exhausted", 429, "enforced_spend_limit_reached"),
    ),
    "billing": (
        failure(402, error_type="billing_error"),
        ModelFailure("quota_exhausted", 402, "billing_error"),
    ),
    "low-credit": (
        failure(400, error_type="invalid_request_error", message=LOW_CREDIT["message"]),
        ModelFailure("quota_exhausted", 400, "invalid_request_error"),
    ),
}


@each_client
@with_and_without_ledger
def test_a_spent_monthly_cap_is_tried_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: str, recorded: bool
) -> None:
    quota, expected = SPENT["spend-cap"]
    # The provider does not say it: only the agent's hook stops the SDK's retry of a 429.
    assert "x-should-retry" not in quota({}).headers

    error, server = failed(monkeypatch, tmp_path, client, recorded, quota, VALID[client])

    assert len(server.requests) == 1
    assert error.failure_kind == "quota_exhausted"
    assert classify_model_failure(error) == expected


@pytest.mark.parametrize("spent", list(SPENT))
@with_and_without_ledger
def test_every_spent_balance_is_tried_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spent: str, recorded: bool
) -> None:
    quota, expected = SPENT[spent]

    error, server = failed(monkeypatch, tmp_path, "selector", recorded, quota, VALID["selector"])

    assert (len(server.requests), error.failure_kind) == (1, "quota_exhausted")
    assert classify_model_failure(error) == expected


@pytest.mark.parametrize("recorded", [True, False], ids=["ledger", "no-ledger"])
def test_the_router_with_summaries_tries_a_spent_quota_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recorded: bool
) -> None:
    server = ModelServer(SPENT["spend-cap"][0], VALID["router"])
    server.install(monkeypatch)
    router = ClaudeConversationRouter(
        api_key=SecretStr(API_KEY),
        feed=FEED,
        reasoning_summary=True,
        model_requests=ledger(tmp_path / "agent.db", recorded),
    )

    with reasoning_listener(lambda delta: None), pytest.raises(IntentInterpretationError) as raised:
        router.route(MESSAGES)

    assert server.bodies()[0]["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert (len(server.requests), raised.value.failure_kind) == (1, "quota_exhausted")


@each_client
@with_and_without_ledger
def test_a_rate_limit_keeps_the_sdk_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: str, recorded: bool
) -> None:
    limited = failure(429, error_type="rate_limit_error")

    error, server = failed(monkeypatch, tmp_path, client, recorded, limited, limited)

    assert len(server.requests) == 2
    assert error.failure_kind == "rate_limited"
    assert classify_model_failure(error) == ModelFailure("rate_limited", 429, "rate_limit_error")


@each_client
@with_and_without_ledger
def test_a_rate_limit_that_clears_still_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: str, recorded: bool
) -> None:
    server = ModelServer(failure(429, error_type="rate_limit_error"), VALID[client])

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
    error, server = failed(monkeypatch, tmp_path, "router", True, failure(500), failure(500))

    assert len(server.requests) == 2
    assert classify_model_failure(error) == ModelFailure("provider_error", 500, "api_error")


@each_client
def test_an_overloaded_api_is_a_provider_error_after_the_sdk_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: str
) -> None:
    overloaded = failure(529, error_type="overloaded_error")

    error, server = failed(monkeypatch, tmp_path, client, True, overloaded, overloaded)

    assert len(server.requests) == 2
    assert error.failure_kind == "provider_error"
    assert classify_model_failure(error) == ModelFailure("provider_error", 529, "overloaded_error")


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
    def unreadable(body: dict[str, Any]) -> httpx2.Response:
        return httpx2.Response(429, headers={"retry-after-ms": "1"}, text="Too Many Requests")

    error, server = failed(monkeypatch, tmp_path, "router", False, unreadable, unreadable)

    assert len(server.requests) == 2
    assert classify_model_failure(error) == ModelFailure("rate_limited", 429, None)


# Every output a client cannot use -------------------------------------------------------------


def _verdict(decision: str, reason: str) -> Answer:
    return structured(lambda body: {"decision": decision, "reason": reason})


def _empty(body: dict[str, Any]) -> httpx2.Response:
    """A complete answer without any content block."""
    return httpx2.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0},
        },
    )


@pytest.mark.parametrize(
    ("client", "answers"),
    [
        ("router", (_empty,)),
        # An answer cut at max_tokens, or a refusal, is no JSON of the schema.
        ("router", (text('{"action": "respond", "assistant_mess'),)),
        ("router", (text("No puedo ayudar con eso."),)),
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
        "router-cut-answer",
        "router-refusal",
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
    router = ClaudeConversationRouter(api_key=SecretStr(API_KEY), feed=FEED)

    with pytest.raises(UnknownVariantSelection) as raised:
        router.choose_candidate(MESSAGES, INTENT, SHORTLIST)

    assert raised.value.variant_id == "SON-99"
    assert raised.value.failure_kind == "invalid_output"
    assert classify_model_failure(raised.value) == ModelFailure("invalid_output")
