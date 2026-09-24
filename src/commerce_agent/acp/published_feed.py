"""Read-only full feed published by the merchant, separate from ACP checkout calls."""

import json

import httpx

from commerce_agent.acp.feed import (
    MAX_FEED_BYTES,
    ProductFeed,
    ProductFeedError,
    load_product_feed_payload,
)
from commerce_agent.config import validate_service_base_url


class PublishedFeedClient:
    def __init__(
        self,
        merchant_base_url: str,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=validate_service_base_url(merchant_base_url),
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    def get(self) -> ProductFeed:
        try:
            with self._http.stream("GET", "/feeds/current") as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_FEED_BYTES:
                        raise ProductFeedError(
                            "Published feed exceeds the size limit", code="feed_too_large"
                        )
            return load_product_feed_payload(json.loads(data))
        except (httpx.HTTPError, ValueError) as error:
            if isinstance(error, ProductFeedError):
                raise
            raise ProductFeedError(
                "Merchant product feed is unavailable", code="feed_unavailable"
            ) from error

    def close(self) -> None:
        self._http.close()
