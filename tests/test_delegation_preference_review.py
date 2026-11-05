"""`preference_review` (spec D15): the consent says whether the agent reviews the buyer's text.

Only `true` is ever stored or sent, and only with a `preference_text`; absent, it is omitted from
every canonical form, so a stored consent keeps its bytes and its `scope_digest`. It does not
need `priority`. Who sets it (`propose`, from `build_delegation_service`) and what the executor
does when the promised reviewer is missing (`choice_not_confirmed`) are PR 4's.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from commerce_agent.delegation.contract import ScopeView
from commerce_agent.delegation.views import scope_view
from commerce_agent.governance.canonical import canonical_bytes, parse_model
from commerce_agent.governance.hnp_models import (
    ConsentScope,
    consent_scope_bytes,
    parse_consent_scope,
    scope_digest,
)
from tests.governance.test_hnp_models import GOLDEN_SCOPE_DIGEST

FIXTURES = Path(__file__).parent / "governance" / "fixtures"
Json = dict[str, Any]


def stored() -> Json:
    return json.loads((FIXTURES / "consent-scope.json").read_bytes())


def consent(payload: Json) -> ConsentScope:
    return parse_model(ConsentScope, json.dumps(payload).encode())


def reviewed(payload: Json | None = None) -> Json:
    payload = stored() if payload is None else payload
    payload["display"]["preference_review"] = True
    return payload


def without_text(payload: Json) -> Json:
    payload["preference_text"] = payload["display"]["preference_text"] = None
    return payload


def test_a_stored_consent_keeps_its_bytes_and_digest() -> None:
    scope = consent(stored())
    assert scope.display.preference_review is False
    data = consent_scope_bytes(scope)
    assert b"preference_review" not in data
    assert scope_digest(scope) == GOLDEN_SCOPE_DIGEST
    assert parse_consent_scope(data) == scope
    assert "preference_review" not in scope_view(scope).model_dump(mode="json")


def test_a_promised_review_is_consented_shown_and_digested() -> None:
    scope = consent(reviewed())
    assert scope.display.preference_review is True
    data = consent_scope_bytes(scope)
    assert b'"preference_review":true' in data
    assert parse_consent_scope(data) == scope
    assert scope_digest(scope) != GOLDEN_SCOPE_DIGEST
    assert scope_view(scope).preference_review is True
    assert scope_view(scope).model_dump(mode="json")["preference_review"] is True


def test_a_review_does_not_need_a_priority() -> None:
    scope = consent(reviewed())
    assert scope.priority is None and scope.display.preference_review


def test_a_review_with_a_priority_is_valid_too() -> None:
    payload = reviewed()
    payload["priority"] = payload["display"]["priority"] = ["variant-2", "variant-1"]
    assert consent(payload).display.preference_review


def test_a_false_review_is_not_a_stored_form() -> None:
    payload = stored()
    payload["display"]["preference_review"] = False
    with pytest.raises(ValueError, match="not canonical"):
        parse_consent_scope(canonical_bytes(payload))


def test_only_a_preference_text_can_be_reviewed() -> None:
    assert consent(without_text(stored())).display.preference_review is False
    with pytest.raises(ValidationError):
        consent(reviewed(without_text(stored())))


# The card's ScopeView, which the consent's display must always validate as.


def view(**changes: Any) -> Json:
    fixture = Path(__file__).parents[1] / "ui/src/storybook/fixtures/delegation-proposed.json"
    scope = json.loads(fixture.read_text(encoding="utf-8"))["scope"]
    return scope | {"preference_text": "Prefiero el negro."} | changes


def test_the_scope_view_shows_a_promised_review_or_omits_it() -> None:
    shown = ScopeView.model_validate_json(json.dumps(view(preference_review=True)))
    assert shown.model_dump(mode="json")["preference_review"] is True
    plain = ScopeView.model_validate_json(json.dumps(view()))
    assert plain.preference_review is False
    assert "preference_review" not in plain.model_dump(mode="json")


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"preference_review": False}, id="false"),
        pytest.param({"preference_review": None}, id="null"),
        pytest.param({"preference_review": 1}, id="not-a-bool"),
        pytest.param({"preference_review": True, "preference_text": None}, id="without-text"),
    ],
)
def test_the_scope_view_refuses_a_review_that_is_not_promised(changes: Json) -> None:
    with pytest.raises(ValidationError):
        ScopeView.model_validate_json(json.dumps(view(**changes)))
