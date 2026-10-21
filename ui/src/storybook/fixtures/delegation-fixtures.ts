import {
  parseDelegationProposal,
  parseDelegationRevokeAllReceipt,
  parseDelegationView,
  type DelegationView,
} from "@/lib/delegation-contract"

import active from "./delegation-active.json"
import escalated from "./delegation-escalated.json"
import expired from "./delegation-expired.json"
import failed from "./delegation-failed.json"
import proposed from "./delegation-proposed.json"
import purchased from "./delegation-purchased.json"
import revokeReceipts from "./delegation-revoke-receipts.json"
import revoked from "./delegation-revoked.json"
import running from "./delegation-running.json"
import uncertain from "./delegation-uncertain.json"

// Delegaciones sintéticas, una por estado (docs/design/hnp-governed.md §5.3). El backend valida
// estos mismos archivos con Pydantic (tests/test_delegation_contract.py). Cada una pasa por su
// `parseDelegation*` al cargarse: si deja de cumplir el contrato, fallan las historias que la usan.
// Las horas están en UTC.

/** La tarjeta de consentimiento: alcance, `scope_digest` y vencimiento de la propuesta. */
export const DELEGATION_PROPOSAL = parseDelegationProposal(proposed)

export const DELEGATIONS = {
  /** Consentida y lista: la tarea espera el disparo. */
  active: parseDelegationView(active),
  running: parseDelegationView(running),
  purchased: parseDelegationView(purchased),
  /** D1 POL-11: el precio de catálogo ya supera el total máximo; vuelve al comprador. */
  escalated: parseDelegationView(escalated),
  /** Revocada por el dueño antes del disparo. */
  revoked: parseDelegationView(revoked),
  expired: parseDelegationView(expired),
  /** D2 negó con una política que no escala: falla por precaución. */
  failed: parseDelegationView(failed),
  /** Timeout después de comprometer el pago: se concilia a mano. */
  uncertain: parseDelegationView(uncertain),
} satisfies Record<string, DelegationView>

/** «Revocar todas»: un recibo por efecto (`no_effect`, `released`, `in_flight`). */
export const REVOKE_RECEIPTS = parseDelegationRevokeAllReceipt(revokeReceipts)
