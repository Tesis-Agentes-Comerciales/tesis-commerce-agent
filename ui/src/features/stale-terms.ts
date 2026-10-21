import { asApprovalResult, isRecord } from "@/features/agent-results"
import { usePurchaseSession } from "@/features/purchase-session"
import { messagePurchaseRunId, runIdOf } from "@/features/purchase-trace"
import type { AgentToolCallPart } from "@/lib/agent-contract"
import type { StaleTermsReason } from "@/components/agent"
import type { PendingTermsStatus } from "@/lib/commerce-api"

// Términos que ya no valen (b2-governed-pilot, S04): la oferta cambió, venció o no se pudo leer, o
// el checkout venció. La compra sigue pendiente y no se puede aprobar; el comprador la cancela (el
// comercio cancela también un checkout vencido) y la pide de nuevo.

/** Códigos con los que el backend rechaza los términos: 409 y 503 en REST, o el paso en el stream. */
const STALE_TERMS = new Map<string, StaleTermsReason>([
  ["checkout_terms_changed", "changed"],
  ["offer_expired", "expired"],
  ["checkout_expired", "expired"],
  ["offer_freshness_unavailable", "unavailable"],
])

/** Error del paso restaurado: un código que `staleTermsOf` lee como el mismo motivo, y su texto. */
const RESTORED_ERROR: Record<PendingTermsStatus, { code: string; message: string }> = {
  changed: {
    code: "checkout_terms_changed",
    message: "Las condiciones del comercio cambiaron. No se hizo ningún cobro.",
  },
  expired: {
    code: "checkout_expired",
    message: "Las condiciones que revisaste vencieron. No se hizo ningún cobro.",
  },
  unavailable: {
    code: "offer_freshness_unavailable",
    message: "No pude confirmar las condiciones con el comercio. No se hizo ningún cobro.",
  },
}

/** Por qué los términos ya no valen según un código de error, o `null` si es otro error. */
export function staleTermsOf(code: unknown): StaleTermsReason | null {
  return typeof code === "string" ? (STALE_TERMS.get(code) ?? null) : null
}

type StepPart = { type: string; toolName?: string; args?: unknown; result?: unknown; isError?: boolean }

/**
 * Compra de un `request_approval` que falló porque los términos ya no valen, con su `run_id`. Así
 * llegan la compra restaurada con `terms_status` y el paso que falló al presentar los términos.
 */
export function staleApprovalOf(part: StepPart) {
  if (part.type !== "tool-call" || part.toolName !== "request_approval" || !part.isError) return null
  const error = isRecord(part.result) ? part.result.error : undefined
  const terms = staleTermsOf(isRecord(error) ? error.code : undefined)
  const runId = runIdOf(part.args)
  return terms && runId ? { runId, terms } : null
}

/**
 * El mensaje tiene una compra cuyos términos ya no valen: lo dice su paso `request_approval` o aprobar
 * falló en esta sesión. Su tarjeta explica la parada, así que el mensaje no repite el aviso rojo.
 */
export function useMessageStaleTerms(parts: readonly StepPart[]) {
  const { purchases } = usePurchaseSession()
  if (parts.some((part) => staleApprovalOf(part) !== null)) return true
  const runId = messagePurchaseRunId(parts)
  return runId !== null && purchases[runId]?.termsStatus !== undefined
}

/**
 * El mensaje muestra una tarjeta de la compra: la de aprobación o la de términos que ya no valen.
 * Ella anuncia a los lectores de pantalla lo que pasa al aprobar o cancelar, así que la fila de
 * actividad no lo repite.
 */
export function messageHasPurchaseCard(parts: readonly StepPart[]) {
  return parts.some(
    (part) =>
      staleApprovalOf(part) !== null ||
      (part.type === "tool-call" &&
        part.toolName === "request_approval" &&
        !part.isError &&
        asApprovalResult(part.result) !== null),
  )
}

/** Paso `request_approval` con el que se restaura una compra pendiente cuyos términos ya no valen. */
export function staleApprovalPart(runId: string, terms: PendingTermsStatus) {
  const args = { run_id: runId }
  return {
    type: "tool-call",
    toolCallId: `restored-${runId}`,
    toolName: "request_approval",
    args,
    argsText: JSON.stringify(args),
    result: { error: RESTORED_ERROR[terms] },
    isError: true,
  } satisfies AgentToolCallPart<"request_approval">
}
