import type { BuyerProfile, CompletedCheckout } from "@/lib/commerce-types"

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

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${AGENT_API_URL}${path}`, init)
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

export function health(signal?: AbortSignal): Promise<{ status: string }> {
  return requestJson("/health", { signal })
}

export function approvePurchase(runId: string, fingerprint: string): Promise<void> {
  return requestJson(`/purchases/${encodeURIComponent(runId)}/approval`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_fingerprint: fingerprint }),
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
