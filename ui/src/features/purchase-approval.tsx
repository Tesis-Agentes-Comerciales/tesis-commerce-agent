import { useAssistantTransportSendCommand, useAssistantTransportState } from "@assistant-ui/react"
import { useRef } from "react"

import {
  PurchaseApprovalCard,
  StaleTermsCard,
  type ApprovalCardStatus,
  type StaleTermsCardStatus,
} from "@/components/agent"
import { FAILURE_COPY } from "@/components/trace/trace-copy"
import { usePurchaseSession, type LocalPurchase } from "@/features/purchase-session"
import { approvalReceipt } from "@/features/purchase-trace"
import { staleTermsOf } from "@/features/stale-terms"
import type { AgentPurchase, ApprovalTerms, ProductCard } from "@/lib/agent-contract"
import {
  AgentApiError,
  approvePurchase,
  cancelPurchase,
  completePurchase,
  getApprovalTerms,
  type PendingTermsStatus,
} from "@/lib/commerce-api"

const KNOWN_ERRORS: Record<string, string> = {
  budget_exceeded: "El total autoritativo del comercio supera tu presupuesto.",
  transport_error:
    "No pude confirmar el resultado con el comercio. Consulta de nuevo antes de empezar otra compra.",
  timeout:
    "El comercio tardó demasiado en responder. Consulta de nuevo antes de empezar otra compra.",
  INVALID_INPUT: "El ecommerce rechazó los datos del checkout. Revisa tus datos de entrega.",
  payment_declined: `${FAILURE_COPY.payment_declined} Esta compra no se completó; inicia una nueva compra.`,
  out_of_stock: `${FAILURE_COPY.out_of_stock} Elige otro.`,
  invalid_item: "El producto ya no está disponible en el comercio.",
  PROVIDER_UNAVAILABLE:
    "El proveedor de pagos del comercio no está disponible. Puedes reintentar más tarde.",
  idempotency_in_flight:
    "El comercio sigue procesando esta compra. Espera un momento y consulta de nuevo.",
  http_error: "El ecommerce no pudo procesar la solicitud.",
}

const TERMINAL_ERRORS = new Set(["payment_declined", "out_of_stock"])

/** Mensaje en español para un error REST; el texto de un `TypeError` nunca llega a la UI. */
function purchaseErrorMessage(error: unknown, fallback: string) {
  if (error instanceof AgentApiError) {
    const message = KNOWN_ERRORS[error.code] ?? error.message
    const reference = error.merchantRequestId ? ` Ref. comercio: ${error.merchantRequestId}` : ""
    return (message.trim() + reference).slice(0, 300) || fallback
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
  terminal: boolean,
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
      error: local?.error ?? "El comercio no pudo completar la compra. Puedes iniciar otra compra.",
      actionable: false,
    }
  }
  const stageRecorded = stage === "approved" || stage === "completing" || stage === "uncertain"
  if (local?.status === "error") {
    return {
      status: "error",
      approvalRecorded: recorded || stageRecorded,
      error: local.error ?? null,
      actionable: !terminal,
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

/**
 * Cancelar por REST y pedir al backend que registre la etapa con `purchase-updated`. Lo comparten la
 * tarjeta de aprobación y la de términos que ya no valen. `exclusive` corre una acción a la vez: evita
 * dobles clics antes de que el estado local se vuelva a renderizar.
 */
function usePurchaseActions(runId: string) {
  const sendCommand = useAssistantTransportSendCommand()
  const { updatePurchase } = usePurchaseSession()
  const working = useRef(false)

  const exclusive = async (action: () => Promise<void>) => {
    if (working.current) return
    working.current = true
    try {
      await action()
    } finally {
      working.current = false
    }
  }

  const notifyBackend = () => sendCommand({ type: "purchase-updated", runId })

  const cancel = () =>
    exclusive(async () => {
      try {
        updatePurchase(runId, { status: "canceling", error: undefined, errorCode: undefined })
        await cancelPurchase(runId)
        updatePurchase(runId, { status: "canceled" })
        notifyBackend()
      } catch (error) {
        updatePurchase(runId, {
          status: "error",
          error: purchaseErrorMessage(error, "No se pudo cancelar la compra."),
          errorCode: error instanceof AgentApiError ? error.code : undefined,
        })
      }
    })

  return { exclusive, notifyBackend, cancel }
}

type StaleTermsApprovalProps = {
  runId: string
  terms: PendingTermsStatus
  product: ProductCard | null
}

/**
 * Compra cuyos términos ya no valen: no se puede aprobar, solo cancelar. Después el comprador la
 * pide de nuevo en el chat y obtiene un checkout con las condiciones vigentes.
 */
export function StaleTermsApproval({ runId, terms, product }: StaleTermsApprovalProps) {
  const purchase = useAssistantTransportState((state) => state.purchase)
  const { purchases } = usePurchaseSession()
  const { cancel } = usePurchaseActions(runId)
  const local = purchases[runId]
  const stage = purchase?.run_id === runId ? purchase.stage : null
  const status: StaleTermsCardStatus =
    local?.status === "canceling"
      ? "canceling"
      : stage === "canceled" || local?.status === "canceled"
        ? "canceled"
        : "stale"

  return (
    <StaleTermsCard
      terms={local?.termsStatus ?? terms}
      status={status}
      productTitle={product?.title ?? null}
      error={status === "stale" && local?.status === "error" ? (local.error ?? null) : null}
      onCancel={cancel}
      // Llegó al aprobar en esta sesión: reemplaza a la tarjeta de aprobación, que tenía el foco.
      autoFocus={local?.termsStatus !== undefined}
    />
  )
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
  const { purchases, updatePurchase, traces, onNewPurchase } = usePurchaseSession()
  const { exclusive, notifyBackend, cancel: cancelPurchaseRun } = usePurchaseActions(runId)
  const local = purchases[runId]
  const terminal =
    (purchase?.run_id === runId && purchase.stage === "failed") ||
    TERMINAL_ERRORS.has(local?.errorCode ?? "")
  const view = cardView(purchase, local, runId, terminal)

  const approve = () => {
    if (!view.actionable) return
    return exclusive(async () => {
      let approvalRecorded = view.approvalRecorded
      try {
        if (!approvalRecorded) {
          updatePurchase(runId, { status: "approving", error: undefined, errorCode: undefined })
          const refreshed = await getApprovalTerms(runId)
          if (refreshed.fingerprint !== terms.fingerprint) {
            updatePurchase(runId, {
              status: "error",
              error: undefined,
              errorCode: "checkout_terms_changed",
              termsStatus: "changed",
            })
            return
          }
          await approvePurchase(runId, terms.fingerprint, refreshed.checkout_jwt)
          approvalRecorded = true
        }
        updatePurchase(runId, {
          status: "completing",
          approvalRecorded,
          error: undefined,
          errorCode: undefined,
        })
        const completed = await completePurchase(runId)
        updatePurchase(runId, { status: "completed", approvalRecorded, order: completed.order })
        notifyBackend()
      } catch (error) {
        const code = error instanceof AgentApiError ? error.code : undefined
        // Antes de registrar la aprobación, un 409 o 503 de frescura deja solo la opción de cancelar.
        const termsStatus = approvalRecorded ? null : staleTermsOf(code)
        updatePurchase(runId, {
          status: "error",
          approvalRecorded,
          error: termsStatus
            ? undefined
            : purchaseErrorMessage(error, "No se pudo completar la compra."),
          errorCode: code,
          ...(termsStatus && { termsStatus }),
        })
      }
    })
  }

  const cancel = () => {
    if (!view.actionable || view.approvalRecorded) return
    return cancelPurchaseRun()
  }

  if (local?.termsStatus) {
    return <StaleTermsApproval runId={runId} terms={local.termsStatus} product={product} />
  }

  const order =
    local?.order ?? (purchase?.run_id === runId && purchase.order_id ? { id: purchase.order_id } : null)

  return (
    <PurchaseApprovalCard
      terms={terms}
      status={view.status}
      product={product}
      approvalRecorded={view.approvalRecorded}
      terminal={terminal}
      error={view.error}
      priceChanged={priceChanged}
      order={order}
      receipt={approvalReceipt(traces[runId] ?? null)}
      onApprove={view.actionable ? approve : undefined}
      onCancel={view.actionable && !view.approvalRecorded ? cancel : undefined}
      onNewPurchase={
        terminal || view.status === "completed" || view.status === "canceled"
          ? onNewPurchase
          : undefined
      }
    />
  )
}
