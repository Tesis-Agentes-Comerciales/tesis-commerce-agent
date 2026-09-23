import { createContext, useContext } from "react"

import type { CompletedCheckout } from "@/lib/commerce-types"

export type LocalPurchaseStatus =
  | "idle"
  | "approving"
  | "completing"
  | "canceling"
  | "completed"
  | "canceled"
  | "error"

/**
 * Resultado de las llamadas REST de esta sesión para un `run_id`. La etapa autoritativa sigue siendo
 * `state.purchase` (SQLite); esto solo cubre lo que el backend aún no reflejó y el enlace de la orden.
 */
export type LocalPurchase = {
  status: LocalPurchaseStatus
  approvalRecorded: boolean
  order?: CompletedCheckout["order"]
  error?: string
  errorCode?: string
}

type PurchaseSession = {
  purchases: Record<string, LocalPurchase>
  updatePurchase: (runId: string, update: Partial<LocalPurchase>) => void
  onNewPurchase?: () => void
}

export const PurchaseSessionContext = createContext<PurchaseSession>({
  purchases: {},
  updatePurchase: () => {},
})

export function usePurchaseSession() {
  return useContext(PurchaseSessionContext)
}
