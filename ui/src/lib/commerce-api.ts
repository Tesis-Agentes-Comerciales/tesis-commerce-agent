import { buyerHeaders } from "@/lib/auth-client"
import type { AgentPurchase, ApprovalTerms } from "@/lib/agent-contract"
import type { BuyerProfile, CompletedCheckout } from "@/lib/commerce-types"
import { parsePurchaseTrace, type PurchaseTrace } from "@/lib/trace-contract"

const configuredBaseUrl = import.meta.env.VITE_AGENT_API_URL ?? "http://127.0.0.1:43120"
export const AGENT_API_URL = configuredBaseUrl.replace(/\/$/, "")

export class AgentApiError extends Error {
  readonly code: string
  readonly status: number
  readonly merchantRequestId: string | null

  constructor(message: string, code: string, status: number, merchantRequestId: string | null) {
    super(message)
    this.name = "AgentApiError"
    this.code = code
    this.status = status
    this.merchantRequestId = merchantRequestId
  }
}

/** Pide JSON al agente con el token del comprador; una respuesta no 2xx lanza `AgentApiError`. */
export async function requestJson<T>(
  path: string,
  init?: RequestInit,
  authenticated = true,
): Promise<T> {
  const headers = new Headers(init?.headers)
  if (authenticated) {
    for (const [name, value] of Object.entries(await buyerHeaders())) headers.set(name, value)
  }
  const response = await fetch(`${AGENT_API_URL}${path}`, { ...init, headers })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const code = typeof body?.code === "string" ? body.code : "http_error"
    const message =
      typeof body?.message === "string"
        ? body.message
        : `El agente respondió con HTTP ${response.status}`
    const rawRequestId = body?.merchant_request_id
    const merchantRequestId =
      typeof rawRequestId === "string" && /^[A-Za-z0-9_.:-]{1,128}$/.test(rawRequestId)
        ? rawRequestId
        : null
    throw new AgentApiError(message, code, response.status, merchantRequestId)
  }
  return response.json() as Promise<T>
}

/** `CheckoutDetails` del backend; viaja en el cuerpo de `POST /assistant`, nunca al modelo. */
export function checkoutDetails(profile: BuyerProfile) {
  const fullName = `${profile.firstName} ${profile.lastName}`.trim()
  return {
    buyer: {
      email: profile.email,
      first_name: profile.firstName,
      last_name: profile.lastName,
    },
    fulfillment: {
      name: fullName,
      email: profile.email,
      phone_number: profile.phoneNumber,
      address: {
        name: fullName,
        line_one: profile.addressLine,
        city: profile.city,
        state: profile.state,
        country: profile.country,
        postal_code: profile.postalCode,
      },
    },
  }
}

/** `hnp_enabled` solo llega con `AGENT_HNP_ENABLED`; ausente, HNP está apagado (§5.4). */
export function health(signal?: AbortSignal): Promise<{ status: string; hnp_enabled?: boolean }> {
  return requestJson("/health", { signal }, false)
}

/**
 * Por qué faltan los términos de una compra que espera confirmación: la oferta o el checkout
 * cambiaron (también un checkout que dejó de estar listo para pagar), vencieron o no se pudieron
 * confirmar con el comercio. Con `authorization_*` los términos valen, pero al aprobar la política
 * no autorizó la compra (`denied`) o no se pudo verificar la autorización (`unverified`): aprobar de
 * nuevo se rechazaría igual.
 */
export type PendingTermsStatus =
  | "changed"
  | "expired"
  | "unavailable"
  | "authorization_denied"
  | "authorization_unverified"

export type PendingPurchase = {
  purchase: AgentPurchase & { checkout_id: string | null }
  approval_terms: ApprovalTerms | null
  terms_status: PendingTermsStatus | null
}

export function pendingPurchase(): Promise<PendingPurchase | null> {
  return requestJson("/me/pending-purchase")
}

export function getApprovalTerms(runId: string): Promise<ApprovalTerms> {
  return requestJson(`/purchases/${encodeURIComponent(runId)}/approval`)
}

export function approvePurchase(runId: string, fingerprint: string, checkoutJwt?: string | null): Promise<void> {
  return requestJson(`/purchases/${encodeURIComponent(runId)}/approval`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_fingerprint: fingerprint, checkout_jwt: checkoutJwt ?? null }),
  })
}

export function cancelPurchase(runId: string): Promise<void> {
  return requestJson(`/purchases/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
  })
}

export function completePurchase(runId: string): Promise<CompletedCheckout> {
  return requestJson(`/purchases/${encodeURIComponent(runId)}/complete`, {
    method: "POST",
  })
}

/** Traza de controles de una compra propia; una respuesta fuera de contrato se rechaza. */
export async function getPurchaseTrace(runId: string, signal?: AbortSignal): Promise<PurchaseTrace> {
  const trace = parsePurchaseTrace(
    await requestJson(`/purchases/${encodeURIComponent(runId)}/trace`, { signal }),
  )
  if (trace.run_id !== runId) throw new TypeError("Purchase trace belongs to another run")
  return trace
}
