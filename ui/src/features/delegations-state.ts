import type { DelegationInChatFlow } from "@/components/delegation/delegation-in-chat"
import type { DelegationRevocation as Revocation } from "@/components/delegation/delegation-list"
import type { DelegationDraft } from "@/lib/delegation-draft"
import type {
  DelegationProposal,
  DelegationView,
  RevokeReceipt,
} from "@/lib/delegation-contract"

// Estado de «Delegaciones» (docs/design/hnp-governed.md §3.2 y §8.4): el formulario, la propuesta
// con su consentimiento y «Mis delegaciones» con sus revocaciones. Es un reductor puro: el
// componente de `delegations.tsx` llama a la API y despacha lo que respondió.

/**
 * El formulario y la propuesta: la forma que muestra la tarjeta del hilo. Con `canAuthorize` en
 * falso, una propuesta vencida o distinta no se reintenta: hace falta otra.
 */
export type ProposalFlow = DelegationInChatFlow

export type DelegationsState = {
  list: "loading" | "ready" | "failed"
  listError: string | null
  /** Más recientes primero, como `GET /me/delegations`. */
  delegations: DelegationView[]
  revocations: Record<string, Revocation>
  revokingAll: boolean
  revokeAllError: string | null
  flow: ProposalFlow
  /** Lo último que se envió: volver de la propuesta al formulario no borra nada. */
  draft: DelegationDraft | null
  /** La delegación recién autorizada: su tarjeta toma el lugar y el foco de la propuesta. */
  consentedId: string | null
}

export type DelegationsAction =
  | { type: "list-loading" }
  | { type: "list-loaded"; delegations: DelegationView[] }
  | { type: "list-failed"; error: string }
  | { type: "form-opened" }
  | { type: "form-closed" }
  | { type: "proposing"; draft: DelegationDraft }
  | { type: "proposed"; proposal: DelegationProposal }
  | { type: "propose-failed"; error: string }
  | { type: "authorizing" }
  | { type: "authorized"; delegation: DelegationView }
  | { type: "authorize-failed"; error: string; canAuthorize: boolean }
  | { type: "revoking"; grantIds: string[]; all?: boolean }
  | { type: "revoked"; receipts: RevokeReceipt[]; all?: boolean }
  | { type: "revoke-failed"; grantId: string; error: string }
  | { type: "revoke-all-failed"; error: string }
  | { type: "delegation-read"; delegation: DelegationView }

export const INITIAL_DELEGATIONS: DelegationsState = {
  list: "loading",
  listError: null,
  delegations: [],
  revocations: {},
  revokingAll: false,
  revokeAllError: null,
  flow: { step: "idle" },
  draft: null,
  consentedId: null,
}

const IDLE_REVOCATION: Revocation = { revoking: false, error: null, receipt: null }

function revocations(
  state: DelegationsState,
  grantIds: string[],
  update: (current: Revocation, grantId: string) => Revocation,
) {
  const next = { ...state.revocations }
  for (const grantId of grantIds) next[grantId] = update(next[grantId] ?? IDLE_REVOCATION, grantId)
  return next
}

/** La vista releída reemplaza a la vieja en su lugar; una nueva va primero, como la más reciente. */
function withDelegation(delegations: DelegationView[], delegation: DelegationView) {
  const known = delegations.some((item) => item.grant_id === delegation.grant_id)
  if (!known) return [delegation, ...delegations]
  return delegations.map((item) => (item.grant_id === delegation.grant_id ? delegation : item))
}

export function delegationsReducer(
  state: DelegationsState,
  action: DelegationsAction,
): DelegationsState {
  switch (action.type) {
    case "list-loading":
      return { ...state, list: "loading", listError: null }
    case "list-loaded":
      return { ...state, list: "ready", delegations: action.delegations }
    case "list-failed":
      return { ...state, list: "failed", listError: action.error }
    case "form-opened":
      return { ...state, flow: { step: "form", submitting: false, error: null }, consentedId: null }
    case "form-closed":
      return { ...state, flow: { step: "idle" } }
    case "proposing":
      return { ...state, draft: action.draft, flow: { step: "form", submitting: true, error: null } }
    case "proposed":
      return {
        ...state,
        flow: {
          step: "consent",
          proposal: action.proposal,
          authorizing: false,
          error: null,
          canAuthorize: true,
        },
      }
    case "propose-failed":
      return { ...state, flow: { step: "form", submitting: false, error: action.error } }
    case "authorizing":
    case "authorize-failed": {
      if (state.flow.step !== "consent") return state
      const failed = action.type === "authorize-failed"
      return {
        ...state,
        flow: {
          ...state.flow,
          authorizing: !failed,
          error: failed ? action.error : null,
          canAuthorize: failed ? action.canAuthorize : state.flow.canAuthorize,
        },
      }
    }
    case "authorized":
      return {
        ...state,
        flow: { step: "idle" },
        draft: null,
        consentedId: action.delegation.grant_id,
        delegations: withDelegation(state.delegations, action.delegation),
      }
    case "revoking":
      return {
        ...state,
        revokingAll: action.all ?? state.revokingAll,
        revokeAllError: null,
        revocations: revocations(state, action.grantIds, (current) => ({
          ...current,
          revoking: true,
          error: null,
        })),
      }
    case "revoked": {
      // Cada recibo va a su tarjeta. «Revocar todo» termina además las que había marcado: una que
      // ya no estaba activa no trae recibo y conserva el que tenía.
      const receipts = new Map(action.receipts.map((receipt) => [receipt.grant_id, receipt]))
      const pending = action.all
        ? Object.keys(state.revocations).filter((id) => state.revocations[id]?.revoking)
        : []
      const grantIds = [...new Set([...pending, ...receipts.keys()])]
      return {
        ...state,
        revokingAll: action.all ? false : state.revokingAll,
        revocations: revocations(state, grantIds, (current, grantId) => ({
          revoking: false,
          error: null,
          receipt: receipts.get(grantId) ?? current.receipt,
        })),
      }
    }
    case "revoke-failed":
      return {
        ...state,
        revocations: revocations(state, [action.grantId], (current) => ({
          ...current,
          revoking: false,
          error: action.error,
        })),
      }
    case "revoke-all-failed":
      return {
        ...state,
        revokingAll: false,
        revokeAllError: action.error,
        revocations: revocations(state, Object.keys(state.revocations), (current) => ({
          ...current,
          revoking: false,
        })),
      }
    case "delegation-read":
      return { ...state, delegations: withDelegation(state.delegations, action.delegation) }
  }
}

/** Las delegaciones que «Revocar todo» alcanzaría: las que siguen activas. */
export const activeGrantIds = (state: DelegationsState) =>
  state.delegations.filter((item) => item.state === "active").map((item) => item.grant_id)

/**
 * Si alguna delegación todavía puede cambiar sola: su tarea espera el disparo o está comprando.
 * Una tarea programada de una delegación revocada o vencida ya no corre; una que compra, aunque se
 * haya revocado con el pago comprometido, sí termina (§8.4).
 */
export const hasTaskInFlight = (delegations: readonly DelegationView[]) =>
  delegations.some(
    ({ state, task }) => task.state === "running" || (task.state === "scheduled" && state === "active"),
  )
