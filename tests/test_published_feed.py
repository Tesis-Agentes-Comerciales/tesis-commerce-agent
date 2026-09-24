import json
from pathlib import Path

import httpx
import pytest

from commerce_agent.acp.feed import ProductFeedError, load_product_feed_payload
from commerce_agent.acp.published_feed import PublishedFeedClient

FIXTURE = Path(__file__).parent / "fixtures" / "feed"


def _payload() -> dict[str, object]:
    return {
        "metadata": json.loads((FIXTURE / "metadata.json").read_text(encoding="utf-8")),
        "products": [
            json.loads(line)
            for line in (FIXTURE / "products.jsonl").read_text(encoding="utf-8").splitlines()
        ],
    }


def test_published_feed_reloads_merchant_changes() -> None:
    payload = _payload()
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/feeds/current"
        return httpx.Response(200, json=payload)

    client = PublishedFeedClient(
        "https://merchant.example.test", transport=httpx.MockTransport(respond)
    )
    first = client.get()
    products = payload["products"]
    assert isinstance(products, list)
    first_product = products[0]
    assert isinstance(first_product, dict)
    first_variant = first_product["variants"][0]
    first_variant["price"]["amount"] = 123_456
    first_variant["availability"] = {"available": False, "status": "out_of_stock"}
    second = client.get()
    client.close()

    variant_id = first_variant["id"]
    original = first.candidate(variant_id)
    refreshed = second.candidate(variant_id)
    assert original is not None and refreshed is not None
    assert original.observed_unit_amount_minor != 123_456
    assert refreshed.observed_unit_amount_minor == 123_456
    assert refreshed.available is False
    assert len(requests) == 2


def test_published_feed_rejects_duplicate_variants() -> None:
    payload = _payload()
    products = payload["products"]
    assert isinstance(products, list)
    products[1]["variants"][0]["id"] = products[0]["variants"][0]["id"]
    with pytest.raises(ProductFeedError, match="Duplicate variant"):
        load_product_feed_payload(payload)


def test_published_feed_fails_closed_when_merchant_is_unavailable() -> None:
    client = PublishedFeedClient(
        "https://merchant.example.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    )
    with pytest.raises(ProductFeedError) as captured:
        client.get()
    client.close()
    assert captured.value.code == "feed_unavailable"
