"""What a case can seed, and where the runner applies it (docs/design/eval-suite.md §4).

The kinds and the triggers come from the scenarios of scenarios/p0/matrix.json and from the
injection scenarios. An event applies once, when the flow reaches `at`, and stays in force for
the rest of the trial: a fault answers every later request it matches, and a change to the
catalog or to the delegation stays made.
"""

from typing import Literal, get_args

Trigger = Literal[
    "start",  # before the agent acts
    "after_consent",  # delegado: consented, the task not yet triggered
    "before_checkout",  # an offer chosen, its checkout not yet created
    "after_checkout",  # the checkout created, not yet approved or completed
    "on_commit",  # delegado: at the commitment of the purchase, concurrently
    "after_commit",  # delegado: after the commitment and the payment
    "on_interpretation",  # the model call `model.route`
    "on_selection",  # `model.choose_candidate` or `model.hnp_select`
    "on_review",  # `model.hnp_review`
    "on_checkout_create",  # the checkout POST, through FaultProxy
    "on_payment",  # the payment request to the PSP
]
DELEGATED_TRIGGERS = frozenset({"after_consent", "on_commit", "after_commit"})

# The Merchant changes one offer.
OfferEvent = Literal[
    "stock_out",
    "restock",
    "price_change",
    "shipping_change",
    "variant_removed",
    "competing_purchase",
]
# The owner changes the delegation. A grant is never edited in place: a narrower scope replaces
# it (revocation plus a new consent).
OwnerEvent = Literal["revoke", "delegation_replaced"]
# A scripted provider answer instead of the model (FakeTransport). With no model to measure,
# a case with one runs in regression only.
ModelFault = Literal[
    "model_rate_limited", "model_quota_exhausted", "model_timeout", "model_invalid_output"
]
# The network, through FaultProxy, and the PSP.
Fault = Literal[
    "connection_lost",
    "response_lost",
    "merchant_unavailable",
    "merchant_timeout",
    "payment_declined",
]
# A third party's text in a catalog field the model reads (p0-llm-batch2.json `ct21`,
# injection-scenarios-v1.json). Seeded at `start`, within the Merchant's offer name limit.
CatalogText = Literal["catalog_text"]
CatalogField = Literal["variant_title", "brand"]
MAX_CATALOG_TEXT = 200

EventKind = Literal[OfferEvent, OwnerEvent, ModelFault, Fault, CatalogText]
OFFER_EVENTS = frozenset(get_args(OfferEvent))
PRICED_EVENTS = frozenset({"price_change", "shipping_change"})
OWNER_EVENTS = frozenset(get_args(OwnerEvent))
MODEL_FAULTS = frozenset(get_args(ModelFault))
# What can make the agent's API answer 5xx on purpose: offer, owner and catalog events make the
# product answer 409 or 404, never 5xx.
INJECTS_5XX = frozenset(get_args(Fault)) | MODEL_FAULTS
PARAMETERS = ("variant", "amount_minor", "replacement", "field", "text")


def parameters_of(kind: EventKind) -> tuple[str, ...]:
    """The parameters an event of `kind` takes, in the order of PARAMETERS."""
    if kind == "catalog_text":
        return ("variant", "field", "text")
    if kind in PRICED_EVENTS:
        return ("variant", "amount_minor")
    if kind in OFFER_EVENTS:
        return ("variant",)
    if kind == "delegation_replaced":
        return ("replacement",)
    return ()
