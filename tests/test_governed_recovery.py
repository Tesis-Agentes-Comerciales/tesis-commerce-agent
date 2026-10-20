"""Governed recovery (B2, S02): explain rejected selections instead of technical errors.

Router doubles only (never a real model), real SQLite in tmp_path and the merchant behind
httpx.MockTransport. Off, shadow and the blocked flow must keep B0, its 502 and 409 included.
"""

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import SecretStr

from commerce_agent.acp import ACPClient, load_product_feed
from commerce_agent.acp.feed import ProductCandidate
from commerce_agent.api import create_app, error_response
from commerce_agent.application import PurchaseService
from commerce_agent.application.conversation import (
    ConversationService,
    ConversationTurnRequest,
    turn_run_id,
)
from commerce_agent.application.governed_recovery import (
    GovernedRecovery,
    HpRecoveryEvidence,
    RecoveryContext,
    governed_recovery_enabled,
)
from commerce_agent.application.purchase import CheckoutDetails, PurchaseServiceError
from commerce_agent.approval import ApprovalService
from commerce_agent.config import Settings
from commerce_agent.domain import HardConstraints, OptionConstraint, PurchaseIntent, RankedCandidate
from commerce_agent.governance.post_create import PostCreateFlow
from commerce_agent.llm import (
    CandidateDecision,
    CatalogRecovery,
    CommerceDecision,
    ConversationMessage,
    UnknownVariantSelection,
)
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.hp_evidence_sqlite import SqliteHpEvidenceStore
from tests.governance.test_hp_continuation import PAYMENT_TOKEN, StoreMerchant
from tests.governance.test_runtime_dispatch import governed
from tests.governance.test_shadow_integration import configuration
from tests.test_api import StubConversationRouter, checkout_details
from tests.test_selection import feed_with, variant

PHONES_PATH = Path(__file__).parent / "fixtures" / "phones_feed"
PHONES = load_product_feed(PHONES_PATH)
MERCHANT = "http://127.0.0.1:4120"
DISCOVERY = {
    "protocol": {"name": "acp", "version": "2026-04-17", "supported_versions": ["2026-04-17"]},
    "api_base_url": MERCHANT,
    "transports": ["rest"],
    "capabilities": {
        "services": ["checkout"],
        "supported_currencies": ["cop"],
        "supported_locales": ["es-CO"],
    },
}
# The S02 request of evaluation/data/pilot-scenarios.json.
S02_REQUEST = (
    "Quiero exactamente el Google Pixel 8 de 128 GB negro, pero no puedo gastar mas de "
    "1.000.000 COP y no acepto otro modelo."
)
PIXEL_8 = "BBGooglePixel8-128GB"
PIXEL_8A = "BBGooglePixel8a-128GB"
CHECKOUT_ID = "cs_pixel_1"
S02_RUN_ID = turn_run_id("turn-s02")
OBSERVED_PRICES = {PIXEL_8: 154_000_000, PIXEL_8A: 89_900_000}
MODEL_RECOVERY = "Puedo mostrarte otras opciones del catálogo."
# Codes, identifiers and traces never reach the buyer: Spanish prose has no snake_case.
INTERNAL_DETAILS = re.compile(r"[A-Za-z]+_[A-Za-z_]+|BBGoogle|Traceback|Error|variant")
# What B0 answers when the recovery cannot leave evidence.
B0_UNKNOWN_VARIANT = (
    502,
    {"code": "intent_model_error", "message": "The candidate model selected an unknown variant"},
)
B0_INVALID_CANDIDATE = (
    409,
    {"code": "invalid_candidate", "message": "Selected candidate does not satisfy the request"},
)
B0_BUDGET_EXCEEDED = (
    409,
    {"code": "budget_exceeded", "message": "Authoritative checkout total exceeds the budget"},
)
EVIDENCE_UNAVAILABLE = "governed_recovery_evidence_unavailable"


def s02_intent(*, max_unit: int | None = None, max_total: int | None = None) -> PurchaseIntent:
    return PurchaseIntent(
        hard=HardConstraints(
            currency="COP",
            max_unit_amount_minor=max_unit,
            max_total_amount_minor=max_total,
            categories=("smartphones",),
            required_terms=("pixel 8",),
            required_options=(
                OptionConstraint(name="color", value="negro"),
                OptionConstraint(name="almacenamiento", value="128 gb"),
            ),
        )
    )


def pick_pixel_8() -> CandidateDecision:
    return CandidateDecision(
        action="purchase",
        assistant_message="Elegí el Google Pixel 8 que pediste.",
        selected_variant_id=PIXEL_8,
    )


class S02Router:
    """Router double: the model insists on the exact Pixel 8 that the budget rejected."""

    def __init__(
        self, intent: PurchaseIntent, selection: CandidateDecision | Exception | None = None
    ) -> None:
        self.intent = intent
        self.selection = selection
        self.calls: list[str] = []

    def route(self, messages: tuple[ConversationMessage, ...]) -> CommerceDecision:
        self.calls.append("route")
        return CommerceDecision(
            action="purchase",
            assistant_message="Buscaré ese teléfono.",
            purchase_request=S02_REQUEST,
            intent=self.intent,
        )

    def choose_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        candidates: tuple[RankedCandidate, ...],
    ) -> CandidateDecision:
        self.calls.append("choose_candidate")
        if isinstance(self.selection, Exception):
            raise self.selection
        assert self.selection is not None
        return self.selection

    def recover_no_candidate(
        self,
        messages: tuple[ConversationMessage, ...],
        intent: PurchaseIntent,
        rejection_counts: dict[str, int],
        alternatives: tuple[ProductCandidate, ...],
    ) -> CatalogRecovery:
        self.calls.append("recover_no_candidate")
        return CatalogRecovery(action="clarify", assistant_message=MODEL_RECOVERY)


def pixel_checkout(status: str) -> dict[str, Any]:
    return {
        "id": CHECKOUT_ID,
        "protocol": {"version": "2026-04-17"},
        "status": status,
        "currency": "cop",
        "capabilities": {"payment": {"handlers": []}},
        "line_items": [
            {
                "id": "li_1",
                "item": {
                    "id": PIXEL_8,
                    "name": "Google Pixel 8 128 GB negro",
                    "unit_amount": 154_000_000,
                },
                "quantity": 1,
                "totals": [{"type": "total", "display_text": "Artículo", "amount": 154_000_000}],
            }
        ],
        "totals": [{"type": "total", "display_text": "Total", "amount": 156_500_000}],
        "fulfillment_options": [
            {
                "type": "shipping",
                "id": "ship_1",
                "title": "Envío simulado a Bogotá",
                "earliest_delivery_time": "2099-09-22T13:00:00Z",
                "latest_delivery_time": "2099-09-22T13:00:00Z",
                "totals": [{"type": "total", "display_text": "Envío", "amount": 2_500_000}],
            }
        ],
        "selected_fulfillment_options": [],
        "messages": [],
        "links": [],
        "created_at": "2026-10-20T11:00:00Z",
        "updated_at": "2026-10-20T11:00:00Z",
        "expires_at": "2099-10-20T13:00:00Z",
    }


class PhoneMerchant:
    """ACP merchant whose authoritative Pixel 8 total exceeds the S02 budget."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = (request.method, request.url.path)
        self.requests.append(route)
        if route == ("GET", "/.well-known/acp.json"):
            return httpx.Response(200, json=DISCOVERY)
        if route == ("POST", "/checkout_sessions"):
            return httpx.Response(201, json=pixel_checkout("not_ready_for_payment"))
        if route == ("POST", f"/checkout_sessions/{CHECKOUT_ID}/cancel"):
            return httpx.Response(200, json=pixel_checkout("canceled"))
        raise AssertionError(f"Unexpected merchant request: {route}")

    @property
    def checkout_posts(self) -> int:
        return self.requests.count(("POST", "/checkout_sessions"))


def hp_recovery_evidence(tmp_path: Path, *, available: bool = True) -> HpRecoveryEvidence:
    """The runtime sink on the agent database; without `initialize` every write fails."""
    store = SqliteHpEvidenceStore(tmp_path / "agent.db")
    if available:
        store.initialize()
    return HpRecoveryEvidence(store)


def recorded_recoveries(tmp_path: Path) -> list[dict[str, Any]]:
    """Every `governed_recovery` row of hp_evidence; none when the table was never created."""
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection:
        table = "SELECT 1 FROM sqlite_master WHERE name = 'hp_evidence'"
        if connection.execute(table).fetchone() is None:
            return []
        rows = connection.execute(
            "SELECT body FROM hp_evidence WHERE record_kind = 'governed_recovery' "
            "ORDER BY run_id, sequence"
        ).fetchall()
    return [json.loads(body) for (body,) in rows]


def lose_hp_evidence(tmp_path: Path) -> None:
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection:
        connection.execute("DROP TABLE hp_evidence")


def assert_evidence_unavailable_logged(caplog: pytest.LogCaptureFixture) -> None:
    (warning,) = [record for record in caplog.records if EVIDENCE_UNAVAILABLE in record.message]
    assert warning.levelname == "WARNING"
    assert "Quiero" not in warning.message and "buyer@" not in warning.message


class StepLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def reasoning_delta(self, delta: str) -> None:
        del delta

    def step_started(self, step: str, args: dict[str, Any]) -> None:
        self.events.append(("started", step))

    def step_finished(self, step: str, result: dict[str, Any]) -> None:
        self.events.append(("finished", step))

    def step_failed(self, step: str, error: dict[str, str]) -> None:
        self.events.append(("failed", step))


@contextmanager
def api(
    tmp_path: Path, router: S02Router, *, mode: str, flow: str = "blocked"
) -> Iterator[tuple[TestClient, PhoneMerchant]]:
    merchant = PhoneMerchant()
    settings = configuration(
        tmp_path / "agent.db", mode=mode, agent_hp_flow=flow, agent_feed_path=PHONES_PATH
    )
    app = create_app(
        settings=settings,
        transport=httpx.MockTransport(merchant),
        payment_transport=httpx.MockTransport(merchant),
        conversation_router=router,
    )
    with TestClient(app) as client:
        yield client, merchant


@contextmanager
def conversation(
    tmp_path: Path, router: S02Router, merchant: PhoneMerchant, evidence: HpRecoveryEvidence
) -> Iterator[ConversationService]:
    """The service `/conversation/turn` and `/assistant` share, with recovery on and no D1."""
    store = SqliteAgentStore(tmp_path / "agent.db")
    store.initialize()
    client = ACPClient(
        MERCHANT, SecretStr("merchant-token-for-tests"), transport=httpx.MockTransport(merchant)
    )
    approvals = ApprovalService(
        store, SecretStr("approval-secret-with-at-least-32-characters"), merchant_origin=MERCHANT
    )
    purchases = PurchaseService(feed=PHONES, checkout=client, store=store, approvals=approvals)
    graph = sqlite3.connect(tmp_path / "agent.graph.db", check_same_thread=False)
    with closing(graph), closing(client):
        yield ConversationService(
            feed=PHONES,
            store=store,
            purchases=purchases,
            completion=None,
            discover=client.discover,
            router=router,
            checkpointer=SqliteSaver(graph),
            recovery=GovernedRecovery(evidence),
        )


def s02_body(turn_id: str = "turn-s02") -> dict[str, Any]:
    return {
        "turn_id": turn_id,
        "conversation_id": "conversation-s02",
        "messages": [{"role": "user", "content": S02_REQUEST}],
        "details": checkout_details(),
    }


def purchase_rows(tmp_path: Path) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection:
        return connection.execute("SELECT stage, checkout_id FROM purchases").fetchall()


def assert_explains(message: str, *causes: str) -> None:
    assert INTERNAL_DETAILS.search(message) is None, message
    for cause in causes:
        assert cause in message, message


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# Governed: AGENT_GOVERNANCE_MODE=enforce and AGENT_HP_FLOW=governed.


@pytest.mark.parametrize(
    "selection",
    [pick_pixel_8(), UnknownVariantSelection(PIXEL_8)],
    ids=["router-returns-it", "openai-router-raises"],
)
def test_governed_s02_explains_a_choice_outside_the_shortlist_without_checkout(
    tmp_path: Path, selection: CandidateDecision | Exception
) -> None:
    router = S02Router(s02_intent(max_unit=100_000_000), selection)
    with api(tmp_path, router, mode="enforce", flow="governed") as (client, merchant):
        response = client.post("/conversation/turn", json=s02_body())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "respond"
    assert body["purchase"] is None and body["approval_terms"] is None
    assert_explains(
        body["assistant_message"],
        "Google Pixel 8 128 GB negro",
        "COP 1.540.000",
        "presupuesto de COP 1.000.000",
        "No preparé ningún checkout",
    )
    assert merchant.requests == []
    assert purchase_rows(tmp_path) == []
    (recorded,) = recorded_recoveries(tmp_path)
    assert (recorded["control"], recorded["kind"], recorded["run_id"]) == (
        "application",
        "selection_outside_shortlist",
        S02_RUN_ID,
    )
    assert recorded["explanation_sha256"] == sha256(body["assistant_message"])
    assert SqliteHpEvidenceStore(tmp_path / "agent.db").verify_chain(S02_RUN_ID)


@pytest.mark.parametrize(
    ("selection", "b0"),
    [
        (pick_pixel_8(), B0_INVALID_CANDIDATE),
        (UnknownVariantSelection(PIXEL_8), B0_UNKNOWN_VARIANT),
    ],
    ids=["router-returns-it", "openai-router-raises"],
)
def test_without_evidence_a_choice_outside_the_shortlist_answers_as_b0(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    selection: CandidateDecision | Exception,
    b0: tuple[int, dict[str, str]],
) -> None:
    router = S02Router(s02_intent(max_unit=100_000_000), selection)
    with api(tmp_path, router, mode="enforce", flow="governed") as (client, merchant):
        lose_hp_evidence(tmp_path)
        response = client.post("/conversation/turn", json=s02_body())

    assert (response.status_code, response.json()) == b0
    assert merchant.requests == []
    assert purchase_rows(tmp_path) == []
    assert_evidence_unavailable_logged(caplog)


def test_without_evidence_no_candidate_keeps_the_model_recovery(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    router = S02Router(s02_intent(max_unit=50_000_000))
    with api(tmp_path, router, mode="enforce", flow="governed") as (client, merchant):
        lose_hp_evidence(tmp_path)
        response = client.post("/conversation/turn", json=s02_body())

    assert response.status_code == 200
    assert response.json()["action"] == "clarify"
    assert response.json()["assistant_message"] == MODEL_RECOVERY
    assert router.calls == ["route", "recover_no_candidate"]
    assert merchant.requests == []
    assert_evidence_unavailable_logged(caplog)


def test_governed_no_candidate_is_explained_without_calling_the_model(tmp_path: Path) -> None:
    router = S02Router(s02_intent(max_unit=50_000_000))
    with api(tmp_path, router, mode="enforce", flow="governed") as (client, merchant):
        response = client.post("/conversation/turn", json=s02_body())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "respond" and body["purchase"] is None
    assert_explains(
        body["assistant_message"],
        "Google Pixel 8a 128 GB negro",
        "COP 899.000",
        "presupuesto de COP 500.000",
    )
    assert router.calls == ["route"]
    assert merchant.requests == []
    assert [item["kind"] for item in recorded_recoveries(tmp_path)] == ["no_candidate"]


def test_governed_budget_exceeded_after_checkout_declares_the_cancellation(
    tmp_path: Path,
) -> None:
    # The shared conversation service without D1; the D1 path is covered through the API below.
    merchant = PhoneMerchant()
    router = S02Router(s02_intent(max_total=100_000_000), pick_pixel_8())
    steps = StepLog()
    with conversation(tmp_path, router, merchant, hp_recovery_evidence(tmp_path)) as service:
        response = service.execute(
            (ConversationMessage(role="user", content=S02_REQUEST),),
            CheckoutDetails.model_validate_json(json.dumps(checkout_details())),
            turn_id="turn-s02",
            conversation_id="conversation-s02",
            observer=steps,
        )

    assert response.action == "respond" and response.purchase is None
    assert_explains(
        response.assistant_message,
        "checkout",
        "Google Pixel 8 128 GB negro",
        "COP 1.540.000",
        "presupuesto de COP 1.000.000",
        "lo cancelé",
        "No se hizo ningún cobro",
    )
    assert merchant.requests == [
        ("GET", "/.well-known/acp.json"),
        ("POST", "/checkout_sessions"),
        ("POST", f"/checkout_sessions/{CHECKOUT_ID}/cancel"),
    ]
    assert purchase_rows(tmp_path) == [("canceled", CHECKOUT_ID)]
    assert steps.events[-2:] == [("started", "prepare_checkout"), ("failed", "prepare_checkout")]


def over_budget_sonora() -> StubConversationRouter:
    """Sonora fits the shortlist at 72.000 but the merchant's total, with shipping, is 74.000."""
    intent = PurchaseIntent(hard=HardConstraints(currency="COP", max_total_amount_minor=73_000))
    return StubConversationRouter(
        CommerceDecision(action="purchase", assistant_message="Preparo.", intent=intent),
        CandidateDecision(
            action="purchase", assistant_message="Elegí Sonora.", selected_variant_id="SON-01"
        ),
    )


def journal_phases(tmp_path: Path) -> list[str]:
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection:
        rows = connection.execute(
            "SELECT body FROM d1_post_create_events ORDER BY version"
        ).fetchall()
    return [json.loads(body)["phase"] for (body,) in rows]


@pytest.mark.parametrize("evidence", ["recorded", "lost"])
def test_governed_budget_cancellation_through_d1_ends_the_journal_canceled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    evidence: str,
) -> None:
    # The D1 checkout is canceled through the post-create journal (`cancel_budget`), which then
    # ends `canceled`; nothing after it may try to mark the journal uncertain.
    failures: list[str] = []
    monkeypatch.setattr(PostCreateFlow, "fail", lambda flow: failures.append(flow.run_id))
    merchant = StoreMerchant()
    app = create_app(
        settings=governed(tmp_path / "agent.db"),
        transport=httpx.MockTransport(merchant),
        payment_transport=httpx.MockTransport(
            lambda request: httpx.Response(201, json=PAYMENT_TOKEN)
        ),
        conversation_router=over_budget_sonora(),
    )
    with TestClient(app) as client:
        if evidence == "lost":
            lose_hp_evidence(tmp_path)
        response = client.post("/conversation/turn", json=s02_body("turn-budget-d1"))

    if evidence == "recorded":
        assert response.status_code == 200, response.text
        assert response.json()["action"] == "respond"
        assert_explains(response.json()["assistant_message"], "lo cancelé", "COP 730")
        (record,) = recorded_recoveries(tmp_path)
        assert (record["kind"], record["checkout_id"]) == (
            "budget_exceeded_after_checkout",
            "cs_api_1",
        )
    else:
        assert (response.status_code, response.json()) == B0_BUDGET_EXCEEDED
        assert_evidence_unavailable_logged(caplog)
    assert merchant.effects() == [("POST", "/checkout_sessions/cs_api_1/cancel")]
    assert purchase_rows(tmp_path) == [("canceled", "cs_api_1")]
    assert journal_phases(tmp_path)[-3:] == ["intent", "observed", "canceled"]
    assert failures == []


def test_a_retried_turn_records_its_recovery_once(tmp_path: Path) -> None:
    class FailsOnce(StepLog):
        """The turn fails after the recovery was recorded; the client retries the same turn."""

        def step_finished(self, step: str, result: dict[str, Any]) -> None:
            if step == "select_product" and not self.events.count(("finished", step)):
                self.events.append(("finished", step))
                raise ConnectionResetError("stream closed")
            super().step_finished(step, result)

    router = S02Router(s02_intent(max_unit=100_000_000), pick_pixel_8())
    observer = FailsOnce()
    evidence = hp_recovery_evidence(tmp_path)
    with conversation(tmp_path, router, PhoneMerchant(), evidence) as service:
        turn = {
            "messages": (ConversationMessage(role="user", content=S02_REQUEST),),
            "details": CheckoutDetails.model_validate_json(json.dumps(checkout_details())),
            "turn_id": "turn-s02",
            "conversation_id": "conversation-s02",
            "observer": observer,
        }
        with pytest.raises(ConnectionResetError):
            service.execute(**turn)
        retried = service.execute(**turn)

    assert retried.action == "respond"
    (record,) = recorded_recoveries(tmp_path)
    assert record["explanation_sha256"] == sha256(retried.assistant_message)
    assert SqliteHpEvidenceStore(tmp_path / "agent.db").verify_chain(S02_RUN_ID)


def test_a_budget_without_currency_is_explained_by_the_currency_not_the_price(
    tmp_path: Path,
) -> None:
    # A budget without currency meets COP and USD prices: the shortlist cuts every product.
    feed = feed_with(
        variant("COP-1", amount=900_000), variant("USD-1", amount=20_000, currency="USD")
    )
    intent = PurchaseIntent(hard=HardConstraints(max_unit_amount_minor=500_000))
    context = RecoveryContext(run_id="run-1", feed=feed, intent=intent)

    message = GovernedRecovery(hp_recovery_evidence(tmp_path)).no_candidate(context)

    assert_explains(message, "no indica la moneda", "varias monedas", "No preparé ningún checkout")
    assert "supera tu presupuesto" not in message
    (record,) = recorded_recoveries(tmp_path)
    assert (record["cause"], record["variant_id"], record["currency"]) == ("currency", None, None)


def test_without_evidence_budget_exceeded_after_checkout_keeps_the_409_of_b0(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    merchant = PhoneMerchant()
    router = S02Router(s02_intent(max_total=100_000_000), pick_pixel_8())
    evidence = hp_recovery_evidence(tmp_path, available=False)
    with (
        conversation(tmp_path, router, merchant, evidence) as service,
        pytest.raises(PurchaseServiceError) as raised,
    ):
        service.respond(ConversationTurnRequest.model_validate_json(json.dumps(s02_body())))

    rejection = error_response(raised.value)
    assert (rejection.status_code, json.loads(bytes(rejection.body))) == B0_BUDGET_EXCEEDED
    assert ("POST", f"/checkout_sessions/{CHECKOUT_ID}/cancel") in merchant.requests
    assert purchase_rows(tmp_path) == [("canceled", CHECKOUT_ID)]
    assert_evidence_unavailable_logged(caplog)


def test_budget_recovery_declares_the_cancellation_without_a_stored_checkout_id(
    tmp_path: Path,
) -> None:
    # The cancellation happened before the recovery runs, whatever the purchase row kept.
    context = RecoveryContext(run_id="run-1", feed=PHONES, intent=s02_intent(max_total=100_000_000))

    message = GovernedRecovery(hp_recovery_evidence(tmp_path)).budget_exceeded(
        context, PIXEL_8, None
    )

    assert_explains(message, "lo cancelé", "No se hizo ningún cobro")
    assert "No preparé ningún checkout" not in message


@pytest.mark.parametrize(
    ("intent", "selection", "kind", "variant_id", "checkout_id"),
    [
        (s02_intent(max_unit=50_000_000), None, "no_candidate", PIXEL_8A, None),
        (
            s02_intent(max_unit=100_000_000),
            pick_pixel_8(),
            "selection_outside_shortlist",
            PIXEL_8,
            None,
        ),
        (
            s02_intent(max_total=100_000_000),
            pick_pixel_8(),
            "budget_exceeded_after_checkout",
            PIXEL_8,
            CHECKOUT_ID,
        ),
    ],
)
def test_each_governed_recovery_is_recorded_as_an_application_control(
    tmp_path: Path,
    intent: PurchaseIntent,
    selection: CandidateDecision | None,
    kind: str,
    variant_id: str,
    checkout_id: str | None,
) -> None:
    router = S02Router(intent, selection)
    evidence = hp_recovery_evidence(tmp_path)
    with conversation(tmp_path, router, PhoneMerchant(), evidence) as service:
        response = service.respond(
            ConversationTurnRequest.model_validate_json(json.dumps(s02_body()))
        )

    (record,) = recorded_recoveries(tmp_path)
    assert (record["event"], record["control"]) == ("governed_recovery", "application")
    assert (record["kind"], record["cause"], record["variant_id"]) == (kind, "budget", variant_id)
    assert record["checkout_id"] == checkout_id
    assert record["run_id"] == S02_RUN_ID
    assert record["currency"] == "COP"
    assert record["budget_minor"] == (
        intent.hard.max_unit_amount_minor or intent.hard.max_total_amount_minor
    )
    assert record["observed_unit_amount_minor"] == OBSERVED_PRICES[variant_id]
    assert record["intent_sha256"] == sha256(intent.model_dump_json())
    assert record["explanation_sha256"] == sha256(response.assistant_message)
    assert SqliteHpEvidenceStore(tmp_path / "agent.db").verify_chain(S02_RUN_ID)
    # Digests stand in for the buyer's words; checkout details never reach the evidence.
    recorded = json.dumps(record)
    assert "Quiero" not in recorded and "buyer@example.test" not in recorded
    assert "Calle 100" not in recorded and "+573000000000" not in recorded


@pytest.mark.parametrize(
    ("intent", "variant_id", "causes"),
    [
        (
            s02_intent(max_unit=100_000_000),
            "BBGooglePixel9-128GB",
            ("Google Pixel 9", "no está disponible en este momento"),
        ),
        (
            s02_intent(max_unit=100_000_000),
            "NOT-IN-THE-FEED",
            ("no está disponible en el catálogo",),
        ),
        (
            s02_intent(max_unit=100_000_000),
            PIXEL_8A,
            ("Google Pixel 8a", "no estaba entre las opciones"),
        ),
        (
            PurchaseIntent(
                hard=HardConstraints(
                    currency="COP",
                    required_options=(OptionConstraint(name="color", value="blanco"),),
                )
            ),
            PIXEL_8,
            ("Google Pixel 8", "no tiene las características que pediste", "color blanco"),
        ),
    ],
    ids=["out-of-stock", "not-in-catalog", "valid-but-not-shortlisted", "hard-option"],
)
def test_a_rejected_choice_is_explained_with_the_domain_rules(
    tmp_path: Path, intent: PurchaseIntent, variant_id: str, causes: tuple[str, ...]
) -> None:
    context = RecoveryContext(run_id="run-1", feed=PHONES, intent=intent, rejection_counts={})

    message = GovernedRecovery(hp_recovery_evidence(tmp_path)).outside_shortlist(
        context, variant_id
    )

    assert_explains(message, *causes, "No preparé ningún checkout")
    if variant_id == "NOT-IN-THE-FEED":
        assert recorded_recoveries(tmp_path)[0]["variant_id"] is None


@pytest.mark.parametrize(
    ("mode", "flow", "enabled"),
    [
        ("off", "blocked", False),
        ("off", "governed", False),
        ("shadow", "governed", False),
        ("enforce", "blocked", False),
        ("enforce", "governed", True),
    ],
)
def test_recovery_is_enabled_only_for_enforce_with_the_governed_flow(
    mode: str, flow: str, enabled: bool
) -> None:
    settings = Settings.model_validate({"agent_governance_mode": mode, "agent_hp_flow": flow})
    assert governed_recovery_enabled(settings) is enabled


# B0' (off), shadow and the blocked flow: behavior and status codes stay as in B0.


@pytest.mark.parametrize(
    ("mode", "flow"),
    [("off", "blocked"), ("off", "governed"), ("shadow", "governed"), ("enforce", "blocked")],
)
def test_without_the_governed_flow_a_choice_outside_the_shortlist_keeps_the_502(
    tmp_path: Path, mode: str, flow: str
) -> None:
    router = S02Router(s02_intent(max_unit=100_000_000), UnknownVariantSelection(PIXEL_8))
    with api(tmp_path, router, mode=mode, flow=flow) as (client, merchant):
        response = client.post("/conversation/turn", json=s02_body())

    assert (response.status_code, response.json()) == B0_UNKNOWN_VARIANT
    assert merchant.requests == []
    assert recorded_recoveries(tmp_path) == []


def test_off_keeps_refusing_a_returned_outside_choice_at_checkout_preparation(
    tmp_path: Path,
) -> None:
    router = S02Router(s02_intent(max_unit=100_000_000), pick_pixel_8())
    with api(tmp_path, router, mode="off") as (client, merchant):
        response = client.post("/conversation/turn", json=s02_body())

    assert (response.status_code, response.json()) == B0_INVALID_CANDIDATE
    assert merchant.checkout_posts == 0
    assert recorded_recoveries(tmp_path) == []


@pytest.mark.parametrize(
    ("mode", "flow"), [("off", "governed"), ("shadow", "governed"), ("enforce", "blocked")]
)
def test_without_the_governed_flow_no_candidate_keeps_the_model_recovery(
    tmp_path: Path, mode: str, flow: str
) -> None:
    router = S02Router(s02_intent(max_unit=50_000_000))
    with api(tmp_path, router, mode=mode, flow=flow) as (client, _):
        response = client.post("/conversation/turn", json=s02_body())

    assert response.status_code == 200
    assert response.json()["action"] == "clarify"
    assert response.json()["assistant_message"] == MODEL_RECOVERY
    assert router.calls == ["route", "recover_no_candidate"]
    assert recorded_recoveries(tmp_path) == []


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_without_enforce_budget_exceeded_after_checkout_keeps_the_409(
    tmp_path: Path, mode: str
) -> None:
    router = S02Router(s02_intent(max_total=100_000_000), pick_pixel_8())
    with api(tmp_path, router, mode=mode, flow="governed") as (client, merchant):
        response = client.post("/conversation/turn", json=s02_body())

    assert (response.status_code, response.json()) == B0_BUDGET_EXCEEDED
    assert merchant.checkout_posts == 1
    assert ("POST", f"/checkout_sessions/{CHECKOUT_ID}/cancel") in merchant.requests
    assert purchase_rows(tmp_path) == [("canceled", CHECKOUT_ID)]
    assert recorded_recoveries(tmp_path) == []
