import unicodedata
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from commerce_agent.acp.feed import FeedProduct, FeedVariant, ProductFeed

MAX_IMAGE_URL_LENGTH = 2048
MAX_DESCRIPTION_LENGTH = 500
MAX_ALT_LENGTH = 200


class ProductImage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    url: str
    alt: str


class ProductOption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    value: str


class ProductCard(BaseModel):
    """Display projection for the UI. It never reaches model prompts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    product_id: str
    variant_id: str
    title: str
    variant_title: str
    description: str | None
    image: ProductImage | None
    observed_unit_amount_minor: int
    currency: str
    available: bool
    options: tuple[ProductOption, ...]
    product_url: str | None


def plain_text(value: object, *, limit: int) -> str | None:
    """Return untrusted feed text as one collapsed line without control or format characters."""
    if not isinstance(value, str):
        return None
    visible = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} else char for char in value
    )
    collapsed = " ".join(visible.split())
    if not collapsed:
        return None
    if len(collapsed) > limit:
        return collapsed[: limit - 1].rstrip() + "…"
    return collapsed


def _web_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_IMAGE_URL_LENGTH:
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Raises for malformed ports.
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if any(unicodedata.category(char) in {"Cc", "Cf", "Zs"} for char in value):
        return None
    return value


def safe_image_url(url: object, product_url: str | None) -> str | None:
    """Accept only credential-free HTTPS images served by the product page's host."""
    image = _web_url(url)
    if image is None or product_url is None:
        return None
    parsed = urlsplit(image)
    if parsed.scheme != "https":
        return None
    product_host = urlsplit(product_url).hostname
    if product_host is None or parsed.hostname != product_host:
        return None
    return image


def _extra(model: FeedProduct | FeedVariant, name: str) -> Any:
    return (model.model_extra or {}).get(name)


def _description(product: FeedProduct, variant: FeedVariant) -> str | None:
    for source in (variant, product):
        value = _extra(source, "description")
        if isinstance(value, dict):
            text = plain_text(value.get("plain"), limit=MAX_DESCRIPTION_LENGTH)
            if text is not None:
                return text
    return None


def _image(
    product: FeedProduct, variant: FeedVariant, product_url: str | None
) -> ProductImage | None:
    for source in (variant, product):
        media = _extra(source, "media")
        if not isinstance(media, list):
            continue
        for item in media:
            if not isinstance(item, dict) or item.get("type") != "image":
                continue
            url = safe_image_url(item.get("url"), product_url)
            if url is None:
                continue
            alt = plain_text(item.get("alt_text"), limit=MAX_ALT_LENGTH)
            fallback = plain_text(product.title, limit=MAX_ALT_LENGTH) or product.id
            return ProductImage(url=url, alt=alt or fallback)
    return None


def product_card(feed: ProductFeed, variant_id: str) -> ProductCard | None:
    """Project display data from the validated feed for one variant, or None if unknown."""
    for product in feed.products:
        for variant in product.variants:
            if variant.id != variant_id:
                continue
            product_url = _web_url(product.url)
            options: list[ProductOption] = []
            for row in variant.variant_options:
                name = row.get("name")
                value = row.get("value")
                if isinstance(name, str) and isinstance(value, str):
                    options.append(ProductOption(name=name, value=value))
            return ProductCard(
                product_id=product.id,
                variant_id=variant.id,
                title=product.title,
                variant_title=variant.title,
                description=_description(product, variant),
                image=_image(product, variant, product_url),
                observed_unit_amount_minor=variant.price.amount,
                currency=variant.price.currency,
                available=variant.availability.available,
                options=tuple(options),
                product_url=product_url,
            )
    return None
