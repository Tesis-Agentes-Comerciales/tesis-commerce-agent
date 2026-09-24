import pytest
from pydantic import ValidationError

from commerce_agent.acp.models import (
    CheckoutCreateRequest,
    CheckoutItemRequest,
    CheckoutUpdateRequest,
)


def test_create_request_contains_no_authority_fields() -> None:
    schema = CheckoutCreateRequest.model_json_schema()
    assert set(schema["properties"]) == {"line_items", "currency", "capabilities"}
    request = CheckoutCreateRequest(line_items=[CheckoutItemRequest(id="SON-01")])
    assert request.model_dump(mode="json") == {
        "line_items": [{"id": "SON-01"}],
        "currency": "cop",
        "capabilities": {"payment": {"handlers": []}},
    }


def test_model_cannot_inject_approval_or_idempotency() -> None:
    with pytest.raises(ValidationError):
        CheckoutCreateRequest.model_validate(
            {
                "line_items": [{"id": "SON-01"}],
                "currency": "cop",
                "approved": True,
                "idempotency_key": "model-key",
            }
        )


def test_update_rejects_empty_or_unsupported_fields() -> None:
    with pytest.raises(ValidationError):
        CheckoutUpdateRequest()
    with pytest.raises(ValidationError):
        CheckoutUpdateRequest.model_validate({"line_items": [{"id": "ALT-01"}]})
