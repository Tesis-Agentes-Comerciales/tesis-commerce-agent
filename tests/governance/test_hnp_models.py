"""Human Not Present facts: the consented scope, its digest and the strict D2/D3-HNP inputs."""

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from commerce_agent.delegation.contract import ScopeView
from commerce_agent.governance.canonical import canonical_bytes, parse_model
from commerce_agent.governance.hnp_models import (
    SCOPE_DIGEST_DOMAIN,
    ConsentScope,
    DispatchPolicyInput,
    HnpAuthorizationPolicyInput,
    consent_scope_bytes,
    parse_consent_scope,
    scope_digest,
)

FIXTURES = Path(__file__).parent / "fixtures"
# Frozen: the delegation API consents with it and the evidence verifier recomputes it.
GOLDEN_SCOPE_DIGEST = "sha256:a51dd220b0f18adf895392ba585464b6cc42e75a9f1b3050a7e6fcacae5030ef"
Change = Callable[[dict[str, Any]], None]


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_bytes())


def parse[T: BaseModel](model: type[T], payload: dict[str, Any]) -> T:
    """As the boundary parses: JSON bytes, duplicate keys and non-JSON numbers rejected first."""
    return parse_model(model, json.dumps(payload).encode())


def consent(payload: dict[str, Any]) -> ConsentScope:
    return parse(ConsentScope, payload)


def _set(path: str, value: Any) -> Change:
    """A change at a dotted path; a numeric segment indexes a list."""

    def change(payload: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        target: Any = payload
        for key in parents:
            target = target[int(key)] if key.isdigit() else target[key]
        target[leaf] = value

    return change


def _both(*changes: Change) -> Change:
    def change(payload: dict[str, Any]) -> None:
        for each in changes:
            each(payload)

    return change


def changed(payload: dict[str, Any], change: Change) -> dict[str, Any]:
    copied = copy.deepcopy(payload)
    change(copied)
    return copied


# The consented scope and its digest (§4.2).


def test_scope_digest_is_golden() -> None:
    scope = consent(load("consent-scope.json"))
    data = consent_scope_bytes(scope)
    assert scope_digest(scope) == GOLDEN_SCOPE_DIGEST
    assert (
        "sha256:" + hashlib.sha256(b"commerce-delegation-scope/v1\0" + data).hexdigest()
        == GOLDEN_SCOPE_DIGEST
    )
    assert SCOPE_DIGEST_DOMAIN == b"commerce-delegation-scope/v1\0"
    # commerce-canonical-json/1: sorted keys, no spaces, Unicode kept as UTF-8.
    assert data.startswith(b'{"display":{"destination_label":"Bogot\xc3\xa1, Cundinamarca",')


def test_only_canonical_bytes_are_accepted_as_stored() -> None:
    raw = (FIXTURES / "consent-scope.json").read_bytes()
    scope = consent(json.loads(raw))
    canonical = consent_scope_bytes(scope)
    assert parse_consent_scope(canonical) == scope
    reordered = json.dumps(json.loads(canonical), ensure_ascii=False, sort_keys=False).encode()
    escaped = json.dumps(json.loads(canonical), sort_keys=True, separators=(",", ":")).encode()
    for other in (raw, reordered, escaped):
        assert parse_model(ConsentScope, other) == scope
        with pytest.raises(ValueError):
            parse_consent_scope(other)
    duplicated = canonical.replace(b'{"display":', b'{"proposal_id":"dpr_2","display":', 1)
    with pytest.raises(ValueError):
        parse_consent_scope(duplicated)


@pytest.mark.parametrize(
    "change",
    [
        _both(
            _set("scope.total_max_minor", 99_999_999), _set("display.total_max.minor", 99_999_999)
        ),
        _set("scope.destination_binding.value", "4" * 64),
        _both(
            _set("scope.variant_ids", ["variant-1"]),
            lambda payload: payload["display"]["variants"].pop(),
        ),
        _both(_set("preference_text", None), _set("display.preference_text", None)),
        _set("display.variants.0.title", "Audífonos inalámbricos, rojo"),
        _set("owner_ref", "owner:" + "6f" * 32),
        _set("presented_at", "2026-10-21T15:00:00.001Z"),
    ],
)
def test_any_consented_value_changes_the_digest(change: Change) -> None:
    payload = load("consent-scope.json")
    assert scope_digest(consent(changed(payload, change))) != GOLDEN_SCOPE_DIGEST


@pytest.mark.parametrize(
    "change",
    [
        _set("display.total_max.minor", 90_000_000),
        _set("display.expires_at", "2026-10-22T14:59:59.999Z"),
        _set("display.preference_text", None),
        _set("display.variants.1.variant_id", "variant-3"),
        lambda payload: payload["display"]["variants"].reverse(),
        _set("presented_at", "2026-10-22T15:00:00.000Z"),
    ],
)
def test_the_card_shows_exactly_the_consented_scope(change: Change) -> None:
    with pytest.raises(ValidationError):
        consent(changed(load("consent-scope.json"), change))


SCOPE_VIOLATIONS: dict[str, Change] = {
    "no variant": _set("scope.variant_ids", []),
    "six variants": _set("scope.variant_ids", [f"variant-{n}" for n in range(1, 7)]),
    "repeated variant": _set("scope.variant_ids", ["variant-1", "variant-1"]),
    "unsorted variants": _set("scope.variant_ids", ["variant-2", "variant-1"]),
    "no merchant": _set("scope.merchant_refs", []),
    "unsorted merchants": _set("scope.merchant_refs", ["merchant-2", "merchant-1"]),
    "zero total": _set("scope.total_max_minor", 0),
    "total as text": _set("scope.total_max_minor", "100000000"),
    "total as bool": _set("scope.total_max_minor", True),
    "total as float": _set("scope.total_max_minor", 100_000_000.0),
    "other currency": _set("scope.currency", "USD"),
    "two units": _set("scope.quantity_max", 2),
    "two uses": _set("scope.uses_max", 2),
    "other instrument": _set("scope.instrument_ref", "card-1"),
    "unknown constraint": _set("scope.max_unit_minor", 1),
    "plain destination": _set("scope.destination_binding.scheme", "plain"),
    "empty preference": _both(_set("preference_text", ""), _set("display.preference_text", "")),
    "long preference": _both(
        _set("preference_text", "x" * 281), _set("display.preference_text", "x" * 281)
    ),
    "owner in clear": _set("owner_ref", "buyer@example.com"),
    "unprefixed proposal": _set("proposal_id", "proposal-1"),
    "other schema": _set("schema_version", "commerce-policy/1.0"),
    "time without zone": _set("presented_at", "2026-10-21T15:00:00.000"),
    "time without milliseconds": _set("presented_at", "2026-10-21T15:00:00Z"),
    "empty title": _set("display.variants.0.title", ""),
    "display extra field": _set("display.address", "Calle 1"),
    "null display": _set("display", None),
}


@pytest.mark.parametrize("case", sorted(SCOPE_VIOLATIONS))
def test_the_scope_is_strict(case: str) -> None:
    with pytest.raises(ValueError):
        consent(changed(load("consent-scope.json"), SCOPE_VIOLATIONS[case]))


def test_a_scope_without_preference_text_is_valid() -> None:
    payload = changed(
        load("consent-scope.json"),
        _both(_set("preference_text", None), _set("display.preference_text", None)),
    )
    assert consent(payload).preference_text is None


def test_the_consented_display_is_the_card_s_scope_view() -> None:
    """`ScopeDisplay` keeps the JSON of the delegation views' `ScopeView`, so the proposal shows
    exactly the bytes the buyer consents to."""
    display = consent(load("consent-scope.json")).display
    view = ScopeView.model_validate_json(canonical_bytes(display))
    assert canonical_bytes(view) == canonical_bytes(display)


# D2-HNP and D3-HNP inputs (policy-v1 §4): strict, no coercion and no HP branch.

D2_VIOLATIONS: dict[str, Change] = {
    "HP purchase mode": _set("purchase_mode", "HP"),
    "D3 phase": _set("phase", "D3"),
    "other action": _set("action", "commit_dispatch"),
    "hp facts": _set("hp", {}),
    "human approval": _set("hnp.human_approved", True),
    "unknown grant state": _set("hnp.grant.state", "suspended"),
    "grant missing": lambda payload: payload["hnp"].pop("grant"),
    "null grant": _set("hnp.grant", None),
    "zero epoch": _set("hnp.grant.authority_epoch", 0),
    "unknown action": _set("hnp.grant.permitted_actions", ["refund"]),
    "repeated action": _set("hnp.grant.permitted_actions", ["commit_dispatch"] * 2),
    "uses as bool": _set("hnp.usage.uses_consumed", False),
    "uses as text": _set("hnp.usage.uses_reserved_other", "0"),
    "negative amount": _set("hnp.usage.amount_consumed_minor", -1),
    "amount as float": _set("hnp.reservation_intent.amount_minor", 90_000_000.0),
    "reservation in USD": _set("hnp.reservation_intent.currency", "USD"),
    "unknown proof status": _set("hnp.open_proof.status", "trusted"),
    "valid proof without profile": _set("hnp.open_proof.profile_ref", None),
    "expiry without zone": _set("hnp.grant.expires_at", "2026-10-22T15:00:00.000+00:00"),
    "unknown scope constraint": _set("hnp.grant.scope.max_unit_minor", 1),
    "scope over budget type": _set("hnp.grant.scope.total_max_minor", "100000000"),
    "unknown own reservation state": lambda payload: payload["hnp"]["usage"].update(
        same_operation_reservation={
            **payload["hnp"]["reservation_intent"],
            "state": "pending",
            "state_version": 1,
        }
    ),
}


@pytest.mark.parametrize("case", sorted(D2_VIOLATIONS))
def test_the_d2_hnp_input_is_strict(case: str) -> None:
    with pytest.raises(ValueError):
        parse(HnpAuthorizationPolicyInput, changed(load("d2-hnp-input.json"), D2_VIOLATIONS[case]))


D3_VIOLATIONS: dict[str, Change] = {
    "D2 phase": _set("phase", "D2"),
    "other action": _set("action", "complete_purchase"),
    "HP purchase mode": _set("purchase_mode", "HP"),
    "no closed proof": lambda payload: payload.pop("closed_proof"),
    "d2 without transition": lambda payload: payload["d2"].pop("application_transition_ref"),
    "unknown reservation state": _set("reservation.state", "held"),
    "reservation version as bool": _set("reservation.state_version", True),
    "jwt in the proof": _set("closed_proof.checkout_jwt", "eyJ..."),
    "digest not hex": _set("closed_proof.original_checkout_jwt_digest", "sha256:XYZ"),
    "valid proof without profile": _set("closed_proof.profile_ref", None),
}


@pytest.mark.parametrize("case", sorted(D3_VIOLATIONS))
def test_the_d3_hnp_input_is_strict(case: str) -> None:
    with pytest.raises(ValueError):
        parse(DispatchPolicyInput, changed(load("d3-hnp-input.json"), D3_VIOLATIONS[case]))


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("d2-hnp-input.json", HnpAuthorizationPolicyInput),
        ("d3-hnp-input.json", DispatchPolicyInput),
        ("consent-scope.json", ConsentScope),
    ],
)
def test_fixtures_round_trip_through_the_canonical_form(name: str, model: type[BaseModel]) -> None:
    raw = (FIXTURES / name).read_bytes()
    parsed = parse_model(model, raw)
    assert json.loads(canonical_bytes(parsed)) == json.loads(raw)
    with pytest.raises(ValueError):
        parse_model(model, raw.replace(b"{", b'{"run_id":"x","run_id":"y",', 1))


def test_an_unresolved_proof_needs_no_profile() -> None:
    payload = changed(
        load("d2-hnp-input.json"),
        _both(
            _set("hnp.open_proof.status", "unresolved"), _set("hnp.open_proof.profile_ref", None)
        ),
    )
    assert parse(HnpAuthorizationPolicyInput, payload).hnp.open_proof.profile_ref is None
