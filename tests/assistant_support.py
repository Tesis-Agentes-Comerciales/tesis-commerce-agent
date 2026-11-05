"""Deterministic harness for `/assistant`: synthetic feed, scripted router and ACP merchant.

Shared by tests/test_assistant_transport.py and scripts/export_assistant_fixtures.py. All data is
synthetic and aligned with ui/src/storybook/fixtures.ts.
"""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from commerce_agent.acp.feed import ProductCandidate
from commerce_agent.api import create_app
from commerce_agent.buyer_auth import BuyerAuthenticator, BuyerPrincipal
from commerce_agent.config import Settings
from commerce_agent.domain import PurchaseIntent, RankedCandidate
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


SHIPPING_MINOR = 25_000_00


def feed_items() -> dict[str, dict[str, Any]]:
    """Each variant of the synthetic feed as the ACP item a checkout line names."""
    items: dict[str, dict[str, Any]] = {}
    for line in (ASSISTANT_FEED / "products.jsonl").read_text(encoding="utf-8").splitlines():
        for variant in json.loads(line)["variants"]:
            items[variant["id"]] = {
                "id": variant["id"],
                "name": variant["title"],
                "unit_amount": variant["price"]["amount"],
            }
    return items


def _checkout(
    status: str,
    *,
    with_details: bool,
    checkout_id: str = CHECKOUT_ID,
    variant_id: str = "SON-XM6-BLK",
) -> dict[str, Any]:
    item = feed_items()[variant_id]
    payload: dict[str, Any] = {
        "id": checkout_id,
        "protocol": {"version": "2026-04-17"},
        "status": status,
        "currency": "cop",
        "capabilities": {"payment": {"handlers": []}},
        "line_items": [
            {
                "id": "li_1",
                "item": item,
                "quantity": 1,
                "totals": [
                    {"type": "total", "display_text": "Artículo", "amount": item["unit_amount"]}
                ],
            }
        ],
        "totals": [
            {
                "type": "total",
                "display_text": "Total",
                "amount": item["unit_amount"] + SHIPPING_MINOR,
            }
        ],
        "fulfillment_options": [
            {
                "type": "shipping",
                "id": "ship_1",
                "title": "Envío simulado a Bogotá",
                "earliest_delivery_time": "2099-09-22T13:00:00Z",
                "latest_delivery_time": "2099-09-22T13:00:00Z",
                "totals": [{"type": "total", "display_text": "Envío", "amount": SHIPPING_MINOR}],
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


@dataclass
class Merchant:
    """In-memory ACP merchant served through httpx.MockTransport.

    Each creation opens a new session (`cs_storybook_1`, `cs_storybook_2`…), so a purchase asked
    for again after canceling gets its own checkout, as with the real merchant. The session's
    line is the variant the creation asked for (p0-ux08-ux10.md §5.3), named and priced as in
    the synthetic feed.
    """

    fail_checkout: bool = False
    requests: list[str] = field(default_factory=list)
    status: str = "ready_for_payment"
    created: int = 0
    # None leaves it out of the order, as a merchant that only sends the order ID.
    order_number: str | None = ORDER_NUMBER
    variant_id: str = "SON-XM6-BLK"

    @property
    def checkout_id(self) -> str:
        return CHECKOUT_ID if self.created <= 1 else f"cs_storybook_{self.created}"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

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
        if request.method == "POST" and path == "/checkout_sessions":
            self.created += 1
            self.status = "ready_for_payment"
            self.variant_id = json.loads(request.content)["line_items"][0]["id"]
            created = _checkout(
                "not_ready_for_payment",
                with_details=False,
                checkout_id=self.checkout_id,
                variant_id=self.variant_id,
            )
            return httpx.Response(201, json=created)
        checkout_path = f"/checkout_sessions/{self.checkout_id}"
        if path == checkout_path and request.method in {"GET", "POST"}:
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

    def _current(self) -> dict[str, Any]:
        return deepcopy(
            _checkout(
                self.status,
                with_details=True,
                checkout_id=self.checkout_id,
                variant_id=self.variant_id,
            )
        )


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
