"""The agent's mirror of the merchant's delivery address (hnp-governed.md §5.1).

`tests/fixtures/merchant_destination/cases.json` records, case by case, what the merchant at the
commit it names accepts and echoes as `fulfillment_details`. The delegation scope binds the
agent's normalization, so a drift from the merchant would make POL-08 escalate every purchase.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from commerce_agent.acp.destination import merchant_fulfillment
from commerce_agent.acp.models import FulfillmentDetails

TABLE: dict[str, Any] = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "merchant_destination" / "cases.json").read_text(
        encoding="utf-8"
    )
)


def _with_address(fulfillment: dict[str, Any], changes: dict[str, str]) -> dict[str, Any]:
    return fulfillment | {"address": fulfillment["address"] | changes}


@pytest.mark.parametrize("case", TABLE["cases"], ids=[case["name"] for case in TABLE["cases"]])
def test_the_agent_normalizes_the_destination_as_the_merchant_echoes_it(
    case: dict[str, Any],
) -> None:
    sent = _with_address(TABLE["fulfillment"], case["address"])

    echoed = merchant_fulfillment(FulfillmentDetails.model_validate(sent))

    if case["echo"] is None:
        assert echoed is None
    else:
        assert echoed is not None
        assert echoed.model_dump(mode="json") == _with_address(sent, case["echo"])


def test_the_table_covers_every_normalized_and_rejected_field() -> None:
    changed = {field for case in TABLE["cases"] for field in case["address"]}
    assert {"city", "state", "country", "postal_code"} <= changed
    assert any(case["echo"] for case in TABLE["cases"])  # a normalization, not only rejections
