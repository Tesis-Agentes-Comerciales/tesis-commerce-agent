export type BuyerProfile = {
  firstName: string
  lastName: string
  email: string
  phoneNumber: string
  addressLine: string
  city: string
  state: string
  country: string
  postalCode: string
}

export type ConversationMessage = {
  role: "user" | "assistant"
  content: string
}

export type ProductCandidate = {
  product_id: string
  product_title: string
  variant_id: string
  variant_title: string
  observed_unit_amount_minor: number
  currency: string
}

export type CheckoutSession = {
  id: string
  status: string
  currency: string
  totals: Array<{ type: string; display_text?: string; amount: number }>
  expires_at: string
}

export type PurchasePreparation = {
  run_id: string
  stage: string
  selected_candidate: ProductCandidate
  checkout: CheckoutSession
  price_changed: boolean
}

export type ApprovalTerms = {
  merchant_origin: string
  checkout_id: string
  currency: string
  total_minor: number
  lines: Array<{
    item_id: string
    name: string
    quantity: number
    unit_amount_minor: number
  }>
  selected_fulfillment_options: Array<Record<string, unknown>>
  buyer: Record<string, unknown> | null
  fulfillment_details: {
    address?: { city?: string; country?: string }
  } | null
  payment_handler: string
  action: string
  expires_at: string
  fingerprint_version: string
  fingerprint: string
}

export type ConversationTurn = {
  action: "respond" | "clarify" | "purchase"
  assistant_message: string
  purchase: PurchasePreparation | null
  approval_terms: ApprovalTerms | null
}

export type CompletedCheckout = CheckoutSession & {
  order: {
    id: string
    checkout_session_id: string
    permalink_url: string
    order_number?: string | null
    status?: string | null
  }
}

export type PurchaseFlowStatus =
  | "idle"
  | "preparing"
  | "awaiting_approval"
  | "approving"
  | "completing"
  | "completed"
  | "error"

export type PurchaseFlow = {
  status: PurchaseFlowStatus
  request?: string
  preparation?: PurchasePreparation
  terms?: ApprovalTerms
  order?: CompletedCheckout
  approvalRecorded?: boolean
  error?: string
}

export const EMPTY_FLOW: PurchaseFlow = { status: "idle" }

export const DEMO_PROFILE: BuyerProfile = {
  firstName: "Ana",
  lastName: "Demo",
  email: "ana@example.test",
  phoneNumber: "+573000000000",
  addressLine: "Calle 100 # 10-20",
  city: "Bogota",
  state: "DC",
  country: "CO",
  postalCode: "110111",
}
