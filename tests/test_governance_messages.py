"""Governance stops reach the chat as clear Spanish copy, never as «Ocurrió un error inesperado»."""

import json
import re
from pathlib import Path

import pytest
from assistant_support import PURCHASE_REQUEST, add_message, assistant_body, post_assistant
from test_purchase_trace_api import ALLIED_STORE, as_buyer, shop

from commerce_agent.api import error_response
from commerce_agent.application import PurchaseServiceError
from commerce_agent.assistant_transport import DEFAULT_ERROR, error_message
from commerce_agent.completion import CompletionError

SOURCE = Path(__file__).parents[1] / "src" / "commerce_agent"
RAISED_CODE = re.compile(r"""code=["'](d1_[a-z_]+)["']""")
INTERNAL_TERMS = re.compile(r"\bD1\b|POL-0|shadow|enforce|\bd1_|GOV_|runtime|ledger", re.IGNORECASE)


def d1_codes() -> set[str]:
    """Every D1 code a service raises, in the governance and application layers and beyond."""
    return {
        code
        for path in SOURCE.rglob("*.py")
        for code in RAISED_CODE.findall(path.read_text(encoding="utf-8"))
    }


def test_the_scan_finds_the_known_d1_codes() -> None:
    assert {"d1_denied", "d1_unavailable", "d1_dispatch_blocked"} <= d1_codes()


@pytest.mark.parametrize(
    "code",
    sorted(d1_codes() | {"checkout_terms_changed", "offer_expired", "offer_freshness_unavailable"}),
)
def test_governance_code_has_clear_copy_without_internals(code: str) -> None:
    message = error_message(code)
    assert message != DEFAULT_ERROR
    assert not INTERNAL_TERMS.search(message), message
    assert message.endswith(".")


@pytest.mark.parametrize("code", ["checkout_expired", "checkout_not_ready"])
def test_an_unapprovable_checkout_has_spanish_copy(code: str) -> None:
    # Completion also raises these, maybe after an uncertain attempt: no promise about charges.
    message = error_message(code)
    assert message != DEFAULT_ERROR and "no seguí con la compra" in message
    assert "cobro" not in message


@pytest.mark.parametrize("error_type", [PurchaseServiceError, CompletionError])
@pytest.mark.parametrize("code", sorted(d1_codes()))
def test_a_rest_governance_stop_carries_the_chat_copy(code: str, error_type: type) -> None:
    # REST clients (approval card, /complete) show `message` as is; never the internal text.
    response = error_response(error_type("D1 internal detail", code=code))
    assert json.loads(bytes(response.body)) == {"code": code, "message": error_message(code)}


@pytest.mark.parametrize("code", ["out_of_stock", "offer_expired", "invalid_item"])
def test_a_governed_creation_refusal_carries_the_chat_copy(code: str) -> None:
    # The merchant refused D1's one POST: REST shows the same copy as the chat and as B0.
    response = error_response(PurchaseServiceError("Merchant refused the checkout", code=code))
    assert response.status_code == 409
    assert json.loads(bytes(response.body)) == {"code": code, "message": error_message(code)}
    assert error_message(code) != DEFAULT_ERROR


def test_policy_block_in_the_chat_says_what_did_not_happen(tmp_path: Path) -> None:
    with shop(tmp_path, "enforce", agent_d1_allowed_merchant_origins=ALLIED_STORE) as current:
        current.client.headers.update(as_buyer())
        _, state = post_assistant(
            current.client, assistant_body([add_message(PURCHASE_REQUEST)], turn_id="blocked")
        )

    message = state["messages"][-1]
    assert message["status"]["reason"] == "error"
    assert message["status"]["error"] == error_message("d1_denied") != DEFAULT_ERROR
    assert "No se creó ningún checkout ni se hizo ningún cobro" in message["status"]["error"]
    failed = [part for part in message["content"] if part.get("isError")]
    assert failed[-1]["result"]["error"]["code"] == "d1_denied"


def test_a_policy_block_names_its_purchase_so_the_chat_can_read_the_trace(tmp_path: Path) -> None:
    # The failed step has no result to carry the run, so the chat reads it from the step's args.
    with shop(tmp_path, "enforce", agent_d1_allowed_merchant_origins=ALLIED_STORE) as current:
        current.client.headers.update(as_buyer())
        _, state = post_assistant(
            current.client, assistant_body([add_message(PURCHASE_REQUEST)], turn_id="blocked")
        )
        prepare = next(
            part
            for part in state["messages"][-1]["content"]
            if part.get("toolName") == "prepare_checkout"
        )
        trace = current.trace(prepare["args"]["run_id"])

    assert prepare["isError"] is True
    assert trace["status"] == "blocked"
    assert trace["steps"][-1]["decision"]["rules"][1]["result"] == "fail"
