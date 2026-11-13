"""What tests/integration/test_p0_model_failures.py changes from its frozen protocol now that the
agent runs on Claude (`ON_CLAUDE`), checked without the stack: only the provider's fields of the
OpenAI rows change, and each amended stimulus is what its amended oracle expects. Also what the
router's two requests per turn change (`on_router_first`): only the HP cells whose interpretation
answers, never INF27-HP and INF28-HP, whose first request fails."""

import copy
import json
from typing import Any

import httpx2
import pytest
from anthropic._error_classes import make_status_error

from commerce_agent.llm.model_failures import SPEND_CAP, classify_model_failure
from tests.integration.live_model import ROUTER_FIRST
from tests.integration.test_p0_model_failures import (
    ANSWERS,
    CASES,
    ON_CLAUDE_STIMULI,
    PROTOCOL,
    ROUTER_DECISION_ROW,
    amendment,
    on_claude,
    on_router_first,
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


def ok_row(stage: str, retries: int = 0) -> dict[str, Any]:
    return {**ROUTER_DECISION_ROW, "stage": stage, "retries": retries}


def test_the_router_first_answer_amends_only_the_hp_cells_that_route() -> None:
    before = on_claude(frozen())
    after = on_router_first(copy.deepcopy(before))

    amended = {cell for cell in before if after[cell] != before[cell]}
    assert amended == {"INF29-HP", "INF27-HP-control", "CTRL-HP"}
    view = {
        cell: (
            after[cell]["script"],
            after[cell]["oracle"]["model_http_attempts_per_surface"],
            after[cell]["oracle"]["model_requests_per_surface"],
        )
        for cell in amended
    }
    lost = ok_row("model.choose_candidate", 1) | {
        "outcome": "error",
        "error_kind": "APIConnectionError",
    }
    assert view == {
        "INF29-HP": (
            [ROUTER_FIRST, "route_ok", "disconnect"],
            4,
            [ok_row("model.route"), ok_row("model.route"), lost],
        ),
        # The 429 hits the first request; its SDK retry gets the first answer.
        "INF27-HP-control": (
            ["http_429_rate_limit", ROUTER_FIRST, "route_ok", "choice_ok"],
            4,
            [ok_row("model.route", 1), ok_row("model.route"), ok_row("model.choose_candidate")],
        ),
        "CTRL-HP": (
            [ROUTER_FIRST, "route_ok", "choice_ok"],
            3,
            [ok_row("model.route"), ok_row("model.route"), ok_row("model.choose_candidate")],
        ),
    }
    # Everything else of an amended cell stays as frozen.
    for cell in amended:
        for key in ("model_http_attempts_per_surface", "model_requests_per_surface"):
            after[cell]["oracle"][key] = before[cell]["oracle"][key]
        after[cell]["script"] = before[cell]["script"]
        assert after[cell] == before[cell]


@pytest.mark.parametrize(("cell", "attempts"), [("INF27-HP", 2), ("INF28-HP", 1)])
def test_a_failed_interpretation_is_still_one_request(cell: str, attempts: int) -> None:
    """The first request fails, so the router never sends its second: one row, and the SDK's
    retry of a rate limit or none for a spent cap."""
    case = CASES[cell]

    assert ROUTER_FIRST not in case["script"]
    assert case["oracle"]["model_http_attempts_per_surface"] == attempts
    [row] = case["oracle"]["model_requests_per_surface"]
    assert (row["stage"], row["outcome"], row["retries"]) == ("model.route", "error", attempts - 1)


def test_the_summary_records_the_router_first_answer_and_its_cells() -> None:
    recorded = amendment()["router_first"]

    assert recorded["stimulus"]["name"] == ROUTER_FIRST
    assert recorded["stimulus"]["status"] == 200
    [block] = recorded["stimulus"]["body"]["content"]
    assert block["type"] == "text"
    assert set(recorded["cells"]) == {"INF29-HP", "INF27-HP-control", "CTRL-HP"}
    assert recorded["cells"]["CTRL-HP"]["model_http_attempts_per_surface"] == 3
