import pytest
from pydantic import ValidationError

from commerce_agent.config import Settings, validate_browser_origin, validate_service_base_url


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


def test_ui_origin_is_restricted_to_an_origin() -> None:
    assert validate_browser_origin("http://127.0.0.1:5173/") == "http://127.0.0.1:5173"
    assert validate_browser_origin("https://agent.example") == "https://agent.example"

    with pytest.raises(ValueError):
        validate_browser_origin("https://agent.example/chat")

    with pytest.raises(ValueError):
        validate_browser_origin("http://agent.example")


def test_sandbox_clock_requires_an_explicit_timezone() -> None:
    configured = Settings(agent_sandbox_clock_at="2026-09-10T14:05:00Z")  # type: ignore[arg-type]
    assert configured.agent_sandbox_clock_at is not None
    assert configured.agent_sandbox_clock_at.utcoffset() is not None

    with pytest.raises(ValidationError):
        Settings(agent_sandbox_clock_at="2026-09-10T14:05:00")  # type: ignore[arg-type]


def test_local_feed_requires_an_explicit_path() -> None:
    with pytest.raises(ValidationError, match="AGENT_FEED_PATH"):
        Settings(agent_feed_source="file", agent_feed_path=None)
