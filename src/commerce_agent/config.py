from datetime import datetime
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
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


def validate_browser_origin(value: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("UI origin must be an absolute HTTP(S) origin")
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("UI origin cannot contain credentials, path, query, or fragment")
    try:
        loopback = ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if parsed.scheme != "https" and not loopback:
        raise ValueError("UI origin must use HTTPS outside loopback development")
    return normalized


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    merchant_base_url: str = "http://127.0.0.1:4120"
    merchant_bearer_token: SecretStr | None = Field(default=None, repr=False)
    merchant_webhook_secret: SecretStr | None = Field(default=None, repr=False)
    merchant_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    agent_approval_secret: SecretStr | None = Field(default=None, repr=False)
    agent_ap2_required: bool = False
    agent_ap2_private_key_pem: SecretStr | None = Field(default=None, repr=False)
    payment_provider_url: str = "http://127.0.0.1:4130"
    payment_agent_bearer_token: SecretStr | None = Field(default=None, repr=False)
    agent_database_path: Path = Path(".local/agent.db")
    agent_feed_source: Literal["merchant", "file"] = "merchant"
    agent_feed_path: Path | None = None
    agent_principal_id: str = Field(default="local-human", min_length=1, max_length=128)
    agent_auth_mode: Literal["local", "jwt"] = "local"
    agent_auth_issuer: str | None = None
    agent_ui_origin: str = "http://127.0.0.1:5173"
    agent_sandbox_clock_at: datetime | None = None
    agent_governance_mode: Literal["off", "shadow"] = "off"
    # Decode/validate shadow-only configuration in the isolated bootstrap boundary,
    # so missing or malformed B2 settings never prevent ordinary B0 construction.
    agent_d1_policy_set_id: str | None = None
    agent_d1_policy_revision: str | None = None
    agent_d1_policy_path: Path | None = None
    agent_d1_policy_digest: str | None = None
    agent_d1_key_id: str | None = None
    agent_d1_hmac_key_hex: SecretStr | None = Field(default=None, repr=False)
    agent_d1_aes_key_hex: SecretStr | None = Field(default=None, repr=False)
    agent_d1_commercial_clock_id: str | None = None
    openai_api_key: SecretStr | None = Field(default=None, repr=False)
    openai_model: str | None = Field(default=None, min_length=1, max_length=128)
    openai_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    openai_reasoning_summary: bool = False
    openai_reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None

    @field_validator("merchant_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return validate_service_base_url(value)

    @field_validator("payment_provider_url")
    @classmethod
    def validate_payment_provider_url(cls, value: str) -> str:
        return validate_service_base_url(value)

    @field_validator("agent_ui_origin")
    @classmethod
    def validate_ui_origin(cls, value: str) -> str:
        return validate_browser_origin(value)

    @field_validator("agent_sandbox_clock_at")
    @classmethod
    def validate_sandbox_clock(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Sandbox clock must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_authentication(self) -> "Settings":
        if self.agent_feed_source == "file" and self.agent_feed_path is None:
            raise ValueError("AGENT_FEED_PATH is required when AGENT_FEED_SOURCE=file")
        if self.agent_ap2_required and self.agent_auth_mode != "jwt":
            raise ValueError("AP2 Human Present requires authenticated buyers")
        if self.agent_ap2_required and self.agent_ap2_private_key_pem is None:
            raise ValueError("AGENT_AP2_PRIVATE_KEY_PEM is required for AP2")
        if self.agent_auth_mode == "jwt":
            if self.agent_auth_issuer is None:
                raise ValueError("AGENT_AUTH_ISSUER is required in jwt mode")
            self.agent_auth_issuer = validate_browser_origin(self.agent_auth_issuer)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
