import json
import re
import unicodedata

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from commerce_agent.acp.feed import ProductFeed

MAX_TOOL_CANDIDATES = 10


class CatalogLookupArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1, max_length=120)


def _search_terms(value: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(char for char in folded if not unicodedata.combining(char))
    return set(re.findall(r"\w+", plain))


def make_catalog_lookup(feed: ProductFeed) -> BaseTool:
    @tool("lookup_catalog", args_schema=CatalogLookupArgs)
    def lookup_catalog(query: str) -> str:
        """Find products in the validated feed. Prices are observations, not checkout totals."""
        terms = _search_terms(query)
        available = feed.available_candidates
        ranked = []
        for candidate in available:
            searchable = _search_terms(
                " ".join(
                    (
                        candidate.product_title,
                        candidate.variant_title,
                        *candidate.categories,
                        *candidate.conditions,
                        *(value for _, value in candidate.options),
                    )
                )
            )
            score = len(terms & searchable)
            if score:
                ranked.append((score, candidate))
        ranked.sort(key=lambda item: -item[0])
        candidates = [candidate for _, candidate in ranked] if ranked else list(available)
        result = {
            "exact_text_matches": sum(score == len(terms) for score, _ in ranked),
            "partial_matches": len(ranked),
            "sampled_fallback": not ranked,
            "total_available": len(available),
            "products": [
                {
                    "variant_id": candidate.variant_id,
                    "title": candidate.product_title,
                    "variant": candidate.variant_title,
                    "observed_unit_amount_minor": candidate.observed_unit_amount_minor,
                    "currency": candidate.currency,
                    "categories": candidate.categories,
                    "conditions": candidate.conditions,
                    "options": candidate.options,
                }
                for candidate in candidates[:MAX_TOOL_CANDIDATES]
            ],
            "prices_are_authoritative": False,
        }
        return json.dumps(result, ensure_ascii=False)

    return lookup_catalog
