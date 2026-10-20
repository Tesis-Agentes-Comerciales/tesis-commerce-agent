"""Only experimental coordinator owns evidence, resolver admission and sink.

A is a separate offline control condition; no operational module imports this code.
"""

from time import perf_counter_ns

from commerce_agent.governance.canonical import parse_model

from .backend import mandatory_failures
from .context import buyer_context, check_refs
from .deadline import call_with_deadline
from .evidence import ExperimentalEvidence
from .models import (
    CHECKS,
    SCHEMA,
    Admission,
    BuyerDraft,
    Condition,
    DemoResult,
    ProposalEnvelope,
    ReviewRecord,
    SourceRef,
    Timing,
    TrustedReviewContext,
    ValidatorAssessment,
    digest,
    wire_bytes,
)
from .ports import BuyerAgent, RoleOutputError, TrustedContextResolver, ValidatorAgent
from .sink import SimulatedEffectSink, _Permit


class ReviewCoordinator:
    def __init__(
        self,
        *,
        buyer: BuyerAgent,
        validator: ValidatorAgent | None,
        resolver: TrustedContextResolver,
        evidence: ExperimentalEvidence,
        sink: SimulatedEffectSink,
        condition: Condition,
        validator_budget_ns: int = 100_000_000,
        case_configuration_ref: SourceRef | None = None,
    ):
        if condition not in ("A", "B") or (condition == "A" and validator is not None):
            raise ValueError("Invalid isolated experiment condition")
        if validator_budget_ns <= 0 or isinstance(validator_budget_ns, bool):
            raise ValueError("Invalid deadline")
        self.buyer, self.validator, self.resolver = buyer, validator, resolver
        self.evidence, self.sink = evidence, sink
        self.condition: Condition = condition
        self.budget = validator_budget_ns
        self.case_configuration_ref = case_configuration_ref
        self._issuer = object()
        sink._bind(self._issuer)
        self._used = False
        self.configuration_digest = digest(
            "hnp-configuration/v1",
            {
                "schema_version": SCHEMA,
                "required_checks": CHECKS,
                "case_configuration_ref": case_configuration_ref,
                "validator_budget_ns": validator_budget_ns,
                "backend_profile": "synthetic-only:1",
            },
        )

    def run(self, experiment_id: str) -> DemoResult:
        if self._used:
            raise ValueError("One shot experiment: no automatic resend")
        self._used = True
        began = perf_counter_ns()
        times: list[Timing] = []
        context: TrustedReviewContext | None = None
        proposal: ProposalEnvelope | None = None
        assessment: ValidatorAssessment | None = None
        assessment_status = "not_called"
        code = "SIMULATED_ONLY"
        failures = ()
        started = perf_counter_ns()
        try:
            context = parse_model(TrustedReviewContext, wire_bytes(self.resolver.resolve()))
        except Exception:
            code = "CONTEXT_UNAVAILABLE"
        times.append(Timing(component="resolver", elapsed_ns=perf_counter_ns() - started))
        if context is not None:
            started = perf_counter_ns()
            try:
                raw = self.buyer.propose(buyer_context(context))
                # Wire errors and RoleOutputError are ValueError: the buyer's own output.
                draft = parse_model(BuyerDraft, wire_bytes(raw))
                body = dict(
                    schema_version=SCHEMA,
                    proposal_id=context.episode_ref.id + ":proposal",
                    revision=1,
                    episode_ref=context.episode_ref,
                    intent_ref=context.intent_ref,
                    buyer_adapter_id=self.buyer.adapter_id,
                    draft=draft,
                )
                proposal = parse_model(
                    ProposalEnvelope,
                    wire_bytes(
                        {
                            **body,
                            "proposal_digest": digest("hnp-proposal/v1", body),
                        }
                    ),
                )
            except ValueError:
                code = "BUYER_INVALID"
            except Exception:
                # Provider or harness failure: blocked, but not a buyer observation.
                code = "BUYER_UNAVAILABLE"
            times.append(Timing(component="buyer", elapsed_ns=perf_counter_ns() - started))
        if context is not None and proposal is not None:
            if self.condition == "B":
                started = perf_counter_ns()
                validator = self.validator
                try:
                    if validator is None:
                        raise RuntimeError("No validator")
                    reviewed = (
                        parse_model(ProposalEnvelope, wire_bytes(proposal)),
                        parse_model(TrustedReviewContext, wire_bytes(context)),
                    )
                    # The deadline is enforced while waiting, not measured after the fact.
                    raw = call_with_deadline(lambda: validator.review(*reviewed), self.budget)
                except RoleOutputError:
                    code = "VALIDATOR_INVALID"
                    assessment_status = "invalid"
                except TimeoutError:
                    code = "VALIDATOR_UNAVAILABLE"
                    assessment_status = "timeout"
                except Exception:
                    code = "VALIDATOR_UNAVAILABLE"
                    assessment_status = "unavailable"
                else:
                    try:
                        assessment = parse_model(ValidatorAssessment, wire_bytes(raw))
                        if (
                            assessment.coverage != CHECKS
                            or assessment.proposal_digest != proposal.proposal_digest
                            or assessment.context_ref != context.context_ref
                            or assessment.validator_adapter_id != validator.adapter_id
                            or any(
                                c.fact_refs != check_refs(c.check_id, context)
                                for c in assessment.checks
                            )
                        ):
                            raise ValueError("Unbound or incomplete assessment")
                    except Exception:
                        code = "VALIDATOR_INVALID"
                        assessment_status = "invalid"
                    else:
                        assessment_status = "admitted"
                        if assessment.verdict != "accept":
                            code = "VALIDATOR_REJECTED"
                times.append(Timing(component="validator", elapsed_ns=perf_counter_ns() - started))
            started = perf_counter_ns()
            try:
                failures = mandatory_failures(proposal, context)
                if failures and code == "SIMULATED_ONLY":
                    code = "BACKEND_DENIED"
            except Exception:
                # Controls that cannot run never admit; this is a harness fault, not a denial.
                code = "BACKEND_UNAVAILABLE"
            times.append(Timing(component="backend", elapsed_ns=perf_counter_ns() - started))

        admitted = code == "SIMULATED_ONLY"
        record = ReviewRecord(
            experiment_id=experiment_id,
            condition=self.condition,
            original_context=context,
            proposal=proposal,
            assessment=assessment,
            assessment_status=assessment_status,
            configuration_ref=self.case_configuration_ref,
            configuration_digest=self.configuration_digest,
            admission=Admission.model_validate_json(
                wire_bytes(
                    {
                        "status": "simulated_admitted" if admitted else "blocked",
                        "code": code,
                        "backend_failures": failures,
                    }
                )
            ),
            timings=tuple(times),
        )
        record_hash = digest("hnp-review-record/v1", record)
        effect = False
        delivery = "NONE"

        def persist_and_emit():
            nonlocal effect, delivery
            started = perf_counter_ns()
            try:
                entry_hash = self.evidence.append(record)
            except Exception:
                delivery = "EVIDENCE_UNAVAILABLE"
                times.append(Timing(component="evidence", elapsed_ns=perf_counter_ns() - started))
                return
            times.append(Timing(component="evidence", elapsed_ns=perf_counter_ns() - started))
            if admitted and proposal is not None:
                started = perf_counter_ns()
                try:
                    receipt = self.sink._emit(
                        _Permit(proposal.proposal_id, entry_hash, self._issuer)
                    )
                except Exception:
                    delivery = "SINK_UNAVAILABLE"
                    times.append(Timing(component="sink", elapsed_ns=perf_counter_ns() - started))
                    return
                effect = True
                times.append(Timing(component="sink", elapsed_ns=perf_counter_ns() - started))
                started = perf_counter_ns()
                try:
                    self.evidence.append(receipt)
                except Exception:
                    # A fictitious effect happened: never relabel this as no effect.
                    delivery = "RECEIPT_UNAVAILABLE"
                else:
                    delivery = "EMITTED"
                times.append(Timing(component="evidence", elapsed_ns=perf_counter_ns() - started))

        if admitted and context is not None:
            try:
                current = self.resolver.commit_if_current(context.context_ref, persist_and_emit)
            except Exception:
                current = False
            if not current:
                admitted = False
                record = record.model_copy(
                    update={
                        "admission": Admission(
                            status="blocked",
                            code="CONTEXT_CHANGED",
                            backend_failures=("binding",),
                        )
                    }
                )
                record_hash = digest("hnp-review-record/v1", record)
                persist_and_emit()
        else:
            persist_and_emit()
        times.append(Timing(component="total", elapsed_ns=perf_counter_ns() - began))
        return DemoResult.model_validate_json(
            wire_bytes(
                {
                    "record": record,
                    "record_digest": record_hash,
                    "effect_occurred": effect,
                    "effective_admission": {
                        "status": "blocked",
                        "code": delivery,
                        "backend_failures": (),
                    }
                    if delivery in ("EVIDENCE_UNAVAILABLE", "SINK_UNAVAILABLE")
                    else record.admission,
                    "delivery_code": delivery,
                    "timings": times,
                }
            )
        )
