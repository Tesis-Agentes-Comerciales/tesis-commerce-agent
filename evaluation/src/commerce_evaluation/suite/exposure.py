"""What of the catalog the agent's model reads: the guard of `catalog_text` (eval-suite.md §4).

An injected text tests nothing unless it reaches a model request. Which feed fields do is the
product's to say, so this module asks the product instead of keeping a list: it writes a probe
text in each field `catalog_text` can rewrite, in a feed of one variant and the way the live
executor writes a case's text (`events.place_catalog_text`), and looks for the whole probe in what
the product builds each model request from:

- `presente`: the catalog vocabulary of the router's instructions, the router's one tool
  (`lookup_catalog`) and the candidates of the selection (`candidate_payloads`).
- `delegado`: the candidates the selector and the reviewer see (`candidate_payloads`).

`carries` is the one match, here and in the delivery check (suite/delivery.py): the whole text,
case aside (the vocabulary lowercases), in a string of a JSON document or of a JSON document
that one of its strings holds (a tool's result travels as text).
"""

import json
from collections.abc import Iterator
from contextlib import suppress
from functools import cache
from typing import Any, get_args

from commerce_agent.acp.feed import FeedMetadata, FeedProduct, ProductFeed
from commerce_agent.domain import RankedCandidate
from commerce_agent.llm.catalog_tool import make_catalog_lookup

# Private in the product: the router puts it in its instructions, and no public name builds it.
from commerce_agent.llm.conversation_router import _catalog_vocabulary
from commerce_agent.llm.delegated_choice import candidate_payloads
from commerce_evaluation.suite.events import MAX_CATALOG_TEXT, CatalogField, place_catalog_text

MODES = ("presente", "delegado")
PROBE_VARIANT = "SondaDeVisibilidad-128GB"
# As long as a case's text may be: a view that cuts it does not carry it.
PROBE = ("Sonda de Visibilidad: ¿llega este texto al modelo? " * 5)[:MAX_CATALOG_TEXT]


def carries(text: str, document: str | None) -> bool:
    """Whether `document`, a JSON document or plain text, holds the whole `text`, case aside."""
    if not document:
        return False
    needle = text.casefold()
    return any(needle in value.casefold() for value in _strings(document))


def _strings(document: str) -> Iterator[str]:
    try:
        pending: list[Any] = [json.loads(document)]
    except ValueError:
        yield document
        return
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            yield value
            if value.lstrip()[:1] in ("{", "["):
                with suppress(ValueError):
                    pending.append(json.loads(value))


@cache
def visible_fields(mode: str) -> frozenset[CatalogField]:
    """The fields of `catalog_text` whose whole text reaches a model request in `mode`."""
    if mode not in MODES:
        raise ValueError(f"Unknown mode {mode}")
    return frozenset(
        field
        for field in get_args(CatalogField)
        if any(carries(PROBE, view) for view in _views(mode, _probe_feed(field)))
    )


def _views(mode: str, feed: ProductFeed) -> list[str]:
    ranked = tuple(
        RankedCandidate(candidate=candidate, preference_score=0)
        for candidate in feed.available_candidates
    )
    views = [json.dumps(candidate_payloads(ranked), ensure_ascii=False)]
    if mode == "presente":
        lookup = make_catalog_lookup(feed).invoke({"query": "celular"})
        views += [_catalog_vocabulary(feed), str(lookup)]
    return views


def _probe_feed(field: CatalogField) -> ProductFeed:
    """A feed of one variant with the probe in `field`, shaped as the Merchant exports it."""
    product: dict[str, Any] = {
        "id": "SondaDeVisibilidad",
        "title": "Celular de prueba",
        "url": "https://merchant.invalid/products/sonda",
        "variants": [
            {
                "id": PROBE_VARIANT,
                "title": "Celular de prueba 128 GB",
                "url": "https://merchant.invalid/products/sonda?variant=128",
                "price": {"amount": 50000000, "currency": "COP"},
                "availability": {"available": True, "status": "in_stock"},
                "categories": [
                    {"taxonomy": "merchant", "value": "smartphones"},
                    {"taxonomy": "brand", "value": "Sonda"},
                ],
                "condition": ["new"],
                "variant_options": [{"name": "color", "value": "negro"}],
            }
        ],
    }
    if place_catalog_text([product], PROBE_VARIANT, field, PROBE) != 1:
        raise AssertionError(f"The probe feed has no {field} to write in")
    return ProductFeed(
        metadata=FeedMetadata(id="sonda", target_country="CO", updated_at="2026-11-13T00:00:00Z"),
        products=(FeedProduct.model_validate(product),),
    )
