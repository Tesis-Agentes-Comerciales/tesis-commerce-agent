from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

NormalizedText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
CurrencyCode = Annotated[
    str,
    StringConstraints(strict=True, min_length=3, max_length=3, pattern=r"^[A-Z]{3}$"),
]


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


class IntentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OptionConstraint(IntentModel):
    name: NormalizedText
    value: NormalizedText

    @field_validator("name", "value", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return _normalize_text(value) if isinstance(value, str) else value


class HardConstraints(IntentModel):
    currency: CurrencyCode = "USD"
    max_unit_amount_minor: int | None = Field(default=None, ge=0)
    max_total_amount_minor: int | None = Field(default=None, ge=0)
    categories: tuple[NormalizedText, ...] = ()
    conditions: tuple[NormalizedText, ...] = ()
    required_options: tuple[OptionConstraint, ...] = ()
    required_terms: tuple[NormalizedText, ...] = ()

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("categories", "conditions", "required_terms", mode="before")
    @classmethod
    def normalize_text_tuple(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(_normalize_text(item) if isinstance(item, str) else item for item in value)
        return value

    @field_validator("required_options", mode="before")
    @classmethod
    def normalize_options_tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, (list, tuple)) else value


class SoftPreferences(IntentModel):
    terms: tuple[NormalizedText, ...] = ()
    categories: tuple[NormalizedText, ...] = ()
    conditions: tuple[NormalizedText, ...] = ()
    options: tuple[OptionConstraint, ...] = ()

    @field_validator("terms", "categories", "conditions", mode="before")
    @classmethod
    def normalize_text_tuple(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(_normalize_text(item) if isinstance(item, str) else item for item in value)
        return value

    @field_validator("options", mode="before")
    @classmethod
    def normalize_options_tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, (list, tuple)) else value


class PurchaseIntent(IntentModel):
    hard: HardConstraints = Field(default_factory=HardConstraints)
    preferences: SoftPreferences = Field(default_factory=SoftPreferences)
