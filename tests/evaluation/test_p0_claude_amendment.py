"""What tests/integration/test_p0_model_failures.py changes from its frozen protocol now that the
agent runs on Claude (`ON_CLAUDE`), checked without the stack: only the provider's fields of the
OpenAI rows change, and each amended stimulus is what its amended oracle expects."""

import copy
import json
from typing import Any

import httpx2
import pytest
from anthropic._error_classes import make_status_error

from commerce_agent.llm.model_failures import SPEND_CAP, classify_model_failure
from tests.integration.test_p0_model_failures import (
    ANSWERS,
    ON_CLAUDE_STIMULI,
    PROTOCOL,
    amendment,
    on_claude,
)

ROWS = ("model_requests", "model_requests_per_surface")


def frozen() -> dict[str, dict[str, Any]]:
    return json.loads(PROTOCOL.read_text(encoding="utf-8"))["cases"]


def rows(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for key in ROWS for row in case["oracle"].get(key, ())]


def outside_rows(case: dict[str, Any]) -> dict[str, Any]:
    oracle = {key: value for key, value in case["oracle"].items() if key not in ROWS}
    return {**case, "oracle": oracle}


def test_only_the_provider_fields_of_the_openai_rows_change() -> None:
    before = frozen()
    after = on_claude(copy.deepcopy(before))

    changed: dict[str, list[dict[str, Any]]] = {}
    for cell, case in before.items():
        # Everything but the oracle's model_requests rows stays as frozen.
        assert outside_rows(after[cell]) == outside_rows(case), cell
        for old, new in zip(rows(case), rows(after[cell]), strict=True):
            if old != new:
                changed.setdefault(cell, []).append(
                    {key: [old[key], new[key]] for key in old if old[key] != new[key]}
                )

    rate_limit = {"provider_error_code": ["rate_limit_exceeded", "rate_limit_error"]}
    spend_cap = {
        "error_kind": ["RateLimitError", "EnforcedSpendLimitReachedError"],
        "provider_error_code": ["insufficient_quota", SPEND_CAP],
    }
    assert changed == {
        "INF27-HP": [rate_limit],
        "INF28-HP": [spend_cap],
        "INF27-HNP": [rate_limit],
        "INF28-HNP": [spend_cap],
    }


@pytest.mark.parametrize(
    ("stimulus", "cell"),
    [("http_429_rate_limit", "INF27-HNP"), ("http_429_insufficient_quota", "INF28-HNP")],
)
def test_each_amended_stimulus_is_what_its_amended_oracle_expects(stimulus: str, cell: str) -> None:
    response = ANSWERS[stimulus]({})
    response.request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = make_status_error("scripted", body=response.json(), response=response)
    failure = classify_model_failure(error)

    [row] = on_claude(frozen())[cell]["oracle"]["model_requests"]
    assert [type(error).__name__, failure.http_status, failure.provider_error_code] == [
        row["error_kind"],
        row["http_status"],
        row["provider_error_code"],
    ]


def test_the_summary_records_the_bodies_sent_and_the_oracle_fields_replaced() -> None:
    recorded = amendment()

    assert set(recorded["stimuli"]) == set(ON_CLAUDE_STIMULI)
    spend_cap = recorded["stimuli"]["http_429_insufficient_quota"]
    assert spend_cap["status"] == 429
    assert spend_cap["headers"] == {"retry-after-ms": "1", "x-should-retry": None}
    assert spend_cap["body"]["error"] == {
        "type": "rate_limit_error",
        "message": "scripted failure",
        "details": {"error_code": SPEND_CAP},
    }
    assert recorded["stimuli"]["http_429_rate_limit"]["body"]["error"]["type"] == (
        "rate_limit_error"
    )
    assert recorded["oracles"]["insufficient_quota"]["provider_error_code"] == SPEND_CAP
