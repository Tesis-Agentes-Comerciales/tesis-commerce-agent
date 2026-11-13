"""The Merchant control worker's loop: one JSON command per line, one JSON answer."""

import json
import sys

from commerce_evaluation.stack.merchant_control.core import bootstrap
from commerce_evaluation.stack.merchant_control.ct19 import Ct19Control
from commerce_evaluation.stack.merchant_control.offers import OfferControl
from commerce_evaluation.stack.merchant_control.p3 import P3Control


class Control(OfferControl, Ct19Control, P3Control):
    """Every operation the harness may call."""


def main() -> None:
    control = Control(bootstrap())
    operations = {
        name: getattr(control, name)
        for name in (
            "prepare",
            "identity",
            "episode",
            "session",
            "purchase_episode",
            "dispatch_webhooks",
            "order_update",
            "session_state",
            "snapshot",
            "effects",
            "exp01_export",
            "exp01_observe",
            "internal_create",
            "commit_pending",
            "cancel_fixture",
            "alter_offer_fixture",
            "managed_catalog",
            "edit_offer",
            "observe_offer",
            "reprice_offer",
            "projection",
            "p0_provenance",
            "p0_managed_update",
            "p0_episode_state",
            "ct19_state",
            "ct19_capture",
            "ct19_audit",
            "ct19_constraints",
            "ct19_mutate",
            "p3_state",
            "p3_mutate",
        )
    }
    try:
        for line in sys.stdin:
            command = json.loads(line)
            if command["operation"] == "shutdown":
                print(json.dumps({"ok": True, "data": None}), flush=True)
                return
            try:
                data = operations[command["operation"]](command)
                print(json.dumps({"ok": True, "data": data}), flush=True)
            except Exception as error:
                # Never reflect tokens, requests, DSNs, rows or exception text.
                print(json.dumps({"ok": False, "error_type": type(error).__name__}), flush=True)
    finally:
        control.close()


if __name__ == "__main__":
    main()
