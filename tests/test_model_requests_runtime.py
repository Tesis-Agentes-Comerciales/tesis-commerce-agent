"""`model_requests` as the runtime assembles it (run-traceability.md §4.4, T2).

A Human Present turn records its requests under the run the purchase of the turn has, also when
the turn buys nothing; a delegated (HNP) choice records its request under the task's run. Real
SQLite, the ACP merchant doubles and the real SDK over a scripted `httpx.MockTransport`.
"""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from commerce_agent.api import create_app
from commerce_agent.application.conversation import turn_run_id
from commerce_agent.application.hnp_selection import MODEL_STAGE, ModelHnpSelector
from commerce_agent.llm import CommerceDecision
from commerce_agent.llm.delegated_choice import ClaudeDelegatedChooser
from commerce_agent.persistence.model_requests_sqlite import SqliteModelRequests
from commerce_agent.persistence.stage_timings_sqlite import SqliteStageTimings
from tests.assistant_support import (
    GREETING_REPLY,
    PURCHASE_REPLY,
    PURCHASE_REQUEST,
    Merchant,
    checkout_details,
    counter_ids,
    purchase_intent,
    settings,
)
from tests.delegation.test_hnp_runtime import consented, runtime
from tests.support.hnp_dispatch import HnpMerchant, Psp
from tests.support.hnp_executor import Bench
from tests.support.model_server import ModelServer, routed, shortlisted_choice, structured
from tests.test_delegation_api import MERCHANT, hnp_settings

API_KEY = "sk-test-runtime-0123456789abcdef"


def requests(path: Path) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT run_id, stage, request_index, outcome, retries, input_tokens, "
            "json_extract(output_json, '$.structured.assistant_message') "
            "FROM model_requests ORDER BY sequence"
        ).fetchall()


def chosen(body: dict[str, Any]) -> dict[str, Any]:
    """The variant the merchant double sells, which the shortlist must offer."""
    choice = shortlisted_choice(body)
    enum = body["output_config"]["format"]["schema"]["properties"]["selected_variant_id"]["enum"]
    assert "SON-XM6-BLK" in enum
    return {**choice, "assistant_message": PURCHASE_REPLY, "selected_variant_id": "SON-XM6-BLK"}


def turn(client: TestClient, turn_id: str, text: str) -> dict[str, Any]:
    response = client.post(
        "/conversation/turn",
        json={
            "turn_id": turn_id,
            "messages": [{"role": "user", "content": text}],
            "details": checkout_details(),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_human_present_turn_records_its_requests_under_the_turn_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "agent.db"
    buys = CommerceDecision(
        action="purchase",
        assistant_message="Buscaré audífonos negros dentro de tu presupuesto.",
        purchase_request=PURCHASE_REQUEST,
        intent=purchase_intent(2_000_000_00, black=True),
    )
    greeting = CommerceDecision(action="respond", assistant_message=GREETING_REPLY)
    server = ModelServer(*routed(buys), structured(chosen), *routed(greeting))
    server.install(monkeypatch)
    app = create_app(
        settings=settings(database).model_copy(
            update={"anthropic_api_key": SecretStr(API_KEY), "agent_model": "test-model"}
        ),
        transport=Merchant().transport(),
        payment_transport=httpx.MockTransport(
            lambda request: httpx.Response(201, json={"id": "vt_" + "a" * 64})
        ),
        id_factory=counter_ids(),
    )

    with TestClient(app) as client:
        purchase = turn(client, "turn-buys", PURCHASE_REQUEST)
        run_id = purchase["purchase"]["run_id"]
        assert client.post(f"/purchases/{run_id}/cancel", json={}).status_code == 200
        assert turn(client, "turn-greets", "hola")["action"] == "respond"

    assert run_id == turn_run_id("turn-buys")
    greets = turn_run_id("turn-greets")
    # The router's first request of each turn calls no tool and asks for no schema.
    assert requests(database) == [
        (run_id, "model.route", 1, "ok", 0, 1825, None),
        (run_id, "model.route", 2, "ok", 0, 1825, buys.assistant_message),
        (run_id, "model.choose_candidate", 1, "ok", 0, 1825, PURCHASE_REPLY),
        # A turn without a purchase still has the run id its purchase would have had.
        (greets, "model.route", 1, "ok", 0, 1825, None),
        (greets, "model.route", 2, "ok", 0, 1825, GREETING_REPLY),
    ]


def test_the_delegated_choice_is_recorded_under_the_task_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "agent.db"
    SqliteModelRequests(path).initialize()
    timings = SqliteStageTimings(path)
    timings.initialize_model_tokens()
    server = ModelServer(structured(shortlisted_choice))
    server.install(monkeypatch)
    chooser = ClaudeDelegatedChooser(
        api_key=SecretStr(API_KEY), model="test-model", model_requests=SqliteModelRequests(path)
    )
    bench = Bench(tmp_path, selector=ModelHnpSelector(chooser, timings))

    assert bench.run().status == "applied"

    (row,) = requests(path)
    assert row[:6] == (bench.run_id(), MODEL_STAGE, 1, "ok", 0, 1825)
    assert row[6] == "Elijo SON-01 porque es el más barato."
    # The stage timing keeps its two counters, as before.
    with closing(sqlite3.connect(path)) as connection:
        timed = connection.execute(
            "SELECT stage, outcome, input_tokens, output_tokens FROM stage_timings"
        ).fetchall()
    assert timed == [(MODEL_STAGE, "ok", 1825, 786)]


def test_the_runtime_gives_the_llm_selector_the_agent_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = ModelServer(structured(shortlisted_choice))
    server.install(monkeypatch)
    configured = hnp_settings(
        tmp_path,
        agent_hnp_selector="llm",
        anthropic_api_key=SecretStr(API_KEY),
        agent_model="test-model",
    )
    built = runtime(configured, HnpMerchant(MERCHANT), Psp())
    try:
        assert built.hnp_executor is not None
        step = built.hnp_executor.run(consented(built))
    finally:
        built.close()

    assert step.execution is not None
    run_id = step.execution.task.run_id
    assert run_id is not None
    (row,) = requests(tmp_path / "agent.db")
    assert row[:4] == (run_id, MODEL_STAGE, 1, "ok")
