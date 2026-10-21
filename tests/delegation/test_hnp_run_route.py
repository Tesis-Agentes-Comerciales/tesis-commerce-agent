"""HNP-09b end to end: the service trigger and the assembled executor with its dispatch
(docs/design/hnp-governed.md §3.3, §5.2 and §6).

The runtime is the product's, with real keys, real SQLite and, in enforce, D1-HNP and the real
D2/D3-HNP gate. Only the merchant (the AP2 double of the HP tests plus `/ap2/complete`) and the PSP
(tests/support/hnp_dispatch.py) are doubles, signing their receipts with test keys.
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from commerce_agent.ap2_hnp import AgentKeyCloser
from commerce_agent.api import create_app
from commerce_agent.config import Settings
from commerce_agent.runtime import AgentRuntime, build_runtime
from tests.delegation.test_hnp_runtime import consented, decisions, governed_hnp
from tests.support.ap2_hnp_fixtures import private_key_pem
from tests.support.hnp_dispatch import HNP_ORDER_ID, HnpMerchant, Psp
from tests.test_delegation_api import (
    MERCHANT,
    OPERATOR_TOKEN,
    SERVICE_TOKEN,
    TwoBuyers,
    bearer,
    delegated,
    error,
)


def runtime(settings: Settings, merchant: HnpMerchant, psp: Psp) -> AgentRuntime:
    return build_runtime(
        settings,
        transport=httpx.MockTransport(merchant),
        payment_transport=httpx.MockTransport(psp),
    )


def outcome(path: Path, task_id: str) -> tuple[Any, ...]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT t.state, t.outcome_code, p.stage, "
            "(SELECT COUNT(*) FROM dispatch_outbox), "
            "(SELECT state FROM grant_reservations) "
            "FROM hnp_tasks t JOIN purchases p ON p.run_id = t.run_id WHERE t.task_id = ?",
            (task_id,),
        ).fetchone()


# A D3 deny of the real gate (§3.4, §8.1)


def test_a_d3_deny_of_the_real_gate_commits_nothing_and_alerts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    merchant, psp = HnpMerchant(MERCHANT), Psp()
    settings = governed_hnp(tmp_path)
    built = runtime(settings, merchant, psp)
    try:
        executor = built.hnp_executor
        assert executor is not None
        # A key the grant never named closes: D3's closed proof is invalid and POL-10 fails.
        executor._closer = AgentKeyCloser(private_key_pem(), merchant_issuer=MERCHANT)
        with caplog.at_level(logging.ERROR, logger="commerce_agent.hnp.alerts"):
            step = executor.run(consented(built))
    finally:
        built.close()

    assert step.status == "closed" and step.execution is not None
    task, run_id = step.execution.task, step.execution.task.run_id
    assert outcome(settings.agent_database_path, task.task_id) == (
        "failed",
        "mandate_proof_invalid",
        "failed",
        0,
        "released",
    )
    assert run_id is not None
    assert decisions(settings.agent_database_path, run_id) == [
        ("D1", "HNP", "allow"),
        ("D2", "HNP", "allow"),
        ("D3", "HNP", "deny"),
    ]
    assert (psp.delegations, merchant.completions) == ([], [])
    assert [r.levelno for r in caplog.records if r.name == "commerce_agent.hnp.alerts"] == [
        logging.ERROR
    ]


# The service trigger (§5.2)


@pytest.fixture
def merchant() -> HnpMerchant:
    return HnpMerchant(MERCHANT)


@pytest.fixture
def psp() -> Psp:
    return Psp()


@pytest.fixture
def client(tmp_path: Path, merchant: HnpMerchant, psp: Psp) -> Iterator[TestClient]:
    app = create_app(
        settings=governed_hnp(tmp_path),
        transport=httpx.MockTransport(merchant),
        payment_transport=httpx.MockTransport(psp),
        buyer_authenticator=TwoBuyers(),
    )
    with TestClient(app) as client:
        yield client


def run(client: TestClient, task_id: str, **headers: str) -> Any:
    return client.post(
        f"/internal/hnp/tasks/{task_id}/run", headers=bearer(SERVICE_TOKEN) | headers
    )


def test_the_trigger_answers_running_and_buys_in_the_background(
    client: TestClient, merchant: HnpMerchant
) -> None:
    task_id = delegated(client)["task"]["task_id"]

    response = run(client, task_id)

    assert response.status_code == 202, response.text
    assert response.json()["state"] == "running"
    after = client.get(f"/internal/hnp/tasks/{task_id}", headers=bearer(SERVICE_TOKEN)).json()
    assert after["state"] == "purchased"
    assert HNP_ORDER_ID in after["message"]
    error(run(client, task_id), 409, "hnp_task_not_runnable")
    assert len(merchant.completions) == 1


def test_a_claim_that_ends_the_task_answers_the_terminal(
    client: TestClient, merchant: HnpMerchant
) -> None:
    task_id = delegated(client)["task"]["task_id"]
    client.post("/internal/hnp/kill-switch", json={"enabled": True}, headers=bearer(OPERATOR_TOKEN))

    response = run(client, task_id)

    assert response.status_code == 200, response.text
    assert (response.json()["state"], response.json()["outcome_code"]) == (
        "canceled",
        "kill_switch",
    )
    assert merchant.creations() == 0


def test_the_trigger_takes_only_the_service_token_and_no_browser(client: TestClient) -> None:
    task_id = delegated(client)["task"]["task_id"]
    path = f"/internal/hnp/tasks/{task_id}/run"

    for headers in ({}, bearer(OPERATOR_TOKEN), {"Authorization": "Bearer buyer-a"}):
        error(client.post(path, headers=headers), 401, "hnp_unauthorized")
    origin = bearer(SERVICE_TOKEN) | {"Origin": "http://127.0.0.1:5173"}
    error(client.post(path, headers=origin), 403, "hnp_origin_forbidden")
    error(run(client, "ht_missing"), 404, "hnp_task_not_found")


def test_a_task_with_a_live_lease_is_not_run_twice(
    tmp_path: Path, merchant: HnpMerchant, psp: Psp
) -> None:
    settings = governed_hnp(tmp_path)
    built = runtime(settings, merchant, psp)
    try:
        task_id = consented(built)
        assert built.hnp_executor is not None
        assert built.hnp_executor.claim(task_id).status == "applied"
    finally:
        built.close()
    app = create_app(
        settings=settings,
        transport=httpx.MockTransport(merchant),
        payment_transport=httpx.MockTransport(psp),
        buyer_authenticator=TwoBuyers(),
    )
    with TestClient(app) as client:
        error(run(client, task_id), 409, "hnp_task_not_runnable")
    assert merchant.creations() == 0
