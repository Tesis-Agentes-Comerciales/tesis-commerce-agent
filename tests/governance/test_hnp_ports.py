"""The executor's port to D2-HNP and D3-HNP: a gate in `off` fabricates no decision."""

import dataclasses

import pytest

from commerce_agent.acp.models import CheckoutSession
from commerce_agent.governance.hnp_ports import HnpGateDecision, HnpPolicyGate


class OffGate:
    """B0-HNP: no evaluator, no decision, no evidence and nothing to run in the ledger."""

    def decide_d2(
        self,
        *,
        run_id: str,
        grant_id: str,
        checkout: CheckoutSession,
        reservation_id: str,
        operation_id: str,
    ) -> HnpGateDecision:
        return HnpGateDecision(decision=None, proof=None, evidence_id=None)

    def decide_d3(
        self,
        *,
        run_id: str,
        grant_id: str,
        checkout: CheckoutSession,
        reservation_id: str,
        operation_id: str,
    ) -> HnpGateDecision:
        return HnpGateDecision(decision=None, proof=None, evidence_id=None)


def test_a_gate_in_off_satisfies_the_port_without_a_decision() -> None:
    gate: HnpPolicyGate = OffGate()
    checkout = CheckoutSession.model_construct()
    for decide in (gate.decide_d2, gate.decide_d3):
        result = decide(
            run_id="run-1",
            grant_id="dg_1",
            checkout=checkout,
            reservation_id="rsv_1",
            operation_id="op_1",
        )
        assert (result.decision, result.proof, result.evidence_id, result.hooks) == (
            None,
            None,
            None,
            (),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.evidence_id = "evidence-1"  # type: ignore[misc]
