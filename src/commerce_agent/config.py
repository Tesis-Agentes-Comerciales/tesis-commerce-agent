from functools import lru_cache
from ipaddress import ip_address
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def validate_service_base_url(value: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Merchant base URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Merchant base URL cannot contain credentials, query, or fragment")
    try:
        loopback = ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if parsed.scheme != "https" and not loopback:
        raise ValueError("Merchant base URL must use HTTPS outside loopback development")
    return normalized


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    merchant_base_url: str = "http://127.0.0.1:4120"
    merchant_bearer_token: SecretStr | None = Field(default=None, repr=False)
    merchant_webhook_secret: SecretStr | None = Field(default=None, repr=False)
    merchant_timeout_seconds: float = Field(default=5.0, gt=0, le=30)

    @field_validator("merchant_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return validate_service_base_url(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()
