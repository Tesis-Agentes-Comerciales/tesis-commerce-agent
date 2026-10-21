"""POL-07..POL-10, pure: the P/N/F matrix of policy-acceptance-v1 §2 at the rule layer.

P and N evaluate the rules on the HNP fixtures (§1 of the acceptance: 89.000.000 + 1.000.000 of
shipping against a 100.000.000 grant); each N changes only its fields and fails only its rule,
except N10, which lists POL-09 and POL-10. F covers what the rule layer owns: incoherent facts
(GOV_FACTS_UNAVAILABLE), terms no longer current and an unresolved profile or proof, before any
rule. The rest of F07/F09/F10 (an unresolvable grant, a lost CAS, failed evidence) belongs to the
store, the boundary and the executor.
"""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from commerce_agent.governance.canonical import parse_model
from commerce_agent.governance.hnp_models import DispatchPolicyInput, HnpAuthorizationPolicyInput
from commerce_agent.governance.hnp_rules import (
    DELEGATION_RULES,
    DISPATCH_RULES,
    delegation_prerequisite,
    delegation_results,
    dispatch_prerequisite,
    dispatch_results,
)
from commerce_agent.governance.models import POLICY_REASONS, Action, PolicyId, RuleResult

FIXTURES = Path(__file__).parent / "fixtures"
DISPATCH_ACTIONS: tuple[Action, ...] = ("commit_dispatch",)
NOW = "2026-10-21T16:00:00.000Z"
Change = Callable[[dict[str, Any]], None]


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_bytes())


def d2(change: Change | None = None) -> HnpAuthorizationPolicyInput:
    payload = load("d2-hnp-input.json")
    if change:
        change(payload)
    return parse_model(HnpAuthorizationPolicyInput, json.dumps(payload).encode())


def d3(change: Change | None = None) -> DispatchPolicyInput:
    payload = load("d3-hnp-input.json")
    if change:
        change(payload)
    return parse_model(DispatchPolicyInput, json.dumps(payload).encode())


def _set(path: str, value: Any) -> Change:
    """A change at a dotted path; a numeric segment indexes a list."""

    def change(payload: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        target: Any = payload
        for key in parents:
            target = target[int(key)] if key.isdigit() else target[key]
        target[leaf] = copy.deepcopy(value)

    return change


def _both(*changes: Change) -> Change:
    def change(payload: dict[str, Any]) -> None:
        for each in changes:
            each(payload)

    return change


def _own_reservation(**fields: Any) -> Change:
    """D2 with this operation's own reservation already in the store (a retry)."""

    def change(payload: dict[str, Any]) -> None:
        usage = payload["hnp"]["usage"]
        own = {**payload["hnp"]["reservation_intent"], "state": "reserved", "state_version": 1}
        usage["same_operation_reservation"] = {**own, **fields}

    return change


def _reservation(**fields: Any) -> Change:
    """D3 with another reservation row; the usage projection shows the same row."""

    def change(payload: dict[str, Any]) -> None:
        payload["reservation"].update(fields)
        payload["usage"]["same_operation_reservation"] = copy.deepcopy(payload["reservation"])

    return change


def failing(results: tuple[RuleResult, ...]) -> list[PolicyId]:
    return [rule.policy_id for rule in results if rule.result == "fail"]


def reasons(results: tuple[RuleResult, ...]) -> tuple[str, ...]:
    return tuple(POLICY_REASONS[rule.policy_id] for rule in results if rule.result == "fail")


OTHER_REF = {
    "schema_version": "commerce-policy/1.0",
    "id": "dg_2",
    "version": 1,
    "digest": "sha256:" + "0" * 64,
}

# P07, P08 and P09 (D2), P09 and P10 (D3).


def test_valid_d2_passes_every_delegation_rule() -> None:
    data = d2()
    assert delegation_prerequisite(data) is None
    results = delegation_results(data)
    assert [(rule.policy_id, rule.result) for rule in results] == [
        (policy, "pass") for policy in DELEGATION_RULES
    ]
    assert DELEGATION_RULES == ("POL-07", "POL-08", "POL-09")


def test_valid_d3_passes_both_dispatch_rules() -> None:
    data = d3()
    assert dispatch_prerequisite(data) is None
    results = dispatch_results(data, DISPATCH_ACTIONS)
    assert [(rule.policy_id, rule.result) for rule in results] == [
        ("POL-09", "pass"),
        ("POL-10", "pass"),
    ]
    assert DISPATCH_RULES == ("POL-09", "POL-10")


def test_a_retry_reuses_its_own_reservation_without_counting_it_twice() -> None:
    """P09: the same operation's identical `reserved` row is not another use."""
    data = d2(_own_reservation())
    assert delegation_prerequisite(data) is None
    assert failing(delegation_results(data)) == []


@pytest.mark.parametrize(
    ("valid_from", "expires_at", "allowed"),
    [
        (NOW, "2026-10-22T15:00:00.000Z", True),
        ("2026-10-21T15:00:00.000Z", "2026-10-21T16:00:00.001Z", True),
        ("2026-10-21T16:00:00.001Z", "2026-10-22T15:00:00.000Z", False),
        ("2026-10-21T15:00:00.000Z", NOW, False),
        ("2026-10-21T15:00:00.000Z", "2026-10-21T15:59:59.999Z", False),
    ],
    ids=["from now", "a millisecond left", "not yet valid", "now is expires_at", "expired"],
)
def test_validity_is_closed_at_valid_from_and_open_at_expires_at(
    valid_from: str, expires_at: str, allowed: bool
) -> None:
    """HNP01 on the authority clock: before `valid_from`, at `expires_at` and after, deny."""
    window = _both(_set("grant.valid_from", valid_from), _set("grant.expires_at", expires_at))
    in_d2 = delegation_results(d2(lambda payload: window(payload["hnp"])))
    in_d3 = dispatch_results(d3(window), DISPATCH_ACTIONS)
    assert failing(in_d2) == ([] if allowed else ["POL-07"])
    assert failing(in_d3) == ([] if allowed else ["POL-10"])


# N07, N08 and N09 in D2: each change fails its own rule and no other.

D2_VIOLATIONS: dict[str, tuple[PolicyId, Change]] = {
    # POL-07: the grant in force, its owner, the verified Agent Key and the action.
    "grant revoked": ("POL-07", _set("hnp.grant.state", "revoked")),
    "grant expired": ("POL-07", _set("hnp.grant.state", "expired")),
    "grant consumed": ("POL-07", _set("hnp.grant.state", "consumed")),
    "grant of another owner": ("POL-07", _set("hnp.grant.owner_ref", "owner:" + "6f" * 32)),
    "another agent key": ("POL-07", _set("hnp.open_proof.verified_agent_key_ref", "agent-key-2")),
    "open proof invalid": ("POL-07", _set("hnp.open_proof.status", "invalid")),
    "action not in the grant": (
        "POL-07",
        _set("hnp.grant.permitted_actions", ["prepare_checkout", "commit_dispatch"]),
    ),
    # POL-08: destination, merchant, variant, total and instrument of the scope.
    "another destination": ("POL-08", _set("terms.destination_binding.value", "4" * 64)),
    "destination under another key": ("POL-08", _set("terms.destination_binding.key_id", "key-2")),
    "merchant outside the scope": ("POL-08", _set("hnp.grant.scope.merchant_refs", ["merchant-2"])),
    "variant outside the scope": ("POL-08", _set("hnp.grant.scope.variant_ids", ["variant-2"])),
    "total over the scope": ("POL-08", _set("hnp.grant.scope.total_max_minor", 89_999_999)),
    "another instrument": ("POL-08", _set("hnp.requested_instrument_ref", "card-1")),
    # POL-09: uses of the grant and the reservation of this operation.
    "use already consumed": (
        "POL-09",
        _both(
            _set("hnp.usage.uses_consumed", 1),
            _set("hnp.usage.amount_consumed_minor", 90_000_000),
        ),
    ),
    "reserved by another operation": (
        "POL-09",
        _both(
            _set("hnp.usage.uses_reserved_other", 1),
            _set("hnp.usage.amount_reserved_other_minor", 90_000_000),
        ),
    ),
    "own reservation for another amount": ("POL-09", _own_reservation(amount_minor=80_000_000)),
    "own reservation released": ("POL-09", _own_reservation(state="released")),
    "own reservation under another id": ("POL-09", _own_reservation(reservation_id="rsv_2")),
    "reservation for another amount": (
        "POL-09",
        _set("hnp.reservation_intent.amount_minor", 80_000_000),
    ),
    "reservation for an old epoch": ("POL-09", _set("hnp.reservation_intent.expected_epoch", 2)),
    "reservation of another run": ("POL-09", _set("hnp.reservation_intent.run_id", "run-2")),
    "reservation of another operation": (
        "POL-09",
        _set("hnp.reservation_intent.operation_id", "op_2"),
    ),
    "reservation of another grant": ("POL-09", _set("hnp.reservation_intent.grant_ref", OTHER_REF)),
}


@pytest.mark.parametrize("case", sorted(D2_VIOLATIONS))
def test_each_d2_violation_fails_only_its_rule(case: str) -> None:
    policy, change = D2_VIOLATIONS[case]
    data = d2(change)
    assert delegation_prerequisite(data) is None
    results = delegation_results(data)
    assert failing(results) == [policy]
    assert reasons(results) == (POLICY_REASONS[policy],)


def test_every_d2_rule_is_evaluated_and_reasons_follow_the_catalog() -> None:
    def change(payload: dict[str, Any]) -> None:
        for _, each in (
            D2_VIOLATIONS["use already consumed"],
            D2_VIOLATIONS["another destination"],
            D2_VIOLATIONS["grant revoked"],
        ):
            each(payload)

    results = delegation_results(d2(change))
    assert reasons(results) == (
        "DELEGATION_NOT_ACTIVE",
        "DELEGATION_SCOPE_EXCEEDED",
        "DELEGATION_USE_UNAVAILABLE",
    )


# N09 and N10 in D3.

D3_VIOLATIONS: dict[str, tuple[list[PolicyId], Change]] = {
    # POL-09: the reservation itself.
    "own reservation for another amount": (["POL-09"], _reservation(amount_minor=80_000_000)),
    "own reservation released": (["POL-09"], _reservation(state="released")),
    "own reservation already committed": (["POL-09"], _reservation(state="committed")),
    "reservation of another run": (["POL-09"], _reservation(run_id="run-2")),
    # POL-10: the authority D2 relied on.
    "grant revoked": (["POL-10"], _set("grant.state", "revoked")),
    "grant of another owner": (["POL-10"], _set("grant.owner_ref", "owner:" + "6f" * 32)),
    "principal not verified": (["POL-10"], _set("principal.authentication_verified", False)),
    "principal not bound to the owner": (["POL-10"], _set("principal.owns_run", False)),
    "principal with a user token": (["POL-10"], _set("principal.authentication_basis", "user_jwt")),
    "principal without dispatch": (["POL-10"], _set("principal.permitted_actions", [])),
    "grant without dispatch": (["POL-10"], _set("grant.permitted_actions", ["complete_purchase"])),
    "d2 denied": (["POL-10"], _set("d2.outcome", "deny")),
    "d2 of another run": (["POL-10"], _set("d2.run_id", "run-2")),
    "d2 observed in shadow": (["POL-10"], _set("d2.governance_mode", "shadow")),
    "d2 under another policy": (["POL-10"], _set("d2.policy_binding.version", 2)),
    "context changed since d2": (["POL-10"], _set("context_version", 2)),
    "d2 for another intent": (["POL-10"], _set("d2.intent_ref", OTHER_REF)),
    "terms changed since d2": (["POL-10"], _set("terms.material_digest", "sha256:" + "5" * 64)),
    "d2 for another grant": (["POL-10"], _set("d2.grant_ref", OTHER_REF)),
    "d2 for another operation": (["POL-10"], _set("d2.operation_id", "op_2")),
    "d2 for another reservation": (
        ["POL-10"],
        _both(_set("d2.reservation_id", "rsv_2"), _set("d2.application_transition_ref", "rsv_2")),
    ),
    "d2 reservation not applied": (["POL-10"], _set("d2.application_transition_ref", "rsv_2")),
    "closed proof invalid": (["POL-10"], _set("closed_proof.status", "invalid")),
    "closed by another key": (["POL-10"], _set("closed_proof.verified_agent_key_ref", "key-x")),
    "closed for another authorization": (
        ["POL-10"],
        _set("closed_proof.authorization_id", "authorization-2"),
    ),
    "closed for another operation": (["POL-10"], _set("closed_proof.operation_id", "op_2")),
    "closed for another grant": (["POL-10"], _set("closed_proof.grant_ref", OTHER_REF)),
    "closed over other terms": (
        ["POL-10"],
        _set("closed_proof.material_digest", "sha256:" + "5" * 64),
    ),
    "closed for another instrument": (
        ["POL-10"],
        _set("closed_proof.requested_instrument_ref", "card-1"),
    ),
    # N10: a revocation or kill switch bumps the epoch; the reservation loses it too.
    "epoch changed": (["POL-09", "POL-10"], _set("grant.authority_epoch", 2)),
}


@pytest.mark.parametrize("case", sorted(D3_VIOLATIONS))
def test_each_d3_violation_fails_exactly_its_rules(case: str) -> None:
    policies, change = D3_VIOLATIONS[case]
    data = d3(change)
    assert dispatch_prerequisite(data) is None
    results = dispatch_results(data, DISPATCH_ACTIONS)
    assert failing(results) == policies
    assert reasons(results) == tuple(POLICY_REASONS[policy] for policy in policies)


def test_n10_lists_pol_09_and_pol_10_in_order() -> None:
    results = dispatch_results(d3(_set("grant.authority_epoch", 2)), DISPATCH_ACTIONS)
    assert [(rule.policy_id, rule.result) for rule in results] == [
        ("POL-09", "fail"),
        ("POL-10", "fail"),
    ]
    assert reasons(results) == ("DELEGATION_USE_UNAVAILABLE", "DISPATCH_AUTHORITY_CHANGED")


def test_dispatch_needs_the_action_in_the_verified_artifact() -> None:
    assert failing(dispatch_results(d3(), ())) == ["POL-10"]


# F: what never reaches a rule.

INCOHERENT_D2: dict[str, Change] = {
    "zero uses hide a consumed amount": _set("hnp.usage.amount_consumed_minor", 1),
    "zero uses hide a reserved amount": _set("hnp.usage.amount_reserved_other_minor", 1),
    "two uses of a single-use grant": _both(
        _set("hnp.usage.uses_consumed", 1),
        _set("hnp.usage.uses_reserved_other", 1),
        _set("hnp.usage.amount_consumed_minor", 1),
        _set("hnp.usage.amount_reserved_other_minor", 1),
    ),
    "own reservation besides a consumed use": _both(
        _own_reservation(),
        _set("hnp.usage.uses_consumed", 1),
        _set("hnp.usage.amount_consumed_minor", 1),
    ),
    "usage of another grant": _set("hnp.usage.grant_ref", OTHER_REF),
    "usage at another grant version": _set("hnp.usage.state_version", 2),
    "header at another grant version": _set("expected_local_state_version", 2),
    "own reservation of another operation": _own_reservation(operation_id="op_2"),
    "own reservation of another grant": _own_reservation(grant_ref=OTHER_REF),
}


@pytest.mark.parametrize("case", sorted(INCOHERENT_D2))
def test_incoherent_usage_is_facts_unavailable_in_d2(case: str) -> None:
    assert delegation_prerequisite(d2(INCOHERENT_D2[case])) == "GOV_FACTS_UNAVAILABLE"


INCOHERENT_D3: dict[str, Change] = {
    "usage without the reservation": _set("usage.same_operation_reservation", None),
    "usage shows another reservation": _set(
        "usage.same_operation_reservation.reservation_id", "rsv_2"
    ),
    "own reservation besides a consumed use": _both(
        _set("usage.uses_consumed", 1), _set("usage.amount_consumed_minor", 1)
    ),
    "own reservation besides another": _both(
        _set("usage.uses_reserved_other", 1), _set("usage.amount_reserved_other_minor", 1)
    ),
    "usage at another grant version": _set("usage.state_version", 3),
    "reservation of another operation": _reservation(operation_id="op_2"),
}


@pytest.mark.parametrize("case", sorted(INCOHERENT_D3))
def test_incoherent_usage_is_facts_unavailable_in_d3(case: str) -> None:
    assert dispatch_prerequisite(d3(INCOHERENT_D3[case])) == "GOV_FACTS_UNAVAILABLE"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_set("freshness.status", "stale"), "GOV_FRESHNESS_UNRESOLVED"),
        (_set("freshness.status", "unresolved"), "GOV_FRESHNESS_UNRESOLVED"),
        (_set("terms.commercial_expires_at", NOW), "GOV_FRESHNESS_UNRESOLVED"),
        (
            _set(
                "authorization_profile",
                {
                    "schema_version": "commerce-policy/1.0",
                    "status": "unresolved",
                    "profile_ref": None,
                },
            ),
            "GOV_AUTHORIZATION_PROFILE_UNRESOLVED",
        ),
        (_set("hnp.open_proof.status", "unresolved"), "GOV_AUTHORIZATION_PROFILE_UNRESOLVED"),
        # Incoherent facts are reported before anything else.
        (
            _both(_set("freshness.status", "stale"), _set("hnp.usage.amount_consumed_minor", 1)),
            "GOV_FACTS_UNAVAILABLE",
        ),
    ],
)
def test_d2_prerequisites(change: Change, reason: str) -> None:
    assert delegation_prerequisite(d2(change)) == reason


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_set("freshness.status", "stale"), "GOV_FRESHNESS_UNRESOLVED"),
        (_set("terms.commercial_expires_at", NOW), "GOV_FRESHNESS_UNRESOLVED"),
        (_set("closed_proof.status", "unresolved"), "GOV_AUTHORIZATION_PROFILE_UNRESOLVED"),
    ],
)
def test_d3_prerequisites(change: Change, reason: str) -> None:
    assert dispatch_prerequisite(d3(change)) == reason


def test_an_invalid_proof_is_a_policy_deny_not_a_missing_fact() -> None:
    """`invalid` reaches the rules and denies (→ mandate_proof_invalid); only `unresolved`
    is a guard, which escalates (§8.1)."""
    open_invalid = d2(_set("hnp.open_proof.status", "invalid"))
    closed_invalid = d3(_set("closed_proof.status", "invalid"))
    assert delegation_prerequisite(open_invalid) is None
    assert dispatch_prerequisite(closed_invalid) is None
    assert failing(delegation_results(open_invalid)) == ["POL-07"]
    assert failing(dispatch_results(closed_invalid, DISPATCH_ACTIONS)) == ["POL-10"]
