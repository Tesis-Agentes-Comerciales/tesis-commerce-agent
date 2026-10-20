import { createContext, useContext } from "react"

import type { PendingTermsStatus } from "@/lib/commerce-api"
import type { CompletedCheckout } from "@/lib/commerce-types"
import type { PurchaseTrace } from "@/lib/trace-contract"

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
  /** Aprobar falló porque los términos ya no valen: solo queda cancelar. Cancelar no lo borra. */
  termsStatus?: PendingTermsStatus
}

type PurchaseSession = {
  purchases: Record<string, LocalPurchase>
  updatePurchase: (runId: string, update: Partial<LocalPurchase>) => void
  /** Última traza leída de cada compra; la comparten la fila de actividad y el recibo. */
  traces: Record<string, PurchaseTrace>
  recordTrace: (runId: string, trace: PurchaseTrace) => void
  onNewPurchase?: () => void
}

export const PurchaseSessionContext = createContext<PurchaseSession>({
  purchases: {},
  updatePurchase: () => {},
  traces: {},
  recordTrace: () => {},
})

export function usePurchaseSession() {
  return useContext(PurchaseSessionContext)
}
