"""The Human Present Agent of a purchase lane, in this process, and its scripted router.

`agent` builds the Agent's purchase services for one episode on the lane's Merchant origin;
`StaticConversationRouter` stands at the model boundary with the typed decisions an LLM would
return, so no model is ever called.
"""

from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from commerce_agent.acp import ACPClient, load_product_feed
from commerce_agent.acp.feed import ProductCandidate
from commerce_agent.acp.models import Address, Buyer, FulfillmentDetails
from commerce_agent.ap2 import AgentMandateSigner
from commerce_agent.application import CheckoutDetails, PurchaseService
from commerce_agent.approval import ApprovalService
from commerce_agent.completion import CompletionService
from commerce_agent.delegated_payment import DelegatedPaymentClient
from commerce_agent.domain import PurchaseIntent, RankedCandidate
from commerce_agent.llm import (
    CandidateDecision,
    CatalogRecovery,
    CommerceDecision,
    ConversationMessage,
)
from commerce_agent.persistence import SqliteAgentStore
from commerce_evaluation.stack.purchase_lane import APPROVED_CARD, Episode, PurchaseLane

# A natural request the seeded B0 catalog can meet, and the typed intent a model extracts.
PHONE_REQUEST = (
    "Quiero comprar un telefono nuevo de 256 GB que cueste maximo dos millones "
    "incluyendo el envio. Prefiero la marca Sony."
)
PHONE_INTENT = PurchaseIntent.model_validate(
    {
        "hard": {
            "currency": "COP",
            "max_total_amount_minor": 2_000_000_00,
            "categories": ["smartphones"],
            "conditions": ["new"],
            "required_options": [{"name": "almacenamiento", "value": "256 gb"}],
        },
        "preferences": {"terms": ["sony"]},
    }
)


def buyer_details(name: str) -> CheckoutDetails:
    email = f"buyer.{name}@example.test"
    return CheckoutDetails(
        buyer=Buyer(email=email, first_name="Buyer", last_name=name.upper()),
        fulfillment=FulfillmentDetails(
            name=f"Buyer {name.upper()}",
            email=email,
            phone_number="+573000000000",
            address=Address(
                name=f"Buyer {name.upper()}",
                line_one="Calle 100 # 10-20",
                city="Bogota",
                state="DC",
                country="CO",
                postal_code="110111",
            ),
        ),
    )


# The Human Present approval happens on the episode's synthetic clock.
APPROVED_AT = datetime(2026, 9, 10, 14, 5, tzinfo=UTC)
# Variants of the seeded B0 catalog (Sony Xperia 5 II and OnePlus 8T, 256 GB, new).
PHONE = "Q100286751-256GB"
OTHER_PHONE = "Q100348826-256GB"


@dataclass
class Agent:
    """The Agent's purchase services for one episode, on the lane's Merchant origin."""

    purchases: PurchaseService
    approvals: ApprovalService
    gateway: ACPClient
    store: SqliteAgentStore

    def completion(self, payments: DelegatedPaymentClient) -> CompletionService:
        return CompletionService(self.gateway, self.approvals, payments, self.store)

    def approve(self, run_id: str):
        terms = self.purchases.approval_terms(run_id)
        return self.purchases.approve(
            run_id=run_id,
            actor_id="human-live",
            expected_fingerprint=terms.fingerprint,
            checkout_jwt=terms.checkout_jwt,
        )


@contextmanager
def agent(lane: PurchaseLane, episode: Episode) -> Iterator[Agent]:
    signer = None
    if lane.agent_key is not None:
        signer = AgentMandateSigner(
            lane.agent_key.get_secret_value(),
            merchant_id="tesis_merchant",
            merchant_name="Tesis Commerce Merchant",
        )
    approvals = ApprovalService(
        lane.store,
        lane.approval_secret,
        merchant_origin=lane.origin,
        clock=lambda: APPROVED_AT,
        mandate_signer=signer,
    )
    with ACPClient(lane.origin, episode.bearer, timeout_seconds=10) as gateway:
        purchases = PurchaseService(
            feed=load_product_feed(episode.feed),
            checkout=gateway,
            store=lane.store,
            approvals=approvals,
        )
        yield Agent(purchases, approvals, gateway, lane.store)


def payments(lane: PurchaseLane, card: str = APPROVED_CARD) -> closing[DelegatedPaymentClient]:
    return closing(DelegatedPaymentClient(lane.payment.origin, lane.payment_token, test_card=card))


class StaticConversationRouter:
    """The model boundary without a model: the typed decisions an LLM would return."""

    def __init__(self, request: str, intent: PurchaseIntent) -> None:
        self._request = request
        self._intent = intent

    def route(self, messages: tuple[ConversationMessage, ...]) -> CommerceDecision:
        assert messages[-1].content == self._request
        return CommerceDecision(
            action="purchase",
            assistant_message="Voy a preparar la compra.",
            purchase_request=self._request,
            intent=self._intent,
        )

    def choose_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        candidates: tuple[RankedCandidate, ...],
    ) -> CandidateDecision:
        assert messages and intent == self._intent and candidates
        return CandidateDecision(
            action="purchase",
            assistant_message="Elegí la opción disponible que cumple tu solicitud.",
            selected_variant_id=candidates[0].candidate.variant_id,
        )

    def recover_no_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        rejection_counts: dict[str, int],
        alternatives: tuple[ProductCandidate, ...],
    ) -> CatalogRecovery:
        assert messages and intent == self._intent and rejection_counts and alternatives
        return CatalogRecovery(
            action="clarify",
            assistant_message="No encontré una coincidencia exacta.",
        )
