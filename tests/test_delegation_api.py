"""Delegation API (docs/design/hnp-governed.md §5.1, §5.2 without `run`, §5.3, §5.4 and §6).

The app runs on real SQLite in `tmp_path` with the fixture feed, a settable authority clock and
two fake buyers; the task states the executor writes (HNP-09) are driven through the ledger.
"""

import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwcrypto.jwk import JWK
from pydantic import SecretBytes, SecretStr

from commerce_agent import ap2_hnp
from commerce_agent.ap2_hnp import MandateTrust, verify_open_proof
from commerce_agent.api import create_app
from commerce_agent.application.purchase import CheckoutDetails
from commerce_agent.buyer_auth import BuyerPrincipal
from commerce_agent.config import Settings
from commerce_agent.delegation.contract import (
    HNP_MESSAGES,
    DelegationList,
    DelegationProposal,
    DelegationView,
    RevokeAllReceipt,
    RevokeReceipt,
    escalation_message,
)
from commerce_agent.delegation.models import chain_is_valid
from commerce_agent.domain import HardConstraints, PurchaseIntent
from commerce_agent.governance.canonical import canonical_bytes
from commerce_agent.governance.destination import destination_binding
from commerce_agent.governance.hnp_models import parse_consent_scope, scope_digest
from commerce_agent.governance.provenance import ProvenanceKeys, merchant_pseudonym, pseudonym
from commerce_agent.persistence import SqliteAgentStore
from commerce_agent.persistence.delegation_sqlite import SqliteDelegationLedger
from tests.support.delegation import START, AuthorityClock, Flow, receipt

FEED = Path(__file__).parent / "fixtures" / "feed"
MERCHANT = "http://127.0.0.1:4120"
SERVICE_TOKEN = "service-token-" + "s" * 32
OPERATOR_TOKEN = "operator-token-" + "o" * 32
KEYS = ProvenanceKeys(
    key_id="fixture-key",
    pseudonym_key=SecretBytes(bytes.fromhex("1" * 64)),
    encryption_key=SecretBytes(bytes.fromhex("2" * 64)),
)


def _pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


# Fixture-only keys, generated per test session.
PROVIDER_PEM, AGENT_PEM = _pem(), _pem()


class Answer(Protocol):
    """A TestClient response (starlette's own httpx build, not the `httpx` package)."""

    @property
    def status_code(self) -> int: ...
    @property
    def text(self) -> str: ...
    def json(self) -> Any: ...


def public_jwk(pem: str) -> dict[str, Any]:
    return JWK.from_pem(pem.encode()).export_public(as_dict=True)


class TwoBuyers:
    def authenticate(self, authorization: str | None) -> BuyerPrincipal | None:
        buyers = {"Bearer buyer-a": "buyer-a", "Bearer buyer-b": "buyer-b"}
        return BuyerPrincipal(buyers[authorization]) if authorization in buyers else None


def buyer(name: str = "buyer-a") -> dict[str, str]:
    return {"Authorization": f"Bearer {name}"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def hnp_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "merchant_bearer_token": SecretStr("merchant-token"),
        "merchant_webhook_secret": SecretStr("webhook-secret-with-at-least-32-characters"),
        "agent_approval_secret": SecretStr("approval-secret-with-at-least-32-characters"),
        "payment_agent_bearer_token": SecretStr("payment-agent-token-with-at-least-32-characters"),
        "agent_database_path": tmp_path / "agent.db",
        "agent_feed_source": "file",
        "agent_feed_path": FEED,
        "agent_auth_mode": "jwt",
        "agent_auth_issuer": "http://127.0.0.1:5173",
        "agent_ap2_required": True,
        "agent_ap2_private_key_pem": SecretStr(PROVIDER_PEM),
        "agent_hnp_enabled": True,
        "agent_hnp_agent_key_pem": SecretStr(AGENT_PEM),
        "agent_hnp_service_token": SecretStr(SERVICE_TOKEN),
        "agent_hnp_operator_token": SecretStr(OPERATOR_TOKEN),
        "agent_d1_key_id": "fixture-key",
        "agent_d1_hmac_key_hex": SecretStr("1" * 64),
        "agent_d1_aes_key_hex": SecretStr("2" * 64),
    }
    return Settings(**(values | overrides))


def build(tmp_path: Path, clock: AuthorityClock, **overrides: Any) -> FastAPI:
    def unexpected_request(_: httpx.Request) -> httpx.Response:
        raise AssertionError("The delegation API must not contact the merchant")

    return create_app(
        settings=hnp_settings(tmp_path, **overrides),
        transport=httpx.MockTransport(unexpected_request),
        authority_clock=clock,
        buyer_authenticator=TwoBuyers(),
    )


@pytest.fixture
def clock() -> AuthorityClock:
    return AuthorityClock()


@pytest.fixture
def client(tmp_path: Path, clock: AuthorityClock) -> Iterator[TestClient]:
    with TestClient(build(tmp_path, clock)) as client:
        yield client


@pytest.fixture
def ledger(tmp_path: Path, clock: AuthorityClock, client: TestClient) -> SqliteDelegationLedger:
    del client  # the app created the tables
    return SqliteDelegationLedger(tmp_path / "agent.db", clock=clock)


def details() -> dict[str, Any]:
    return {
        "buyer": {"email": "buyer@example.test", "first_name": "Buyer", "last_name": "Test"},
        "fulfillment": {
            "name": "Buyer Test",
            "email": "buyer@example.test",
            "phone_number": "+573000000000",
            "address": {
                "name": "Buyer Test",
                "line_one": "Calle 100 # 10-20",
                "city": "Bogota",
                "state": "DC",
                "country": "CO",
                "postal_code": "110111",
            },
        },
    }


def proposal_body(**changes: Any) -> dict[str, Any]:
    body = {
        "variant_ids": ["SON-01"],
        "total_max_minor": 150_000,
        "currency": "COP",
        "valid_for_seconds": 3_600,
        "details": details(),
        "preference_text": "Solo si llega esta semana.",
    }
    return body | changes


def propose(client: TestClient, who: str = "buyer-a", **changes: Any) -> Answer:
    return client.post("/delegations/proposals", json=proposal_body(**changes), headers=buyer(who))


def consent(client: TestClient, proposal: dict[str, Any], who: str = "buyer-a") -> Answer:
    body = {"proposal_id": proposal["proposal_id"], "scope_digest": proposal["scope_digest"]}
    return client.post("/delegations", json=body, headers=buyer(who))


def delegated(client: TestClient, **changes: Any) -> dict[str, Any]:
    response = consent(client, propose(client, **changes).json())
    assert response.status_code == 201, response.text
    return response.json()


def flow(ledger: SqliteDelegationLedger, clock: AuthorityClock, view: dict[str, Any]) -> Flow:
    return Flow(ledger, clock, view["task"]["task_id"], view["grant_id"])


def error(response: Answer, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"code", "message"} and body["code"] == code and body["message"]
    return body


def view_of(client: TestClient, grant_id: str) -> DelegationView:
    response = client.get(f"/delegations/{grant_id}", headers=buyer())
    assert response.status_code == 200, response.text
    return DelegationView.model_validate_json(response.text)


# Flag off (§5, §6): nothing of HNP is mounted.

ROUTES = (
    ("POST", "/delegations/proposals"),
    ("POST", "/delegations"),
    ("GET", "/me/delegations"),
    ("GET", "/delegations/dg_1"),
    ("POST", "/delegations/dg_1/revoke"),
    ("POST", "/me/delegations/revoke-all"),
    ("GET", "/internal/hnp/tasks/ht_1"),
    ("POST", "/internal/hnp/kill-switch"),
)


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_without_the_flag_no_delegation_route_exists(
    tmp_path: Path, clock: AuthorityClock, method: str, path: str
) -> None:
    with TestClient(build(tmp_path, clock, agent_hnp_enabled=False)) as client:
        response = client.request(method, path, json={}, headers=bearer(OPERATOR_TOKEN))
        health = client.get("/health").json()

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert not any(name.startswith("hnp_") for name in health)
    with closing(sqlite3.connect(tmp_path / "agent.db")) as connection:
        tables = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'delegation_grants'"
        ).fetchall()
    assert tables == []


# Proposal (§3.2, §4.2, §5.1)


def test_a_proposal_shows_the_exact_scope_and_binds_its_digest(
    client: TestClient, ledger: SqliteDelegationLedger
) -> None:
    response = propose(client)

    assert response.status_code == 201, response.text
    proposal = DelegationProposal.model_validate_json(response.text)
    assert proposal.presented_at == "2026-10-21T15:00:00.000Z"
    assert proposal.expires_at == "2026-10-21T15:10:00.000Z"  # AGENT_HNP_PROPOSAL_TTL_SECONDS
    assert response.json()["scope"] == {
        "merchant_name": "Synthetic Commerce Merchant",
        "variants": [{"variant_id": "SON-01", "title": "Audifonos Sonora SON-01"}],
        "total_max": {"minor": 150_000, "currency": "COP"},
        "expires_at": "2026-10-21T16:00:00.000Z",
        "destination_label": "Bogota, DC",
        "instrument_label": "Tarjeta de prueba del sandbox",
        "uses": 1,
        "preference_text": "Solo si llega esta semana.",
    }
    assert "Calle 100" not in response.text and "buyer@example.test" not in response.text

    stored = ledger.proposal("buyer-a", proposal.proposal_id)
    assert stored is not None
    scope = parse_consent_scope(stored.scope_json.encode())
    assert scope_digest(scope) == stored.scope_digest == proposal.scope_digest
    assert scope.owner_ref == "owner:" + pseudonym(KEYS, "owner", b"buyer-a")
    assert scope.scope.merchant_refs == (merchant_pseudonym(KEYS, MERCHANT),)
    assert scope.scope.variant_ids == ("SON-01",)
    assert scope.display.model_dump(mode="json") == response.json()["scope"]
    stored_details = CheckoutDetails.model_validate_json(stored.checkout_details_json)
    assert stored_details == CheckoutDetails.model_validate_json(json.dumps(details()))


def test_the_destination_binding_is_the_one_d2_computes_over_the_merchant_echo(
    client: TestClient, ledger: SqliteDelegationLedger
) -> None:
    proposal = propose(client).json()

    stored = ledger.proposal("buyer-a", proposal["proposal_id"])
    assert stored is not None
    binding = parse_consent_scope(stored.scope_json.encode()).scope.destination_binding
    # K8: POL-08 compares (scheme, key_id, value) with D2's MaterialTerms over the checkout's
    # fulfillment_details, which the merchant echoes as the agent sent them.
    expected = destination_binding(KEYS, "any", details()["fulfillment"])
    assert (binding.scheme, binding.key_id, binding.value) == (
        expected["scheme"],
        expected["key_id"],
        expected["value"],
    )
    assert binding.binding_id == f"destination:{proposal['proposal_id']}"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"variant_ids": ["SON-99"]}, "delegation_variant_unavailable"),
        ({"variant_ids": ["SON-01", "SON-01"]}, "delegation_request_invalid"),
        ({"variant_ids": [f"SON-0{n}" for n in range(1, 7)]}, "delegation_request_invalid"),
        ({"variant_ids": []}, "delegation_request_invalid"),
        ({"total_max_minor": 0}, "delegation_request_invalid"),
        ({"total_max_minor": 150_000.0}, "delegation_request_invalid"),
        ({"currency": "USD"}, "delegation_request_invalid"),
        ({"valid_for_seconds": 59}, "delegation_request_invalid"),
        ({"valid_for_seconds": 86_401}, "delegation_validity_exceeded"),
        ({"preference_text": "x" * 281}, "delegation_request_invalid"),
        ({"preference_text": ""}, "delegation_request_invalid"),
        ({"run_id": "run_1"}, "delegation_request_invalid"),
        ({"details": {"buyer": {"email": "a@b.c"}}}, "delegation_request_invalid"),
    ],
)
def test_a_proposal_outside_the_contract_is_rejected(
    client: TestClient, changes: dict[str, Any], code: str
) -> None:
    error(propose(client, **changes), 422, code)
    assert client.get("/me/delegations", headers=buyer()).json() == {"delegations": []}


def test_a_body_that_is_not_json_is_an_invalid_request(client: TestClient) -> None:
    response = client.post(
        "/delegations/proposals",
        content=b"{not json",
        headers=buyer() | {"Content-Type": "application/json"},
    )
    error(response, 422, "delegation_request_invalid")


def test_a_variant_priced_in_another_currency_is_unavailable(
    tmp_path: Path, clock: AuthorityClock
) -> None:
    feed = tmp_path / "feed"
    shutil.copytree(FEED, feed)
    line = (FEED / "products.jsonl").read_text().splitlines()[0]
    product = json.loads(line.replace("SON-01", "SON-USD").replace("son-01", "son-usd"))
    product["variants"][0]["price"]["currency"] = "USD"
    with (feed / "products.jsonl").open("a") as products:
        products.write(json.dumps(product) + "\n")

    with TestClient(build(tmp_path, clock, agent_feed_path=feed)) as client:
        error(propose(client, variant_ids=["SON-USD"]), 422, "delegation_variant_unavailable")
        assert propose(client, variant_ids=["SON-01", "SON-07"]).status_code == 201


@pytest.mark.parametrize(
    ("method", "path"),
    [route for route in ROUTES if not route[1].startswith("/internal")],
)
def test_buyer_routes_require_the_buyer_jwt(client: TestClient, method: str, path: str) -> None:
    for headers in ({}, bearer(SERVICE_TOKEN), bearer(OPERATOR_TOKEN)):
        assert client.request(method, path, json={}, headers=headers).status_code == 401


# Consent (§3.2 step 4, §4.5 consent)


def test_consent_issues_open_mandates_that_verify_and_schedules_the_task(
    client: TestClient, ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    proposal = propose(client, variant_ids=["SON-07", "SON-01"]).json()
    clock.advance(30)
    response = consent(client, proposal)

    assert response.status_code == 201, response.text
    view = DelegationView.model_validate_json(response.text)
    assert (view.state, view.revoked_reason, view.escalation) == ("active", None, None)
    assert view.consented_at == "2026-10-21T15:00:30.000Z"
    assert view.expires_at == view.scope.expires_at == "2026-10-21T16:00:00.000Z"
    assert view.task.state == "scheduled" and view.task.message == HNP_MESSAGES["task.scheduled"]
    assert view.last_activity_at == view.consented_at
    assert "~" not in response.text and "scope_digest" not in response.text

    grant = ledger.grant("buyer-a", view.grant_id)
    assert grant is not None
    assert grant.agent_key_kid == JWK.from_pem(AGENT_PEM.encode()).thumbprint()
    assert grant.issuer_kid == JWK.from_pem(PROVIDER_PEM.encode()).thumbprint()
    intent = PurchaseIntent(hard=HardConstraints(currency="COP", max_total_amount_minor=150_000))
    assert grant.intent_json == canonical_bytes(intent).decode()
    mandates = {mandate.kind: mandate for mandate in ledger.mandates(view.grant_id)}
    assert set(mandates) == {"open_checkout", "open_payment"}
    assert all(json.loads(m.public_jwk_json) == public_jwk(PROVIDER_PEM) for m in mandates.values())
    trust = MandateTrust(public_jwk(PROVIDER_PEM), public_jwk(AGENT_PEM), MERCHANT)
    proof = verify_open_proof(
        mandates["open_checkout"].token,
        mandates["open_payment"].token,
        trust=trust,
        variant_ids=("SON-01", "SON-07"),
        total_max_minor=150_000,
        expires_at=START + timedelta(hours=1),
        now=clock.now,
    )
    assert proof.status == "valid"
    events = ledger.events(view.grant_id)
    assert chain_is_valid(view.grant_id, events)
    owner_ref = "owner:" + pseudonym(KEYS, "owner", b"buyer-a")
    assert [(e.event_type, e.actor_ref) for e in events] == [
        ("consented", owner_ref),
        ("mandates_issued", "service:hnp-executor"),
    ]


def test_consenting_again_returns_the_same_grant(client: TestClient) -> None:
    proposal = propose(client).json()
    first = consent(client, proposal)
    again = consent(client, proposal)

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert len(client.get("/me/delegations", headers=buyer()).json()["delegations"]) == 1


def test_a_different_digest_is_a_scope_mismatch(client: TestClient) -> None:
    proposal = propose(client).json()

    tampered = proposal | {"scope_digest": "sha256:" + "0" * 64}
    error(consent(client, tampered), 409, "delegation_scope_mismatch")
    assert client.get("/me/delegations", headers=buyer()).json() == {"delegations": []}


@pytest.mark.parametrize(
    ("valid_for_seconds", "elapsed"),
    [(3_600, 600), (60, 60)],
    ids=["proposal-ttl", "grant-already-lapsed"],
)
def test_an_expired_proposal_cannot_be_consented(
    client: TestClient, clock: AuthorityClock, valid_for_seconds: int, elapsed: int
) -> None:
    proposal = propose(client, valid_for_seconds=valid_for_seconds).json()
    clock.advance(elapsed)

    error(consent(client, proposal), 409, "delegation_proposal_expired")
    assert client.get("/me/delegations", headers=buyer()).json() == {"delegations": []}


def test_an_unknown_proposal_or_body_is_rejected(client: TestClient) -> None:
    unknown = {"proposal_id": "dpr_missing", "scope_digest": "sha256:" + "0" * 64}
    error(consent(client, unknown), 404, "delegation_proposal_not_found")
    response = client.post("/delegations", json={"proposal_id": "dpr_1"}, headers=buyer())
    error(response, 422, "delegation_request_invalid")


def test_a_signer_failure_writes_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = propose(client).json()

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("private signer detail")

    monkeypatch.setattr(ap2_hnp.OpenMandateIssuer, "issue", broken)
    body = error(consent(client, proposal), 503, "delegation_signer_unavailable")
    assert "private" not in body["message"]
    assert client.get("/me/delegations", headers=buyer()).json() == {"delegations": []}


# Ownership (§5.1): another owner's proposal or grant is not found.


def test_another_owner_cannot_see_consent_or_revoke(client: TestClient) -> None:
    proposal = propose(client).json()
    error(consent(client, proposal, "buyer-b"), 404, "delegation_proposal_not_found")
    view = consent(client, proposal).json()
    grant_id = view["grant_id"]

    other = buyer("buyer-b")
    error(client.get(f"/delegations/{grant_id}", headers=other), 404, "delegation_not_found")
    error(
        client.post(f"/delegations/{grant_id}/revoke", headers=other), 404, "delegation_not_found"
    )
    assert client.get("/me/delegations", headers=other).json() == {"delegations": []}
    assert client.post("/me/delegations/revoke-all", headers=other).json() == {"receipts": []}
    assert view_of(client, grant_id).state == "active"


def test_my_delegations_are_listed_newest_first(client: TestClient, clock: AuthorityClock) -> None:
    first = delegated(client)
    clock.advance(1)
    second = delegated(client, variant_ids=["SON-07"])

    response = client.get("/me/delegations", headers=buyer())
    listed = DelegationList.model_validate_json(response.text)
    assert [view.grant_id for view in listed.delegations] == [
        second["grant_id"],
        first["grant_id"],
    ]
    assert response.json()["delegations"][1] == first
    error(client.get("/delegations/dg_missing", headers=buyer()), 404, "delegation_not_found")


# Revocation (§5.1, §8.4)


def test_revocation_is_idempotent(client: TestClient, clock: AuthorityClock) -> None:
    view = delegated(client)
    clock.advance(5)
    path = f"/delegations/{view['grant_id']}/revoke"

    first = client.post(path, headers=buyer())
    clock.advance(5)
    again = client.post(path, headers=buyer())

    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json()
    assert RevokeReceipt.model_validate_json(first.text).model_dump() == {
        "grant_id": view["grant_id"],
        "state": "revoked",
        "revoked_reason": "owner",
        "revoked_at": "2026-10-21T15:00:05.000Z",
        "effect": "no_effect",
        "message": HNP_MESSAGES["revoke.no_effect"],
    }
    revoked = view_of(client, view["grant_id"])
    assert (revoked.state, revoked.revoked_reason) == ("revoked", "owner")
    assert (revoked.task.state, revoked.task.outcome_code) == ("canceled", "revoked_by_owner")
    assert revoked.task.message == HNP_MESSAGES["task.revoked_by_owner"]


def test_revoke_all_reports_each_active_grant_with_its_effect(
    client: TestClient, ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    idle = delegated(client)
    held = delegated(client)
    in_flight = delegated(client)
    closed = delegated(client)
    client.post(f"/delegations/{closed['grant_id']}/revoke", headers=buyer())
    held_flow = flow(ledger, clock, held)
    assert held_flow.claim().status == "applied"
    assert held_flow.reserve(2).status == "applied"
    flow(ledger, clock, in_flight).committed()

    response = client.post("/me/delegations/revoke-all", headers=buyer())

    assert response.status_code == 200, response.text
    receipts = RevokeAllReceipt.model_validate_json(response.text).receipts
    effects = {receipt.grant_id: (receipt.effect, receipt.message) for receipt in receipts}
    assert effects == {
        idle["grant_id"]: ("no_effect", HNP_MESSAGES["revoke.no_effect"]),
        held["grant_id"]: ("released", HNP_MESSAGES["revoke.released"]),
        in_flight["grant_id"]: ("in_flight", HNP_MESSAGES["revoke.in_flight"]),
    }
    assert client.post("/me/delegations/revoke-all", headers=buyer()).json() == {"receipts": []}


def test_revoking_a_grant_the_operator_closed_has_no_effect(
    client: TestClient, ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    view = delegated(client)
    switch = client.post(
        "/internal/hnp/kill-switch", json={"enabled": True}, headers=bearer(OPERATOR_TOKEN)
    )
    assert switch.status_code == 200
    clock.advance(10)
    assert ledger.claim(view["task"]["task_id"]).outcome == "kill_switch"

    response = client.post(f"/delegations/{view['grant_id']}/revoke", headers=buyer())

    assert response.status_code == 200, response.text
    assert RevokeReceipt.model_validate_json(response.text).model_dump() == {
        "grant_id": view["grant_id"],
        "state": "revoked",
        "revoked_reason": "operator",
        "revoked_at": "2026-10-21T15:00:10.000Z",
        "effect": "no_effect",
        "message": HNP_MESSAGES["revoke.already_closed"],
    }
    closed = view_of(client, view["grant_id"])
    assert (closed.state, closed.revoked_reason) == ("revoked", "operator")
    assert closed.task.message == HNP_MESSAGES["task.kill_switch"]


def test_an_expired_delegation_shows_expired_and_is_not_revocable(
    client: TestClient, clock: AuthorityClock
) -> None:
    view = delegated(client)
    clock.advance(3_600)

    expired = view_of(client, view["grant_id"])
    assert (expired.state, expired.task.state) == ("expired", "scheduled")
    assert expired.task.message == HNP_MESSAGES["task.grant_expired"]
    response = client.post(f"/delegations/{view['grant_id']}/revoke", headers=buyer())
    body = error(response, 409, "delegation_not_revocable")
    assert body["message"] == HNP_MESSAGES["revoke.not_revocable"]


# The view follows the task the executor runs (§5.3, §8.1, §8.3).


def test_a_running_task_and_its_failure(
    client: TestClient, ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    view = delegated(client)
    task = flow(ledger, clock, view)
    task.claim()
    clock.advance(1)

    running = view_of(client, view["grant_id"])
    assert (running.state, running.task.state) == ("active", "running")
    assert running.task.run_id is not None and running.task.message == HNP_MESSAGES["task.running"]

    assert task.finish("failed", "policy_denied").status == "closed"
    failed = view_of(client, view["grant_id"])
    assert (failed.state, failed.revoked_reason) == ("revoked", "failed")
    assert (failed.task.outcome_code, failed.task.message) == (
        "policy_denied",
        HNP_MESSAGES["task.failed"],
    )


def test_an_escalation_carries_its_own_message_and_prefill(
    client: TestClient, ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    view = delegated(client, variant_ids=["SON-07", "SON-01"])
    task = flow(ledger, clock, view)
    task.claim()
    task.finish("escalated", "no_candidate_in_scope")

    escalated = view_of(client, view["grant_id"])
    assert (escalated.state, escalated.revoked_reason) == ("revoked", "escalated")
    assert (escalated.task.state, escalated.task.message) == ("escalated", None)
    assert escalated.escalation is not None
    assert escalated.escalation.model_dump() == {
        "reason": "no_candidate_in_scope",
        "title": "Necesito tu decisión",
        "message": escalation_message("no_candidate_in_scope"),
        # No variant was selected: the first one of the scope, with the consented maximum.
        "prefill_request": "Quiero comprar Audifonos Sonora SON-01 por hasta COP 1.500",
    }


def test_a_purchase_names_the_bought_variant_its_total_and_order(
    tmp_path: Path, client: TestClient, ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    view = delegated(client, variant_ids=["SON-01", "SON-07"])
    task = flow(ledger, clock, view)
    task.committed()
    task.advance("payment_sent", "payment_accepted", "complete_sent", "completed")
    run_id = task.state().task.run_id
    assert run_id is not None
    store = SqliteAgentStore(tmp_path / "agent.db")
    store.create_purchase(run_id, "delegated purchase", owner_id="buyer-a")
    store.update_purchase(run_id, stage="completed", selected_variant_id="SON-07")
    step = ledger.record_completion(
        task.task_id, task.lease_id, receipt("payment"), receipt("checkout")
    )
    assert step.status == "closed"

    purchased = view_of(client, view["grant_id"])
    assert (purchased.state, purchased.task.state) == ("consumed", "purchased")
    assert purchased.task.order_id == "order_1"
    assert purchased.task.message == (
        "Compré Audifonos Sonora SON-07 por COP 1.200 con tu delegación. Orden order_1."
    )


def test_an_uncertain_dispatch_keeps_the_grant_active(
    client: TestClient, ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    view = delegated(client)
    task = flow(ledger, clock, view)
    task.committed()
    task.advance("payment_sent")
    ledger.mark_uncertain(task.task_id, task.lease_id, "dispatch_timeout")

    uncertain = view_of(client, view["grant_id"])
    assert (uncertain.state, uncertain.task.state) == ("active", "uncertain")
    assert uncertain.task.message == HNP_MESSAGES["task.uncertain"]


def test_a_canceled_task_says_who_closed_it(
    client: TestClient, ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    by_owner = delegated(client)
    by_operator = delegated(client)
    assert ledger.revoke("buyer-a", by_owner["grant_id"]) is not None
    ledger.set_kill_switch(True)
    clock.advance(10)
    assert ledger.claim(by_operator["task"]["task_id"]).outcome == "kill_switch"

    revoked = view_of(client, by_owner["grant_id"])
    assert (revoked.state, revoked.revoked_reason) == ("revoked", "owner")
    assert (revoked.task.state, revoked.task.outcome_code) == ("canceled", "revoked_by_owner")
    assert revoked.task.message == HNP_MESSAGES["task.revoked_by_owner"]
    stopped = view_of(client, by_operator["grant_id"])
    assert (stopped.state, stopped.revoked_reason) == ("revoked", "operator")
    assert stopped.task.message == HNP_MESSAGES["task.kill_switch"]


# Internal routes (§5.2): tokens, no browser origin.


def test_the_kill_switch_needs_the_operator_token(client: TestClient) -> None:
    path = "/internal/hnp/kill-switch"
    for headers in ({}, bearer(SERVICE_TOKEN), buyer(), bearer(OPERATOR_TOKEN[:-1])):
        error(client.post(path, json={"enabled": True}, headers=headers), 401, "hnp_unauthorized")
    origin = bearer(OPERATOR_TOKEN) | {"Origin": "http://127.0.0.1:5173"}
    response = client.post(path, json={"enabled": True}, headers=origin)
    error(response, 403, "hnp_origin_forbidden")
    for body in ({}, {"enabled": "true"}, {"enabled": True, "actor": "x"}):
        response = client.post(path, json=body, headers=bearer(OPERATOR_TOKEN))
        error(response, 422, "hnp_request_invalid")
    assert client.get("/health").json()["hnp_kill_switch"] is False


def test_the_kill_switch_adds_an_epoch_only_when_it_changes(
    client: TestClient, clock: AuthorityClock
) -> None:
    def switch(enabled: bool) -> dict[str, Any]:
        response = client.post(
            "/internal/hnp/kill-switch",
            json={"enabled": enabled},
            headers=bearer(OPERATOR_TOKEN),
        )
        assert response.status_code == 200, response.text
        return response.json()

    clock.advance(1)
    on = switch(True)
    clock.advance(1)
    assert on == {"enabled": True, "control_epoch": 2, "changed_at": "2026-10-21T15:00:01.000Z"}
    assert switch(True) == on
    assert switch(False) == {
        "enabled": False,
        "control_epoch": 3,
        "changed_at": "2026-10-21T15:00:02.000Z",
    }


def test_a_task_is_read_with_the_service_token(client: TestClient) -> None:
    view = delegated(client)
    path = f"/internal/hnp/tasks/{view['task']['task_id']}"

    for headers in ({}, bearer(OPERATOR_TOKEN), buyer()):
        error(client.get(path, headers=headers), 401, "hnp_unauthorized")
    response = client.get(path, headers=bearer(SERVICE_TOKEN))
    assert response.status_code == 200 and response.json() == view["task"]
    missing = client.get("/internal/hnp/tasks/ht_missing", headers=bearer(SERVICE_TOKEN))
    error(missing, 404, "hnp_task_not_found")
    origin = bearer(SERVICE_TOKEN) | {"Origin": "https://evil.example"}
    error(client.get(path, headers=origin), 403, "hnp_origin_forbidden")


def test_there_is_no_run_route_yet(client: TestClient) -> None:
    view = delegated(client)
    path = f"/internal/hnp/tasks/{view['task']['task_id']}/run"
    assert client.post(path, headers=bearer(SERVICE_TOKEN)).status_code == 404


# /health (§5.4)


def test_health_counts_delegations_and_reports_the_switch(
    client: TestClient, ledger: SqliteDelegationLedger, clock: AuthorityClock
) -> None:
    base = {"status": "ok", "governance_mode": "off", "hp_flow": "blocked"}
    assert client.get("/health").json() == base | {
        "hnp_enabled": True,
        "hnp_kill_switch": False,
        "hnp_active_grants": 0,
        "hnp_running_tasks": 0,
        "hnp_uncertain_tasks": 0,
    }
    flow(ledger, clock, delegated(client)).claim()
    delegated(client)
    uncertain = flow(ledger, clock, delegated(client))
    uncertain.committed()
    uncertain.advance("payment_sent")
    ledger.mark_uncertain(uncertain.task_id, uncertain.lease_id, "dispatch_timeout")
    client.post("/internal/hnp/kill-switch", json={"enabled": True}, headers=bearer(OPERATOR_TOKEN))

    health = client.get("/health").json()
    assert {name: value for name, value in health.items() if name.startswith("hnp_")} == {
        "hnp_enabled": True,
        "hnp_kill_switch": True,
        "hnp_active_grants": 3,
        "hnp_running_tasks": 1,
        "hnp_uncertain_tasks": 1,
    }
    clock.advance(3_600)
    assert client.get("/health").json()["hnp_active_grants"] == 0


# Startup (§6): any other combination fails, without echoing secrets.


@pytest.mark.parametrize(
    "overrides",
    [
        {"agent_auth_mode": "local", "agent_ap2_required": False},
        {"agent_ap2_required": False},
        {"agent_hnp_agent_key_pem": None},
        {"agent_hnp_service_token": None},
        {"agent_hnp_operator_token": SecretStr("short")},
        {"agent_hnp_operator_token": SecretStr(SERVICE_TOKEN)},
        {"agent_d1_key_id": None},
        {"agent_d1_hmac_key_hex": None},
        {"agent_d1_hmac_key_hex": SecretStr("not-hex")},
        {"agent_governance_mode": "shadow"},
        {"agent_hnp_agent_key_pem": SecretStr(PROVIDER_PEM)},
        {"agent_hnp_agent_key_pem": SecretStr("not a key")},
    ],
    ids=[
        "local-auth",
        "no-ap2",
        "no-agent-key",
        "no-service-token",
        "short-token",
        "same-tokens",
        "no-d1-key-id",
        "no-d1-hmac",
        "bad-d1-hmac",
        "shadow",
        "agent-key-is-provider-key",
        "bad-agent-key",
    ],
)
def test_startup_refuses_an_unsafe_hnp_configuration(
    tmp_path: Path, clock: AuthorityClock, overrides: dict[str, Any]
) -> None:
    with pytest.raises(RuntimeError) as refused:
        build(tmp_path, clock, **overrides)

    message = str(refused.value)
    assert "AGENT_HNP" in message
    for secret in (SERVICE_TOKEN, OPERATOR_TOKEN, "1" * 64, PROVIDER_PEM, AGENT_PEM):
        assert secret not in message


def test_enforce_needs_the_governed_hp_flow(tmp_path: Path, clock: AuthorityClock) -> None:
    enforce = {
        "agent_governance_mode": "enforce",
        "agent_d1_policy_set_id": "d1-demo-policy",
        "agent_d1_policy_revision": "3",
    }
    with pytest.raises(RuntimeError, match="AGENT_HNP"):
        build(tmp_path, clock, **enforce)


@pytest.mark.parametrize(
    "overrides",
    [
        {"agent_hnp_max_validity_seconds": 59},
        {"agent_hnp_max_validity_seconds": 86_401},
        {"agent_hnp_proposal_ttl_seconds": 59},
        {"agent_hnp_proposal_ttl_seconds": 1_801},
    ],
)
def test_validity_settings_have_their_ranges(tmp_path: Path, overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        hnp_settings(tmp_path, **overrides)


def test_a_shorter_maximum_validity_is_enforced(tmp_path: Path, clock: AuthorityClock) -> None:
    with TestClient(build(tmp_path, clock, agent_hnp_max_validity_seconds=600)) as client:
        error(propose(client, valid_for_seconds=601), 422, "delegation_validity_exceeded")
        assert propose(client, valid_for_seconds=600).status_code == 201


def test_hnp_settings_default_to_off() -> None:
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.agent_hnp_enabled is False
    assert (settings.agent_hnp_max_validity_seconds, settings.agent_hnp_proposal_ttl_seconds) == (
        86_400,
        600,
    )
    assert settings.agent_hnp_service_token is None and settings.agent_hnp_operator_token is None
