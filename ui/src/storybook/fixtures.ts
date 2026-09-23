import type { ApprovalTerms, CompletedCheckout, PurchasePreparation } from "@/lib/commerce-types"
import type { AgentApiMock } from "@/storybook/agent-api-mock"
import { recordPurchaseOutcome, scriptedAssistant } from "@/storybook/assistant-transport"

// Datos sintéticos alineados con e2e/smoke.py; nunca deben contener datos reales.

export const PURCHASE_REQUEST = "Quiero audífonos negros por menos de 2 millones"

export const PREPARATION: PurchasePreparation = {
  run_id: "run_storybook_1",
  stage: "awaiting_confirmation",
  selected_candidate: {
    product_id: "prod-sony-wh-1000xm6",
    product_title: "Sony WH-1000XM6",
    variant_id: "SON-XM6-BLK",
    variant_title: "Sony WH-1000XM6 negro",
    observed_unit_amount_minor: 72000,
    currency: "COP",
  },
  checkout: {
    id: "cs_storybook_1",
    status: "ready_for_payment",
    currency: "cop",
    totals: [{ type: "total", display_text: "Total", amount: 74000 }],
    expires_at: "2099-09-20T13:00:00Z",
  },
  price_changed: false,
}

export const TERMS: ApprovalTerms = {
  merchant_origin: "http://127.0.0.1:4120",
  checkout_id: "cs_storybook_1",
  currency: "cop",
  total_minor: 74000,
  lines: [
    {
      item_id: "SON-XM6-BLK",
      name: "Sony WH-1000XM6",
      quantity: 1,
      unit_amount_minor: 72000,
    },
  ],
  selected_fulfillment_options: [{ type: "shipping", option_id: "ship_1", item_ids: ["li_1"] }],
  buyer: { email: "ana@example.test" },
  fulfillment_details: { address: { city: "Bogotá", country: "CO" } },
  payment_handler: "tesis_sandbox",
  action: "complete_purchase",
  expires_at: "2099-09-20T13:00:00Z",
  fingerprint_version: "v1",
  fingerprint: "a".repeat(64),
}

export const COMPLETED: CompletedCheckout = {
  ...PREPARATION.checkout,
  status: "completed",
  order: {
    id: "ord_storybook_1",
    checkout_session_id: "cs_storybook_1",
    permalink_url: "http://127.0.0.1:4120/orders/ord_storybook_1",
    order_number: "STORYBOOK-001",
    status: "confirmed",
  },
}

export const PURCHASE_REPLY =
  "Elegí los Sonora negros porque cumplen el presupuesto y están disponibles."

export const DEFAULT_AGENT_API: AgentApiMock = {
  "GET /health": { body: { status: "ok" } },
  // Streams reales del backend (fixtures/assistant-transport), elegidos por el texto del mensaje.
  "POST /assistant": (request) => scriptedAssistant(request),
  "POST /purchases/:runId/approval": ({ params }) => ({
    delayMs: 600,
    body: {
      run_id: params.runId,
      checkout_id: TERMS.checkout_id,
      principal_id: "local-human",
      approved_at: "2026-09-20T12:00:00Z",
      stage: "approved",
    },
  }),
  "POST /purchases/:runId/cancel": ({ params }) => {
    recordPurchaseOutcome(params.runId ?? "", "canceled")
    return { delayMs: 400, body: { run_id: params.runId, stage: "canceled" } }
  },
  "POST /purchases/:runId/complete": ({ params }) => {
    recordPurchaseOutcome(params.runId ?? "", "completed")
    return { delayMs: 900, body: COMPLETED }
  },
}
