import pytest

from commerce_agent.config import validate_service_base_url


def test_allows_https_and_loopback_http() -> None:
    assert validate_service_base_url("https://merchant.example") == "https://merchant.example"
    assert validate_service_base_url("http://127.0.0.1:4120/") == "http://127.0.0.1:4120"


@pytest.mark.parametrize(
    "value",
    [
        "http://merchant.example",
        "https://user:password@merchant.example",
        "https://merchant.example?token=secret",
        "file:///tmp/merchant",
    ],
)
def test_rejects_unsafe_merchant_urls(value: str) -> None:
    with pytest.raises(ValueError):
        validate_service_base_url(value)
