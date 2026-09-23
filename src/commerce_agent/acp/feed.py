import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from referencing import Registry, Resource

MAX_FEED_BYTES = 50 * 1024 * 1024
MAX_PRODUCT_LINES = 100_000
MAX_PRODUCT_LINE_BYTES = 1024 * 1024


class ProductFeedError(ValueError):
    def __init__(self, message: str, *, code: str, line: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.line = line


class FeedMetadata(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    id: str = Field(min_length=1)
    target_country: str = Field(min_length=2, max_length=2)
    updated_at: str


class FeedPrice(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    amount: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)


class FeedAvailability(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    available: bool
    status: str = Field(min_length=1)


class FeedVariant(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str
    price: FeedPrice
    availability: FeedAvailability
    categories: list[dict[str, Any]] = Field(default_factory=list)
    condition: list[str] = Field(default_factory=list)
    variant_options: list[dict[str, Any]] = Field(default_factory=list)


class FeedProduct(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str
    variants: list[FeedVariant] = Field(min_length=1)


class ProductCandidate(BaseModel):
    """Search projection. Observed feed values are never authoritative checkout terms."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    product_id: str
    product_title: str
    variant_id: str
    variant_title: str
    product_url: str
    variant_url: str
    observed_unit_amount_minor: int
    currency: str
    available: bool
    availability_status: str
    categories: tuple[str, ...]
    brands: tuple[str, ...] = ()
    conditions: tuple[str, ...]
    options: tuple[tuple[str, str], ...]


class ProductFeed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    metadata: FeedMetadata
    products: tuple[FeedProduct, ...]

    @property
    def candidates(self) -> tuple[ProductCandidate, ...]:
        candidates: list[ProductCandidate] = []
        for product in self.products:
            for variant in product.variants:
                category_values: list[str] = []
                brand_values: list[str] = []
                for row in variant.categories:
                    value = row.get("value")
                    if not isinstance(value, str):
                        continue
                    # El feed publica varias taxonomías. Solo la del comercio describe la
                    # categoría del producto; la marca viaja aparte y no debe colarse en el
                    # filtro duro de categoría.
                    taxonomy = row.get("taxonomy")
                    if taxonomy == "brand":
                        brand_values.append(value)
                    elif taxonomy in (None, "merchant"):
                        category_values.append(value)
                option_values: list[tuple[str, str]] = []
                for row in variant.variant_options:
                    name = row.get("name")
                    value = row.get("value")
                    if isinstance(name, str) and isinstance(value, str):
                        option_values.append((name, value))
                candidates.append(
                    ProductCandidate(
                        product_id=product.id,
                        product_title=product.title,
                        variant_id=variant.id,
                        variant_title=variant.title,
                        product_url=product.url,
                        variant_url=variant.url,
                        observed_unit_amount_minor=variant.price.amount,
                        currency=variant.price.currency,
                        available=variant.availability.available,
                        availability_status=variant.availability.status,
                        categories=tuple(category_values),
                        brands=tuple(brand_values),
                        conditions=tuple(variant.condition),
                        options=tuple(option_values),
                    )
                )
        return tuple(candidates)

    @property
    def available_candidates(self) -> tuple[ProductCandidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.available)

    def candidate(self, variant_id: str) -> ProductCandidate | None:
        return next(
            (candidate for candidate in self.candidates if candidate.variant_id == variant_id),
            None,
        )


def _schema_validators() -> tuple[Draft202012Validator, Draft202012Validator]:
    resource = files("commerce_agent.vendor.acp").joinpath("2026-04-17/schema.feed.json")
    bundle: dict[str, Any] = json.loads(resource.read_text(encoding="utf-8"))
    registry = Registry().with_resource(str(bundle["$id"]), Resource.from_contents(bundle))
    checker = FormatChecker()
    metadata = Draft202012Validator(
        {"$ref": f"{bundle['$id']}#/$defs/FeedMetadata"},
        registry=registry,
        format_checker=checker,
    )
    product = Draft202012Validator(
        {"$ref": f"{bundle['$id']}#/$defs/Product"},
        registry=registry,
        format_checker=checker,
    )
    return metadata, product


_METADATA_VALIDATOR, _PRODUCT_VALIDATOR = _schema_validators()


def load_product_feed(directory: Path) -> ProductFeed:
    metadata_path = directory / "metadata.json"
    products_path = directory / "products.jsonl"
    if not metadata_path.is_file() or not products_path.is_file():
        raise ProductFeedError(
            "Product feed requires metadata.json and products.jsonl",
            code="missing_feed_file",
        )
    total_bytes = metadata_path.stat().st_size + products_path.stat().st_size
    if total_bytes > MAX_FEED_BYTES:
        raise ProductFeedError(
            "Product feed exceeds the configured size limit", code="feed_too_large"
        )

    metadata_raw = _load_json_object(metadata_path, code="invalid_metadata_json")
    _validate_schema(_METADATA_VALIDATOR, metadata_raw, code="invalid_metadata")
    try:
        metadata = FeedMetadata.model_validate(metadata_raw)
    except ValidationError as error:
        raise ProductFeedError(
            "Product feed metadata is invalid", code="invalid_metadata"
        ) from error

    products: list[FeedProduct] = []
    product_ids: set[str] = set()
    variant_ids: set[str] = set()
    try:
        product_lines = products_path.open("r", encoding="utf-8")
    except UnicodeError as error:
        raise ProductFeedError("Product feed must use UTF-8", code="invalid_encoding") from error
    try:
        for line_number, raw_line in enumerate(product_lines, start=1):
            if line_number > MAX_PRODUCT_LINES:
                raise ProductFeedError(
                    "Product feed exceeds the configured product limit",
                    code="too_many_products",
                    line=line_number,
                )
            encoded_size = len(raw_line.encode("utf-8"))
            if encoded_size > MAX_PRODUCT_LINE_BYTES:
                raise ProductFeedError(
                    "Product feed line exceeds the configured size limit",
                    code="product_line_too_large",
                    line=line_number,
                )
            if not raw_line.strip():
                raise ProductFeedError(
                    "Product feed contains an empty line",
                    code="empty_product_line",
                    line=line_number,
                )
            try:
                product_raw = json.loads(raw_line)
            except json.JSONDecodeError as error:
                raise ProductFeedError(
                    "Product feed contains invalid JSON",
                    code="invalid_product_json",
                    line=line_number,
                ) from error
            if not isinstance(product_raw, dict):
                raise ProductFeedError(
                    "Each Product Feed line must contain one object",
                    code="invalid_product",
                    line=line_number,
                )
            _validate_schema(
                _PRODUCT_VALIDATOR,
                product_raw,
                code="invalid_product",
                line=line_number,
            )
            try:
                product = FeedProduct.model_validate(product_raw)
            except ValidationError as error:
                raise ProductFeedError(
                    "Product Feed product is invalid",
                    code="invalid_product",
                    line=line_number,
                ) from error
            if product.id in product_ids:
                raise ProductFeedError(
                    "Product Feed contains a duplicate product ID",
                    code="duplicate_product_id",
                    line=line_number,
                )
            product_ids.add(product.id)
            for variant in product.variants:
                if variant.id in variant_ids:
                    raise ProductFeedError(
                        "Product Feed contains a duplicate variant ID",
                        code="duplicate_variant_id",
                        line=line_number,
                    )
                variant_ids.add(variant.id)
            products.append(product)
    except UnicodeError as error:
        raise ProductFeedError("Product feed must use UTF-8", code="invalid_encoding") from error
    finally:
        product_lines.close()

    if not products:
        raise ProductFeedError("Product Feed must contain at least one product", code="empty_feed")
    return ProductFeed(metadata=metadata, products=tuple(products))


def _load_json_object(path: Path, *, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeError as error:
        raise ProductFeedError("Product feed must use UTF-8", code="invalid_encoding") from error
    except json.JSONDecodeError as error:
        raise ProductFeedError("Product feed contains invalid JSON", code=code) from error
    if not isinstance(value, dict):
        raise ProductFeedError("Product feed document must contain an object", code=code)
    return value


def _validate_schema(
    validator: Draft202012Validator,
    value: object,
    *,
    code: str,
    line: int | None = None,
) -> None:
    try:
        validator.validate(value)
    except SchemaValidationError as error:
        raise ProductFeedError(
            "Product feed violates ACP 2026-04-17", code=code, line=line
        ) from error
