import { useAssistantTransportSendCommand, useAssistantTransportState } from "@assistant-ui/react"
import { useRef } from "react"

import { PurchaseApprovalCard, type ApprovalCardStatus } from "@/components/agent"
import { usePurchaseSession, type LocalPurchase } from "@/features/purchase-session"
import type { AgentPurchase, ApprovalTerms, ProductCard } from "@/lib/agent-contract"
import {
  AgentApiError,
  approvePurchase,
  cancelPurchase,
  completePurchase,
} from "@/lib/commerce-api"

const KNOWN_ERRORS: Record<string, string> = {
  budget_exceeded: "El total autoritativo del comercio supera tu presupuesto.",
  transport_error: "No pude comunicarme con el ecommerce ACP.",
  timeout: "El ecommerce tardó demasiado en responder.",
  INVALID_INPUT: "El ecommerce rechazó los datos del checkout. Revisa tus datos de entrega.",
  http_error: "El ecommerce no pudo procesar la solicitud.",
}

/** Mensaje en español para un error REST; el texto de un `TypeError` nunca llega a la UI. */
function purchaseErrorMessage(error: unknown, fallback: string) {
  if (error instanceof AgentApiError) {
    const message = KNOWN_ERRORS[error.code] ?? error.message
    return message.trim().slice(0, 300) || fallback
  }
  if (error instanceof TypeError) return "No pude comunicarme con el agente."
  return fallback
}

type CardView = {
  status: ApprovalCardStatus
  approvalRecorded: boolean
  error: string | null
  actionable: boolean
}

/**
 * Combina la etapa leída de SQLite (`state.purchase`) con lo que esta sesión hizo por REST. Una
 * acción en curso manda; después, la etapa del backend; y el resultado local solo cubre el tramo
 * entre la respuesta REST y el `purchase-updated`.
 */
function cardView(
  purchase: AgentPurchase | null,
  local: LocalPurchase | undefined,
  runId: string,
): CardView {
  const busy = local && ["approving", "completing", "canceling"].includes(local.status)
  const recorded = local?.approvalRecorded ?? false
  if (busy && local) {
    return {
      status: local.status as ApprovalCardStatus,
      approvalRecorded: recorded,
      error: null,
      actionable: true,
    }
  }

  const stage = purchase?.run_id === runId ? purchase.stage : null
  if (stage === "completed" || local?.status === "completed") {
    return { status: "completed", approvalRecorded: true, error: null, actionable: false }
  }
  if (stage === "canceled" || local?.status === "canceled") {
    return { status: "canceled", approvalRecorded: false, error: null, actionable: false }
  }
  if (stage === "failed") {
    return {
      status: "error",
      approvalRecorded: true,
      error: "El comercio no pudo completar la compra.",
      actionable: false,
    }
  }
  const stageRecorded = stage === "approved" || stage === "completing" || stage === "uncertain"
  if (local?.status === "error") {
    return {
      status: "error",
      approvalRecorded: recorded || stageRecorded,
      error: local.error ?? null,
      actionable: true,
    }
  }
  if (purchase !== null && purchase.run_id !== runId) {
    // Otra compra reemplazó a esta: la tarjeta queda como referencia, sin acciones.
    return {
      status: "error",
      approvalRecorded: recorded,
      error: "Esta compra ya no está activa.",
      actionable: false,
    }
  }
  return {
    status: "awaiting_approval",
    approvalRecorded: recorded || stageRecorded,
    error: null,
    actionable: true,
  }
}

type PurchaseApprovalProps = {
  runId: string
  terms: ApprovalTerms
  product: ProductCard | null
  priceChanged: boolean
}

/**
 * Tarjeta de aprobación conectada: aprueba con el fingerprint de los términos y completa por REST,
 * o cancela, y después pide al backend que registre la etapa con `purchase-updated`. Ningún comando
 * del transporte lleva evidencia de aprobación.
 */
export function PurchaseApproval({ runId, terms, product, priceChanged }: PurchaseApprovalProps) {
  const purchase = useAssistantTransportState((state) => state.purchase)
  const sendCommand = useAssistantTransportSendCommand()
  const { purchases, updatePurchase, onNewPurchase } = usePurchaseSession()
  const local = purchases[runId]
  const view = cardView(purchase, local, runId)
  // Evita dobles clics antes de que el estado local se vuelva a renderizar.
  const working = useRef(false)

  const notifyBackend = () => sendCommand({ type: "purchase-updated", runId })

  const approve = async () => {
    if (working.current || !view.actionable) return
    working.current = true
    let approvalRecorded = view.approvalRecorded
    try {
      if (!approvalRecorded) {
        updatePurchase(runId, { status: "approving", error: undefined })
        await approvePurchase(runId, terms.fingerprint)
        approvalRecorded = true
      }
      updatePurchase(runId, { status: "completing", approvalRecorded, error: undefined })
      const completed = await completePurchase(runId)
      updatePurchase(runId, { status: "completed", approvalRecorded, order: completed.order })
      notifyBackend()
    } catch (error) {
      updatePurchase(runId, {
        status: "error",
        approvalRecorded,
        error: purchaseErrorMessage(error, "No se pudo completar la compra."),
      })
    } finally {
      working.current = false
    }
  }

  const cancel = async () => {
    if (working.current || !view.actionable || view.approvalRecorded) return
    working.current = true
    try {
      updatePurchase(runId, { status: "canceling", error: undefined })
      await cancelPurchase(runId)
      updatePurchase(runId, { status: "canceled" })
      notifyBackend()
    } catch (error) {
      updatePurchase(runId, {
        status: "error",
        error: purchaseErrorMessage(error, "No se pudo cancelar la compra."),
      })
    } finally {
      working.current = false
    }
  }

  const order =
    local?.order ?? (purchase?.run_id === runId && purchase.order_id ? { id: purchase.order_id } : null)

  return (
    <PurchaseApprovalCard
      terms={terms}
      status={view.status}
      product={product}
      approvalRecorded={view.approvalRecorded}
      error={view.error}
      priceChanged={priceChanged}
      order={order}
      onApprove={view.actionable ? approve : undefined}
      onCancel={view.actionable && !view.approvalRecorded ? cancel : undefined}
      onNewPurchase={onNewPurchase}
    />
  )
}
