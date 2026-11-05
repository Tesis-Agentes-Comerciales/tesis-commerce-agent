import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from commerce_evaluation.p3_protocol import (
    CheckoutWitness,
    Creation,
    ProtocolBlocked,
    ProtocolGate,
    equivalent_clones,
    verified_cleanup,
)
from commerce_evaluation.scenarios_p0 import choose_ux03

ORACLE = Path("evaluation/scenarios/p0/ux03-oracle.json")
ROLES = ("A-cal", "B-cal", "V", "E")


def witness(offer_id, run_id):
    offer = {
        "id": offer_id,
        "revision": 3,
        "availability": "in_stock",
        "pricing": {
            "currency": "cop",
            "items_total_minor": 100,
            "shipping_total_minor": 7,
            "total_minor": 107,
        },
        "delivery_context": {"country": "CO"},
        "expires_at": "2026-09-10T15:00:00Z",
    }
    checkout = f"{run_id}:{offer_id}"
    wire = {
        "id": checkout,
        "currency": "cop",
        "expires_at": offer["expires_at"],
        "totals": [
            {"type": "items", "amount": 100},
            {"type": "fulfillment", "amount": 7},
            {"type": "total", "amount": 107},
        ],
        "line_items": [{"quantity": 1, "item": {"id": offer_id}}],
    }
    persisted = dict(
        id=checkout,
        run_id=run_id,
        actor_id=f"actor-{run_id}",
        offer_id=offer_id,
        quantity=1,
        offer_revision=3,
        pricing=offer["pricing"].copy(),
        delivery_context={"country": "CO"},
    )
    return CheckoutWitness(wire, persisted, offer, run_id, f"actor-{run_id}")


def clone_snapshot() -> dict[str, dict[str, Any]]:
    return {
        role: dict(
            run_id=role,
            actor_id=f"actor-{role}",
            session_alias=role,
            agent_store=role,
            thread=role,
            operation_scope=role,
            offers=[witness("A", role).offer, witness("B", role).offer],
            feed_sha256="same-feed",
            clock="2026-09-10T14:00:00Z",
            configuration={"mode": "HP", "governance": "enforce", "quantity": 1},
        )
        for role in ROLES
    }


def oracle_bytes():
    return ORACLE.read_text(encoding="utf-8").encode()


def test_pv01_authority_is_the_global_total_not_the_line():
    assert witness("A", "A-cal").total() == 107


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "bool",
        "float",
        "currency",
        "quantity",
        "other_offer",
        "actor",
        "revision",
        "context",
        "shipping",
    ],
)
def test_pv01_rejects_incompatible_witnesses(defect):
    w = witness("A", "A-cal")
    if defect == "missing":
        w.wire["totals"].pop()
    elif defect in {"bool", "float"}:
        w.wire["totals"][2]["amount"] = True if defect == "bool" else 107.0
    elif defect == "currency":
        w.wire["currency"] = "usd"
    elif defect == "quantity":
        w.wire["line_items"][0]["quantity"] = True
    elif defect == "other_offer":
        w.wire["line_items"][0]["item"]["id"] = "B"
    elif defect == "actor":
        w.persisted["actor_id"] = "foreign"
    elif defect == "revision":
        w.persisted["offer_revision"] = 4
    elif defect == "context":
        w.persisted["delivery_context"] = {"country": "US"}
    else:
        w.wire["totals"][1]["amount"] = 8
    with pytest.raises(ProtocolBlocked):
        w.total()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision", 4),
        ("availability", "out_of_stock"),
        ("expires_at", "different"),
        ("delivery_context", {"country": "US"}),
        ("pricing", {"currency": "usd"}),
    ],
)
def test_pv02_detects_offer_drift_with_identical_feed(field, value):
    clones = clone_snapshot()
    assert equivalent_clones(clones)
    clones["V"]["offers"][0][field] = value
    with pytest.raises(ProtocolBlocked, match="Non-equivalent"):
        equivalent_clones(clones)


@pytest.mark.parametrize("field", ["clock", "configuration", "session_alias", "agent_store"])
def test_pv02_detects_context_drift_or_shared_state(field):
    clones = clone_snapshot()
    clones["E"][field] = clones["V"][field] if field in {"session_alias", "agent_store"} else {}
    with pytest.raises(ProtocolBlocked):
        equivalent_clones(clones)


def test_pv03_replay_and_recovered_creation_count_once():
    lost = Creation("V", "key", "lost", "checkout", "lookup")
    replay = replace(lost, result="replay", witness="response")
    readbacks = {"checkout": "canceled"}
    persisted = {"checkout": ("V", "canceled")}
    assert verified_cleanup([lost, replay], readbacks, persisted)
    for attempts, reads, rows in (
        ([replace(lost, checkout=None)], readbacks, persisted),
        ([lost, replace(replay, checkout="other")], readbacks, persisted),
        ([lost], {}, persisted),
        ([lost], readbacks, persisted | {"extra": ("V", "canceled")}),
    ):
        with pytest.raises(ProtocolBlocked):
            verified_cleanup(attempts, reads, rows)


def gate_through_cleanup():
    gate = ProtocolGate(oracle_bytes())
    witnesses = {
        "A": (witness("A", "A-cal"), witness("A", "V")),
        "B": (witness("B", "B-cal"), witness("B", "V")),
    }
    gate.calibrate(witnesses)
    attempts = [
        Creation(w.run_id, w.wire["id"], "accepted", w.wire["id"], "response")
        for pair in witnesses.values()
        for w in pair
    ]
    persisted = {w.wire["id"]: (w.run_id, "canceled") for pair in witnesses.values() for w in pair}
    gate.cleanup(attempts, dict.fromkeys(persisted, "canceled"), persisted)
    return gate


def test_pv04_fails_closed_on_early_turn_and_e_contamination():
    gate = gate_through_cleanup()
    with pytest.raises(ProtocolBlocked, match="Seal out of phase"):
        gate.seal({})
    with pytest.raises(ProtocolBlocked, match="Observation before seal"):
        gate.observe("UX03-A", "B", {})
    with pytest.raises(ProtocolBlocked, match="E state changed"):
        gate.compare(clone_snapshot(), {"run_id": "E"}, {"run_id": "E", "effects": 1})


def test_pv04_blocks_equivalent_clones_that_are_not_bound_to_calibration():
    gate = gate_through_cleanup()
    clones = clone_snapshot()
    for clone in clones.values():
        clone["offers"][0]["revision"] += 1
    assert equivalent_clones(clones)
    with pytest.raises(ProtocolBlocked, match="Calibration Offer absent"):
        gate.compare(clones, {"run_id": "E"}, {"run_id": "E"})


def test_pv09_seal_and_scorer_leave_an_adverse_proposal_incorrect():
    with pytest.raises(ProtocolBlocked, match="oracle hash"):
        ProtocolGate(oracle_bytes() + b" ")
    gate = gate_through_cleanup()
    gate.compare(clone_snapshot(), {"run_id": "E", "effects": 0}, {"run_id": "E", "effects": 0})
    cells = json.loads(oracle_bytes())["cells"]
    inputs = {c["id"]: {"input": c["id"]} for c in cells}
    with pytest.raises(ProtocolBlocked, match="Incomplete case inputs"):
        gate.seal({"UX03-A": {"input": "one case"}})
    assert gate.seal(inputs)
    with pytest.raises(ProtocolBlocked, match="Cannot score"):
        gate.score("UX03-A")
    with pytest.raises(ProtocolBlocked, match="inputs changed"):
        gate.observe("UX03-A", "B", inputs | {"UX03-A": {"input": "tampered"}})
    for cell in cells:
        case = cell["id"]
        proposal = "B" if case in {"UX03-A", "UX03-UNKNOWN-A"} else cell["expected"]
        gate.observe(case, proposal, inputs)
        assert gate.score(case) is (proposal == cell["expected"])
    assert choose_ux03(cells[1]["a"], cells[1]["b"]) == "B"
    unknown = next(c for c in cells if c["id"] == "UX03-UNKNOWN-A")
    assert choose_ux03(unknown["a"], unknown["b"]) == "undetermined"


def test_pv09_clarify_is_scored_as_adverse_product_decision():
    """Unexpected clarify is not an invalid experimental setup or a changed oracle."""
    gate = gate_through_cleanup()
    initial_e = {"run_id": "E", "effects": 0}
    gate.compare(clone_snapshot(), initial_e, initial_e)
    cells = json.loads(oracle_bytes())["cells"]
    inputs = {cell["id"]: {"input": cell["id"]} for cell in cells}
    gate.seal(inputs)
    gate.observe("UX03-A", "clarify", inputs)
    assert gate.score("UX03-A") is False
    with pytest.raises(ProtocolBlocked, match="Invalid proposal label"):
        gate.observe("UX03-B", "not-a-decision", inputs)
