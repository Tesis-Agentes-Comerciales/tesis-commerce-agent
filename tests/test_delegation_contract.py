"""The Pydantic delegation views validate the same fixtures as `parseDelegation*` in the UI.

docs/design/hnp-governed.md §5.3 and §8.3. The rejection cases mirror the `Delegation/Contract`
stories in ui/src/storybook/fixtures/delegation-contract.stories.tsx.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from commerce_agent.delegation.contract import (
    HNP_MESSAGES,
    DelegationList,
    DelegationProposal,
    DelegationView,
    EscalationCode,
    RevokeAllReceipt,
    RevokeReceipt,
    escalation_message,
)

FIXTURES = Path(__file__).parents[1] / "ui" / "src" / "storybook" / "fixtures"
VIEW_STATES = (
    "active",
    "running",
    "purchased",
    "escalated",
    "revoked",
    "expired",
    "failed",
    "uncertain",
)
MODELS: dict[str, type[BaseModel]] = {
    "proposed": DelegationProposal,
    "revoke-receipts": RevokeAllReceipt,
    **dict.fromkeys(VIEW_STATES, DelegationView),
}

Json = dict[str, Any]
Edit = Callable[[Json], object]


def fixture(name: str) -> Json:
    return json.loads((FIXTURES / f"delegation-{name}.json").read_text(encoding="utf-8"))


def parse[Model: BaseModel](model: type[Model], item: object) -> Model:
    """The views cross the API as JSON, which is also how strict mode reads a tuple."""
    return model.model_validate_json(json.dumps(item))


def view(name: str) -> DelegationView:
    return parse(DelegationView, fixture(name))


def test_there_is_one_fixture_per_state_and_one_for_the_revoke_receipts() -> None:
    files = {path.stem.removeprefix("delegation-") for path in FIXTURES.glob("delegation-*.json")}
    assert files == set(MODELS)


@pytest.mark.parametrize("name", sorted(MODELS))
def test_ui_fixture_validates_and_round_trips(name: str) -> None:
    raw = (FIXTURES / f"delegation-{name}.json").read_text(encoding="utf-8")
    parsed = MODELS[name].model_validate_json(raw)
    assert parsed.model_dump(mode="json") == json.loads(raw)


def test_the_view_fixtures_cover_each_grant_and_task_state() -> None:
    states = {name: (view(name).state, view(name).task.state) for name in VIEW_STATES}
    assert states == {
        "active": ("active", "scheduled"),
        "running": ("active", "running"),
        "purchased": ("consumed", "purchased"),
        "escalated": ("revoked", "escalated"),
        "revoked": ("revoked", "canceled"),
        "expired": ("expired", "canceled"),
        "failed": ("revoked", "failed"),
        "uncertain": ("active", "uncertain"),
    }
    receipts = parse(RevokeAllReceipt, fixture("revoke-receipts")).receipts
    assert [receipt.effect for receipt in receipts] == ["no_effect", "released", "in_flight"]


def test_a_list_holds_the_same_views() -> None:
    delegations = [fixture(name) for name in VIEW_STATES]
    listed = parse(DelegationList, {"delegations": delegations})
    assert listed.model_dump(mode="json") == {"delegations": delegations}


def test_b0_hnp_fails_with_an_escalation_code_and_no_escalation_view() -> None:
    """§8.1: B0-HNP never escalates; the cause that escalates in B2 fails with the same code."""
    failed = fixture("failed")
    failed["task"]["outcome_code"] = "no_candidate_in_scope"
    assert parse(DelegationView, failed).escalation is None


def test_revoking_a_grant_closed_by_someone_else_has_no_effect() -> None:
    """§5.1: a grant revoked by the operator, an escalation or a failure answers no_effect."""
    receipt = fixture("revoke-receipts")["receipts"][0]
    receipt.update(revoked_reason="operator", message=HNP_MESSAGES["revoke.already_closed"])
    assert parse(RevokeReceipt, receipt).effect == "no_effect"


def escalated(item: Json) -> Json:
    return item["escalation"]


REJECTED: list[Any] = [
    # Fields beyond the contract, at every level.
    pytest.param("proposed", lambda item: item.update(owner_id="buyer-a"), id="proposal-extra"),
    pytest.param(
        "proposed", lambda item: item["scope"].update(address="Calle 1"), id="scope-extra"
    ),
    pytest.param(
        "proposed",
        lambda item: item["scope"]["variants"][0].update(price=1),
        id="variant-extra",
    ),
    pytest.param(
        "proposed",
        lambda item: item["scope"]["total_max"].update(display="COP 2.000.000"),
        id="money-extra",
    ),
    pytest.param(
        "active", lambda item: item.update(scope_digest="sha256:" + "a" * 64), id="view-digest"
    ),
    pytest.param("running", lambda item: item["task"].update(lease_id="l-1"), id="task-extra"),
    pytest.param(
        "escalated", lambda item: escalated(item).update(checkout_id="c-1"), id="escalation-extra"
    ),
    pytest.param(
        "revoke-receipts",
        lambda item: item["receipts"][0].update(reservation_id="rsv_1"),
        id="receipt-extra",
    ),
    pytest.param("revoke-receipts", lambda item: item.update(count=3), id="receipts-extra"),
    # Values outside the contract.
    pytest.param("proposed", lambda item: item["scope"].update(uses=2), id="two-uses"),
    pytest.param("proposed", lambda item: item["scope"].update(uses=True), id="boolean-uses"),
    pytest.param("proposed", lambda item: item["scope"].update(variants=[]), id="no-variants"),
    pytest.param(
        "proposed",
        lambda item: item["scope"].update(variants=item["scope"]["variants"] * 3),
        id="six-variants",
    ),
    pytest.param(
        "proposed", lambda item: item["scope"].update(preference_text=""), id="empty-preference"
    ),
    pytest.param(
        "proposed",
        lambda item: item["scope"].update(preference_text="ñ" * 281),
        id="long-preference",
    ),
    pytest.param(
        "proposed", lambda item: item.update(scope_digest="sha256:abc"), id="short-digest"
    ),
    pytest.param(
        "proposed",
        lambda item: item.update(presented_at="2026-10-21T15:00:00+00:00"),
        id="offset-time",
    ),
    pytest.param("proposed", lambda item: item.update(proposal_id="dg_1"), id="proposal-id-prefix"),
    pytest.param("active", lambda item: item.update(grant_id="ht_1"), id="grant-id-prefix"),
    pytest.param("active", lambda item: item.update(state="pending"), id="unknown-grant-state"),
    pytest.param("purchased", lambda item: item["task"].update(state="done"), id="unknown-task"),
    pytest.param(
        "active", lambda item: item.update(revoked_reason="owner"), id="reason-while-active"
    ),
    pytest.param(
        "revoked", lambda item: item.update(revoked_reason=None), id="revoked-without-reason"
    ),
    pytest.param(
        "active",
        lambda item: item["task"].update(outcome_code="kill_switch"),
        id="code-while-scheduled",
    ),
    pytest.param(
        "failed", lambda item: item["task"].update(outcome_code=None), id="failed-without-code"
    ),
    pytest.param(
        "revoked",
        lambda item: item["task"].update(outcome_code="dispatch_timeout"),
        id="code-of-another-state",
    ),
    pytest.param("escalated", lambda item: item.update(escalation=None), id="missing-escalation"),
    pytest.param(
        "failed",
        lambda item: item.update(escalation=fixture("escalated")["escalation"]),
        id="escalation-without-escalated-task",
    ),
    pytest.param(
        "escalated",
        lambda item: escalated(item).update(reason="offer_changed"),
        id="escalation-of-another-code",
    ),
    pytest.param(
        "revoke-receipts",
        lambda item: item["receipts"][1].update(revoked_reason="operator"),
        id="closed-grant-released",
    ),
    pytest.param(
        "revoke-receipts",
        lambda item: item["receipts"][0].update(state="active"),
        id="receipt-not-revoked",
    ),
]


@pytest.mark.parametrize(("name", "edit"), REJECTED)
def test_views_are_as_strict_as_the_ui_validator(name: str, edit: Edit) -> None:
    item = fixture(name)
    edit(item)
    with pytest.raises(ValidationError):
        parse(MODELS[name], item)


def test_a_list_rejects_fields_beyond_the_contract() -> None:
    with pytest.raises(ValidationError):
        parse(DelegationList, {"delegations": [], "next_cursor": None})


SPEC_MESSAGE_KEYS = {
    "escalation.budget_not_satisfiable",
    "escalation.no_candidate_in_scope",
    "escalation.offer_changed",
    "escalation.terms_out_of_scope",
    "escalation.authorization_profile_unresolved",
    "escalation.verifier_unresolved_constraint",
    "escalation.verifier_mandates_not_supported",
    "escalation.suffix_before_presentation",
    "escalation.suffix_rejected_by_psp",
    "revoke.no_effect",
    "revoke.released",
    "revoke.in_flight",
    "revoke.not_revocable",
    "revoke.already_closed",
    "task.kill_switch",
    "task.grant_expired",
    "task.revoked_by_owner",
    "task.failed",
    "task.uncertain",
    "task.purchased",
    "task.running",
    "task.scheduled",
}


def test_the_canonical_texts_are_the_spec_table_plus_the_escalation_title_and_prefill() -> None:
    assert set(HNP_MESSAGES) == SPEC_MESSAGE_KEYS | {
        "escalation.title",
        "escalation.prefill_request",
    }
    assert HNP_MESSAGES["escalation.title"] == "Necesito tu decisión"
    assert all(text.strip() == text and text for text in HNP_MESSAGES.values())


@pytest.mark.parametrize(
    ("reason", "suffix"),
    [
        ("budget_not_satisfiable", "suffix_before_presentation"),
        ("offer_changed", "suffix_before_presentation"),
        ("verifier_unresolved_constraint", "suffix_rejected_by_psp"),
        ("verifier_mandates_not_supported", "suffix_rejected_by_psp"),
    ],
)
def test_an_escalation_says_whether_anything_reached_the_payment_provider(
    reason: EscalationCode, suffix: str
) -> None:
    message = escalation_message(reason)
    assert (
        message == f"{HNP_MESSAGES[f'escalation.{reason}']} {HNP_MESSAGES[f'escalation.{suffix}']}"
    )


@pytest.mark.parametrize("name", VIEW_STATES)
def test_fixture_messages_are_the_canonical_texts(name: str) -> None:
    task = view(name).task
    if task.state == "escalated":
        assert task.message is None
        return
    key = f"task.{task.outcome_code if task.state == 'canceled' else task.state}"
    if task.state == "purchased":
        expected = HNP_MESSAGES[key].format(
            title="Sony WH-1000XM6 negro", total="COP 1.924.000", order_id=task.order_id
        )
    else:
        expected = HNP_MESSAGES[key]
    assert task.message == expected


def test_the_escalation_fixture_carries_the_canonical_title_message_and_prefill() -> None:
    escalation = view("escalated").escalation
    assert escalation is not None
    assert escalation.title == HNP_MESSAGES["escalation.title"]
    assert escalation.message == escalation_message(escalation.reason)
    assert escalation.prefill_request == HNP_MESSAGES["escalation.prefill_request"].format(
        title="Sony WH-1000XM6 negro", total="COP 1.500.000"
    )


def test_revoke_receipts_carry_the_canonical_text_of_their_effect() -> None:
    for receipt in parse(RevokeAllReceipt, fixture("revoke-receipts")).receipts:
        assert receipt.message == HNP_MESSAGES[f"revoke.{receipt.effect}"]
