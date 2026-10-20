"""EC-HNP-02 settings: every experiment parameter comes from ``HNP_*`` variables.

Documented values live in ``.env.example``. No model, temperature, seed, timeout, token limit,
repetition count, price or path is defaulted in code; the only code defaults are the safe
ones (live calls off, no API key).
"""

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .cost import TokenPrices

ReasoningEffort = Literal["minimal", "low", "medium", "high"]


class HnpSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HNP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
        env_parse_none_str="none",
        frozen=True,
    )

    validator_model: str = Field(min_length=1, max_length=128)
    # Phase 1 keeps scripted buyer payloads; the buyer model is recorded for Phase 2.
    buyer_model: str = Field(min_length=1, max_length=128)
    # "none" omits the parameter: reasoning models only accept their default temperature.
    temperature: float | None = Field(ge=0, le=2)
    reasoning_effort: ReasoningEffort | None
    # The Responses API has no seed: it fixes episode order and the fake transport.
    seed: int = Field(ge=0)
    timeout_seconds: float = Field(gt=0, le=120)
    max_output_tokens: int = Field(ge=16, le=32_768)
    repetitions: int = Field(ge=1, le=100)
    validator_input_usd_per_mtok: Decimal = Field(ge=0)
    validator_output_usd_per_mtok: Decimal = Field(ge=0)
    buyer_input_usd_per_mtok: Decimal = Field(ge=0)
    buyer_output_usd_per_mtok: Decimal = Field(ge=0)
    # Live runs refuse to start when the upper-bound estimate exceeds this budget.
    max_cost_usd: Decimal = Field(ge=0)
    validator_prompt: str = Field(pattern=r"^[a-z]+-v[0-9]+$")
    buyer_prompt: str = Field(pattern=r"^[a-z]+-v[0-9]+$")
    cases_path: Path
    oracle_path: Path
    output_dir: Path
    live: bool = False
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias="OPENAI_API_KEY", repr=False
    )

    @classmethod
    def load(cls, env_file: Path | None) -> "HnpSettings":
        """Read HNP_* from the process environment and ``env_file`` (None: environment only).

        Every experiment field is required, so a missing variable fails here, before any run.
        """
        # Type checkers model only the field arguments, not pydantic-settings' env loading.
        return cls(_env_file=env_file)  # pyright: ignore[reportCallIssue]

    @property
    def validator_prices(self) -> TokenPrices:
        return TokenPrices(self.validator_input_usd_per_mtok, self.validator_output_usd_per_mtok)

    @property
    def buyer_prices(self) -> TokenPrices:
        return TokenPrices(self.buyer_input_usd_per_mtok, self.buyer_output_usd_per_mtok)

    def live_blockers(self) -> tuple[str, ...]:
        """Why a provider call must not happen; empty only when every opt-in gate is met."""
        blockers: list[str] = []
        if not self.live:
            blockers.append("HNP_LIVE=1 is required for provider calls")
        if self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip():
            blockers.append("OPENAI_API_KEY is not set")
        if not (self.validator_input_usd_per_mtok and self.validator_output_usd_per_mtok):
            blockers.append("validator token prices must be set from the provider's price list")
        if not self.max_cost_usd:
            blockers.append("HNP_MAX_COST_USD is 0: no approved budget")
        return tuple(blockers)

    def public(self) -> dict[str, object]:
        """Settings safe to record in summaries: never the API key."""
        return {
            key: (str(value) if isinstance(value, (Decimal, Path)) else value)
            for key, value in self.model_dump(exclude={"openai_api_key"}).items()
        }
