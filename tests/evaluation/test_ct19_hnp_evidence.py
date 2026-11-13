from collections.abc import Callable
from typing import Any

import pytest

from commerce_evaluation.ct19_hnp import verify_control


def removed() -> dict[str, Any]:
    state = {"checkouts": [], "tables": {"orders": 0, "checkout_completion_attempts": 0}}
    psp = {"delegations": {"count": 0}}
    return {
        "sku": "variant",
        "grant_variants": ["variant"],
        "agent_variant": "variant",
        "timeline": [1, 2, 3],
        "catalog_restored": True,
        "feed_unchanged": True,
        "stimulus": {
            "row": None,
            "references": 0,
            "peer_unchanged": True,
            "scoped_read": {
                "status": 200,
                "request_id": "read",
                "body": {"error": {"code": "OFFER_NOT_FOUND"}},
            },
        },
        "creates": [
            {
                "upstream": True,
                "fault": "pass",
                "sku": "variant",
                "accepted_sku": None,
                "request_id": "create",
                "status": 404,
                "code": "invalid_item",
                "audit": {
                    "request_id": "create",
                    "operation": "create",
                    "http_status": 404,
                    "error_code": "invalid_item",
                    "idempotency_sha256": "hash",
                },
            }
        ],
        "idempotency_sha256": "hash",
        "merchant_before": state,
        "merchant_after": state,
        "after_cleanup": state,
        "psp_before": psp,
        "psp_after": psp,
        "psp_after_cleanup": psp,
        "payment_calls": 0,
        "completion_calls": 0,
        "agent_effect_counts": dict.fromkeys(
            ("approvals", "orders", "completion_results", "dispatch_outbox"), 0
        ),
        "mandate_kinds": ["open_checkout", "open_payment"],
        "task": {
            "state": "failed",
            "outcome_code": "checkout_failed",
            "merchant_refusal": "invalid_item",
        },
        "reservations": [],
        "dispatch": [["definitive_failure", None, "VERIFIED_NOT_CREATED"]],
        "safety_revoked": False,
    }


def test_accepts_only_removed_control() -> None:
    assert verify_control("removed", removed())
    assert not verify_control("stock_zero", removed())
    assert not verify_control("present", removed())


def test_tokens_other_tests_left_in_the_session_provider_do_not_count() -> None:
    facts = removed()
    earlier = {"delegations": {"count": 1}}
    facts.update(psp_before=earlier, psp_after=earlier, psp_after_cleanup=earlier)
    assert verify_control("removed", facts)


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda f: f["grant_variants"].append("other"),
        lambda f: f.update(agent_variant="other"),
        lambda f: f["stimulus"].update(row={}),
        lambda f: f.update(timeline=[1, 3, 2]),
        lambda f: f["creates"].append(f["creates"][0]),
        lambda f: f["creates"][0].update(upstream=False),
        lambda f: f["creates"][0]["audit"].update(error_code="out_of_stock"),
        lambda f: f["task"].update(state="uncertain"),
        lambda f: f.update(catalog_restored=False),
        lambda f: f.update(after_cleanup={}),
        lambda f: f.update(payment_calls=1),
        lambda f: f.update(psp_after={"delegations": {"count": 1}}),
        # A snapshot without the provider's delegations proves nothing about its tokens.
        lambda f: f.update(psp_before={}, psp_after={}, psp_after_cleanup={}),
        lambda f: f.update(reservations=[["reserved", None]]),
    ],
)
def test_rejects_incompatible_observations(corrupt: Callable[[dict[str, Any]], None]) -> None:
    facts = removed()
    corrupt(facts)
    assert not verify_control("removed", facts)
