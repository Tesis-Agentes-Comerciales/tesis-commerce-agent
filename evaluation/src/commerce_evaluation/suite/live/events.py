"""The case's events on the live stack (docs/design/eval-suite.md §4, suite/events.py).

An event applies once, when the flow reaches `at`, and stays in force for the rest of the trial.
The live executor applies these; a case with any other kind or moment is refused before its
trial starts, naming it, rather than run without it:

- `catalog_text` at `start`: the text goes in the episode's exported feed, the catalog the
  Agent reads, before the Agent starts: `variant_title` is the variant's title and `brand` its
  brand category. The Merchant's offer and its checkouts keep their own (§4), so the text is
  held to the feed's contract, not to the Merchant's field limits.
- `stock_out` at `start`: the offer sells out and the feed is exported again, before any
  catalog text goes in. At `after_consent` only the Merchant's offer changes: the feed the
  owner consented on stays.
- `merchant_unavailable` at `on_checkout_create`: FaultProxy answers the checkout POST 503.
- `revoke` at `on_selection` and `delegation_replaced` at `after_consent`: the owner's act, by
  the Agent's own API (suite/live/delegado.py).
"""

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from commerce_evaluation.stack.fault_proxy import CREATE_PATH
from commerce_evaluation.stack.purchase_lane import Episode, PurchaseLane
from commerce_evaluation.suite.cases import DelegatedCase, Event, PresentCase
from commerce_evaluation.suite.events import Trigger

SUPPORTED: frozenset[tuple[Trigger, str]] = frozenset(
    {
        ("start", "catalog_text"),
        ("start", "stock_out"),
        ("after_consent", "stock_out"),
        ("after_consent", "delegation_replaced"),
        ("on_selection", "revoke"),
        ("on_checkout_create", "merchant_unavailable"),
    }
)
# The Merchant's offer events, by the control worker's `suite_offer`.
OFFER_KINDS = frozenset({"stock_out"})
# The exported feed's products, one JSON document per line (commerce_lab.feed).
FEED_PRODUCTS = "products.jsonl"


class UnsupportedEvent(ValueError):
    """The live executor does not apply this event yet."""


class EventRefused(ValueError):
    """The case's change cannot be made (the Merchant refused it, or its variant is not in the
    feed): the case is wrong, not the agent."""


def unsupported_events(cases: Iterable[PresentCase | DelegatedCase]) -> list[str]:
    """Each case of a plan with events the live executor does not apply, as
    "<case>: <kind>@<at>, ..."; empty when it applies them all. Pure, so a run can refuse its
    plan before the stack starts or anything is spent."""
    found = []
    for case in cases:
        refused = [
            f"{event.kind}@{event.at}"
            for event in case.environment.events
            if (event.at, event.kind) not in SUPPORTED
        ]
        if refused:
            found.append(f"{case.id}: {', '.join(refused)}")
    return found


def check(case: PresentCase | DelegatedCase) -> None:
    if unsupported := unsupported_events([case]):
        raise UnsupportedEvent(f"the live executor does not apply {unsupported[0]}")


def at(case: PresentCase | DelegatedCase, trigger: Trigger) -> list[Event]:
    return [event for event in case.environment.events if event.at == trigger]


def described(event: Event) -> dict[str, Any]:
    """What the trace keeps of an applied event: never its text, which the record already has
    in the case."""
    return {"at": event.at, "kind": event.kind, "variant": event.variant, "field": event.field}


def at_start(
    case: PresentCase | DelegatedCase, lane: PurchaseLane, episode: Episode
) -> list[dict[str, Any]]:
    """The `start` events and the faults armed from the start; what was applied, in order."""
    applied = []
    # The offer events export the feed again, so the catalog text goes in after them.
    for event in sorted(at(case, "start"), key=lambda event: event.kind == "catalog_text"):
        if event.kind == "catalog_text":
            catalog_text(case, episode.feed, event)
        else:
            offer(case, lane, episode, event, feed=True)
        applied.append(described(event))
    for event in at(case, "on_checkout_create"):
        # Only the checkout POST takes this path: the fault answers it, and nothing else.
        lane.proxy.faults[CREATE_PATH] = "status_503"
        applied.append(described(event))
    return applied


def offer(
    case: PresentCase | DelegatedCase,
    lane: PurchaseLane,
    episode: Episode,
    event: Event,
    *,
    feed: bool,
) -> None:
    assert event.kind in OFFER_KINDS and event.variant is not None
    try:
        lane.rpc(
            "suite_offer",
            run_id=episode.run_id,
            variant=event.variant,
            kind=event.kind,
            field=event.field,
            text=event.text,
            feed_dir=str(episode.feed) if feed else None,
        )
    except AssertionError as error:
        # The worker names only the error's class (a contract's ValidationError, for one).
        raise EventRefused(
            f"{case.id}: the Merchant refused {event.kind}@{event.at} on {event.variant} ({error})"
        ) from None


def catalog_text(case: PresentCase | DelegatedCase, feed: Path, event: Event) -> None:
    """Puts the event's text in the variant's field of the exported feed, as a third party
    would have published it."""
    assert event.kind == "catalog_text" and event.text is not None
    path = feed / FEED_PRODUCTS
    products = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    placed = 0
    for variant in (item for product in products for item in product["variants"]):
        if variant["id"] != event.variant:
            continue
        if event.field == "variant_title":
            variant["title"] = event.text
            placed += 1
            continue
        for category in variant["categories"]:
            if category["taxonomy"] == "brand":
                category["value"] = event.text
                placed += 1
    if placed != 1:
        raise EventRefused(
            f"{case.id}: catalog_text@start found {placed} {event.field} fields of "
            f"{event.variant} in the feed"
        )
    path.write_text(
        "".join(json.dumps(product, ensure_ascii=False) + "\n" for product in products),
        encoding="utf-8",
    )
