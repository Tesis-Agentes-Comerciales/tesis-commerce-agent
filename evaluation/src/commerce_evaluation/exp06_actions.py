"""Attestation of a direct, explicit Actions dispatch; never calls a model."""

import argparse
import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from commerce_evaluation.exp06 import SpendingBlocked, protocol
from commerce_evaluation.exp06_execution import ORIGINAL_HASH

REPOSITORY = "Tesis-Agentes-Comerciales/tesis-commerce-agent"
WORKFLOW = REPOSITORY + "/.github/workflows/exp06-smoke.yml@"
ACKNOWLEDGEMENT = "AUTHORIZE_EXP06_SMOKE"


def dispatch_receipt() -> dict[str, Any]:
    """Only a direct dispatch with matching SHA/model/USD/execution can authorize CI."""
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or not os.environ.get("GITHUB_WORKFLOW_REF", "").startswith(WORKFLOW)
        or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"
        or os.environ.get("EXP06_MANUAL_SMOKE") != "1"
    ):
        raise SpendingBlocked("direct_manual_smoke_dispatch_required_no_reruns")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    inputs = event["inputs"]
    sha = inputs.get("agent_sha", "")
    amount = Decimal(inputs.get("max_usd", "0"))
    if (
        inputs.get("mode") != "smoke"
        or inputs.get("model") != protocol()["model"]
        or inputs.get("authorization") != ACKNOWLEDGEMENT
        or str(inputs.get("billing_project_confirmed")).lower() != "true"
        or str(inputs.get("standard_pricing_confirmed")).lower() != "true"
        or not re.fullmatch(r"[0-9a-f]{40}", sha)
        or sha != os.environ.get("GITHUB_SHA")
        or not amount.is_finite()
        or not Decimal("0") < amount <= Decimal("9")
        or not os.environ.get("GITHUB_RUN_ID", "").isdigit()
    ):
        raise SpendingBlocked("model_SHA_budget_and_explicit_execution_must_match")
    run = os.environ["GITHUB_RUN_ID"]
    return {
        "authorized": True,
        "authorization_id": REPOSITORY + ":" + run,
        "human_reference": "https://github.com/" + REPOSITORY + "/actions/runs/" + run,
        "executor_sha": sha,
        "model": protocol()["model"],
        "protocol_sha256": ORIGINAL_HASH,
        "stage": "smoke",
        "max_usd": str(amount),
        "billing_project_confirmed": True,
        "standard_pricing_confirmed": True,
    }


def matches_dispatch(data: dict[str, Any]) -> bool:
    try:
        expected = dispatch_receipt()
        return data == expected
    except (SpendingBlocked, OSError, KeyError, ValueError, TypeError, InvalidOperation):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-authorization", type=Path, required=True)
    args = parser.parse_args()
    receipt = dispatch_receipt()
    with args.create_authorization.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(receipt, indent=2) + "\n")
    print("Explicit dispatch scope verified; no inference performed")


if __name__ == "__main__":
    main()
