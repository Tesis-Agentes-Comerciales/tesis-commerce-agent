"""Why a call to the model failed, named once (docs/design/p0-model-failures.md §3.1 to §3.3).

The three model clients (the HP router, the HNP selector and the HNP reviewer) raise
`IntentInterpretationError` with its `failure_kind`, and `classify_model_failure` gives the same
kind, the HTTP status and the provider's error code for the `model_requests` ledger, which also
keeps the provider's error message (`provider_error_message`):

- `rate_limited`: HTTP 429 without a spent balance.
- `quota_exhausted`: the account cannot pay for the call, and retrying will not change that
  (docs/api/errors and docs/api/rate-limits): a 402 `billing_error`; the tier's monthly spend cap,
  a 429 whose `error.details.error_code` is `enforced_spend_limit_reached`; or a 400 whose message
  says the credit balance is too low or a spend limit was reached. Those two 400s carry no code of
  their own, so only they are told apart by their message; if its wording changes they become
  `rejected`, which the SDK does not retry either.
- `unreachable`: no response, from a connection error or a timeout.
- `provider_error`: HTTP 5xx (529 `overloaded_error` included), and any other SDK error.
- `rejected`: any other HTTP status error (400, 401, 403, 404, 408, 409...).
- `invalid_output`: an answer the client cannot use: no structured output, one outside the
  schema, an incoherent one.

A spent balance does not come back by retrying. The SDK never retries a 400 or a 402, but it does
retry every 429. `refuse_quota_retry` is an httpx response hook on the client the SDK gets
(llm/model_requests.py): it marks a spend-cap 429 `x-should-retry: false`, which the SDK obeys
before it retries. Everything else keeps the SDK's own retry decision (one retry of 408, 409,
429, 5xx and connection errors). The hook only reads the body and adds that header, and it never
raises: httpx would turn an exception there into a failed attempt that the SDK retries.
"""

import logging
import re
from dataclasses import dataclass
from typing import Literal

import httpx2
from anthropic import AnthropicError, APIConnectionError, APIStatusError
from pydantic import ValidationError

_log = logging.getLogger(__name__)

ModelFailureKind = Literal[
    "rate_limited",
    "quota_exhausted",
    "unreachable",
    "provider_error",
    "invalid_output",
    "rejected",
]

SPEND_CAP = "enforced_spend_limit_reached"
BILLING_ERROR = "billing_error"
# The start of the two 400s that name a spent balance in their message alone.
SPENT_MESSAGES = ("your credit balance is too low", "you have reached your specified")
NO_RETRY_HEADER = "x-should-retry"
# The ledger keeps this much of the provider's error message.
PROVIDER_MESSAGE_LIMIT = 500
# The shape of an Anthropic key, never kept even if a message ever quoted one.
_API_KEY = re.compile(r"sk-ant-[A-Za-z0-9_-]+")


class IntentInterpretationError(RuntimeError):
    """No usable answer from the model; `failure_kind` says why when the model is the cause.

    The three clients name the kind of every error they raise. The conversation graph also raises
    it for causes outside the model (no router configured, missing checkout details), with no kind.
    """

    code = "intent_model_error"

    def __init__(self, message: str, *, failure_kind: ModelFailureKind | None = None) -> None:
        super().__init__(message)
        self.failure_kind: ModelFailureKind | None = failure_kind


@dataclass(frozen=True)
class ModelFailure:
    kind: ModelFailureKind
    # The provider's answer, when there was one.
    http_status: int | None = None
    # The `error.details.error_code` of the provider's error body, or its `error.type`.
    provider_error_code: str | None = None


def classify_model_failure(error: BaseException) -> ModelFailure:
    """The failure of a raw SDK error, or of an `IntentInterpretationError`.

    An `IntentInterpretationError` is classified by its SDK cause when it has one, otherwise by
    the kind its client gave it; one without either is a `provider_error`.
    """
    if isinstance(error, IntentInterpretationError):
        cause = error.__cause__
        if isinstance(cause, APIStatusError | APIConnectionError):
            return classify_model_failure(cause)
        return ModelFailure(error.failure_kind or "provider_error")
    # APITimeoutError is an APIConnectionError.
    if isinstance(error, APIConnectionError):
        return ModelFailure("unreachable")
    if isinstance(error, APIStatusError):
        status = error.status_code
        if _spent(status, error.type, error.error_code, error.api_message):
            kind: ModelFailureKind = "quota_exhausted"
        elif status == 429:
            kind = "rate_limited"
        elif status >= 500:
            kind = "provider_error"
        else:
            kind = "rejected"
        return ModelFailure(kind, status, error.error_code or error.type)
    # A structured output outside the schema fails inside `parse`, after the HTTP exchange.
    if isinstance(error, ValidationError):
        return ModelFailure("invalid_output")
    return ModelFailure("provider_error")


def provider_error_message(error: BaseException) -> str | None:
    """The `error.message` of the provider's error body, cut to `PROVIDER_MESSAGE_LIMIT`
    characters, so a rejected request can be diagnosed from the ledger alone (e.g. the 400 "The
    compiled grammar is too large"). Only the body is read, never the headers, and a key's shape
    is replaced. None when no response came back or its body had no message."""
    status = error.__cause__ if isinstance(error, IntentInterpretationError) else error
    if not isinstance(status, APIStatusError) or not isinstance(status.api_message, str):
        return None
    return _API_KEY.sub("[redacted]", status.api_message)[:PROVIDER_MESSAGE_LIMIT]


def model_failure_kind(error: BaseException) -> ModelFailureKind | None:
    """The kind of a failed model call, or None when the model was not the cause.

    The kind a client gave its `IntentInterpretationError`, else that of its SDK cause or of a raw
    SDK error. Anything else (a bug, a selector without an answer) names no kind.
    """
    if isinstance(error, IntentInterpretationError) and error.failure_kind is not None:
        return error.failure_kind
    sdk_error = error.__cause__ if isinstance(error, IntentInterpretationError) else error
    if isinstance(sdk_error, AnthropicError | ValidationError):
        return classify_model_failure(sdk_error).kind
    return None


def refuse_quota_retry(response: httpx2.Response) -> None:
    """httpx response hook: the SDK does not retry a 429 whose monthly spend cap is reached."""
    if response.status_code != 429:
        return
    try:
        response.read()
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        details = error.get("details") if isinstance(error, dict) else None
        if isinstance(details, dict) and details.get("error_code") == SPEND_CAP:
            response.headers[NO_RETRY_HEADER] = "false"
    except Exception as error:
        # The SDK retries it, as it did before this hook.
        _log.warning("Model 429 not inspected for its spend cap: %s", type(error).__name__)


def _spent(status: int, error_type: str | None, code: str | None, message: str | None) -> bool:
    if status == 402 or error_type == BILLING_ERROR or code == SPEND_CAP:
        return True
    return status == 400 and (message or "").casefold().startswith(SPENT_MESSAGES)
