"""When one trial's record stops the whole run (docs/design/eval-suite.md §4 and §8).

Two failures of the agent's model are not fixed by a retry or by the next trial. The runner
(suite/run.py) keeps the attempt in `infra.jsonl` and stops, and the resumed run does that
trial again:

- `funds_exhausted`: a spent balance or spend cap (`quota_exhausted`) that broke the trial,
  that is, one the case did not inject (reduce.is_infra_failure);
- `misconfigured`: a setup the provider refuses. That is a `rejected` failure with a 401, 403 or
  404 (`authentication_error`, `permission_error`, `not_found_error`), or the third `rejected`
  in a row with the same stage and provider code. A single `rejected` 400 stays the agent's.
"""

from dataclasses import dataclass
from typing import NamedTuple

from pydantic import JsonValue

from commerce_evaluation.llm.transport import FundsExhausted
from commerce_evaluation.suite.trial import Row, TrialRecord

SETUP_STATUSES = frozenset({401, 403, 404})
SETUP_CODES = frozenset({"authentication_error", "permission_error", "not_found_error"})
# Trials in a row refused the same way before the refusal is the setup's, not the agent's.
REFUSED_IN_A_ROW = 3


class Misconfigured(RuntimeError):
    """The provider refuses the agent's model for its setup: its key, its permissions, its
    name or the request every trial sends."""


class Stop(NamedTuple):
    error: Exception
    # What the runner says, and what `infra.jsonl` keeps as the reason.
    reason: str


@dataclass
class Refusals:
    """The trials in a row whose model the provider refused the same way."""

    way: tuple[JsonValue, JsonValue] | None = None
    count: int = 0

    def add(self, record: TrialRecord) -> int:
        """Counts the record in, and says how many in a row were refused this way."""
        way = _refused(record)
        self.count = 0 if way is None else self.count + 1 if way == self.way else 1
        self.way = way
        return self.count


def stop(record: TrialRecord, broken: bool, refusals: Refusals) -> Stop | None:
    """Why this record stops the run, or None. `broken`: the trial is the infrastructure's.
    Every attempt goes through it, so that `refusals` counts each in order."""
    in_a_row = refusals.add(record)
    failure = record.model_failure
    if failure is None:
        return None
    if broken and failure.kind == "quota_exhausted":
        reason = "el modelo del agente se quedó sin saldo (quota_exhausted)"
        return Stop(FundsExhausted("CreditBalanceExhausted", attempts=0), reason)
    if failure.kind != "rejected":
        return None
    setup = next((row for row in failure.requests if _setup(row)), None)
    if setup is not None:
        return _misconfigured(_named(setup))
    if in_a_row >= REFUSED_IN_A_ROW:
        return _misconfigured(f"{in_a_row} intentos seguidos con {_named(failure.requests[-1])}")
    return None


def _misconfigured(what: str) -> Stop:
    reason = f"la configuración del modelo falla: {what}"
    return Stop(Misconfigured(reason), reason)


def _refused(record: TrialRecord) -> tuple[JsonValue, JsonValue] | None:
    """How the provider refused the trial's model: the stage and code of its last request."""
    failure = record.model_failure
    if failure is None or failure.kind != "rejected" or not failure.requests:
        return None
    last = failure.requests[-1]
    return (last.get("stage"), last.get("provider_error_code"))


def _setup(row: Row) -> bool:
    status, code = row.get("http_status"), row.get("provider_error_code")
    return (isinstance(status, int) and status in SETUP_STATUSES) or (
        isinstance(code, str) and code in SETUP_CODES
    )


def _named(row: Row) -> str:
    cells = (row.get("http_status"), row.get("provider_error_code"))
    named = " ".join(str(cell) for cell in cells if cell is not None) or "un rechazo"
    return f"{named} en {row.get('stage') or 'una etapa sin nombre'}"
