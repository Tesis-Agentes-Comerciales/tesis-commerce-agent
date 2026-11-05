"""Deterministic harness for `/assistant`: synthetic feed, scripted router and ACP merchant.

Shared by tests/test_assistant_transport.py and scripts/export_assistant_fixtures.py. All data is
synthetic and aligned with ui/src/storybook/fixtures.ts.
"""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from commerce_agent.acp import load_product_feed
from commerce_agent.acp.feed import ProductCandidate
from commerce_agent.acp.offer_snapshot import merchant_time
from commerce_agent.api import create_app
from commerce_agent.buyer_auth import BuyerAuthenticator, BuyerPrincipal
from commerce_agent.config import Settings
from commerce_agent.domain import HardConstraints, PurchaseIntent, RankedCandidate
from commerce_agent.domain.conditional import BranchMatch, ConditionalIntent, PurchaseBranch
from commerce_agent.domain.intent import SoftPreferences
from commerce_agent.llm import (
    CandidateDecision,
    CatalogRecovery,
    CommerceDecision,
    ConversationMessage,
    IntentInterpretationError,
)
from commerce_agent.llm.reasoning import emit_reasoning

ASSISTANT_FEED = Path(__file__).parent / "fixtures" / "assistant_feed"
MERCHANT_ORIGIN = "http://127.0.0.1:4120"
CHECKOUT_ID = "cs_storybook_1"
ORDER_ID = "ord_storybook_1"
# The number the receipt shows (`order_number`), as in ui/src/storybook/fixtures.ts.
ORDER_NUMBER = "STORYBOOK-001"

GREETING_REQUEST = "hola"
GREETING_REPLY = "¡Hola! Dime qué producto buscas y, si quieres, tu presupuesto o preferencias."
CLARIFY_REQUEST = "Busco algo para escuchar música"
CLARIFY_REPLY = (
    "Puedo ayudarte con audífonos del catálogo. ¿Tienes un presupuesto o un color preferido?"
)
PURCHASE_REQUEST = "Quiero audífonos negros por menos de 2 millones"
PURCHASE_REPLY = (
    "Elegí los Sony WH-1000XM6 negros porque cumplen el presupuesto y están disponibles."
)
NO_CANDIDATE_REQUEST = "Quiero audífonos por menos de 50 mil"
NO_CANDIDATE_REPLY = (
    "No encontré audífonos por menos de $50.000. La opción disponible más económica son los "
    "Apple AirPods Pro 3 por $1.099.000. ¿Quieres que los considere?"
)
MODEL_ERROR_REQUEST = "Necesito ayuda con mi pedido"
PENDING_REQUEST = "También quiero unos audífonos blancos"

ROUTE_REASONING = (
    "La persona pide audífonos negros con un presupuesto de 2 millones de pesos. ",
    "Filtraré el catálogo por categoría, color y precio unitario.",
)
SELECTION_REASONING = (
    "Dos audífonos negros cumplen el presupuesto. ",
    "Sony WH-1000XM6 ofrece cancelación de ruido y sigue dentro del límite.",
)

# The conditional intent (docs/design/p0-conditional-intent.md) over the same synthetic feed, as
# in its §2.2: A = Sony WH-1000XM6 negro, B = Bose QC Ultra negro (cheaper) and C = Sennheiser
# negro, the cheapest, which no branch names. Each lab offer adds the same shipping (§2.4).
CONDITIONAL_A, CONDITIONAL_B, CONDITIONAL_C = "SON-XM6-BLK", "BOS-QCU-BLK", "SEN-MOM4-BLK"
SHIPPING_MINOR = 25_000_00
LAB_EXPIRES_AT = "2099-09-20T13:00:00Z"
LAB_DELIVERY_DAYS = 2
CHECKOUT_CREATED_AT = "2026-09-20T11:00:00Z"
CONDITIONAL_ROUTED = "Reviso tus opciones con el comercio, en el orden que diste."
# One request per conditional fixture; amounts are whole pesos so the copy reads naturally. The
# merchant totals are 1.924.000 (A), 1.824.000 (B) and 1.274.000 (C) with shipping.
CONDITIONAL_FIRST_REQUEST = (
    "Compra los Sony WH-1000XM6 negros si el total con envío es de hasta $2.000.000; si no, los "
    "Bose QuietComfort Ultra negros si es de hasta $1.900.000."
)
CONDITIONAL_SECOND_REQUEST = (
    "Compra los Sony WH-1000XM6 negros si el total con envío es de hasta $1.900.000; si no, los "
    "Bose QuietComfort Ultra negros si es de hasta $1.850.000."
)
CONDITIONAL_NONE_REQUEST = (
    "Compra los Sony WH-1000XM6 negros si el total con envío es de hasta $1.900.000; si no, los "
    "Bose QuietComfort Ultra negros si es de hasta $1.800.000."
)
CONDITIONAL_UNDETERMINED_REQUEST = (
    "Compra los Sony WH-1000XM6 negros si el total con envío es de hasta $1.950.000; si no, los "
    "Bose QuietComfort Ultra negros si es de hasta $1.900.000."
)
CONDITIONAL_OFFER_CHANGED_REQUEST = (
    "Compra los Sony WH-1000XM6 negros si el total con envío es de hasta $1.980.000; si no, los "
    "Bose QuietComfort Ultra negros si es de hasta $1.900.000."
)
CONDITIONAL_CLARIFY_REQUEST = (
    "Compra unos Sony WH-1000XM6 si el total con envío es de hasta $2.000.000; si no, los Bose "
    "QuietComfort Ultra negros si es de hasta $1.900.000."
)
CONDITIONAL_EXCLUSIONS_REQUEST = (
    "Quiero audífonos negros por menos de 2 millones. Prefiero los Sony, pero nunca compres los "
    "Bose QuietComfort Ultra."
)
# The checkout total the merchant prepares for A in `conditional-offer-changed`: its offer said
# 1.924.000, still within the limit of 1.980.000.
CHANGED_CHECKOUT_TOTAL = 1_934_000_00


def by_variant(variant_id: str) -> BranchMatch:
    """A branch as the evidence scripts it (spec §4.1): the variant the pair rules resolved."""
    return BranchMatch(variant_id=variant_id)


def ordered_branches(
    first: int | None, second: int | None, *, first_match: BranchMatch | None = None
) -> ConditionalIntent:
    """«A si el total es ≤ X; si no, B si el total es ≤ Y», with A and B of this feed."""
    return ConditionalIntent(
        branches=(
            PurchaseBranch(
                match=first_match or by_variant(CONDITIONAL_A), max_total_amount_minor=first
            ),
            PurchaseBranch(match=by_variant(CONDITIONAL_B), max_total_amount_minor=second),
        ),
        currency="COP",
    )


def conditional_decision(
    conditional: ConditionalIntent,
    *,
    request: str,
    hard: HardConstraints | None = None,
    preferences: SoftPreferences | None = None,
) -> CommerceDecision:
    """The router's purchase decision for a conditional turn: `intent` drives the search the
    buyer sees, `conditional` the branches the backend evaluates."""
    return CommerceDecision(
        action="purchase",
        assistant_message=CONDITIONAL_ROUTED,
        purchase_request=request,
        intent=PurchaseIntent(
            hard=hard or HardConstraints(currency="COP", categories=("headphones",)),
            preferences=preferences or SoftPreferences(),
        ),
        conditional=conditional,
    )


def _whole(pesos: int) -> int:
    return pesos * 100


CONDITIONAL_DECISIONS: dict[str, CommerceDecision] = {
    CONDITIONAL_FIRST_REQUEST: conditional_decision(
        ordered_branches(_whole(2_000_000), _whole(1_900_000)), request=CONDITIONAL_FIRST_REQUEST
    ),
    CONDITIONAL_SECOND_REQUEST: conditional_decision(
        ordered_branches(_whole(1_900_000), _whole(1_850_000)), request=CONDITIONAL_SECOND_REQUEST
    ),
    CONDITIONAL_NONE_REQUEST: conditional_decision(
        ordered_branches(_whole(1_900_000), _whole(1_800_000)), request=CONDITIONAL_NONE_REQUEST
    ),
    CONDITIONAL_UNDETERMINED_REQUEST: conditional_decision(
        ordered_branches(_whole(1_950_000), _whole(1_900_000)),
        request=CONDITIONAL_UNDETERMINED_REQUEST,
    ),
    CONDITIONAL_OFFER_CHANGED_REQUEST: conditional_decision(
        ordered_branches(_whole(1_980_000), _whole(1_900_000)),
        request=CONDITIONAL_OFFER_CHANGED_REQUEST,
    ),
    # «Unos Sony WH-1000XM6» names the black and the silver ones: the branch is ambiguous.
    CONDITIONAL_CLARIFY_REQUEST: conditional_decision(
        ordered_branches(
            _whole(2_000_000),
            _whole(1_900_000),
            first_match=BranchMatch(required_terms=("sony wh-1000xm6",)),
        ),
        request=CONDITIONAL_CLARIFY_REQUEST,
    ),
    # UX07: «prefiero» stays a preference; «nunca» is an exclusion, and no branch is evaluated.
    CONDITIONAL_EXCLUSIONS_REQUEST: conditional_decision(
        ConditionalIntent(excluded=(BranchMatch(required_terms=("bose quietcomfort ultra",)),)),
        request=CONDITIONAL_EXCLUSIONS_REQUEST,
        hard=HardConstraints(
            currency="COP",
            categories=("headphones",),
            max_unit_amount_minor=2_000_000_00,
        ),
        preferences=SoftPreferences(terms=("sony",)),
    ),
}


def checkout_details() -> dict[str, Any]:
    return {
        "buyer": {"email": "ana@example.test", "first_name": "Ana", "last_name": "Pérez"},
        "fulfillment": {
            "name": "Ana Pérez",
            "email": "ana@example.test",
            "phone_number": "+573000000000",
            "address": {
                "name": "Ana Pérez",
                "line_one": "Calle 100 # 10-20",
                "city": "Bogotá",
                "state": "DC",
                "country": "CO",
                "postal_code": "110111",
            },
        },
    }


def purchase_intent(max_unit_amount_minor: int, *, black: bool) -> PurchaseIntent:
    hard: dict[str, Any] = {
        "categories": ["headphones"],
        "max_unit_amount_minor": max_unit_amount_minor,
    }
    if black:
        hard["required_options"] = [{"name": "color", "value": "negro"}]
    return PurchaseIntent.model_validate({"hard": hard})


class ScriptedRouter:
    """Conversation router double keyed by the latest user message."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[ConversationMessage, ...]]] = []

    def route(self, messages: tuple[ConversationMessage, ...]) -> CommerceDecision:
        self.calls.append(("route", messages))
        text = messages[-1].content
        if text == GREETING_REQUEST:
            return CommerceDecision(action="respond", assistant_message=GREETING_REPLY)
        if text == CLARIFY_REQUEST:
            return CommerceDecision(action="clarify", assistant_message=CLARIFY_REPLY)
        if text == MODEL_ERROR_REQUEST:
            raise IntentInterpretationError("The conversation model request failed")
        if text == NO_CANDIDATE_REQUEST:
            return CommerceDecision(
                action="purchase",
                assistant_message="Buscaré audífonos en ese presupuesto.",
                intent=purchase_intent(50_000_00, black=False),
            )
        if text in CONDITIONAL_DECISIONS:
            return CONDITIONAL_DECISIONS[text]
        for delta in ROUTE_REASONING:
            emit_reasoning(delta)
        return CommerceDecision(
            action="purchase",
            assistant_message="Buscaré audífonos negros dentro de tu presupuesto.",
            purchase_request=PURCHASE_REQUEST,
            intent=purchase_intent(2_000_000_00, black=True),
        )

    def choose_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        candidates: tuple[RankedCandidate, ...],
    ) -> CandidateDecision:
        self.calls.append(("choose_candidate", messages))
        assert intent.hard.categories == ("headphones",)
        assert any(item.candidate.variant_id == "SON-XM6-BLK" for item in candidates)
        for delta in SELECTION_REASONING:
            emit_reasoning(delta)
        return CandidateDecision(
            action="purchase",
            assistant_message=PURCHASE_REPLY,
            selected_variant_id="SON-XM6-BLK",
        )

    def recover_no_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        rejection_counts: dict[str, int],
        alternatives: tuple[ProductCandidate, ...],
    ) -> CatalogRecovery:
        self.calls.append(("recover_no_candidate", messages))
        assert rejection_counts and alternatives
        return CatalogRecovery(action="clarify", assistant_message=NO_CANDIDATE_REPLY)


def _checkout(status: str, *, with_details: bool, checkout_id: str = CHECKOUT_ID) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": checkout_id,
        "protocol": {"version": "2026-04-17"},
        "status": status,
        "currency": "cop",
        "capabilities": {"payment": {"handlers": []}},
        "line_items": [
            {
                "id": "li_1",
                "item": {
                    "id": "SON-XM6-BLK",
                    "name": "Sony WH-1000XM6 negro",
                    "unit_amount": 1_899_000_00,
                },
                "quantity": 1,
                "totals": [{"type": "total", "display_text": "Artículo", "amount": 1_899_000_00}],
            }
        ],
        "totals": [{"type": "total", "display_text": "Total", "amount": 1_924_000_00}],
        "fulfillment_options": [
            {
                "type": "shipping",
                "id": "ship_1",
                "title": "Envío simulado a Bogotá",
                "earliest_delivery_time": "2099-09-22T13:00:00Z",
                "latest_delivery_time": "2099-09-22T13:00:00Z",
                "totals": [{"type": "total", "display_text": "Envío", "amount": 25_000_00}],
            }
        ],
        "selected_fulfillment_options": [],
        "messages": [],
        "links": [],
        "created_at": "2026-09-20T11:00:00Z",
        "updated_at": "2026-09-20T11:30:00Z",
        "expires_at": "2099-09-20T13:00:00Z",
    }
    if with_details:
        details = checkout_details()
        payload["buyer"] = details["buyer"]
        payload["fulfillment_details"] = details["fulfillment"]
        payload["selected_fulfillment_options"] = [
            {"type": "shipping", "option_id": "ship_1", "item_ids": ["li_1"]}
        ]
    return payload


FEED_CANDIDATES = {
    candidate.variant_id: candidate for candidate in load_product_feed(ASSISTANT_FEED).candidates
}


def lab_offer(
    variant_id: str,
    *,
    stock: int = 3,
    status: Literal["active", "inactive"] = "active",
    price: int | None = None,
    shipping: int = SHIPPING_MINOR,
    revision: int = 1,
) -> dict[str, Any]:
    """The Merchant Offer of `POST /lab/offers/get` for a variant of the synthetic feed: its feed
    price unless `price` says otherwise, plus shipping; the total includes it (spec §2.4)."""
    candidate = FEED_CANDIDATES[variant_id]
    items = candidate.observed_unit_amount_minor if price is None else price
    return {
        "id": variant_id,
        "product_id": candidate.product_id,
        "product_status": status,
        "merchant_id": "merchant_lab",
        "revision": revision,
        "category": "headphones",
        "name": candidate.variant_title,
        "brand": candidate.product_title.split()[0],
        "condition": "new",
        "availability": "in_stock" if stock > 0 else "out_of_stock",
        "available_quantity": stock,
        "pricing": {
            "currency": "cop",
            "items_total_minor": items,
            "shipping_total_minor": shipping,
            "total_minor": items + shipping,
            "tax_included": True,
        },
        "delivery_context": {"country": "CO", "city": "Bogota", "postal_code": "110111"},
        "delivery_days": LAB_DELIVERY_DAYS,
        "observed_at": "2026-09-20T11:00:00Z",
        "expires_at": LAB_EXPIRES_AT,
    }


def lab_offers(*variant_ids: str, **changes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The offers of A, B and C (or of `variant_ids`), with `changes` by variant."""
    chosen = variant_ids or (CONDITIONAL_A, CONDITIONAL_B, CONDITIONAL_C)
    return {
        variant_id: lab_offer(variant_id, **changes.get(variant_id, {})) for variant_id in chosen
    }


def _lab_checkout(
    offer: dict[str, Any], status: str, *, with_details: bool, checkout_id: str, total: int | None
) -> dict[str, Any]:
    """The checkout the lab merchant prepares from an offer: its subtotal, shipping and total,
    delivery and expiry, which the freshness guard compares (offer_freshness.py). `total`
    replaces the offer's, as when the offer changed between the read and the creation."""
    payload = _checkout(status, with_details=with_details, checkout_id=checkout_id)
    items = offer["pricing"]["items_total_minor"]
    total = offer["pricing"]["total_minor"] if total is None else total
    shipping = total - items
    payload["line_items"] = [
        {
            "id": "li_1",
            "item": {"id": offer["id"], "name": offer["name"], "unit_amount": items},
            "quantity": 1,
            "totals": [{"type": "total", "display_text": "Artículo", "amount": items}],
        }
    ]
    payload["totals"] = [
        {"type": "subtotal", "display_text": "Productos", "amount": items},
        {"type": "fulfillment", "display_text": "Envío", "amount": shipping},
        {"type": "total", "display_text": "Total", "amount": total},
    ]
    delivery = merchant_time(CHECKOUT_CREATED_AT) + timedelta(days=offer["delivery_days"])
    for option in payload["fulfillment_options"]:
        option["earliest_delivery_time"] = option["latest_delivery_time"] = (
            delivery.isoformat().replace("+00:00", "Z")
        )
        option["totals"] = [{"type": "total", "display_text": "Envío", "amount": shipping}]
    payload["created_at"] = CHECKOUT_CREATED_AT
    payload["expires_at"] = offer["expires_at"]
    return payload


OfferFault = Literal["drop", "unavailable"]


@dataclass
class Merchant:
    """In-memory ACP merchant served through httpx.MockTransport.

    Each creation opens a new session (`cs_storybook_1`, `cs_storybook_2`…), so a purchase asked
    for again after canceling gets its own checkout, as with the real merchant.

    With `offers` it is the lab merchant of the conditional intent: it serves `POST
    /lab/offers/get` and prepares the checkout of the variant asked for, from its offer. Without
    them it is the merchant of the other fixtures, whose checkout is always the Sony's.
    """

    fail_checkout: bool = False
    requests: list[str] = field(default_factory=list)
    status: str = "ready_for_payment"
    created: int = 0
    # None leaves it out of the order, as a merchant that only sends the order ID.
    order_number: str | None = ORDER_NUMBER
    offers: dict[str, dict[str, Any]] = field(default_factory=dict)
    # `drop`: the merchant records the read and the answer never arrives (the proxy's `drop`);
    # `unavailable`: a 503.
    offer_faults: dict[str, OfferFault] = field(default_factory=dict)
    # The checkout total by variant when it differs from its offer.
    checkout_totals: dict[str, int] = field(default_factory=dict)
    # The variant the merchant puts in the checkout when it is not the one asked for.
    checkout_variants: dict[str, str] = field(default_factory=dict)
    # Every `offer_id` read, in order, and every variant a checkout was created for.
    offer_reads: list[str] = field(default_factory=list)
    created_variants: list[str] = field(default_factory=list)
    faults_hit: list[str] = field(default_factory=list)

    @property
    def checkout_id(self) -> str:
        return CHECKOUT_ID if self.created <= 1 else f"cs_storybook_{self.created}"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def reads_before_creation(self) -> list[str]:
        """The decision's reads (spec §4.4): those before the first creation POST; freshness
        reads after it do not count."""
        reads: list[str] = []
        offers = iter(self.offer_reads)
        for call in self.requests:
            if call == "POST /checkout_sessions":
                break
            if call == "POST /lab/offers/get":
                reads.append(next(offers))
        return reads

    def effects(self) -> list[str]:
        """Every POST except an offer read: creations, updates, cancels and completions."""
        return [
            call
            for call in self.requests
            if call.startswith("POST ") and call != "POST /lab/offers/get"
        ]

    def _offer(self, request: httpx.Request) -> httpx.Response:
        offer_id = json.loads(request.content)["offer_id"]
        self.offer_reads.append(offer_id)
        fault = self.offer_faults.get(offer_id)
        if fault is not None:
            self.faults_hit.append(offer_id)
        if fault == "drop":
            raise httpx.ReadError("synthetic dropped answer", request=request)
        if fault == "unavailable":
            return httpx.Response(503, json={"detail": {"code": "unavailable"}})
        offer = self.offers.get(offer_id)
        if offer is None:
            return httpx.Response(200, json={"ok": False, "error": {"code": "OFFER_NOT_FOUND"}})
        return httpx.Response(200, json={"ok": True, "data": offer})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(f"{request.method} {path}")
        if path == "/.well-known/acp.json":
            return httpx.Response(
                200,
                json={
                    "protocol": {
                        "name": "acp",
                        "version": "2026-04-17",
                        "supported_versions": ["2026-04-17"],
                    },
                    "api_base_url": MERCHANT_ORIGIN,
                    "transports": ["rest"],
                    "capabilities": {
                        "services": ["checkout"],
                        "supported_currencies": ["cop"],
                        "supported_locales": ["es-CO"],
                    },
                },
            )
        if self.fail_checkout:
            raise httpx.ConnectError("synthetic merchant outage", request=request)
        if request.method == "POST" and path == "/lab/offers/get" and self.offers:
            return self._offer(request)
        if request.method == "POST" and path == "/checkout_sessions":
            self.created += 1
            # The lab session waits for the buyer's details, as D1's creation check expects.
            self.status = "not_ready_for_payment" if self.offers else "ready_for_payment"
            self.created_variants.append(json.loads(request.content)["line_items"][0]["id"])
            created = self._session("not_ready_for_payment", with_details=False)
            return httpx.Response(201, json=created)
        checkout_path = f"/checkout_sessions/{self.checkout_id}"
        if path == checkout_path and request.method in {"GET", "POST"}:
            if self.offers and request.method == "POST":
                self.status = "ready_for_payment"
            return httpx.Response(200, json=self._current())
        if path == f"{checkout_path}/cancel":
            self.status = "canceled"
            return httpx.Response(200, json=self._current())
        if path == f"{checkout_path}/complete":
            self.status = "completed"
            completed = self._current()
            completed["order"] = {
                "type": "order",
                "id": ORDER_ID,
                "checkout_session_id": self.checkout_id,
                "permalink_url": f"{MERCHANT_ORIGIN}/orders/{ORDER_ID}",
                "status": "confirmed",
                "line_items": [],
                "totals": completed["totals"],
            }
            if self.order_number is not None:
                completed["order"]["order_number"] = self.order_number
            return httpx.Response(200, json=completed)
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    def _session(self, status: str, *, with_details: bool) -> dict[str, Any]:
        if not self.offers:
            return _checkout(status, with_details=with_details, checkout_id=self.checkout_id)
        asked = self.created_variants[-1]
        variant_id = self.checkout_variants.get(asked, asked)
        return _lab_checkout(
            self.offers[variant_id],
            status,
            with_details=with_details,
            checkout_id=self.checkout_id,
            total=self.checkout_totals.get(variant_id),
        )

    def _current(self) -> dict[str, Any]:
        details = not self.offers or self.status != "not_ready_for_payment"
        return deepcopy(self._session(self.status, with_details=details))


def counter_ids() -> Callable[[str], str]:
    counters: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}_{counters[prefix]:03d}"

    return new_id


def settings(database: Path) -> Settings:
    return Settings(
        merchant_bearer_token=SecretStr("merchant-token-for-tests"),
        merchant_webhook_secret=SecretStr("webhook-secret-with-at-least-32-characters"),
        agent_approval_secret=SecretStr("approval-secret-with-at-least-32-characters"),
        payment_agent_bearer_token=SecretStr("payment-agent-token-with-at-least-32-characters"),
        agent_database_path=database,
        agent_feed_source="file",
        agent_feed_path=ASSISTANT_FEED,
        merchant_base_url=MERCHANT_ORIGIN,
    )


# Active policy (D1) whose merchant list leaves out the test merchant, with fixed synthetic keys
# so the blocked stream is reproducible.
POLICY_BLOCKS_MERCHANT: dict[str, Any] = {
    "agent_governance_mode": "enforce",
    "agent_d1_key_id": "fixture-key",
    "agent_d1_policy_set_id": "d1-demo-policy",
    "agent_d1_policy_revision": "3",
    "agent_d1_hmac_key_hex": SecretStr("1" * 64),
    "agent_d1_aes_key_hex": SecretStr("2" * 64),
    "agent_d1_allowed_merchant_origins": "https://tienda-aliada.example.test",
}


def _pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


# HNP switched on, as tests/test_delegation_api.py does: fixture-only keys generated per session.
# They sign nothing in `/assistant`, so the delegate stream stays byte-for-byte reproducible.
HNP_ON: dict[str, Any] = {
    "agent_auth_mode": "jwt",
    "agent_auth_issuer": "http://127.0.0.1:5173",
    "agent_ap2_required": True,
    "agent_ap2_private_key_pem": SecretStr(_pem()),
    "agent_hnp_enabled": True,
    "agent_hnp_agent_key_pem": SecretStr(_pem()),
    "agent_hnp_service_token": SecretStr("service-token-" + "s" * 32),
    "agent_hnp_operator_token": SecretStr("operator-token-" + "o" * 32),
    "agent_d1_key_id": "fixture-key",
    "agent_d1_hmac_key_hex": SecretStr("1" * 64),
    "agent_d1_aes_key_hex": SecretStr("2" * 64),
}


class TwoBuyers:
    """`Bearer buyer-a` and `Bearer buyer-b`; no header is buyer-a, as the fixtures send none."""

    def authenticate(self, authorization: str | None) -> BuyerPrincipal | None:
        buyers = {None: "buyer-a", "Bearer buyer-a": "buyer-a", "Bearer buyer-b": "buyer-b"}
        return BuyerPrincipal(buyers[authorization]) if authorization in buyers else None


def build_app(
    database: Path,
    *,
    router: Any | None = None,
    merchant: Merchant | None = None,
    buyer_authenticator: BuyerAuthenticator | None = None,
    governance: dict[str, Any] | None = None,
) -> FastAPI:
    return create_app(
        settings=settings(database).model_copy(update=governance or {}),
        transport=(merchant or Merchant()).transport(),
        payment_transport=httpx.MockTransport(
            lambda request: httpx.Response(201, json={"id": "vt_" + "a" * 64})
        ),
        conversation_router=router if router is not None else ScriptedRouter(),
        id_factory=counter_ids(),
        buyer_authenticator=buyer_authenticator,
    )


@contextmanager
def assistant_client(
    database: Path,
    *,
    router: Any | None = None,
    merchant: Merchant | None = None,
    governance: dict[str, Any] | None = None,
    buyer_authenticator: BuyerAuthenticator | None = None,
) -> Iterator[TestClient]:
    app = build_app(
        database,
        router=router,
        merchant=merchant,
        governance=governance,
        buyer_authenticator=buyer_authenticator,
    )
    with TestClient(app) as client:
        yield client


def add_message(text: str) -> dict[str, Any]:
    return {
        "type": "add-message",
        "message": {"role": "user", "parts": [{"type": "text", "text": text}]},
        "parentId": None,
        "sourceId": None,
    }


def purchase_updated(run_id: str) -> dict[str, Any]:
    return {"type": "purchase-updated", "runId": run_id}


def assistant_body(
    commands: list[dict[str, Any]],
    *,
    turn_id: str,
    state: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    """Request body shaped like the one `useAssistantTransportRuntime` sends."""
    return {
        "commands": commands,
        "state": state if state is not None else {"messages": [], "purchase": None},
        "system": None,
        "tools": {},
        "threadId": None,
        "callSettings": {},
        "config": {},
        "details": checkout_details(),
        "turn_id": turn_id,
        **extra,
    }


class TextResponse(Protocol):
    @property
    def text(self) -> str: ...


def sse_body(response: TextResponse) -> str:
    """Response body without heartbeat comments, so it is byte-for-byte reproducible."""
    events = [event for event in response.text.split("\n\n") if event and event != ": heartbeat"]
    return "".join(f"{event}\n\n" for event in events)


def parse_sse(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    done = False
    for event in body.split("\n\n"):
        if not event:
            continue
        assert event.startswith("data: "), event
        data = event.removeprefix("data: ")
        if data == "[DONE]":
            done = True
            continue
        assert not done, "frame after [DONE]"
        frames.append(json.loads(data))
    assert done, "missing [DONE]"
    return frames


def _apply(state: Any, path: list[str], operation: dict[str, Any]) -> Any:
    if not path:
        if operation["type"] == "set":
            return operation["value"]
        assert isinstance(state, str), f"append-text on {type(state).__name__}"
        return state + operation["value"]
    head, rest = path[0], path[1:]
    if isinstance(state, list):
        index = int(head)
        assert 0 <= index <= len(state), f"index {index} out of bounds"
        if index == len(state):
            return [*state, _apply(None, rest, operation)]
        return [*state[:index], _apply(state[index], rest, operation), *state[index + 1 :]]
    current = state if isinstance(state, dict) else {}
    return {**current, head: _apply(current.get(head), rest, operation)}


def rebuild_state(frames: list[dict[str, Any]], initial: Any = None) -> Any:
    """Apply update-state operations the way the frontend accumulator does."""
    state = initial
    for frame in frames:
        assert frame["type"] == "update-state", frame
        for operation in frame["operations"]:
            assert operation["type"] in {"set", "append-text"}
            state = _apply(state, operation["path"], operation)
    return state


def post_assistant(client: TestClient, body: dict[str, Any]) -> tuple[str, Any]:
    response = client.post("/assistant", json=body)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    text = sse_body(response)
    return text, rebuild_state(parse_sse(text), body.get("state"))


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "ui/src/storybook/fixtures/assistant-transport"


@dataclass(frozen=True)
class Fixture:
    name: str
    description: str
    commands: list[dict[str, Any]]
    starts_from: str | None
    body: str


def render_fixtures(workdir: Path) -> list[Fixture]:
    """Produce every Storybook SSE fixture from the real backend with deterministic inputs."""
    fixtures: list[Fixture] = []

    def record(
        client: TestClient,
        name: str,
        description: str,
        commands: list[dict[str, Any]],
        *,
        state: Any = None,
        starts_from: str | None = None,
        **extra: Any,
    ) -> Any:
        body, final_state = post_assistant(
            client, assistant_body(commands, turn_id=f"turn-{name}", state=state, **extra)
        )
        fixtures.append(Fixture(name, description, commands, starts_from, body))
        return final_state

    with assistant_client(workdir / "conversation.db") as client:
        greeted = record(
            client,
            "greeting",
            "Saludo: `interpret_request` decide `respond` y el asistente contesta sin buscar.",
            [add_message(GREETING_REQUEST)],
        )
        record(
            client,
            "clarify",
            "Aclaración: `interpret_request` decide `clarify` y el asistente pide más detalles.",
            [add_message(CLARIFY_REQUEST)],
            state=greeted,
            starts_from="greeting",
        )

    with assistant_client(workdir / "purchase.db") as client:
        prepared = record(
            client,
            "purchase",
            "Compra preparada: razonamiento, los cinco pasos, tres productos con imagen y "
            "descripción, términos de aprobación y `purchase` en `awaiting_confirmation`.",
            [add_message(PURCHASE_REQUEST)],
        )
        record(
            client,
            "pending-purchase",
            "Compra pendiente verificada en SQLite: el asistente pide revisarla o cancelarla "
            "sin llamar al modelo.",
            [add_message(PENDING_REQUEST)],
            state=prepared,
            starts_from="purchase",
        )
        run_id = prepared["purchase"]["run_id"]
        terms = client.get(f"/purchases/{run_id}/approval").json()
        approval = client.post(
            f"/purchases/{run_id}/approval",
            json={"expected_fingerprint": terms["fingerprint"]},
        )
        assert approval.status_code == 200, approval.text
        completion = client.post(f"/purchases/{run_id}/complete")
        assert completion.status_code == 200, completion.text
        record(
            client,
            "purchase-completed",
            "Seguimiento tras aprobar y completar por REST: `purchase_update` con la orden y "
            "`purchase.stage = completed`.",
            [purchase_updated(run_id)],
            state=prepared,
            starts_from="purchase",
        )

    with assistant_client(workdir / "canceled.db") as client:
        _, prepared = post_assistant(
            client, assistant_body([add_message(PURCHASE_REQUEST)], turn_id="turn-purchase")
        )
        run_id = prepared["purchase"]["run_id"]
        canceled = client.post(f"/purchases/{run_id}/cancel")
        assert canceled.status_code == 200, canceled.text
        canceled_state = record(
            client,
            "purchase-canceled",
            "Seguimiento tras cancelar por REST: `purchase_update` y `purchase.stage = canceled`.",
            [purchase_updated(run_id)],
            state=prepared,
            starts_from="purchase",
        )
        record(
            client,
            "purchase-repeated",
            "La misma compra pedida de nuevo después de cancelar (S04, términos que cambiaron): "
            "un checkout nuevo con otro `run_id`, términos nuevos y `purchase` en "
            "`awaiting_confirmation`.",
            [add_message(PURCHASE_REQUEST)],
            state=canceled_state,
            starts_from="purchase-canceled",
        )

    with assistant_client(workdir / "no-candidate.db") as client:
        record(
            client,
            "no-candidate",
            "Sin candidatos: `search_catalog` devuelve `products: []` y el texto final es la "
            "recuperación conversacional.",
            [add_message(NO_CANDIDATE_REQUEST)],
        )

    with assistant_client(
        workdir / "delegate.db", governance=HNP_ON, buyer_authenticator=TwoBuyers()
    ) as client:
        record(
            client,
            "delegate",
            'Compra delegada en el chat (`mode: "delegate"`, HNP encendido): '
            "`interpret_request` y `search_catalog`, sin elegir producto ni crear checkout. El "
            "estado termina con `purchase: null` y `delegation_draft` con las opciones, el "
            "presupuesto del mensaje y el mensaje como preferencia.",
            [add_message(PURCHASE_REQUEST)],
            mode="delegate",
        )

    with assistant_client(workdir / "blocked.db", governance=POLICY_BLOCKS_MERCHANT) as client:
        record(
            client,
            "purchase-blocked",
            "Bloqueo de la política de compra en modo activo: la lista de comercios no incluye la "
            "tienda, `prepare_checkout` queda con `isError` y el código `d1_denied`, y el mensaje "
            "termina en `incomplete`/`error`. Los `args` del paso traen el `run_id` de la compra.",
            [add_message(PURCHASE_REQUEST)],
        )

    with assistant_client(workdir / "error.db", merchant=Merchant(fail_checkout=True)) as client:
        record(
            client,
            "error",
            "Error saneado: el comercio no responde durante `prepare_checkout`; el paso queda "
            "con `isError` y el mensaje con estado `incomplete`/`error`.",
            [add_message(PURCHASE_REQUEST)],
        )

    # Intención condicional (docs/design/p0-conditional-intent.md §3.3-§3.8), en B0: el comercio
    # de laboratorio sirve la oferta de A (1.924.000 con envío), B (1.824.000) y C (1.274.000).
    conditional: list[tuple[str, str, str, Merchant]] = [
        (
            "conditional-first",
            "Intención condicional, elegida la primera opción: `select_product` lee solo la "
            "oferta de A, no consulta al modelo y trae `conditional` con A `eligible` y B "
            "`not_evaluated`; se prepara un solo checkout, de A, en `awaiting_confirmation`.",
            CONDITIONAL_FIRST_REQUEST,
            Merchant(offers=lab_offers()),
        ),
        (
            "conditional-second",
            "Intención condicional, elegida la segunda opción: A queda `over_limit` por su total "
            "con envío, B `eligible`, y el único checkout es de B.",
            CONDITIONAL_SECOND_REQUEST,
            Merchant(offers=lab_offers()),
        ),
        (
            "conditional-none",
            "Ninguna opción cumple: A y B quedan `over_limit`, la acción es `respond`, el texto "
            "nombra las dos causas y no menciona a C, y no se crea ningún checkout.",
            CONDITIONAL_NONE_REQUEST,
            Merchant(offers=lab_offers()),
        ),
        (
            "conditional-undetermined",
            "La lectura de la oferta de A falla: A queda `unknown` y B `not_evaluated`, no se pasa "
            "a B y no se crea ningún checkout.",
            CONDITIONAL_UNDETERMINED_REQUEST,
            Merchant(offers=lab_offers(), offer_faults={CONDITIONAL_A: "drop"}),
        ),
        (
            "conditional-offer-changed",
            "El checkout de A trae otro total que su oferta: `select_product` eligió A, "
            "`prepare_checkout` queda con `isError` y el código `offer_changed`, el checkout se "
            "cancela y no se prueba B.",
            CONDITIONAL_OFFER_CHANGED_REQUEST,
            Merchant(offers=lab_offers(), checkout_totals={CONDITIONAL_A: CHANGED_CHECKOUT_TOTAL}),
        ),
        (
            "conditional-clarify",
            "La primera opción coincide con dos variantes (negro y plata): `select_product` "
            "decide `clarify` sin `conditional`, la acción es `clarify` y no se lee ninguna "
            "oferta.",
            CONDITIONAL_CLARIFY_REQUEST,
            Merchant(offers=lab_offers()),
        ),
        (
            "conditional-exclusions",
            "«Nunca compres los Bose»: sin ramas no se lee ninguna oferta para decidir; "
            "`search_catalog` informa `rejections.excluded = 1`, los Bose no están en la lista "
            "corta y la selección normal prepara el checkout de los Sony.",
            CONDITIONAL_EXCLUSIONS_REQUEST,
            Merchant(offers=lab_offers()),
        ),
    ]
    for name, description, request, merchant in conditional:
        with assistant_client(workdir / f"{name}.db", merchant=merchant) as client:
            record(client, name, description, [add_message(request)])
    return fixtures


def fixtures_readme(fixtures: list[Fixture]) -> str:
    lines = [
        "# Fixtures de Assistant Transport",
        "",
        "Generados por `uv run python scripts/export_assistant_fixtures.py`; no se editan a mano.",
        "`tests/test_assistant_transport.py` verifica que coincidan con la salida actual.",
        "",
        "Cada archivo `.sse` es el cuerpo real de `POST /assistant` con router y merchant",
        "simulados, feed sintético (`tests/fixtures/assistant_feed`), ids deterministas y sin",
        "líneas de heartbeat. Todos los datos son sintéticos. El primer frame siempre reemplaza",
        "el estado completo (`set` en la raíz), así que un fixture que continúa otro ya incluye el",
        "estado previo del que parte.",
        "",
        "Cada request lleva además `details` (perfil sintético de Ana Pérez en Bogotá),",
        "`turn_id = turn-<nombre>` y las claves que agrega el runtime (`system`, `tools`,",
        "`threadId`, `callSettings`, `config`), que el backend ignora.",
        "",
    ]
    for fixture in fixtures:
        origin = (
            f"el estado final de `{fixture.starts_from}.sse`"
            if fixture.starts_from
            else "`{messages: [], purchase: null}`"
        )
        lines += [
            f"## `{fixture.name}.sse`",
            "",
            fixture.description,
            "",
            f"- Estado enviado: {origin}.",
            "- Comandos:",
            "",
            "```json",
            json.dumps(fixture.commands, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    return "\n".join(lines)


def fixture_files(workdir: Path) -> dict[str, str]:
    fixtures = render_fixtures(workdir)
    files = {f"{fixture.name}.sse": fixture.body for fixture in fixtures}
    files["README.md"] = fixtures_readme(fixtures)
    return files
