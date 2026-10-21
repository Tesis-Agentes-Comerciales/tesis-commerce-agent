import { AgentApiError, requestJson } from "@/lib/commerce-api"
import {
  parseDelegationList,
  parseDelegationProposal,
  parseDelegationRevokeAllReceipt,
  parseDelegationRevokeReceipt,
  parseDelegationView,
  type DelegationConsent,
} from "@/lib/delegation-contract"
import type { DelegationProposalRequest } from "@/lib/delegation-draft"

// Cliente de las rutas del comprador de la API de delegaciones (docs/design/hnp-governed.md §5.1).
// Solo existen con AGENT_HNP_ENABLED: apagado, cada ruta es el 404 genérico de FastAPI. Toda
// respuesta pasa por su `parseDelegation*`: una fuera de contrato se rechaza como cualquier error.

const post = (body?: unknown): RequestInit =>
  body === undefined
    ? { method: "POST" }
    : {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }

const grantPath = (grantId: string) => `/delegations/${encodeURIComponent(grantId)}`

export async function proposeDelegation(request: DelegationProposalRequest) {
  return parseDelegationProposal(await requestJson("/delegations/proposals", post(request)))
}

/** 201 crea la delegación; 200 devuelve la misma si esa propuesta ya se autorizó. */
export async function consentToDelegation(consent: DelegationConsent) {
  return parseDelegationView(await requestJson("/delegations", post(consent)))
}

export async function myDelegations(signal?: AbortSignal) {
  return parseDelegationList(await requestJson("/me/delegations", { signal }))
}

export async function getDelegation(grantId: string) {
  return parseDelegationView(await requestJson(grantPath(grantId)))
}

export async function revokeDelegation(grantId: string) {
  return parseDelegationRevokeReceipt(await requestJson(`${grantPath(grantId)}/revoke`, post()))
}

/** Un recibo por cada delegación que estaba activa; las demás no se tocan. */
export async function revokeAllDelegations() {
  return parseDelegationRevokeAllReceipt(await requestJson("/me/delegations/revoke-all", post()))
}

// Errores. La API responde `{code, message}`, no el 422 de FastAPI.

/**
 * Códigos de §5.1. Su `message` ya llega en español y sin detalles internos, y se muestra tal cual
 * (`null`); el que la UI explica mejor lleva su propio texto.
 */
const DELEGATION_ERROR_TEXT = {
  delegation_request_invalid: null,
  delegation_variant_unavailable: null,
  delegation_validity_exceeded: null,
  // El comercio solo atiende un destino (#99): la UI dice cuál y qué revisar.
  delegation_destination_unsupported:
    "Por ahora el comercio solo entrega en Bogotá D.C. (departamento DC, código postal 110111). Revisa la dirección de entrega.",
  delegation_proposal_not_found: null,
  delegation_scope_mismatch: null,
  delegation_proposal_expired: null,
  delegation_signer_unavailable: null,
  delegation_not_found: null,
  delegation_not_revocable: null,
} as const satisfies Record<string, string | null>
export type DelegationErrorCode = keyof typeof DELEGATION_ERROR_TEXT

/** Código conocido de §5.1, o `null` para cualquier otra respuesta. */
export function delegationErrorCode(error: unknown): DelegationErrorCode | null {
  if (!(error instanceof AgentApiError) || !Object.hasOwn(DELEGATION_ERROR_TEXT, error.code)) {
    return null
  }
  return error.code as DelegationErrorCode
}

/**
 * Lo que ve el comprador. Un código de §5.1 dice su propio texto; cualquier otra cosa (el `detail`
 * de FastAPI, un 401, un 5xx, la red o una respuesta fuera de contrato) se dice en español aquí.
 */
export function delegationErrorText(error: unknown): string {
  const code = delegationErrorCode(error)
  if (code && error instanceof AgentApiError) return DELEGATION_ERROR_TEXT[code] ?? error.message
  if (error instanceof AgentApiError && error.status === 401) {
    return "Tu sesión venció. Vuelve a entrar para seguir con tus delegaciones."
  }
  if (error instanceof AgentApiError) return "El agente no pudo atender la solicitud. Intenta de nuevo."
  return "No pude comunicarme con el agente."
}

/** Errores de autorizar que no se arreglan reintentando: hace falta otra propuesta (§5.1). */
const NEEDS_NEW_PROPOSAL: ReadonlySet<DelegationErrorCode> = new Set([
  "delegation_proposal_not_found",
  "delegation_scope_mismatch",
  "delegation_proposal_expired",
])

export function needsNewProposal(error: unknown) {
  const code = delegationErrorCode(error)
  return code !== null && NEEDS_NEW_PROPOSAL.has(code)
}
