"""The case's events on the live stack (docs/design/eval-suite.md §4, suite/events.py).

An event applies once, when the flow reaches `at`, and stays in force for the rest of the trial.
The live executor applies these; a case with any other kind or moment is refused before its
trial starts, naming it, rather than run without it:

- `catalog_text` at `start`: the Merchant rewrites the offer's field (`variant_title` is the
  offer's name, `brand` its brand) and the episode's feed is exported again, so the Agent reads
  the text where a third party would have put it.
- `stock_out` at `start`: the offer sells out and the feed is exported again. At
  `after_consent` only the Merchant's offer changes: the feed the owner consented on stays.
- `merchant_unavailable` at `on_checkout_create`: FaultProxy answers the checkout POST 503.
- `revoke` at `on_selection` and `delegation_replaced` at `after_consent`: the owner's act, by
  the Agent's own API (suite/live/delegado.py).
"""

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
OFFER_KINDS = frozenset({"catalog_text", "stock_out"})


class UnsupportedEvent(ValueError):
    """The live executor does not apply this event yet."""


def check(case: PresentCase | DelegatedCase) -> None:
    unsupported = [
        f"{event.kind}@{event.at}"
        for event in case.environment.events
        if (event.at, event.kind) not in SUPPORTED
    ]
    if unsupported:
        raise UnsupportedEvent(
            f"{case.id}: the live executor does not apply {', '.join(unsupported)}"
        )


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
    for event in at(case, "start"):
        offer(lane, episode, event, feed=True)
        applied.append(described(event))
    for event in at(case, "on_checkout_create"):
        # Only the checkout POST takes this path: the fault answers it, and nothing else.
        lane.proxy.faults[CREATE_PATH] = "status_503"
        applied.append(described(event))
    return applied


def offer(lane: PurchaseLane, episode: Episode, event: Event, *, feed: bool) -> None:
    assert event.kind in OFFER_KINDS and event.variant is not None
    lane.rpc(
        "suite_offer",
        run_id=episode.run_id,
        variant=event.variant,
        kind=event.kind,
        field=event.field,
        text=event.text,
        feed_dir=str(episode.feed) if feed else None,
    )
