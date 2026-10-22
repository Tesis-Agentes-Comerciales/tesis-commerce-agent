import { useAssistantTransportState } from "@assistant-ui/react"
import { useEffect, useState } from "react"

import type { ApprovalReceipt } from "@/components/agent"
import { isRecord } from "@/features/agent-results"
import { usePurchaseSession } from "@/features/purchase-session"
import type { AgentMessageStatus } from "@/lib/agent-contract"
import { getPurchaseTrace } from "@/lib/commerce-api"
import type { PolicyDecision, PolicyRule, PurchaseTrace } from "@/lib/trace-contract"

// Traza en vivo de la compra de un mensaje (docs/design/trace-panel.md, «Integración»). El stream
// de /assistant no la trae: se relee por REST cada vez que algo pudo cambiarla.

type MessagePart = {
  type: string
  toolName?: string
  args?: unknown
  result?: unknown
}

const RUN_ID = /^run_[A-Za-z0-9_-]{1,128}$/

/** `run_id` de los `args` o del `result` de un paso, si tiene la forma del contrato. */
export function runIdOf(value: unknown) {
  const runId = isRecord(value) ? value.run_id : undefined
  return typeof runId === "string" && RUN_ID.test(runId) ? runId : null
}

/**
 * Compra que empezó este mensaje, en cuanto existe: `prepare_checkout` terminó (con resultado, o con
 * un bloqueo de la política, que solo deja el run en los `args`) o el mensaje restaurado trae
 * `request_approval`. Un `purchase_update` no cuenta: la fila vive en el mensaje que empezó la compra.
 */
export function messagePurchaseRunId(parts: readonly MessagePart[]) {
  for (const part of parts) {
    if (part.type !== "tool-call" || part.result === undefined) continue
    if (part.toolName === "prepare_checkout" || part.toolName === "request_approval") {
      return runIdOf(part.result) ?? runIdOf(part.args)
    }
  }
  return null
}

/** Cambia cuando termina un paso, cuando cierra el mensaje y con cada acción sobre la compra. */
function traceRevision(
  parts: readonly MessagePart[],
  status: AgentMessageStatus | undefined,
  stage: string | null,
  localStatus: string | null,
) {
  const finished = parts.filter((part) => part.type === "tool-call" && part.result !== undefined)
  return [finished.length, status?.type ?? "", stage ?? "", localStatus ?? ""].join(":")
}

/**
 * Pide la traza cuando aparece el `run_id`, cuando termina cada paso del stream y después de aprobar,
 * completar o cancelar. Un 404, un 503, la red caída o una traza fuera de contrato no rompen el chat:
 * se conserva la última traza leída o, si no hay ninguna, la fila no aparece. `pending` dice si la
 * primera traza viene en camino: la tarjeta de bloqueo deja de esperarla si la lectura falló.
 */
export function useMessagePurchaseTrace(
  parts: readonly MessagePart[],
  status: AgentMessageStatus | undefined,
): { trace: PurchaseTrace | null; pending: boolean } {
  const runId = messagePurchaseRunId(parts)
  const stage = useAssistantTransportState((state) =>
    state.purchase && state.purchase.run_id === runId ? state.purchase.stage : null,
  )
  const { purchases, traces, recordTrace } = usePurchaseSession()
  const localStatus = runId === null ? null : (purchases[runId]?.status ?? null)
  const revision = traceRevision(parts, status, stage, localStatus)
  const [failedRun, setFailedRun] = useState<string | null>(null)

  useEffect(() => {
    if (runId === null) return
    const controller = new AbortController()
    getPurchaseTrace(runId, controller.signal).then(
      (trace) => {
        if (controller.signal.aborted) return
        recordTrace(runId, trace)
        setFailedRun(null)
      },
      () => {
        if (!controller.signal.aborted) setFailedRun(runId)
      },
    )
    return () => controller.abort()
  }, [runId, revision, recordTrace])

  const trace = runId === null ? null : (traces[runId] ?? null)
  return { trace, pending: runId !== null && trace === null && failedRun !== runId }
}

/** La decisión que detuvo la compra, para la tarjeta de bloqueo. */
export function blockingDecision(trace: PurchaseTrace | null): PolicyDecision | null {
  if (trace?.status !== "blocked") return null
  const step = trace.steps.findLast(
    (candidate) => candidate.kind === "policy_decision" && candidate.decision.effect === "blocked",
  )
  return step?.kind === "policy_decision" ? step.decision : null
}

/**
 * Reglas que no se cumplieron cuando, al aprobar, la política no autorizó la compra (D2). Las muestra
 * la tarjeta que reemplaza a la de aprobación; sin traza todavía, ninguna.
 */
export function refusedRules(trace: PurchaseTrace | null): PolicyRule[] {
  const step = trace?.steps.findLast((candidate) => candidate.kind === "authorization_decision")
  if (step?.kind !== "authorization_decision" || step.decision.effect !== "blocked") return []
  return step.decision.rules.filter((rule) => rule.result === "fail")
}

/** Recibo de la tarjeta de aprobación: la hora registrada y si hubo autorización firmada. */
export function approvalReceipt(trace: PurchaseTrace | null): ApprovalReceipt | null {
  const approval = trace?.steps.findLast((step) => step.kind === "approval")
  if (!trace || approval?.kind !== "approval" || approval.approved_at === null) return null
  return {
    approvedAt: approval.approved_at,
    authorizationSigned: trace.steps.some((step) => step.kind === "authorization_signed"),
  }
}

/**
 * Una compra que sigue «en curso» en un mensaje que terminó con error o se detuvo no avanzará
 * desde este turno: la fila giraría para siempre. Ahí basta con el aviso del mensaje.
 */
export function activityVisible(trace: PurchaseTrace | null, status: AgentMessageStatus | undefined) {
  if (trace === null) return false
  return !(trace.status === "in_progress" && status?.type === "incomplete")
}
