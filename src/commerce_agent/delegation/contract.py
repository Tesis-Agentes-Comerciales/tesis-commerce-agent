"""Delegation API views (docs/design/hnp-governed.md §5.3), mirror of delegation-contract.ts.

What the buyer sees of a Human Not Present delegation: the scope they consent to, the grant with
its single task, an escalation and the revoke receipts. The rules match `parseDelegation*` in
ui/src/lib/delegation-contract.ts: no field beyond the contract, non-empty texts, prefixed ids,
times in UTC ending in `Z`, and the state pairs of §4.3, §5.1 and §8.1. Views never carry
mandates, tokens, the full address or the owner's identity, and only the proposal carries
`scope_digest`. These are views, not the ledger rows of `delegation/models.py`.

`HNP_MESSAGES` are the canonical texts of §8.3. They reach the UI in `message`; the UI never
duplicates them.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Literal, Self, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from commerce_agent.governance.models import Digest, Id, One
from commerce_agent.trace.models import Money, Text, UtcTime

ProposalId = Annotated[str, Field(max_length=128, pattern=r"^dpr_[A-Za-z0-9._:-]+$")]
GrantId = Annotated[str, Field(max_length=128, pattern=r"^dg_[A-Za-z0-9._:-]+$")]
TaskId = Annotated[str, Field(max_length=128, pattern=r"^ht_[A-Za-z0-9._:-]+$")]
# A profile constant of §6: up to 280 characters of the buyer's own preference.
PreferenceText = Annotated[str, Field(min_length=1, max_length=280)]

GrantState = Literal["active", "consumed", "revoked", "expired"]
RevokedReason = Literal["owner", "operator", "escalated", "failed"]
TaskState = Literal[
    "scheduled", "running", "purchased", "escalated", "failed", "canceled", "uncertain"
]
EscalationCode = Literal[
    "budget_not_satisfiable",
    "no_candidate_in_scope",
    # The intent reviewer did not confirm the choice (hnp-intent-review.md).
    "choice_not_confirmed",
    # There was no safe choice: the selector failed or did not answer (§8.1), or the consented
    # order stopped at an option it could not verify or try (p0-conditional-intent.md §3.9).
    "selection_unavailable",
    "offer_changed",
    "terms_out_of_scope",
    "authorization_profile_unresolved",
    "verifier_unresolved_constraint",
    "verifier_mandates_not_supported",
]
CancelCode = Literal["revoked_by_owner", "kill_switch", "grant_expired"]
FailureCode = Literal[
    "selection_not_eligible",
    "merchant_not_allowed",
    "mandate_proof_invalid",
    "policy_denied",
    "governance_unavailable",
    "state_conflict",
    "verifier_invalid_mandate",
    "verifier_invalid_credential",
    "checkout_failed",
    "budget_guard",
    "close_expired_before_dispatch",
]
UncertainCode = Literal["dispatch_timeout", "partial_verifier_failure", "receipt_mismatch"]
OutcomeCode = Literal[EscalationCode, CancelCode, FailureCode, UncertainCode]
RevokeEffect = Literal["no_effect", "released", "in_flight"]

_ESCALATIONS = frozenset(get_args(EscalationCode))
# §8.1: the task outcome codes each state allows; `None` is the only outcome of the other states.
# B0-HNP never escalates, so a cause that escalates in B2 fails there with the same code.
_OUTCOMES: Mapping[TaskState, frozenset[str]] = MappingProxyType(
    {
        "escalated": _ESCALATIONS,
        "canceled": frozenset(get_args(CancelCode)),
        "failed": frozenset(get_args(FailureCode)) | _ESCALATIONS,
        "uncertain": frozenset(get_args(UncertainCode)),
    }
)
# §8.2: these escalations come from the payment provider's error receipt, so it saw the mandate.
_REJECTED_BY_PSP = frozenset({"verifier_unresolved_constraint", "verifier_mandates_not_supported"})

# The table of §8.3, plus the escalation title (§8.3) and the chat prefill (§8.2). The spec's
# `<título>`, `<total>` and `<número>` are the `str.format` fields `{title}`, `{total}` and
# `{order_id}`.
HNP_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        "escalation.title": "Necesito tu decisión",
        "escalation.prefill_request": "Quiero comprar {title} por hasta {total}",
        "escalation.budget_not_satisfiable": (
            "El precio de catálogo de la opción disponible ya supera el total máximo que "
            "autorizaste."
        ),
        # Neutral about the cause (p0-conditional-intent.md §3.9): an empty shortlist, or no
        # option of the consented order for sale within the total.
        "escalation.no_candidate_in_scope": (
            "Ninguna de las opciones que autorizaste se puede comprar ahora dentro de tu "
            "autorización."
        ),
        "escalation.choice_not_confirmed": (
            "No pude confirmar que la opción elegida sea la que pediste."
        ),
        # Also neutral: a selector that failed, an option the merchant could not verify, or one
        # the consented order could not try (missing from the feed, or against the intent).
        "escalation.selection_unavailable": (
            "No pude elegir con seguridad entre las opciones que autorizaste."
        ),
        "escalation.offer_changed": "La oferta del comercio cambió antes de que cerrara la compra.",
        "escalation.terms_out_of_scope": (
            "El total del comercio, con envío, o algún término quedó fuera de lo que autorizaste."
        ),
        "escalation.authorization_profile_unresolved": (
            "No pude comprobar que la compra encaje en tu autorización."
        ),
        "escalation.verifier_unresolved_constraint": (
            "El proveedor de pagos no pudo comprobar una condición de tu autorización."
        ),
        "escalation.verifier_mandates_not_supported": (
            "El proveedor de pagos no acepta compras delegadas en este momento."
        ),
        "escalation.suffix_before_presentation": (
            "No firmé ni presenté ninguna autorización de pago, no se hizo ningún cobro y cerré la "
            "delegación. Si todavía quieres comprar, hazlo desde el chat y apruébala tú."
        ),
        "escalation.suffix_rejected_by_psp": (
            "El proveedor de pagos la rechazó, así que no se hizo ningún cobro, y cerré la "
            "delegación. Si todavía quieres comprar, hazlo desde el chat y apruébala tú."
        ),
        "revoke.no_effect": "Revocaste la delegación. El agente ya no puede comprar con ella.",
        "revoke.released": (
            "Revocaste la delegación antes de que el agente comprometiera el pago. No se presentó "
            "ninguna autorización ni se hizo ningún cobro."
        ),
        "revoke.in_flight": (
            "Revocaste la delegación, pero el agente ya había comprometido el pago. Esa compra "
            "sigue en curso y aquí verás cómo termina; no puedo prometer que se cancele."
        ),
        "revoke.not_revocable": "Esta delegación ya terminó, así que no hay nada que revocar.",
        "revoke.already_closed": (
            "Esta delegación ya estaba cerrada, así que el agente no puede comprar con ella."
        ),
        "task.kill_switch": (
            "El operador detuvo las compras delegadas. Cerré tu delegación y no se hizo ningún "
            "cobro. Si todavía quieres comprar, hazlo desde el chat."
        ),
        "task.grant_expired": (
            "Tu delegación venció antes de que el agente comprara. No se hizo ningún cobro."
        ),
        "task.revoked_by_owner": (
            "Revocaste la delegación antes de la compra. No se hizo ningún cobro."
        ),
        "task.failed": (
            "Detuve la compra delegada por precaución porque no pude verificar todo lo necesario. "
            "No se hizo ningún cobro y cerré la delegación."
        ),
        # CT13: a `checkout_failed` whose creation the merchant refused (`merchant_refusal`): the
        # cause in the trace's words (`FAILURE_COPY`), then what `task.failed` says did not happen.
        "task.refused.out_of_stock": (
            "El producto se agotó antes de completar la compra. No se hizo ningún cobro y cerré la "
            "delegación."
        ),
        "task.refused.offer_expired": (
            "La oferta del comercio venció. No se hizo ningún cobro y cerré la delegación."
        ),
        "task.refused.invalid_item": (
            "El producto ya no está disponible en el comercio. No se hizo ningún cobro y cerré la "
            "delegación."
        ),
        "task.uncertain": (
            "No sé todavía si la compra delegada se completó: el comercio o el proveedor de pagos "
            "no confirmó el resultado. No voy a intentarlo con otra autorización; la estoy "
            "conciliando."
        ),
        "task.purchased": "Compré {title} por {total} con tu delegación. Orden {order_id}.",
        "task.running": "Estoy haciendo la compra que delegaste.",
        "task.scheduled": "Tu delegación está activa y lista para que el agente compre.",
    }
)


def escalation_message(reason: EscalationCode) -> str:
    """The cause, then what did not happen: nothing was presented, or the PSP rejected it (§8.2)."""
    suffix = "rejected_by_psp" if reason in _REJECTED_BY_PSP else "before_presentation"
    return f"{HNP_MESSAGES[f'escalation.{reason}']} {HNP_MESSAGES[f'escalation.suffix_{suffix}']}"


class ViewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ScopeVariant(ViewModel):
    variant_id: Id
    title: Text


class ScopeView(ViewModel):
    """The typed scope as the consent card shows it, never the address or the instrument itself."""

    merchant_name: Text
    # A profile constant of §6: 1 to 5 variants per scope.
    variants: Annotated[tuple[ScopeVariant, ...], Field(min_length=1, max_length=5)]
    total_max: Money
    expires_at: UtcTime
    destination_label: Text
    instrument_label: Text
    # `One`, not `Literal[1]`: a JSON literal also accepts `true` and `1.0`.
    uses: One
    preference_text: PreferenceText | None
    # The consented order of the variants (p0-conditional-intent.md §3.9), at most one per
    # evaluator branch. Absent in every scope consented without one; never null.
    priority: Annotated[tuple[Id, ...], Field(min_length=1, max_length=3)] | None = None
    # D15: the agent reviews `preference_text` before buying; only `true` is ever sent.
    preference_review: bool = False

    @field_validator("priority", "preference_review")
    @classmethod
    def _given_only_when_present(cls, value: object) -> object:
        # Runs only on a given value, never on the default: a null priority or a false review
        # would be a second form of absent.
        if value is None or value is False:
            raise ValueError("An absent priority or review is omitted")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        variants = sorted(variant.variant_id for variant in self.variants)
        if self.priority is not None and sorted(self.priority) != variants:
            raise ValueError("A priority orders every variant of the scope exactly once")
        if self.preference_review and self.preference_text is None:
            raise ValueError("Only a preference text can be reviewed")
        return self

    @model_serializer(mode="wrap")
    def _without_absent(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        if data.get("priority") is None:
            data.pop("priority", None)
        if data.get("preference_review") is False:
            data.pop("preference_review")
        return data


class DelegationProposal(ViewModel):
    proposal_id: ProposalId
    scope: ScopeView
    scope_digest: Digest
    presented_at: UtcTime
    # The proposal's own expiry, not the grant's (`scope.expires_at`).
    expires_at: UtcTime


class TaskView(ViewModel):
    task_id: TaskId
    state: TaskState
    outcome_code: OutcomeCode | None
    run_id: Text | None
    order_id: Text | None
    message: Text | None
    updated_at: UtcTime

    @model_validator(mode="after")
    def _outcome_belongs_to_the_state(self) -> Self:
        allowed = _OUTCOMES.get(self.state, frozenset())
        if not (self.outcome_code in allowed if allowed else self.outcome_code is None):
            raise ValueError(f"{self.outcome_code} is not an outcome of a {self.state} task")
        return self


class EscalationView(ViewModel):
    reason: EscalationCode
    title: Text
    message: Text
    prefill_request: Text


class DelegationView(ViewModel):
    grant_id: GrantId
    state: GrantState
    revoked_reason: RevokedReason | None
    scope: ScopeView
    consented_at: UtcTime
    expires_at: UtcTime
    task: TaskView
    escalation: EscalationView | None
    last_activity_at: UtcTime

    @model_validator(mode="after")
    def _states_agree(self) -> Self:
        if (self.state == "revoked") != (self.revoked_reason is not None):
            raise ValueError("revoked_reason is set if and only if the grant is revoked")
        if (self.task.state == "escalated") != (self.escalation is not None):
            raise ValueError("An escalation is shown if and only if the task escalated")
        if self.escalation is not None and self.escalation.reason != self.task.outcome_code:
            raise ValueError("The escalation reason is the task outcome_code")
        return self


class DelegationList(ViewModel):
    """`GET /me/delegations`, newest first."""

    delegations: tuple[DelegationView, ...]


class RevokeReceipt(ViewModel):
    grant_id: GrantId
    state: Literal["revoked"]
    revoked_reason: RevokedReason
    revoked_at: UtcTime
    effect: RevokeEffect
    message: Text

    @model_validator(mode="after")
    def _only_the_owner_revocation_has_an_effect(self) -> Self:
        # §5.1: a grant already closed by the operator, an escalation or a failure is not touched.
        if self.revoked_reason != "owner" and self.effect != "no_effect":
            raise ValueError("A grant revoked by someone else answers no_effect")
        return self


class RevokeAllReceipt(ViewModel):
    """`POST /me/delegations/revoke-all`: one receipt per grant that was active."""

    receipts: tuple[RevokeReceipt, ...]
