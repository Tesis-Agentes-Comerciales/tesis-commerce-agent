"""Independent checks on the three frozen CT19 HNP controls."""

from typing import Any

CONTROLS = ("present", "removed", "stock_zero")
STATUSES = {
    "present": (201, None),
    "removed": (404, "invalid_item"),
    "stock_zero": (409, "out_of_stock"),
}
EFFECTS = {"approvals", "orders", "completion_results", "dispatch_outbox"}


def verify_control(control: str, facts: dict[str, Any]) -> bool:
    try:
        sku = facts["sku"]
        if not sku or facts["grant_variants"] != [sku] or facts["agent_variant"] != sku:
            return False
        consented, stimulus, triggered = facts["timeline"]
        if not 0 < consented < stimulus < triggered:
            return False
        if facts["catalog_restored"] is not True or facts["feed_unchanged"] is not True:
            return False
        stimulus = facts["stimulus"]
        if stimulus["references"] != 0 or stimulus["peer_unchanged"] is not True:
            return False
        read = stimulus["scoped_read"]
        row = stimulus["row"]
        if read["status"] != 200 or not read["request_id"]:
            return False
        if control == "removed":
            if row is not None or read["body"]["error"]["code"] != "OFFER_NOT_FOUND":
                return False
        else:
            snapshot = row["snapshot"]
            availability = "in_stock" if control == "present" else "out_of_stock"
            if snapshot["availability"] != availability or read["body"]["data"] != snapshot:
                return False
            if (snapshot["available_quantity"] > 0) != (control == "present"):
                return False

        (create,) = facts["creates"]
        status, code = STATUSES[control]
        audit = create["audit"]
        if (
            create["upstream"] is not True
            or create["fault"] != "pass"
            or create["sku"] != sku
            or not create["request_id"]
            or audit["request_id"] != create["request_id"]
            or audit["idempotency_sha256"] != facts["idempotency_sha256"]
            or audit["operation"] != "create"
            or (create["status"], create["code"]) != (status, code)
            or (audit["http_status"], audit["error_code"]) != (status, code)
        ):
            return False

        before = facts["merchant_before"]
        after = facts["merchant_after"]
        cleanup = facts["after_cleanup"]
        if before["checkouts"] or any(
            before["tables"][table] != state["tables"][table]
            for state in (after, cleanup)
            for table in ("orders", "checkout_completion_attempts")
        ):
            return False
        if (
            facts["psp_before"]["delegations"]["count"] != 0
            or facts["psp_before"] != facts["psp_after"]
            or facts["psp_before"] != facts["psp_after_cleanup"]
            or facts["payment_calls"] != 0
            or facts["completion_calls"] != 0
            or facts["agent_effect_counts"] != dict.fromkeys(EFFECTS, 0)
            or facts["mandate_kinds"] != ["open_checkout", "open_payment"]
        ):
            return False

        task = facts["task"]
        terminal = task["state"], task["outcome_code"], task["merchant_refusal"]
        if control == "present":
            (checkout,) = after["checkouts"]
            (canceled,) = cleanup["checkouts"]
            return (
                create["accepted_sku"] == sku
                and checkout[2:4] == [sku, 1]
                and canceled == [checkout[0], "canceled", *checkout[2:]]
                and facts["reservations"] == [["released", "no_outbox"]]
                and facts["safety_revoked"] is True
                and terminal == ("canceled", "revoked_by_owner", None)
            )
        return (
            create["accepted_sku"] is None
            and not after["checkouts"]
            and not cleanup["checkouts"]
            and facts["reservations"] == []
            and facts["dispatch"] == [["definitive_failure", None, "VERIFIED_NOT_CREATED"]]
            and facts["safety_revoked"] is False
            and terminal == ("failed", "checkout_failed", code)
        )
    except (IndexError, KeyError, TypeError, ValueError):
        return False
