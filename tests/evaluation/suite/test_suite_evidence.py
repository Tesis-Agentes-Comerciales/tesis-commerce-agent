"""The evidence grader: verify_database's chains are complete and have the expected links ok."""

from pathlib import Path
from typing import Any

from commerce_evaluation.demo_stack import OTHER_MERCHANT
from commerce_evaluation.suite.graders.evidence import GOVERNANCE_LINKS, evidence, verified
from commerce_evaluation.verify_evidence import LINKS
from tests.evaluation.suite.trials import case, record
from tests.governance.test_hp_continuation import ask
from tests.support.ap2_purchase import ap2_shop

DENIED = dict.fromkeys(LINKS, "not_applicable") | {"intent": "ok", "d1_decision": "ok"}


def chain(complete: bool = True, **statuses: str) -> dict[str, Any]:
    """A verify_database document: a D1 deny's links, with `statuses` on top."""
    links = {name: {"status": status, "detail": "", "refs": {}} for name, status in DENIED.items()}
    for name, status in statuses.items():
        links[name]["status"] = status
    return {"run_id": "run_1", "order_id": None, "links": links, "complete": complete}


def test_a_complete_chain_with_the_expected_links_ok_passes() -> None:
    expecting = case(evidence=["intent", "d1_decision"])

    grade = evidence(expecting, record(expecting, evidence=(chain(),)))

    assert (grade.verdict, grade.reason) == (
        "pass",
        "Cadena completa, con ok en intent, d1_decision",
    )


def test_an_incomplete_chain_fails_even_when_none_is_expected() -> None:
    grade = evidence(case(), record(case(), evidence=(chain(False, d1_decision="mismatch"),)))

    assert (grade.verdict, grade.reason) == (
        "fail",
        "La cadena de run_1 no está completa (d1_decision mismatch)",
    )


def test_an_expected_link_that_is_not_ok_fails() -> None:
    expecting = case(evidence=["intent", "checkout_created"])

    grade = evidence(expecting, record(expecting, evidence=(chain(),)))

    assert (grade.verdict, grade.reason) == ("fail", "Ninguna cadena tiene ok en checkout_created")


def test_one_chain_must_have_every_expected_link_ok() -> None:
    expecting = case(evidence=["intent", "d1_decision"])
    halves = (chain(d1_decision="not_applicable"), chain(intent="not_applicable"))

    grade = evidence(expecting, record(expecting, evidence=halves))

    assert (grade.verdict, grade.reason) == ("fail", "Ninguna cadena tiene ok en d1_decision")


def test_expected_links_need_a_chain() -> None:
    expecting = case(evidence=["intent"])

    assert evidence(expecting, record(expecting)).verdict == "fail"
    assert evidence(case(), record(case())).verdict == "pass"


def ungoverned(**statuses: str) -> dict[str, Any]:
    """A B0 purchase with an order: every governance link missing, the webhook ok."""
    missing = dict.fromkeys(GOVERNANCE_LINKS & set(LINKS), "missing")
    return chain(False, **(missing | {"order_webhook": "ok"} | statuses))


def test_under_b0_the_governance_links_are_set_aside() -> None:
    expecting = case(evidence=["order_webhook"])

    grade = evidence(expecting, record(expecting, evidence=(ungoverned(),), governance="B0"))

    assert (grade.verdict, grade.reason) == (
        "pass",
        "Cadena completa sin los eslabones de gobernanza (B0), con ok en order_webhook",
    )


def test_under_b0_any_other_broken_link_fails() -> None:
    expecting = case(evidence=["order_webhook"])
    broken = ungoverned(order_webhook="missing")

    grade = evidence(expecting, record(expecting, evidence=(broken,), governance="B0"))

    assert (grade.verdict, grade.reason) == (
        "fail",
        "La cadena de run_1 no está completa sin los eslabones de gobernanza (B0) "
        "(order_webhook missing)",
    )


def test_under_b0_a_delegado_ledger_chain_must_hold() -> None:
    task = ungoverned() | {"task_id": "ht_1", "ledger_chain": {"status": "mismatch"}}

    grade = evidence(case(), record(case(), evidence=(task,), governance="B0"))

    assert (grade.verdict, grade.reason) == (
        "fail",
        "La cadena de ht_1 no está completa sin los eslabones de gobernanza (B0) "
        "(ledger_chain mismatch)",
    )


def test_under_b2_the_governance_links_count() -> None:
    expecting = case(evidence=["order_webhook"])

    grade = evidence(expecting, record(expecting, evidence=(ungoverned(),)))

    assert grade.verdict == "fail"
    assert grade.reason.startswith("La cadena de run_1 no está completa (intent missing")


def test_a_document_that_is_not_verify_databases_is_unknown() -> None:
    grade = evidence(case(), record(case(), evidence=({"run_id": "run_1", "complete": True},)))

    assert grade.verdict == "unknown"


def test_the_runner_keeps_verify_databases_documents(tmp_path: Path) -> None:
    # A real D1 deny through the app: only the merchant, the PSP and the model are doubles.
    database = tmp_path / "agent.db"
    with ap2_shop(database, agent_d1_allowed_merchant_origins=OTHER_MERCHANT) as shop:
        assert ask(shop.client, "turn-deny").status_code == 409
    expecting = case(evidence=["intent", "d1_decision"])

    documents = verified(database)

    assert [document["complete"] for document in documents] == [True]
    assert evidence(expecting, record(expecting, evidence=documents)).verdict == "pass"
