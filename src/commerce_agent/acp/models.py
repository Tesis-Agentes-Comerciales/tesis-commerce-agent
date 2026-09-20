from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

ACP_VERSION = "2026-04-17"
Identifier = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    ),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DiscoveryProtocol(StrictModel):
    name: Literal["acp"]
    version: str
    supported_versions: list[str]


class DiscoveryCapabilities(StrictModel):
    services: list[str]
    supported_currencies: list[str]
    supported_locales: list[str]


class DiscoveryResponse(StrictModel):
    protocol: DiscoveryProtocol
    api_base_url: str
    transports: list[str]
    capabilities: DiscoveryCapabilities

    @model_validator(mode="after")
    def require_p0_checkout(self) -> "DiscoveryResponse":
        if ACP_VERSION not in self.protocol.supported_versions:
            raise ValueError(f"Merchant does not support ACP {ACP_VERSION}")
        if self.protocol.version != ACP_VERSION:
            raise ValueError(f"Merchant selected unsupported ACP version {self.protocol.version}")
        if "rest" not in self.transports or "checkout" not in self.capabilities.services:
            raise ValueError("Merchant does not advertise the required REST checkout service")
        return self


class CheckoutItemRequest(StrictModel):
    id: Identifier


class PaymentCapabilities(StrictModel):
    handlers: list[dict[str, Any]] = Field(default_factory=list)


class CheckoutCapabilities(StrictModel):
    payment: PaymentCapabilities = Field(default_factory=PaymentCapabilities)


class CheckoutCreateRequest(StrictModel):
    line_items: Annotated[list[CheckoutItemRequest], Field(min_length=1)]
    currency: Literal["usd"] = "usd"
    capabilities: CheckoutCapabilities = Field(default_factory=CheckoutCapabilities)


class Buyer(StrictModel):
    first_name: str
    last_name: str
    email: str
    phone_number: str


class Address(StrictModel):
    name: str
    line_one: str
    city: str
    state: str
    country: str
    postal_code: str


class FulfillmentDetails(StrictModel):
    name: str
    email: str
    phone_number: str
    address: Address


class SelectedFulfillmentOption(StrictModel):
    type: Literal["shipping"]
    option_id: Identifier
    item_ids: Annotated[list[Identifier], Field(min_length=1)]


class CheckoutUpdateRequest(StrictModel):
    buyer: Buyer | None = None
    fulfillment_details: FulfillmentDetails | None = None
    selected_fulfillment_options: list[SelectedFulfillmentOption] | None = None

    @model_validator(mode="after")
    def require_one_update(self) -> "CheckoutUpdateRequest":
        if not any(
            value is not None
            for value in (self.buyer, self.fulfillment_details, self.selected_fulfillment_options)
        ):
            raise ValueError("At least one supported checkout field must be updated")
        return self


class CheckoutProtocol(StrictModel):
    version: str


class CheckoutSession(BaseModel):
    """Validated ACP fields used by the agent while retaining protocol extensions."""

    model_config = ConfigDict(extra="allow", strict=True)

    id: Identifier
    protocol: CheckoutProtocol
    status: Literal[
        "not_ready_for_payment",
        "ready_for_payment",
        "completed",
        "canceled",
        "expired",
    ]
    currency: Literal["usd"]
    capabilities: dict[str, Any]
    line_items: list[dict[str, Any]]
    totals: list[dict[str, Any]]
    messages: list[dict[str, Any]] = Field(default_factory=list)
    links: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str
    updated_at: str
    expires_at: str

    @model_validator(mode="after")
    def require_protocol_version_and_total(self) -> "CheckoutSession":
        if self.protocol.version != ACP_VERSION:
            raise ValueError(f"Checkout uses unsupported ACP version {self.protocol.version}")
        total_rows = [row for row in self.totals if row.get("type") == "total"]
        if len(total_rows) != 1 or not isinstance(total_rows[0].get("amount"), int):
            raise ValueError("Checkout must expose one integer authoritative total")
        return self

    @property
    def total_minor(self) -> int:
        row = next(row for row in self.totals if row.get("type") == "total")
        return int(row["amount"])
