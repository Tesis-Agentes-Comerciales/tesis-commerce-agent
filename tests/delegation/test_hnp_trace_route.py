"""`GET /purchases/{run_id}/trace` of a delegated purchase, as the UI reads it
(docs/design/hnp-governed.md §10, docs/design/trace-panel.md «Compra delegada»).

The buyer consents through the API with their token, the service token triggers the task and the
assembled B2-HNP executor buys through the AP2 merchant and PSP doubles. Then the owner reads the
trace of `task.run_id` over HTTP. Real keys and real SQLite in `tmp_path`; no model, no network.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from commerce_agent.api import create_app
from commerce_agent.trace.models import (
    AuthorizationDecisionStep,
    DispatchDecisionStep,
    OrderConfirmedStep,
    PolicyDecisionStep,
    PurchaseTrace,
    ReservationStep,
)
from tests.delegation.test_hnp_runtime import governed_hnp
from tests.support.hnp_dispatch import HNP_ORDER_ID, HnpMerchant, Psp
from tests.test_delegation_api import MERCHANT, SERVICE_TOKEN, TwoBuyers, bearer, buyer, delegated

# What the owner sees of a B2-HNP purchase, in order. `payment_authorized` is left out on purpose:
# whether the HNP trace emits it is a separate change (trace-panel.md, «Pendiente de 09b»).
DELEGATED_STEPS = [
    "delegation_consented",
    "task_activated",
    "policy_decision",
    "checkout_created",
    "freshness_check",
    "authorization_decision",
    "reservation",
    "mandates_closed",
    "dispatch_decision",
]
# The completion writes these together (UoW-3): only their set is fixed.
COMPLETION_STEPS = {"order_confirmed", "reservation"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        settings=governed_hnp(tmp_path),
        transport=httpx.MockTransport(HnpMerchant(MERCHANT)),
        payment_transport=httpx.MockTransport(Psp()),
        buyer_authenticator=TwoBuyers(),
    )
    with TestClient(app) as client:
        yield client


def bought(client: TestClient) -> dict[str, Any]:
    """A delegation of buyer-a, triggered by the service and bought: its task view."""
    grant = delegated(client)
    path = f"/internal/hnp/tasks/{grant['task']['task_id']}/run"
    assert client.post(path, headers=bearer(SERVICE_TOKEN)).status_code == 202
    view = client.get(f"/delegations/{grant['grant_id']}", headers=buyer())
    task = view.json()["task"]
    assert (task["state"], task["order_id"]) == ("purchased", HNP_ORDER_ID), task
    return task


def test_the_owner_reads_the_governance_of_the_delegated_purchase(client: TestClient) -> None:
    run_id = bought(client)["run_id"]

    response = client.get(f"/purchases/{run_id}/trace", headers=buyer())

    assert response.status_code == 200, response.text
    trace = PurchaseTrace.model_validate_json(response.text)
    assert (trace.run_id, trace.purchase_mode, trace.status) == (run_id, "HNP", "completed")
    assert (trace.policy_mode, trace.request) == ("enforce", "Compra delegada")
    kinds = [step.kind for step in trace.steps if step.kind != "payment_authorized"]
    assert kinds[: len(DELEGATED_STEPS)] == DELEGATED_STEPS
    assert sorted(kinds[len(DELEGATED_STEPS) :]) == sorted(COMPLETION_STEPS)
    # D1, D2-HNP and D3-HNP of b2-hnp-v1 allowed the purchase; the single use ends consumed.
    decisions = [
        (step.kind, step.decision.policy.set_id, step.decision.outcome, step.decision.effect)
        for step in trace.steps
        if isinstance(step, PolicyDecisionStep | AuthorizationDecisionStep | DispatchDecisionStep)
    ]
    assert decisions == [
        ("policy_decision", "b2-hnp-v1", "allow", "continued"),
        ("authorization_decision", "b2-hnp-v1", "allow", "continued"),
        ("dispatch_decision", "b2-hnp-v1", "allow", "continued"),
    ]
    outcomes = [step.outcome for step in trace.steps if isinstance(step, ReservationStep)]
    assert outcomes == ["reserved", "consumed"]
    [order] = [step for step in trace.steps if isinstance(step, OrderConfirmedStep)]
    assert order.order_id == HNP_ORDER_ID


def test_another_buyer_gets_the_same_refusal_as_for_an_hp_purchase(client: TestClient) -> None:
    run_id = bought(client)["run_id"]

    foreign = client.get(f"/purchases/{run_id}/trace", headers=buyer("buyer-b"))
    unknown = client.get("/purchases/run_missing/trace", headers=buyer())
    anonymous = client.get(f"/purchases/{run_id}/trace")

    # As tests/test_purchase_trace_api.py: a run of someone else reads as one that does not exist.
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()
    assert foreign.json()["code"] == "purchase_not_found"
    assert anonymous.status_code == 401
    history = client.get("/me/purchases", headers=buyer("buyer-b")).json()
    assert history["purchases"] == []
