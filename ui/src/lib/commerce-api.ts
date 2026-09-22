import type {
  BuyerProfile,
  CompletedCheckout,
  ConversationMessage,
  ConversationTurn,
} from "@/lib/commerce-types"

const configuredBaseUrl = import.meta.env.VITE_AGENT_API_URL ?? "http://127.0.0.1:43120"
export const AGENT_API_URL = configuredBaseUrl.replace(/\/$/, "")

export class AgentApiError extends Error {
  readonly code: string
  readonly status: number

  constructor(message: string, code: string, status: number) {
    super(message)
    this.name = "AgentApiError"
    this.code = code
    this.status = status
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
    throw new AgentApiError(message, code, response.status)
  }
  return response.json() as Promise<T>
}

function checkoutDetails(profile: BuyerProfile) {
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

export function conversationTurn(
  messages: ConversationMessage[],
  profile: BuyerProfile,
  turnId: string,
  signal: AbortSignal,
): Promise<ConversationTurn> {
  return requestJson("/conversation/turn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ turn_id: turnId, messages, details: checkoutDetails(profile) }),
    signal,
  })
}

export function approvePurchase(runId: string, fingerprint: string): Promise<void> {
  return requestJson(`/purchases/${encodeURIComponent(runId)}/approval`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_fingerprint: fingerprint }),
  })
}

export function completePurchase(runId: string): Promise<CompletedCheckout> {
  return requestJson(`/purchases/${encodeURIComponent(runId)}/complete`, {
    method: "POST",
  })
}
