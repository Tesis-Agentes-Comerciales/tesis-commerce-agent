"""Case execution only: oracle labels never enter roles or coordinator."""

import json
from pathlib import Path
from typing import Any, Literal

from commerce_agent.governance.canonical import parse_model

from .adapters import ScriptedBuyerAgent, ScriptedValidatorAgent, independent_assessment
from .context import FixtureContextResolver, SyntheticSource, source_ref
from .coordinator import ReviewCoordinator
from .evidence import ExperimentalEvidence
from .models import (
    CHECKS,
    REASONS,
    Condition,
    DemoResult,
    ExperimentalModel,
    Id,
    ProposalEnvelope,
    TrustedReviewContext,
    wire_bytes,
)
from .sink import SimulatedEffectSink


class CaseSpec(ExperimentalModel):
    case_id: Id
    source: SyntheticSource
    buyer_payload: dict[str, Any]
    validator_behavior: Literal[
        "normal",
        "false_accept",
        "false_reject",
        "omit",
        "timeout",
        "replay",
        "abstain",
        "malformed",
        "unavailable",
    ]
    next_source: SyntheticSource | None


def load_cases(path: Path) -> tuple[CaseSpec, ...]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    cases = tuple(parse_model(CaseSpec, wire_bytes(row)) for row in rows)
    if len({c.case_id for c in cases}) != len(cases):
        raise ValueError("Duplicate case")
    return cases


def response_for(behavior: str):
    def respond(proposal: ProposalEnvelope, context: TrustedReviewContext) -> object:
        if behavior == "timeout":
            raise TimeoutError("synthetic timeout")
        if behavior == "unavailable":
            raise RuntimeError("private-test-error-must-not-be-retained")
        if behavior == "malformed":
            return {"verdict": "accept", "bearer_token": "private-test-marker"}
        result = independent_assessment(proposal, context).model_dump(mode="json")
        if behavior == "false_accept":
            for check in result["checks"]:
                check["result"] = "pass"
            result.update(verdict="accept", reasons=[])
        elif behavior in ("false_reject", "abstain"):
            result["checks"][0]["result"] = "fail" if behavior == "false_reject" else "unresolved"
            result.update(
                verdict="reject" if behavior == "false_reject" else "abstain",
                reasons=[REASONS[CHECKS[0]]],
            )
        elif behavior == "omit":
            result["checks"] = result["checks"][:-1]
            result["coverage"] = result["coverage"][:-1]
        elif behavior == "replay":
            result["proposal_digest"] = "sha256:" + "0" * 64
        return result

    return respond


def run_case(
    case: CaseSpec,
    condition: Condition,
    evidence: ExperimentalEvidence,
) -> tuple[DemoResult, SimulatedEffectSink]:
    # Same source, buyer payload and mandatory controls in both conditions.
    resolver = FixtureContextResolver(case.source, case.next_source)
    buyer = ScriptedBuyerAgent(case.buyer_payload)
    validator = (
        ScriptedValidatorAgent(response_for(case.validator_behavior)) if condition == "B" else None
    )
    sink = SimulatedEffectSink()
    coordinator = ReviewCoordinator(
        buyer=buyer,
        validator=validator,
        resolver=resolver,
        evidence=evidence,
        sink=sink,
        condition=condition,
        case_configuration_ref=source_ref("case-configuration", 1, case),
    )
    return coordinator.run(case.case_id), sink
