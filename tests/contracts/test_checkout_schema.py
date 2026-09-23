import pytest

from commerce_agent.acp.schema import ACPContractError, validate_acp


def test_schema_validator_rejects_internal_or_unknown_checkout_fields() -> None:
    payload = {
        "id": "cs_contract_001",
        "protocol": {"version": "2026-04-17"},
        "status": "not_ready_for_payment",
        "currency": "cop",
        "capabilities": {"payment": {"handlers": []}},
        "line_items": [],
        "totals": [{"type": "total", "display_text": "Total", "amount": 0}],
        "fulfillment_options": [],
        "messages": [],
        "links": [],
        "internal_revision": 7,
    }

    with pytest.raises(ACPContractError) as captured:
        validate_acp("CheckoutSession", payload)
    assert captured.value.definition == "CheckoutSession"
    assert "internal_revision" not in str(captured.value)


def test_unknown_schema_definition_fails_closed() -> None:
    with pytest.raises(ValueError, match="Unknown ACP definition"):
        validate_acp("NotAProtocolType", {})
