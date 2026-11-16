import type { authClient } from "../lib/auth-client.ts"
import type { ApprovalTerms, CompletedCheckout, PurchasePreparation } from "../lib/commerce-types.ts"

// Datos sintéticos que comparten Storybook y los tests de Playwright (e2e/); nunca deben contener
// datos reales. Solo importa tipos y con rutas relativas, para que Node lo cargue sin Vite.

type BuyerSession = NonNullable<ReturnType<typeof authClient.useSession>["data"]>

const ISSUED_AT = new Date("2026-09-20T12:00:00Z")

/** Sesión de Better Auth de la compradora sintética, alineada con DEMO_PROFILE. */
export const BUYER_SESSION: BuyerSession = {
  user: {
    id: "storybook-user",
    name: "Ana Demo",
    email: "ana@example.test",
    emailVerified: true,
    image: null,
    createdAt: ISSUED_AT,
    updatedAt: ISSUED_AT,
  },
  session: {
    id: "storybook-session",
    userId: "storybook-user",
    token: "storybook-session-token",
    expiresAt: new Date("2099-09-20T13:00:00Z"),
    createdAt: ISSUED_AT,
    updatedAt: ISSUED_AT,
  },
}

/** Lo que devuelve `authClient.token()`; la UI solo lo reenvía como `Bearer` al agente. */
export const BUYER_TOKEN = "storybook-buyer-token"

export const PURCHASE_REQUEST = "Quiero audífonos negros por menos de 2 millones"

/** `run_id` con la forma que genera el backend (`run_` + uuid4 en hex). */
export const PREPARATION: PurchasePreparation = {
  run_id: "run_3f9a6c2e8d1b4f7a9c0e5b2d7f4a1c86",
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
  fulfillment_details: {
    address: { name: "Ana Demo", line_one: "Calle 100 # 10-20", city: "Bogota", country: "CO" },
  },
  payment_handler: "tesis_sandbox",
  action: "complete_purchase",
  expires_at: "2099-09-20T13:00:00Z",
  fingerprint_version: "v1",
  fingerprint: "a".repeat(64),
}

/** Lo que registra `POST /purchases/{run_id}/approval`. */
export function approvalRecord(runId: string) {
  return {
    run_id: runId,
    checkout_id: TERMS.checkout_id,
    principal_id: "local-human",
    approved_at: "2026-09-20T12:00:00Z",
    stage: "approved",
  }
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
