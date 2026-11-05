"""`priority`: the order of a grant's variants the buyer consents to (spec §3.9, D6).

A complete permutation of `scope.variant_ids`, shown on the card as consented and at most three
long (one evaluator branch each). Absent, it is omitted from every canonical form, so a stored
consent keeps its bytes and its `scope_digest`. Until the executor honors it (PR 4), a proposal
that asks for a priority is refused instead of being consented without it.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from commerce_agent.application.conversation import DelegationDraftView
from commerce_agent.application.product_cards import product_card
from commerce_agent.delegation.contract import ScopeView
from commerce_agent.delegation.service import DelegationProposalRequest
from commerce_agent.delegation.views import scope_view
from commerce_agent.domain.conditional import MAX_BRANCHES
from commerce_agent.governance.canonical import canonical_bytes, parse_model
from commerce_agent.governance.hnp_models import (
    MAX_PRIORITY_VARIANTS,
    ConsentScope,
    consent_scope_bytes,
    parse_consent_scope,
    scope_digest,
)
from tests.governance.test_hnp_models import GOLDEN_SCOPE_DIGEST
from tests.support.conditional import A, B, C, phone_feed
from tests.support.delegation import AuthorityClock
from tests.test_delegation_api import build, buyer, error, proposal_body

FIXTURES = Path(__file__).parent / "governance" / "fixtures"
Json = dict[str, Any]


def stored() -> Json:
    return json.loads((FIXTURES / "consent-scope.json").read_bytes())


def with_priority(priority: list[str], display: list[str] | None = None) -> Json:
    payload = stored()
    payload["priority"] = priority
    payload["display"]["priority"] = priority if display is None else display
    return payload


def consent(payload: Json) -> ConsentScope:
    return parse_model(ConsentScope, json.dumps(payload).encode())


# A consent stored before `priority` keeps its bytes and its digest.


def test_a_stored_consent_keeps_its_bytes_and_digest() -> None:
    scope = consent(stored())
    assert scope.priority is None and scope.display.priority is None
    data = consent_scope_bytes(scope)
    assert b"priority" not in data
    assert scope_digest(scope) == GOLDEN_SCOPE_DIGEST
    assert parse_consent_scope(data) == scope
    assert "priority" not in scope_view(scope).model_dump(mode="json")


@pytest.mark.parametrize("where", ["consent", "display"])
def test_an_explicit_null_priority_is_not_a_stored_form(where: str) -> None:
    """One scope, one byte form: null and absent would otherwise be two digests of one consent.

    The model accepts the null (pydantic revalidates nested instances from their fields, where
    absent and null look the same); the canonical parse of the stored bytes refuses it.
    """
    payload = stored()
    (payload if where == "consent" else payload["display"])["priority"] = None
    with pytest.raises(ValueError, match="not canonical"):
        parse_consent_scope(canonical_bytes(payload))


# A consent with a priority.


def test_a_priority_is_consented_shown_and_digested() -> None:
    scope = consent(with_priority(["variant-2", "variant-1"]))
    assert scope.priority == scope.display.priority == ("variant-2", "variant-1")
    data = consent_scope_bytes(scope)
    assert b'"priority":["variant-2","variant-1"]' in data
    assert parse_consent_scope(data) == scope
    assert scope_digest(scope) != GOLDEN_SCOPE_DIGEST
    assert scope_digest(consent(with_priority(["variant-1", "variant-2"]))) != scope_digest(scope)
    assert scope_view(scope).priority == ("variant-2", "variant-1")


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(with_priority(["variant-1"]), id="missing-a-variant"),
        pytest.param(with_priority(["variant-1", "variant-2", "variant-3"]), id="extra"),
        pytest.param(with_priority(["variant-1", "variant-1"]), id="repeated"),
        pytest.param(with_priority([]), id="empty"),
        pytest.param(
            with_priority(["variant-2", "variant-1"], ["variant-1", "variant-2"]),
            id="card-shows-another-order",
        ),
        pytest.param(
            {**stored(), "priority": ["variant-2", "variant-1"]}, id="not-shown-on-the-card"
        ),
    ],
)
def test_a_priority_that_is_not_the_scope_in_another_order_is_refused(payload: Json) -> None:
    with pytest.raises(ValidationError):
        consent(payload)


def test_a_priority_has_at_most_one_variant_per_branch() -> None:
    assert MAX_PRIORITY_VARIANTS == MAX_BRANCHES  # hnp_models imports no domain module
    payload = stored()
    variants = [f"variant-{n}" for n in range(1, 5)]
    payload["scope"]["variant_ids"] = variants
    payload["display"]["variants"] = [{"variant_id": v, "title": v} for v in variants]
    assert consent(payload).priority is None  # four variants without priority are fine
    payload["priority"] = payload["display"]["priority"] = variants[::-1]
    with pytest.raises(ValidationError):
        consent(payload)


# The card's ScopeView.


def view(**changes: Any) -> Json:
    fixture = Path(__file__).parents[1] / "ui/src/storybook/fixtures/delegation-proposed.json"
    scope = json.loads(fixture.read_text(encoding="utf-8"))["scope"]
    return scope | changes


def test_the_scope_view_shows_the_priority_or_omits_it() -> None:
    ordered = ScopeView.model_validate_json(
        json.dumps(view(priority=["SON-XM6-BLK", "BOS-QCU-BLK"]))
    )
    assert ordered.priority == ("SON-XM6-BLK", "BOS-QCU-BLK")
    assert ordered.model_dump(mode="json")["priority"] == ["SON-XM6-BLK", "BOS-QCU-BLK"]
    plain = ScopeView.model_validate_json(json.dumps(view()))
    assert "priority" not in plain.model_dump(mode="json")


@pytest.mark.parametrize("priority", [["SON-XM6-BLK"], ["BOS-QCU-BLK"] * 2, [], None])
def test_the_scope_view_refuses_a_priority_that_is_not_its_variants(
    priority: list[str] | None,
) -> None:
    with pytest.raises(ValidationError):
        ScopeView.model_validate_json(json.dumps(view(priority=priority)))


# The proposal request and the chat's draft.


def request(**changes: Any) -> DelegationProposalRequest:
    return DelegationProposalRequest.model_validate_json(json.dumps(proposal_body(**changes)))


def test_the_request_keeps_the_priority_in_the_buyers_order() -> None:
    proposed = request(variant_ids=["SON-07", "SON-01"], priority=["SON-07", "SON-01"])
    assert proposed.variant_ids == ("SON-01", "SON-07")  # the scope keeps them sorted
    assert proposed.priority == ("SON-07", "SON-01")
    assert request().priority is None


@pytest.mark.parametrize(
    "priority",
    [["SON-01"], ["SON-07", "SON-01", "SON-09"], ["SON-01", "SON-01"], [], None],
)
def test_the_request_refuses_a_priority_that_is_not_its_variants(
    priority: list[str] | None,
) -> None:
    with pytest.raises(ValidationError):
        request(variant_ids=["SON-07", "SON-01"], priority=priority)


def test_a_proposal_with_priority_is_refused_until_the_executor_honors_it(
    tmp_path: Path,
) -> None:
    """Consenting it without the order would let the executor buy the cheapest: fail closed."""
    with TestClient(build(tmp_path, AuthorityClock())) as client:
        body = proposal_body(variant_ids=["SON-07", "SON-01"], priority=["SON-07", "SON-01"])
        answer = client.post("/delegations/proposals", json=body, headers=buyer())
        error(answer, 422, "delegation_request_invalid")
        assert client.get("/me/delegations", headers=buyer()).json() == {"delegations": []}
        body = proposal_body(variant_ids=["SON-07", "SON-01"])
        answer = client.post("/delegations/proposals", json=body, headers=buyer())
        assert answer.status_code == 201
        assert "priority" not in answer.json()["scope"]


def draft(**changes: Any) -> Json:
    feed = phone_feed()
    offers = [product_card(feed, variant) for variant in (A, B, C)]
    return {
        "offers": [offer.model_dump(mode="json") for offer in offers if offer is not None],
        "budget_minor": 300_000_000,
        "currency": "COP",
        "preference_text": "Prefiero el Xperia, nunca el Moto.",
    } | changes


def test_the_draft_proposes_a_priority_among_its_offers_or_omits_it() -> None:
    proposed = DelegationDraftView.model_validate_json(json.dumps(draft(priority=[B, A])))
    assert proposed.priority == (B, A)
    plain = DelegationDraftView.model_validate_json(json.dumps(draft()))
    assert "priority" not in plain.model_dump(mode="json")


@pytest.mark.parametrize("priority", [["SON-X5III-256-BLK"], [A, A], [], None, [A, B, C, A]])
def test_the_draft_refuses_a_priority_outside_its_offers(priority: list[str] | None) -> None:
    with pytest.raises(ValidationError):
        DelegationDraftView.model_validate_json(json.dumps(draft(priority=priority)))


def test_the_old_draft_shape_is_unchanged() -> None:
    payload = draft()
    assert DelegationDraftView.model_validate_json(json.dumps(payload)).model_dump(
        mode="json"
    ) == copy.deepcopy(payload)
