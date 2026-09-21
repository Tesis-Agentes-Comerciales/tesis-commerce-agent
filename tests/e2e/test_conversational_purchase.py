import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from commerce_agent.api import create_app
from commerce_agent.config import Settings

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_JOINT_E2E") != "1",
    reason="requires the pinned merchant service and PostgreSQL",
)


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.fail(f"joint E2E environment is missing {name}")
    return value


def test_natural_request_crosses_the_model_boundary_and_completes() -> None:
    natural_request = (
        "Quiero comprar unos audifonos nuevos, negros, que cuesten maximo 800 dolares "
        "incluyendo el envio. Prefiero la marca Sonora."
    )
    simulated_llm_intent = {
        "hard": {
            "currency": "USD",
            "max_total_amount_minor": 80_000,
            "categories": ["headphones"],
            "conditions": ["new"],
            "required_options": [{"name": "color", "value": "black"}],
        },
        "preferences": {"terms": ["sonora"]},
    }
    settings = Settings(
        merchant_base_url=_required("MERCHANT_BASE_URL"),
        merchant_bearer_token=SecretStr(_required("MERCHANT_BEARER_TOKEN")),
        merchant_webhook_secret=SecretStr(_required("MERCHANT_WEBHOOK_SECRET")),
        agent_approval_secret=SecretStr(_required("AGENT_APPROVAL_SECRET")),
        agent_payment_secret=SecretStr(_required("AGENT_PAYMENT_SECRET")),
        agent_database_path=Path(_required("AGENT_DATABASE_PATH")),
        agent_feed_path=Path(_required("PRODUCT_FEED_PATH")),
        agent_principal_id="human-conversational-e2e",
    )

    with TestClient(
        create_app(
            settings=settings,
            clock=lambda: datetime(2026, 9, 10, 14, 5, tzinfo=UTC),
        )
    ) as client:
        request_body = {
            "request": natural_request,
            "details": {
                "buyer": {
                    "email": "buyer.conversation@example.test",
                    "first_name": "Buyer",
                    "last_name": "Conversation",
                },
                "fulfillment": {
                    "name": "Buyer Conversation",
                    "email": "buyer.conversation@example.test",
                    "phone_number": "+573000000000",
                    "address": {
                        "name": "Buyer Conversation",
                        "line_one": "Calle 100 # 10-20",
                        "city": "Bogota",
                        "state": "DC",
                        "country": "CO",
                        "postal_code": "110111",
                    },
                },
            },
        }
        if os.getenv("RUN_LIVE_LLM") != "1":
            request_body["intent"] = simulated_llm_intent
        prepared = client.post(
            "/purchases",
            json=request_body,
        )
        assert prepared.status_code == 201, prepared.text
        preparation = prepared.json()
        assert preparation["selected_candidate"]["variant_id"] == "SON-01"
        assert preparation["checkout"]["status"] == "ready_for_payment"
        assert preparation["checkout"]["totals"][-1]["amount"] <= 80_000
        run_id = preparation["run_id"]

        blocked = client.post(f"/purchases/{run_id}/complete")
        assert blocked.status_code == 409

        terms = client.get(f"/purchases/{run_id}/approval")
        assert terms.status_code == 200, terms.text
        assert terms.json()["total_minor"] <= 80_000
        approved = client.post(
            f"/purchases/{run_id}/approval",
            json={"expected_fingerprint": terms.json()["fingerprint"]},
        )
        assert approved.status_code == 200
        assert approved.json()["principal_id"] == "human-conversational-e2e"

        completed = client.post(f"/purchases/{run_id}/complete")
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "completed"
        assert completed.json()["order"]["checkout_session_id"] == preparation["checkout"]["id"]
