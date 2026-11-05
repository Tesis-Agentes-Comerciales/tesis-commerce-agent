"""Why a call to the model failed, named once (docs/design/p0-model-failures.md §3.1 to §3.3).

The three model clients (the HP router, the HNP selector and the HNP reviewer) raise
`IntentInterpretationError` with its `failure_kind`, and `classify_model_failure` gives the same
kind, the HTTP status and the provider's error code for the `model_requests` ledger:

- `rate_limited`: HTTP 429 without a spent quota.
- `quota_exhausted`: HTTP 429 with `insufficient_quota` as the error's code or type.
- `unreachable`: no response, from a connection error or a timeout.
- `provider_error`: HTTP 5xx, and any other SDK error, such as a stream that never completed.
- `rejected`: any other HTTP status error (400, 401, 403, 408, 409...).
- `invalid_output`: an answer the client cannot use: no structured output, one outside the
  schema, an incoherent one.

A spent quota does not come back by retrying. `refuse_quota_retry` is an httpx response hook on
the client the SDK gets (llm/model_requests.py): it marks that one response `x-should-retry:
false`, which the SDK obeys before it retries a 429. Everything else keeps the SDK's own retry
decision (one retry of 408, 409, 429, 5xx and connection errors). The hook only reads the body
and adds that header, and it never raises: httpx would turn an exception there into a failed
attempt that the SDK retries.
"""

import logging
from dataclasses import dataclass
from typing import Literal

import httpx
from openai import APIConnectionError, APIStatusError
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

SPENT_QUOTA = "insufficient_quota"
NO_RETRY_HEADER = "x-should-retry"


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
    # The `code` of the provider's error body, or its `type` when the body has no code.
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
        code = _provider_code(error.body)
        if status == 429:
            kind: ModelFailureKind = "quota_exhausted" if _spent(error.body) else "rate_limited"
        elif status >= 500:
            kind = "provider_error"
        else:
            kind = "rejected"
        return ModelFailure(kind, status, code)
    # A structured output outside the schema fails inside `parse`, after the HTTP exchange.
    if isinstance(error, ValidationError):
        return ModelFailure("invalid_output")
    return ModelFailure("provider_error")


def refuse_quota_retry(response: httpx.Response) -> None:
    """httpx response hook: the SDK does not retry a 429 whose quota is spent."""
    if response.status_code != 429:
        return
    try:
        response.read()
        payload = response.json()
        # The SDK reads the same `error` object into the error's body.
        body = payload.get("error", payload) if isinstance(payload, dict) else payload
        if _spent(body):
            response.headers[NO_RETRY_HEADER] = "false"
    except Exception as error:
        # The SDK retries it, as it did before this hook.
        _log.warning("Model 429 not inspected for its quota: %s", type(error).__name__)


def _spent(body: object) -> bool:
    return isinstance(body, dict) and SPENT_QUOTA in (body.get("code"), body.get("type"))


def _provider_code(body: object) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("code", "type"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None
