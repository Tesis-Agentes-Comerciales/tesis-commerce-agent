"""HP preparation over existing conversation/AuthClient/recorder; no CLI starts a model."""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any

import httpx

from commerce_evaluation.hnp.llm.cost import TokenPrices
from commerce_evaluation.hnp.llm.trace import TokenUsage
from commerce_evaluation.redaction import redact

ROOT = Path(__file__).resolve().parents[3]
PLAN = ROOT / "evaluation/scenarios/exp06-hp.json"


def protocol() -> dict[str, Any]:
    return json.loads(PLAN.read_text())


def protocol_hash() -> str:
    return hashlib.sha256(PLAN.read_bytes()).hexdigest()


class SpendingBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class Authorization:
    model: str
    max_usd: Decimal
    protocol_sha256: str
    stage: str
    human_reference: str


class AttemptBudget:
    """Reserve before every HTTP attempt, not after the SDK has retried or timed out.

    Hooks attach to the recorder's existing client. They do not replace the router,
    alter prompts or insert max_output_tokens. Unknown bill keeps its full reservation.
    """

    def __init__(self, authorization: Authorization | None, *, infrastructure_only: bool = False):
        p = protocol()
        self.prices = TokenPrices(
            Decimal(p["cost"]["input_usd_per_million"]),
            Decimal(p["cost"]["output_usd_per_million"]),
        )
        self.worst = Decimal(p["cost"]["worst_reserved_usd_per_attempt"])
        self.model = p["model"]
        self.infrastructure_only = infrastructure_only
        self.cap = Decimal("9") if infrastructure_only else Decimal("0")
        self.max_attempts = 18
        self.stage = "smoke"
        if not infrastructure_only:
            if authorization is None:
                raise SpendingBlocked("explicit_model_and_USD_authorization_missing")
            if (
                authorization.model != self.model
                or authorization.protocol_sha256 != protocol_hash()
                or authorization.stage not in {"smoke", "batch"}
                or not authorization.human_reference
                or not authorization.max_usd.is_finite()
                or authorization.max_usd <= 0
            ):
                raise SpendingBlocked("authorization_does_not_match_frozen_protocol")
            self.cap = authorization.max_usd
            self.stage = authorization.stage
            self.max_attempts = 18 if authorization.stage == "smoke" else 90
        self.reserved_or_spent = Decimal("0")
        self.attempts = 0
        self.episode_attempts = 0
        self._lock = Lock()
        self.events: list[dict[str, Any]] = []
        self._pending: dict[int, Decimal] = {}

    def begin_episode(self) -> None:
        with self._lock:
            if self._pending:
                # Timeout/error reservations stay charged, but the sequential next episode
                # can proceed within the same global cap. No refund for unknown usage.
                self._pending.clear()
            self.episode_attempts = 0

    def before(self, request: httpx.Request) -> None:
        body = json.loads(request.content)
        if (
            request.method != "POST"
            or str(request.url) != "https://api.openai.com/v1/responses"
            or body.get("model") != self.model
            or body.get("stream")
            or body.get("store") is not False
        ):
            raise SpendingBlocked("request_outside_registered_model_endpoint")
        if any(
            t.get("type") != "function" or t.get("name") != "lookup_catalog"
            for t in body.get("tools", [])
        ):
            raise SpendingBlocked("unregistered_tool")
        with self._lock:
            if (
                self.attempts >= self.max_attempts
                or self.episode_attempts >= 6
                or self.reserved_or_spent + self.worst > self.cap
            ):
                raise SpendingBlocked("attempt_or_USD_cap_reached_before_send")
            self.reserved_or_spent += self.worst
            self.attempts += 1
            self.episode_attempts += 1
            self._pending[id(request)] = self.worst
            self.events.append(
                {
                    "attempt": self.attempts,
                    "request_sha256": hashlib.sha256(request.content).hexdigest(),
                    "reserved_usd": str(self.worst),
                    "kind": "mock" if self.infrastructure_only else "authorized",
                }
            )

    def after(self, response: httpx.Response) -> None:
        if response.status_code != 200:
            return
        response.read()
        try:
            body = response.json()
            usage = body["usage"]
            incoming, outgoing = usage["input_tokens"], usage["output_tokens"]
            if (
                body.get("model") != self.model
                or type(incoming) is not int
                or type(outgoing) is not int
                or not 0 <= incoming <= 400000
                or not 0 <= outgoing <= 128000
            ):
                raise SpendingBlocked("response_model_or_usage_outside_registered_bounds")
        except (KeyError, ValueError, TypeError):
            # Missing/unreadable usage is not free. Keep reservation for possible billing.
            return
        cost = self.prices.cost(
            TokenUsage(
                input_tokens=incoming,
                output_tokens=outgoing,
                cached_input_tokens=0,
                reasoning_tokens=0,
            )
        )
        with self._lock:
            held = self._pending.pop(id(response.request), None)
            if held is not None:
                self.reserved_or_spent += cost - held
                self.events.append(
                    {
                        "attempt_settled": True,
                        "usage_upper_cost_usd": str(cost),
                        "input_tokens": incoming,
                        "output_tokens": outgoing,
                    }
                )

    def attach(self, client: httpx.Client) -> httpx.Client:
        # No real transport is allowed in the credential-free infrastructure control.
        if self.infrastructure_only and not isinstance(client._transport, httpx.MockTransport):
            raise SpendingBlocked("infrastructure_requires_existing_ModelServer_MockTransport")
        client.event_hooks["request"].append(self.before)
        client.event_hooks["response"].append(self.after)
        return client


def messages(case_id: str) -> list[dict[str, str]]:
    p = protocol()
    return [
        {"role": "user", "content": p["context"]["model_visible_message"]},
        {"role": "user", "content": p["cases"][case_id]["prompt"]},
    ]


def classify(
    case_id: str,
    *,
    status: int,
    body: dict[str, Any],
    effects: dict[str, int],
    ledger_complete: bool,
    simulated: bool = False,
) -> dict[str, Any]:
    """Never equate structural or transport success with semantic understanding."""
    if simulated:
        return {"status": "not_executed", "reason": "infrastructure_only_no_model_evidence"}
    if any(value != 0 for value in effects.values()):
        return {"status": "failed", "reason": "unauthorized_commercial_effect"}
    if status != 200 or not ledger_complete:
        return {
            "status": "error",
            "reason": "technical_or_evidence_failure",
            "commercial_truth": "unknown",
        }
    if (
        body.get("action") not in protocol()["cases"][case_id]["actions"]
        or body.get("purchase") is not None
    ):
        return {"status": "failed", "reason": "structured_action_violates_oracle"}
    return {
        "status": "pending_review",
        "reason": "independent_semantic_rubric_required",
        "response": json.loads(redact(json.dumps(body))),
    }


def review_template(case_id: str) -> dict[str, Any]:
    return {
        "scenario": case_id,
        "protocol_sha256": protocol_hash(),
        "status": "pending_review",
        "reviewers": [
            {
                "reviewer": None,
                "criteria": {
                    c: {"verdict": "unknown", "quote": None}
                    for c in protocol()["cases"][case_id]["rubric"]
                },
            }
            for _ in range(2)
        ],
    }
